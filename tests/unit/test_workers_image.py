"""Pruebas de src/converter/workers/image.py: conversión de PNG, JPG y WEBP entre sí con Pillow
(Req 5.1, 5.2, 5.3, 5.7, 5.10, 13.4).

Las imágenes son pequeñas y deterministas (`tests/image_samples.py`). Los resultados esperados se
calculan en la prueba, no con las funciones del worker: la composición sobre blanco con la
fórmula del diseño, la Orientación_EXIF con `tests.strategies.images.oriented_pixels` y la
reducción de 16 a 8 bits con un desplazamiento de 8 bits. Los destinos con pérdida (JPG, WEBP desde
JPG) se comparan con una tolerancia; PNG y WEBP desde PNG, de forma exacta.
"""

from __future__ import annotations

import io
import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, JpegImagePlugin, UnidentifiedImageError

from converter.workers import image as worker
from converter.workers.image import (
    DEFAULT_MAX_IMAGE_PIXELS,
    PngHeader,
    WorkerArgs,
    convert_file,
    flatten_on_white,
    format_args,
    normalize_mode,
    parse_args,
    prepare_for_target,
    read_png_header,
    save_options,
)
from tests.image_samples import (
    BLOCK,
    block_centres,
    close,
    expected_blocks,
    grid_file,
    png_bytes,
)
from tests.strategies.images import oriented_pixels

FORMATS = ("PNG", "JPG", "WEBP")
PAIRS = [(source, target) for source in FORMATS for target in FORMATS if source != target]
TAGS = [*range(1, 9), 0, None]  # etiquetas Orientation: de 1 a 8, fuera de rango (0) y ausente
JPEG_TOLERANCE = 16  # por canal, sobre el centro de bloques de color uniforme
# Marcas de EXIF y de XMP en los contenedores de PNG, JPG y WEBP
METADATA_MARKERS = (
    b"Exif",
    b"EXIF",
    b"eXIf",
    b"iTXt",
    b"xmpmeta",
    b"http://ns.adobe.com/xap",
    b"XML:com.adobe.xmp",
)


def _convert(
    tmp_path: Path,
    source: str,
    target: str,
    data: bytes,
    *,
    name: str = "result",
    max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS,
) -> Path:
    """Convierte `data` con el worker y devuelve el archivo resultado."""
    source_file = tmp_path / "source"
    source_file.write_bytes(data)
    destination = tmp_path / name
    convert_file(
        WorkerArgs(
            source_format=source,
            target_format=target,
            source=source_file,
            destination=destination,
            max_image_pixels=max_image_pixels,
        )
    )
    return destination


def _pixels(im: Image.Image) -> list[tuple[int, ...]]:
    """Píxeles de `im` por filas, cada uno con sus canales."""
    bands = len(im.getbands())
    flat = im.tobytes()
    return [tuple(flat[i : i + bands]) for i in range(0, len(flat), bands)]


def _pixel(im: Image.Image, x: int, y: int) -> tuple[int, ...]:
    """Color del píxel (`x`, `y`) de `im` como `RGB`."""
    pixel = im.convert("RGB").getpixel((x, y))
    assert isinstance(pixel, tuple)
    return pixel


def _png_with_orientation(im: Image.Image, tag: int) -> bytes:
    """PNG de `im` con la etiqueta Orientation `tag`."""
    exif = Image.Exif()
    exif[0x0112] = tag
    return _save(im, "PNG", exif=exif)


def _save(im: Image.Image, fmt: str, **params: object) -> bytes:
    out = io.BytesIO()
    im.save(out, fmt, **params)
    return out.getvalue()


def _exact(colour: int, alpha: int) -> int:
    """`round(c·α/255 + 255·(1 − α/255))` con aritmética entera: nunca hay empates."""
    return (colour * alpha + 255 * (255 - alpha) + 127) // 255


# --- flatten_on_white (Req 5.2) ---


@pytest.mark.req("5.2")
def test_flatten_on_white_follows_the_formula_for_every_colour_and_opacity() -> None:
    # Canales distintos: c, 255 − c y 7c módulo 256 recorren los 256 valores cada uno
    rgba = Image.new("RGBA", (256, 256))
    rgba.putdata([(c, 255 - c, 7 * c % 256, a) for a in range(256) for c in range(256)])

    flat = flatten_on_white(rgba)

    assert flat.mode == "RGB"
    assert flat.size == rgba.size
    expected = [
        (_exact(c, a), _exact(255 - c, a), _exact(7 * c % 256, a))
        for a in range(256)
        for c in range(256)
    ]
    assert _pixels(flat) == expected


@pytest.mark.req("5.2")
def test_flatten_on_white_makes_transparent_white_and_opaque_keep_their_colour() -> None:
    rgba = Image.new("RGBA", (3, 1))
    rgba.putdata([(10, 20, 30, 0), (10, 20, 30, 255), (10, 20, 30, 128)])

    flat = flatten_on_white(rgba)

    assert _pixels(flat) == [(255, 255, 255), (10, 20, 30), (132, 137, 142)]


@pytest.mark.req("5.2")
def test_flatten_on_white_accepts_gray_with_alpha() -> None:
    la = Image.new("LA", (256, 256))
    la.putdata([(c, a) for a in range(256) for c in range(256)])

    flat = flatten_on_white(la)

    assert flat.mode == "L"
    assert flat.tobytes() == bytes(_exact(c, a) for a in range(256) for c in range(256))


@pytest.mark.parametrize("mode", ["RGB", "L", "P", "1"])
def test_flatten_on_white_rejects_images_without_alpha(mode: str) -> None:
    with pytest.raises(ValueError, match="RGBA o LA"):
        flatten_on_white(Image.new(mode, (2, 2)))


# --- Cabecera PNG ---


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (png_bytes(1, 1, [[1, 2, 3, 4]], bitdepth=16, alpha=True), PngHeader(16, 6)),
        (png_bytes(2, 1, [[0, 3]], bitdepth=2, greyscale=True), PngHeader(2, 0)),
        (png_bytes(1, 1, [[1, 2, 3]], bitdepth=8), PngHeader(8, 2)),
        (grid_file("JPG"), None),
        (grid_file("PNG")[:20], None),
        (b"", None),
    ],
    ids=["RGBA de 16 bits", "gris de 2 bits", "RGB de 8 bits", "no es PNG", "truncado", "vacío"],
)
def test_read_png_header(tmp_path: Path, data: bytes, expected: PngHeader | None) -> None:
    path = tmp_path / "file"
    path.write_bytes(data)

    assert read_png_header(path) == expected


# --- normalize_mode (Req 5.3) ---


def _open_png(tmp_path: Path, data: bytes) -> tuple[Image.Image, PngHeader | None]:
    path = tmp_path / "keyed.png"
    path.write_bytes(data)
    im = Image.open(path)
    im.load()
    return im, read_png_header(path)


def test_normalize_mode_converts_each_mode_to_l_rgb_or_rgba() -> None:
    one_bit = Image.new("1", (2, 1))
    one_bit.putpixel((1, 0), 1)
    palette = Image.new("P", (2, 1))
    palette.putpalette([10, 20, 30, 40, 50, 60])
    palette.putpixel((1, 0), 1)
    transparent_palette = palette.copy()
    transparent_palette.info["transparency"] = bytes([0, 255])
    gray_alpha = Image.new("LA", (1, 1), (90, 200))
    cmyk = Image.new("CMYK", (1, 1), (0, 0, 0, 0))

    assert normalize_mode(one_bit).mode == "L"
    assert normalize_mode(one_bit).tobytes() == bytes([0, 255])
    assert _pixels(normalize_mode(palette)) == [(10, 20, 30), (40, 50, 60)]
    assert _pixels(normalize_mode(transparent_palette)) == [(10, 20, 30, 0), (40, 50, 60, 255)]
    assert _pixels(normalize_mode(gray_alpha)) == [(90, 90, 90, 200)]
    assert _pixels(normalize_mode(cmyk)) == [(255, 255, 255)]
    for mode in ("L", "RGB", "RGBA"):
        im = Image.new(mode, (1, 1))
        assert normalize_mode(im) is im


@pytest.mark.req("5.3")
def test_normalize_mode_reduces_16_bit_gray_to_its_high_byte() -> None:
    samples = [0, 255, 256, 1000, 0x8081, 0xFFFF]
    im = Image.frombytes("I;16", (len(samples), 1), struct.pack(f"<{len(samples)}H", *samples))

    result = normalize_mode(im)

    assert result.mode == "L"
    assert result.tobytes() == bytes(sample >> 8 for sample in samples)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param(
            png_bytes(4, 1, [[0, 100, 200, 255]], bitdepth=8, greyscale=True, transparent=(200,)),
            [(0, 255), (100, 255), (200, 0), (255, 255)],
            id="gris de 8 bits",
        ),
        pytest.param(
            png_bytes(4, 1, [[0, 1, 2, 3]], bitdepth=2, greyscale=True, transparent=(2,)),
            [(0, 255), (85, 255), (170, 0), (255, 255)],
            id="gris de 2 bits",
        ),
        pytest.param(
            png_bytes(4, 1, [[0, 5, 10, 15]], bitdepth=4, greyscale=True, transparent=(10,)),
            [(0, 255), (85, 255), (170, 0), (255, 255)],
            id="gris de 4 bits",
        ),
        pytest.param(
            png_bytes(4, 1, [[0, 1, 0, 1]], bitdepth=1, greyscale=True, transparent=(1,)),
            [(0, 255), (255, 0), (0, 255), (255, 0)],
            id="gris de 1 bit",
        ),
    ],
)
@pytest.mark.req("5.2", "5.7")
def test_normalize_mode_applies_the_colour_key_of_gray_pngs(
    tmp_path: Path, data: bytes, expected: list[tuple[int, int]]
) -> None:
    im, header = _open_png(tmp_path, data)

    result = normalize_mode(im, header)

    assert result.mode == "RGBA"
    assert _pixels(result) == [(gray, gray, gray, alpha) for gray, alpha in expected]


@pytest.mark.req("5.2", "5.7")
def test_normalize_mode_applies_the_colour_key_of_rgb_pngs(tmp_path: Path) -> None:
    rows = [[10, 20, 30, 10, 20, 31, 40, 50, 60]]
    im, header = _open_png(tmp_path, png_bytes(3, 1, rows, bitdepth=8, transparent=(10, 20, 30)))
    info_before = dict(im.info)

    result = normalize_mode(im, header)

    assert _pixels(result) == [(10, 20, 30, 0), (10, 20, 31, 255), (40, 50, 60, 255)]
    assert im.info == info_before  # no modifica la imagen que recibe


@pytest.mark.req("5.2", "5.3")
def test_normalize_mode_scales_the_colour_key_of_16_bit_rgb_pngs(tmp_path: Path) -> None:
    key = (0x1234, 0x5678, 0x9ABC)
    rows = [[*key, 0x1234, 0x5678, 0x9BBC, 0xFFFF, 0xFFFF, 0xFFFF]]
    im, header = _open_png(tmp_path, png_bytes(3, 1, rows, bitdepth=16, transparent=key))

    result = normalize_mode(im, header)

    assert _pixels(result) == [
        (0x12, 0x56, 0x9A, 0),
        (0x12, 0x56, 0x9B, 255),
        (0xFF, 0xFF, 0xFF, 255),
    ]


@pytest.mark.req("5.2", "5.3")
def test_normalize_mode_compares_the_colour_key_of_16_bit_gray_pngs_exactly(
    tmp_path: Path,
) -> None:
    rows = [[0x1234, 0x1235, 0x1334, 0xFFFF]]
    im, header = _open_png(
        tmp_path, png_bytes(4, 1, rows, bitdepth=16, greyscale=True, transparent=(0x1234,))
    )
    assert im.mode == "I;16"

    result = normalize_mode(im, header)

    assert _pixels(result) == [
        (0x12, 0x12, 0x12, 0),
        (0x12, 0x12, 0x12, 255),
        (0x13, 0x13, 0x13, 255),
        (0xFF, 0xFF, 0xFF, 255),
    ]


# --- prepare_for_target y save_options ---


@pytest.mark.parametrize(
    ("mode", "target", "expected"),
    [
        ("RGBA", "JPG", "RGB"),
        ("RGB", "JPG", "RGB"),
        ("L", "JPG", "L"),
        ("L", "WEBP", "RGB"),
        ("RGB", "WEBP", "RGB"),
        ("RGBA", "WEBP", "RGBA"),
        ("L", "PNG", "L"),
        ("RGBA", "PNG", "RGBA"),
    ],
)
def test_prepare_for_target_keeps_only_modes_the_target_stores(
    mode: str, target: str, expected: str
) -> None:
    assert prepare_for_target(Image.new(mode, (2, 2)), target).mode == expected


@pytest.mark.req("5.7", "5.10")
@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        ("PNG", "JPG", {"quality": 90, "subsampling": "4:2:0", "optimize": False}),
        ("WEBP", "JPG", {"quality": 90, "subsampling": "4:2:0", "optimize": False}),
        ("PNG", "WEBP", {"lossless": True, "quality": 100, "method": 4, "exact": True}),
        ("JPG", "WEBP", {"quality": 90}),
        ("JPG", "PNG", {"compress_level": 6}),
        ("WEBP", "PNG", {"compress_level": 6}),
    ],
)
def test_save_options_of_each_target(source: str, target: str, expected: dict[str, object]) -> None:
    options = save_options(source, target)

    assert options == expected
    assert {"exif", "xmp", "icc_profile"}.isdisjoint(options)


@pytest.mark.parametrize(("source", "target"), [("PNG", "PNG"), ("PNG", "GIF"), ("BMP", "JPG")])
def test_save_options_rejects_pairs_that_are_not_image_pairs(source: str, target: str) -> None:
    with pytest.raises(ValueError, match="par de imágenes"):
        save_options(source, target)


# --- Argumentos ---


def test_args_round_trip_and_defaults() -> None:
    args = WorkerArgs(
        source_format="PNG",
        target_format="WEBP",
        source=Path("-origen"),
        destination=Path("/ws/work/converted"),
        max_image_pixels=1234,
    )

    assert parse_args(format_args(args)) == args
    defaults = parse_args(["--", "JPG", "PNG", "a", "b"])
    assert defaults.max_image_pixels == DEFAULT_MAX_IMAGE_PIXELS == 100_000_000


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["PNG", "PNG", "a", "b"],  # origen y destino iguales
        ["PNG", "GIF", "a", "b"],
        ["PNG", "JPG", "a"],
        ["--max-image-pixels", "0", "PNG", "JPG", "a", "b"],
        ["--max-image-pixels", "1e3", "PNG", "JPG", "a", "b"],
        ["--max-image", "5", "PNG", "JPG", "a", "b"],  # sin abreviaturas
    ],
)
def test_parse_args_rejects_invalid_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(argv)

    assert exit_info.value.code == worker.EXIT_USAGE


# --- Orientación_EXIF y Dimensiones_Mostradas (Req 5.1, 5.10) ---


@pytest.mark.req("5.1", "5.10")
@pytest.mark.parametrize("tag", TAGS, ids=lambda tag: f"etiqueta {tag}")
@pytest.mark.parametrize(("source", "target"), PAIRS)
def test_every_pair_applies_the_orientation_and_keeps_the_displayed_size(
    tmp_path: Path, source: str, target: str, tag: int | None
) -> None:
    result = _convert(tmp_path, source, target, grid_file(source, tag))

    columns, rows, expected = expected_blocks(tag)
    with Image.open(result) as im:
        im.load()
        assert im.format == {"PNG": "PNG", "JPG": "JPEG", "WEBP": "WEBP"}[target]
        assert im.size == (columns * BLOCK, rows * BLOCK)
        centres = block_centres(im)
        assert 0x0112 not in im.getexif()
    tolerance = JPEG_TOLERANCE if "JPG" in (source, target) else 0  # JPG pierde
    assert all(close(a, b, tolerance) for a, b in zip(centres, expected, strict=True)), (
        centres,
        expected,
    )


@pytest.mark.req("5.10")
@pytest.mark.parametrize(("source", "target"), PAIRS)
def test_the_result_has_no_exif_or_xmp(tmp_path: Path, source: str, target: str) -> None:
    data = grid_file(source, 6, xmp=True)
    assert any(marker in data for marker in METADATA_MARKERS)  # el origen sí los tiene

    result = _convert(tmp_path, source, target, data)

    content = result.read_bytes()
    assert [marker for marker in METADATA_MARKERS if marker in content] == []
    with Image.open(result) as im:
        assert not im.getexif()
        assert not {"exif", "xmp", "XML:com.adobe.xmp"} & set(im.info)


@pytest.mark.req("5.10")
@pytest.mark.requires_tool("exiftool")
@pytest.mark.parametrize(("source", "target"), PAIRS)
def test_exiftool_finds_no_exif_xmp_or_orientation_in_the_result(
    tmp_path: Path, source: str, target: str
) -> None:
    result = _convert(tmp_path, source, target, grid_file(source, 6, xmp=True))

    completed = subprocess.run(
        ["exiftool", "-json", "-a", "-G1", str(result)], capture_output=True, check=True
    )

    tags: dict[str, object] = json.loads(completed.stdout)[0]
    exif_groups = ("XMP", "IFD", "ExifIFD", "GPS", "InteropIFD", "SubIFD")
    assert [
        key for key in tags if key.startswith(exif_groups) or key.split(":")[-1] == "Orientation"
    ] == []


# --- Transparencia (Req 5.2) ---


def _two_blocks(mode: str, left: object, right: object) -> Image.Image:
    """Imagen de 32 × 16 con un bloque a la izquierda y otro a la derecha."""
    im = Image.new(mode, (32, 16), left)  # type: ignore[arg-type]
    im.paste(right, (16, 0, 32, 16))  # type: ignore[arg-type]
    return im


def _paletted() -> Image.Image:
    im = _two_blocks("P", 0, 1)
    im.putpalette([200, 50, 50, 10, 10, 10])
    return im


def _rows(width: int, left: list[int], right: list[int]) -> list[list[int]]:
    return [[*(left * (width // 2)), *(right * (width // 2))]] * 16


# Imagen de 32 × 16 con un bloque opaco a la izquierda y uno transparente a la derecha, y el color
# del bloque opaco como RGB
OPAQUE_AND_TRANSPARENT: dict[str, tuple[str, bytes, tuple[int, int, int]]] = {
    "RGBA": (
        "PNG",
        _save(_two_blocks("RGBA", (200, 50, 50, 255), (10, 10, 10, 0)), "PNG"),
        (200, 50, 50),
    ),
    "LA": ("PNG", _save(_two_blocks("LA", (100, 255), (50, 0)), "PNG"), (100, 100, 100)),
    "paleta con tabla de opacidad": (
        "PNG",
        _save(_paletted(), "PNG", transparency=bytes([255, 0])),
        (200, 50, 50),
    ),
    "paleta con un índice transparente": (
        "PNG",
        _save(_paletted(), "PNG", transparency=1),
        (200, 50, 50),
    ),
    "gris con color clave": (
        "PNG",
        png_bytes(32, 16, _rows(32, [100], [50]), bitdepth=8, greyscale=True, transparent=(50,)),
        (100, 100, 100),
    ),
    "gris de 2 bits con color clave": (
        "PNG",
        png_bytes(32, 16, _rows(32, [2], [1]), bitdepth=2, greyscale=True, transparent=(1,)),
        (170, 170, 170),
    ),
    "RGB con color clave": (
        "PNG",
        png_bytes(
            32, 16, _rows(32, [200, 50, 50], [10, 10, 10]), bitdepth=8, transparent=(10, 10, 10)
        ),
        (200, 50, 50),
    ),
    "RGBA de 16 bits": (
        "PNG",
        png_bytes(
            32,
            16,
            _rows(32, [200 * 257, 50 * 257, 50 * 257, 65535], [10 * 257, 10 * 257, 10 * 257, 0]),
            bitdepth=16,
            alpha=True,
        ),
        (200, 50, 50),
    ),
    "gris con alfa de 16 bits": (
        "PNG",
        png_bytes(
            32,
            16,
            _rows(32, [100 * 257, 65535], [50 * 257, 0]),
            bitdepth=16,
            greyscale=True,
            alpha=True,
        ),
        (100, 100, 100),
    ),
    "gris de 16 bits con color clave": (
        "PNG",
        png_bytes(
            32,
            16,
            _rows(32, [100 * 257], [50 * 257]),
            bitdepth=16,
            greyscale=True,
            transparent=(50 * 257,),
        ),
        (100, 100, 100),
    ),
    "WEBP con alfa": (
        "WEBP",
        _save(
            _two_blocks("RGBA", (200, 50, 50, 255), (10, 10, 10, 0)),
            "WEBP",
            lossless=True,
            exact=True,
        ),
        (200, 50, 50),
    ),
}


@pytest.mark.req("5.2")
@pytest.mark.parametrize("case", OPAQUE_AND_TRANSPARENT)
def test_to_jpg_transparent_pixels_become_white_and_opaque_ones_keep_their_colour(
    tmp_path: Path, case: str
) -> None:
    source, data, opaque = OPAQUE_AND_TRANSPARENT[case]

    result = _convert(tmp_path, source, "JPG", data)

    with Image.open(result) as im:
        im.load()
        assert im.size == (32, 16)
        assert close(_pixel(im, 8, 8), opaque, JPEG_TOLERANCE)
        assert close(_pixel(im, 24, 8), (255, 255, 255), JPEG_TOLERANCE)


@pytest.mark.req("5.2")
@pytest.mark.parametrize("source", ["PNG", "WEBP"])
def test_to_jpg_partial_opacity_mixes_the_colour_with_white(tmp_path: Path, source: str) -> None:
    im = _two_blocks("RGBA", (0, 255, 0, 128), (0, 0, 255, 64))
    data = _save(im, "PNG") if source == "PNG" else _save(im, "WEBP", lossless=True, exact=True)

    result = _convert(tmp_path, source, "JPG", data)

    with Image.open(result) as jpg:
        left, right = _pixel(jpg, 8, 8), _pixel(jpg, 24, 8)
    assert close(left, (_exact(0, 128), _exact(255, 128), _exact(0, 128)), JPEG_TOLERANCE)
    assert close(right, (_exact(0, 64), _exact(0, 64), _exact(255, 64)), JPEG_TOLERANCE)


# --- Ida y vuelta sin pérdida (Req 5.7) ---


@pytest.mark.req("5.7", "5.10")
def test_png_to_webp_to_png_keeps_every_rgba_value_including_transparent_pixels(
    tmp_path: Path,
) -> None:
    width, height = 7, 5
    alphas = [0, 255, 128, 0, 1, 254, 255]  # con RGB distinto de 0 también donde α = 0
    pixels = [
        ((37 * x + 11 * y) % 256, (91 * x + 3 * y) % 256, (13 * y + x) % 256, alphas[(x + y) % 7])
        for y in range(height)
        for x in range(width)
    ]
    im = Image.new("RGBA", (width, height))
    im.putdata(pixels)
    data = _png_with_orientation(im, 6)

    webp = _convert(tmp_path, "PNG", "WEBP", data, name="step.webp")
    back = _convert(tmp_path, "WEBP", "PNG", webp.read_bytes(), name="back.png")

    expected_width, expected_height, expected = oriented_pixels(pixels, width, height, 6)
    with Image.open(back) as result:
        assert result.size == (expected_width, expected_height)
        assert _pixels(result.convert("RGBA")) == expected


@pytest.mark.req("5.7")
@pytest.mark.parametrize("mode", ["L", "LA", "1", "RGB", "P"])
def test_png_to_webp_to_png_keeps_the_values_of_other_modes(tmp_path: Path, mode: str) -> None:
    rgb = Image.new("RGB", (4, 3))
    rgb.putdata(
        [
            ((x * 63 + y * 7) % 256, (x * 31 + y * 40) % 256, (x * 5 + y * 90) % 256)
            for y in range(3)
            for x in range(4)
        ]
    )
    im = rgb.convert(mode)
    png = _save(im, "PNG")

    webp = _convert(tmp_path, "PNG", "WEBP", png, name="step.webp")
    back = _convert(tmp_path, "WEBP", "PNG", webp.read_bytes(), name="back.png")

    with Image.open(back) as result:
        assert _pixels(result.convert("RGBA")) == _pixels(im.convert("RGBA"))


# --- 16 bits por canal (Req 5.3) ---

# Muestras de un píxel de 16 bits por canal por modo, y si el PNG es gris y si tiene alfa
SIXTEEN_BIT_SAMPLES: dict[str, tuple[tuple[int, ...], bool, bool]] = {
    "L": ((0x8081,), True, False),
    "LA": ((0x8081, 0xFFFF), True, True),
    "RGB": ((0x1234, 0x5678, 0x9ABC), False, False),
    "RGBA": ((0x1234, 0x5678, 0x9ABC, 0xFFFF), False, True),
}


@pytest.mark.req("5.3")
@pytest.mark.parametrize("target", ["JPG", "WEBP"])
@pytest.mark.parametrize("mode", SIXTEEN_BIT_SAMPLES)
def test_16_bit_pngs_are_stored_with_8_bits_per_channel(
    tmp_path: Path, mode: str, target: str
) -> None:
    sample, greyscale, alpha = SIXTEEN_BIT_SAMPLES[mode]
    data = png_bytes(
        BLOCK, BLOCK, [list(sample) * BLOCK] * BLOCK, bitdepth=16, greyscale=greyscale, alpha=alpha
    )

    result = _convert(tmp_path, "PNG", target, data)

    high = [value >> 8 for value in sample]
    expected = (high[0],) * 3 if mode in ("L", "LA") else tuple(high[:3])
    with Image.open(result) as im:
        im.load()
        assert im.mode in ("L", "RGB", "RGBA")  # 8 bits por canal
        assert im.size == (BLOCK, BLOCK)
        centre = _pixel(im, BLOCK // 2, BLOCK // 2)
    assert close(centre, expected, 2 if target == "JPG" else 0)


# --- Opciones de guardado ---


@pytest.mark.req("5.2")
@pytest.mark.parametrize("source", ["PNG", "WEBP"])
def test_jpg_results_use_quality_90_and_4_2_0_subsampling(tmp_path: Path, source: str) -> None:
    result = _convert(tmp_path, source, "JPG", grid_file(source))

    with Image.open(result) as jpg:
        assert isinstance(jpg, JpegImagePlugin.JpegImageFile)
        assert JpegImagePlugin.get_sampling(jpg) == 2  # 4:2:0
        # El primer valor de la tabla de luminancia a calidad 90: (16 × 20 + 50) // 100
        assert jpg.quantization[0][0] == 3
        assert not jpg.info.get("progressive")


@pytest.mark.req("5.7")
def test_webp_from_png_is_lossless_and_from_jpg_is_lossy(tmp_path: Path) -> None:
    lossless = _convert(tmp_path, "PNG", "WEBP", grid_file("PNG"), name="a.webp").read_bytes()
    lossy = _convert(tmp_path, "JPG", "WEBP", grid_file("JPG"), name="b.webp").read_bytes()

    assert b"VP8L" in lossless
    assert b"VP8 " in lossy
    assert b"VP8L" not in lossy


def test_png_results_use_compression_level_6(tmp_path: Path) -> None:
    result = _convert(tmp_path, "JPG", "PNG", grid_file("JPG")).read_bytes()

    first_idat = result.index(b"IDAT") + 4
    assert result[first_idat : first_idat + 2] == b"\x78\x9c"  # zlib, nivel 6 (el predeterminado)


# --- Determinismo (Req 13.4) ---


@pytest.mark.req("13.4")
@pytest.mark.parametrize(("source", "target"), PAIRS)
def test_the_same_source_gives_the_same_result_bytes(
    tmp_path: Path, source: str, target: str
) -> None:
    data = grid_file(source, 8)

    first = _convert(tmp_path, source, target, data, name="first").read_bytes()
    second = _convert(tmp_path, source, target, data, name="second").read_bytes()

    assert first == second


# --- Errores ---


def test_a_truncated_source_raises_an_os_error(tmp_path: Path) -> None:
    data = grid_file("PNG")

    with pytest.raises(OSError, match="truncated"):
        _convert(tmp_path, "PNG", "JPG", data[: len(data) // 2])


@pytest.mark.parametrize(
    ("source", "data"),
    [("PNG", b"no es una imagen" * 8), ("PNG", grid_file("JPG")), ("JPG", grid_file("WEBP"))],
    ids=["texto", "JPG declarado como PNG", "WEBP declarado como JPG"],
)
def test_a_source_that_is_not_of_the_declared_format_is_not_identified(
    tmp_path: Path, source: str, data: bytes
) -> None:
    target = "WEBP" if source == "PNG" else "PNG"

    with pytest.raises(UnidentifiedImageError):
        _convert(tmp_path, source, target, data)


@pytest.mark.parametrize("limit", [1_000, 100])
def test_an_image_over_the_pixel_limit_is_not_decoded(tmp_path: Path, limit: int) -> None:
    previous = Image.MAX_IMAGE_PIXELS

    # 48 × 32 = 1536 píxeles: con 1000, Pillow solo avisa; con 100 (menos de la mitad), falla
    with pytest.raises((Image.DecompressionBombWarning, Image.DecompressionBombError)):
        _convert(tmp_path, "PNG", "JPG", grid_file("PNG"), max_image_pixels=limit)

    assert previous == Image.MAX_IMAGE_PIXELS


def test_an_image_at_the_pixel_limit_is_converted(tmp_path: Path) -> None:
    result = _convert(tmp_path, "PNG", "JPG", grid_file("PNG"), max_image_pixels=48 * 32)

    assert result.stat().st_size > 0


# --- Línea de comandos ---


def _run_worker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-m", worker.MODULE, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_the_command_line_writes_the_result_and_exits_with_0(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(grid_file("PNG", 6))
    destination = tmp_path / "result"

    completed = _run_worker(
        *format_args(
            WorkerArgs(
                source_format="PNG", target_format="WEBP", source=source, destination=destination
            )
        )
    )

    assert completed.returncode == worker.EXIT_OK
    assert completed.stdout == ""
    with Image.open(destination) as im:
        assert im.format == "WEBP"
        assert im.size == (32, 48)


def test_the_command_line_exits_with_1_and_a_traceback_when_the_source_is_not_an_image(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"no es una imagen" * 8)

    completed = _run_worker(
        *format_args(
            WorkerArgs(
                source_format="PNG",
                target_format="JPG",
                source=source,
                destination=tmp_path / "result",
            )
        )
    )

    assert completed.returncode == 1
    assert "cannot identify image file" in completed.stderr
    assert not (tmp_path / "result").exists()


def test_the_command_line_exits_with_2_on_invalid_arguments() -> None:
    completed = _run_worker("PNG", "PNG", "a", "b")

    assert completed.returncode == worker.EXIT_USAGE


# --- Dimensiones máximas de los destinos (Req 5.5) ---


def _wide_png(width: int) -> bytes:
    return _save(Image.new("RGB", (width, 1), (10, 20, 30)), "PNG")


@pytest.mark.req("5.5")
@pytest.mark.parametrize(("target", "width"), [("WEBP", 16383), ("JPG", 65500)])
def test_the_widest_images_that_the_encoders_write_are_converted(
    tmp_path: Path, target: str, width: int
) -> None:
    result = _convert(tmp_path, "PNG", target, _wide_png(width))

    with Image.open(result) as im:
        assert im.size == (width, 1)


@pytest.mark.req("5.5")
@pytest.mark.xfail(
    raises=OSError,
    strict=True,
    reason="libjpeg escribe como máximo 65500 píxeles por lado y el criterio 5.5 fija 65535",
)
def test_a_jpg_as_wide_as_the_maximum_of_requirement_5_5_is_converted(tmp_path: Path) -> None:
    result = _convert(tmp_path, "PNG", "JPG", _wide_png(65535))

    with Image.open(result) as im:
        assert im.size == (65535, 1)
