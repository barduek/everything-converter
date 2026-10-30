"""Pruebas de src/converter/workers/image_pdf.py: PNG, JPG y WEBP a un PDF de una página con Pillow
(Req 5.4, 5.10, 4.11, 13.4).

Las imágenes son pequeñas y deterministas (`tests/image_samples.py`). El PDF se lee con pypdfium2,
que no es lo que lo escribió: el tamaño de la página, los píxeles y el filtro del objeto de imagen
salen de PDFium, y las marcas de fecha y de metadatos, de los bytes del archivo. Los píxeles
esperados se calculan en la prueba (`tests.strategies.images.oriented_pixels`, la fórmula del
diseño para la composición sobre blanco), no con las funciones del worker.

El PDF recodifica la imagen como JPEG (ver el docstring del worker), así que los colores se
comparan con la tolerancia `PDF_TOLERANCE` sobre el centro de bloques de color uniforme.
"""

from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass
from pathlib import Path

# pypdfium2 no publica anotaciones de tipos; sus módulos quedan como Any
import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pytest
from PIL import Image, JpegImagePlugin, UnidentifiedImageError

from converter.workers import image_pdf as worker
from converter.workers.image import DEFAULT_MAX_IMAGE_PIXELS
from converter.workers.image_pdf import (
    MAX_JPEG_DIMENSION,
    WorkerArgs,
    convert_file,
    format_args,
    main,
    page_resolution,
    parse_args,
    pdf_save_options,
    prepare_for_pdf,
)
from tests.image_samples import (
    BLOCK,
    GRID_COLUMNS,
    GRID_ROWS,
    block_centres,
    close,
    expected_blocks,
    grid_file,
    png_bytes,
)

FORMATS = ("PNG", "JPG", "WEBP")
TAGS = [*range(1, 9), 0, None]  # etiquetas Orientation: de 1 a 8, fuera de rango (0) y ausente
PDF_TOLERANCE = 6  # por canal, sobre el centro de bloques de color uniforme (JPEG de calidad 95)
POINTS_PER_PIXEL = 0.75  # 96 ppp
MAX_PAGE_POINTS = 14_400
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
MEDIA_BOX = re.compile(rb"/MediaBox \[ 0 0 (\S+) (\S+) \]")


@dataclass(frozen=True, slots=True)
class PdfContent:
    """Lo que PDFium lee de un PDF de una página con un único objeto de imagen."""

    page_size: tuple[float, float]  # ancho y alto de la página, en puntos
    bounds: tuple[float, float, float, float]  # izquierda, abajo, derecha y arriba de la imagen
    image: Image.Image  # píxeles decodificados del objeto de imagen
    filters: list[str]  # filtros del flujo de la imagen, p. ej. ["DCTDecode"]
    stream: bytes  # flujo de la imagen sin decodificar


def _read_pdf(path: Path) -> PdfContent:
    """Lee `path` y exige exactamente una página con exactamente un objeto, de imagen."""
    document = pdfium.PdfDocument(str(path))
    try:
        assert len(document) == 1
        page = document[0]
        objects = list(page.get_objects())
        assert len(objects) == 1
        image_object = objects[0]
        assert isinstance(image_object, pdfium.PdfImage)
        pixels = image_object.get_bitmap().to_pil().copy()
        assert pixels.size == tuple(image_object.get_px_size())
        return PdfContent(
            page_size=tuple(page.get_size()),
            bounds=tuple(image_object.get_bounds()),
            image=pixels,
            filters=list(image_object.get_filters()),
            stream=bytes(image_object.get_data(decode_simple=False)),
        )
    finally:
        document.close()


def _convert(
    tmp_path: Path,
    source: str,
    data: bytes,
    *,
    name: str = "result.pdf",
    max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS,
) -> Path:
    """Convierte `data` con el worker y devuelve el PDF."""
    source_file = tmp_path / "source"
    source_file.write_bytes(data)
    destination = tmp_path / name
    convert_file(
        WorkerArgs(
            source_format=source,
            source=source_file,
            destination=destination,
            max_image_pixels=max_image_pixels,
        )
    )
    return destination


def _save(im: Image.Image, fmt: str, **params: object) -> bytes:
    out = io.BytesIO()
    im.save(out, fmt, **params)
    return out.getvalue()


def _flat(width: int, height: int, mode: str = "RGB") -> bytes:
    """PNG de `width` × `height` de un solo color."""
    colour = {"RGB": (10, 80, 160), "L": 90}[mode]
    return _save(Image.new(mode, (width, height), colour), "PNG")  # type: ignore[arg-type]


def _media_box(path: Path) -> tuple[float, float]:
    """Ancho y alto de `/MediaBox` tal como están escritos en el archivo."""
    match = MEDIA_BOX.search(path.read_bytes())
    assert match is not None
    return float(match.group(1)), float(match.group(2))


def _jpeg_markers(data: bytes) -> list[int]:
    """Marcadores de los segmentos de un JPEG hasta el inicio del escaneo (SOS)."""
    markers: list[int] = []
    position = 2  # después de SOI
    while position < len(data):
        assert data[position] == 0xFF
        marker = data[position + 1]
        markers.append(marker)
        if marker == 0xDA:
            break
        position += 2 + int.from_bytes(data[position + 2 : position + 4], "big")
    return markers


def _exact(colour: int, alpha: int) -> int:
    """`round(c·α/255 + 255·(1 − α/255))` con aritmética entera: nunca hay empates."""
    return (colour * alpha + 255 * (255 - alpha) + 127) // 255


def _pixel(im: Image.Image, x: int, y: int) -> tuple[int, ...]:
    pixel = im.convert("RGB").getpixel((x, y))
    assert isinstance(pixel, tuple)
    return pixel


# --- Página con una imagen (Req 5.4, 5.10) ---


@pytest.mark.req("5.4", "5.10")
@pytest.mark.parametrize("tag", TAGS, ids=lambda tag: f"etiqueta {tag}")
@pytest.mark.parametrize("source", FORMATS)
def test_the_pdf_has_one_page_with_one_oriented_image_of_the_displayed_size(
    tmp_path: Path, source: str, tag: int | None
) -> None:
    pdf = _read_pdf(_convert(tmp_path, source, grid_file(source, tag)))

    columns, rows, expected = expected_blocks(tag)
    width, height = columns * BLOCK, rows * BLOCK  # Dimensiones_Mostradas
    assert pdf.image.size == (width, height)
    assert pdf.page_size == pytest.approx((width * POINTS_PER_PIXEL, height * POINTS_PER_PIXEL))
    # La imagen ocupa la página completa: todos sus píxeles quedan dentro
    assert pdf.bounds == pytest.approx((0, 0, *pdf.page_size))
    centres = block_centres(pdf.image)
    assert all(close(a, b, PDF_TOLERANCE) for a, b in zip(centres, expected, strict=True)), (
        centres,
        expected,
    )


@pytest.mark.req("5.10")
@pytest.mark.parametrize("source", FORMATS)
def test_the_pdf_has_no_exif_xmp_or_orientation(tmp_path: Path, source: str) -> None:
    data = grid_file(source, 6, xmp=True)
    assert any(marker in data for marker in METADATA_MARKERS)  # el origen sí los tiene

    result = _convert(tmp_path, source, data)

    content = result.read_bytes()
    assert [marker for marker in METADATA_MARKERS if marker in content] == []
    assert b"/Rotate" not in content
    # El JPEG de la imagen no tiene segmento APP1 (0xE1), donde van el EXIF y el XMP
    assert 0xE1 not in _jpeg_markers(_read_pdf(result).stream)


@pytest.mark.req("5.4")
@pytest.mark.parametrize(
    ("width", "height"),
    [
        (48, 32),
        (1, 1),
        (19200, 10),  # 14 400 pt exactos: sin escala
        (19201, 10),
        (30000, 100),
        (100, 30000),
        (MAX_JPEG_DIMENSION, 3),
    ],
)
def test_the_page_is_075_points_per_pixel_scaled_uniformly_over_14400_points(
    tmp_path: Path, width: int, height: int
) -> None:
    result = _convert(tmp_path, "PNG", _flat(width, height))

    scale = min(1.0, MAX_PAGE_POINTS / (max(width, height) * POINTS_PER_PIXEL))
    expected = (width * POINTS_PER_PIXEL * scale, height * POINTS_PER_PIXEL * scale)
    assert _media_box(result) == pytest.approx(expected, rel=1e-9)
    assert max(_media_box(result)) <= MAX_PAGE_POINTS * (1 + 1e-12)
    pdf = _read_pdf(result)
    assert pdf.image.size == (width, height)  # el objeto de imagen no se escala
    assert pdf.bounds == pytest.approx((0, 0, *pdf.page_size), abs=1e-3)
    # La relación entre ancho y alto con que la página presenta la imagen difiere en 0,5 % o menos
    shown = pdf.page_size[0] / pdf.page_size[1]
    assert abs(shown / (width / height) - 1) <= 0.005


@pytest.mark.req("5.4")
def test_page_resolution_is_96_ppi_until_the_page_would_pass_14400_points() -> None:
    assert page_resolution(48, 32) == 96.0
    assert page_resolution(19200, 5) == 96.0
    assert page_resolution(5, 19200) == 96.0
    assert page_resolution(38400, 5) == pytest.approx(192.0)
    assert page_resolution(5, 28800) == pytest.approx(144.0)


# --- Color, transparencia y 16 bits ---


def _two_blocks(mode: str, left: object, right: object) -> Image.Image:
    """Imagen de 32 × 16 con un bloque a la izquierda y otro a la derecha."""
    im = Image.new(mode, (32, 16), left)  # type: ignore[arg-type]
    im.paste(right, (16, 0, 32, 16))  # type: ignore[arg-type]
    return im


def _paletted() -> Image.Image:
    im = _two_blocks("P", 0, 1)
    im.putpalette([200, 50, 50, 10, 10, 10])
    return im


def _rows(left: list[int], right: list[int]) -> list[list[int]]:
    return [[*(left * 16), *(right * 16)]] * 16


# Imagen de 32 × 16 con un bloque opaco a la izquierda y uno transparente a la derecha: formato de
# origen, archivo y color RGB del bloque opaco
OPAQUE_AND_TRANSPARENT: dict[str, tuple[str, bytes, tuple[int, int, int]]] = {
    "RGBA": (
        "PNG",
        _save(_two_blocks("RGBA", (200, 50, 50, 255), (10, 10, 10, 0)), "PNG"),
        (200, 50, 50),
    ),
    "LA": ("PNG", _save(_two_blocks("LA", (100, 255), (50, 0)), "PNG"), (100, 100, 100)),
    "paleta con un índice transparente": (
        "PNG",
        _save(_paletted(), "PNG", transparency=1),
        (200, 50, 50),
    ),
    "gris con color clave": (
        "PNG",
        png_bytes(32, 16, _rows([100], [50]), bitdepth=8, greyscale=True, transparent=(50,)),
        (100, 100, 100),
    ),
    "RGBA de 16 bits": (
        "PNG",
        png_bytes(
            32,
            16,
            _rows([200 * 257, 50 * 257, 50 * 257, 65535], [10 * 257, 10 * 257, 10 * 257, 0]),
            bitdepth=16,
            alpha=True,
        ),
        (200, 50, 50),
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


@pytest.mark.req("5.4")
@pytest.mark.parametrize("case", OPAQUE_AND_TRANSPARENT)
def test_transparent_pixels_become_white_and_opaque_ones_keep_their_colour(
    tmp_path: Path, case: str
) -> None:
    source, data, opaque = OPAQUE_AND_TRANSPARENT[case]

    result = _convert(tmp_path, source, data)

    pdf = _read_pdf(result)
    assert pdf.image.size == (32, 16)
    assert close(_pixel(pdf.image, 8, 8), opaque, PDF_TOLERANCE)
    assert close(_pixel(pdf.image, 24, 8), (255, 255, 255), PDF_TOLERANCE)
    # Sin canal alfa en el PDF: ni máscara suave ni JPEG 2000
    content = result.read_bytes()
    assert b"/SMask" not in content
    assert b"/JPXDecode" not in content


@pytest.mark.req("5.4")
@pytest.mark.parametrize("source", ["PNG", "WEBP"])
def test_partial_opacity_mixes_the_colour_with_white(tmp_path: Path, source: str) -> None:
    im = _two_blocks("RGBA", (0, 255, 0, 128), (0, 0, 255, 64))
    data = _save(im, "PNG") if source == "PNG" else _save(im, "WEBP", lossless=True, exact=True)

    pdf = _read_pdf(_convert(tmp_path, source, data))

    left, right = _pixel(pdf.image, 8, 8), _pixel(pdf.image, 24, 8)
    assert close(left, (_exact(0, 128), _exact(255, 128), _exact(0, 128)), PDF_TOLERANCE)
    assert close(right, (_exact(0, 64), _exact(0, 64), _exact(255, 64)), PDF_TOLERANCE)


# Muestras de un píxel de 16 bits por canal por modo, y si el PNG es gris y si tiene alfa
SIXTEEN_BIT_SAMPLES: dict[str, tuple[tuple[int, ...], bool, bool]] = {
    "L": ((0x8081,), True, False),
    "LA": ((0x8081, 0xFFFF), True, True),
    "RGB": ((0x1234, 0x5678, 0x9ABC), False, False),
    "RGBA": ((0x1234, 0x5678, 0x9ABC, 0xFFFF), False, True),
}


@pytest.mark.req("5.4")
@pytest.mark.parametrize("mode", SIXTEEN_BIT_SAMPLES)
def test_16_bit_pngs_are_stored_with_8_bits_per_channel(tmp_path: Path, mode: str) -> None:
    sample, greyscale, alpha = SIXTEEN_BIT_SAMPLES[mode]
    data = png_bytes(
        BLOCK, BLOCK, [list(sample) * BLOCK] * BLOCK, bitdepth=16, greyscale=greyscale, alpha=alpha
    )

    result = _convert(tmp_path, "PNG", data)

    pdf = _read_pdf(result)
    high = [value >> 8 for value in sample]
    expected = (high[0],) * 3 if mode in ("L", "LA") else tuple(high[:3])
    assert pdf.image.size == (BLOCK, BLOCK)
    assert close(_pixel(pdf.image, BLOCK // 2, BLOCK // 2), expected, PDF_TOLERANCE)
    assert b"/BitsPerComponent 8" in result.read_bytes()
    assert b"/BitsPerComponent 16" not in result.read_bytes()


@pytest.mark.req("5.4")
@pytest.mark.parametrize(("mode", "colour_space"), [("L", b"/DeviceGray"), ("RGB", b"/DeviceRGB")])
def test_gray_sources_stay_gray_and_colour_sources_stay_colour(
    tmp_path: Path, mode: str, colour_space: bytes
) -> None:
    content = _convert(tmp_path, "PNG", _flat(8, 8, mode)).read_bytes()

    other = b"/DeviceRGB" if colour_space == b"/DeviceGray" else b"/DeviceGray"
    assert colour_space in content
    assert other not in content


def test_prepare_for_pdf_composes_alpha_on_white_and_keeps_l_and_rgb() -> None:
    for mode in ("L", "RGB"):
        im = Image.new(mode, (2, 2))
        assert prepare_for_pdf(im) is im
    flattened = prepare_for_pdf(Image.new("RGBA", (2, 2), (1, 2, 3, 0)))
    assert flattened.mode == "RGB"
    assert _pixel(flattened, 0, 0) == (255, 255, 255)


@pytest.mark.parametrize("mode", ["1", "P", "LA", "CMYK", "I;16"])
def test_prepare_for_pdf_rejects_modes_that_normalize_mode_does_not_return(mode: str) -> None:
    with pytest.raises(ValueError, match="L, RGB o RGBA"):
        prepare_for_pdf(Image.new(mode, (2, 2)))


# --- Recodificación como JPEG (Req 4.11) ---


@pytest.mark.req("4.11")
@pytest.mark.parametrize("source", FORMATS)
def test_the_image_is_recompressed_as_a_jpeg_at_quality_95_without_subsampling(
    tmp_path: Path, source: str
) -> None:
    data = grid_file(source)

    pdf = _read_pdf(_convert(tmp_path, source, data))

    # Limitación del MVP: ningún par es Sin_Pérdida, ni siquiera JPG a PDF, que no inserta el
    # flujo original. Esta prueba cambia cuando exista el escritor propio
    assert pdf.filters == ["DCTDecode"]
    assert pdf.stream != data
    with Image.open(io.BytesIO(pdf.stream)) as jpeg:
        assert isinstance(jpeg, JpegImagePlugin.JpegImageFile)
        assert jpeg.format == "JPEG"
        assert JpegImagePlugin.get_sampling(jpeg) == 0  # 4:4:4
        # El primer valor de la tabla de luminancia a calidad 95: (16 × 10 + 50) // 100
        assert jpeg.quantization[0][0] == 2


def test_save_options_fix_the_dates_and_title_and_carry_no_metadata() -> None:
    options = pdf_save_options(48, 32)

    assert options == {
        "resolution": 96.0,
        "quality": 95,
        "subsampling": 0,
        "creationDate": None,
        "modDate": None,
        "title": None,
    }
    assert {"exif", "xmp", "icc_profile"}.isdisjoint(options)


# --- Determinismo (Req 13.4) ---


@pytest.mark.req("13.4")
@pytest.mark.parametrize("source", FORMATS)
def test_the_pdf_has_no_dates_title_or_info_dictionary(tmp_path: Path, source: str) -> None:
    content = _convert(tmp_path, source, grid_file(source, 8)).read_bytes()

    assert content.startswith(b"%PDF-")
    for name in (b"/CreationDate", b"/ModDate", b"/Title", b"/Producer", b"/Info", b"/ID"):
        assert name not in content


@pytest.mark.req("13.4")
def test_the_same_source_gives_the_same_pdf_bytes_one_second_apart(tmp_path: Path) -> None:
    sources = {fmt: grid_file(fmt, 8) for fmt in FORMATS}
    first = {
        fmt: _convert(tmp_path, fmt, data, name=f"a-{fmt}").read_bytes()
        for fmt, data in sources.items()
    }

    time.sleep(1.1)  # Pillow escribe la hora en segundos: con otras opciones, cambiaría

    second = {
        fmt: _convert(tmp_path, fmt, data, name=f"b-{fmt}").read_bytes()
        for fmt, data in sources.items()
    }
    assert first == second


# --- Argumentos ---


def test_args_round_trip_and_defaults() -> None:
    args = WorkerArgs(
        source_format="WEBP",
        source=Path("-origen"),
        destination=Path("/ws/work/converted"),
        max_image_pixels=1234,
    )

    assert parse_args(format_args(args)) == args
    defaults = parse_args(["--", "JPG", "a", "b"])
    assert defaults.max_image_pixels == DEFAULT_MAX_IMAGE_PIXELS == 100_000_000


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["PNG", "a"],
        ["GIF", "a", "b"],
        ["PDF", "a", "b"],
        ["PNG", "JPG", "a", "b"],  # el formato de destino ya no es un argumento
        ["--max-image-pixels", "0", "PNG", "a", "b"],
        ["--max-image-pixels", "1e3", "PNG", "a", "b"],
        ["--max-image", "5", "PNG", "a", "b"],  # sin abreviaturas
    ],
)
def test_parse_args_rejects_invalid_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(argv)

    assert exit_info.value.code == worker.EXIT_USAGE


def test_main_converts_the_file_and_returns_zero(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(grid_file("PNG"))
    destination = tmp_path / "out.pdf"

    code = main(["--", "PNG", str(source), str(destination)])

    assert code == worker.EXIT_OK
    assert _read_pdf(destination).image.size == (GRID_COLUMNS * BLOCK, GRID_ROWS * BLOCK)


# --- Errores ---


def test_a_truncated_source_raises_an_os_error(tmp_path: Path) -> None:
    data = grid_file("PNG")

    with pytest.raises(OSError, match="truncated"):
        _convert(tmp_path, "PNG", data[: len(data) // 2])


@pytest.mark.parametrize(
    ("source", "data"),
    [("PNG", b"no es una imagen" * 8), ("PNG", grid_file("JPG")), ("JPG", grid_file("WEBP"))],
    ids=["texto", "JPG declarado como PNG", "WEBP declarado como JPG"],
)
def test_a_source_that_is_not_of_the_declared_format_is_not_identified(
    tmp_path: Path, source: str, data: bytes
) -> None:
    with pytest.raises(UnidentifiedImageError):
        _convert(tmp_path, source, data)


@pytest.mark.parametrize("limit", [1_000, 100])
def test_an_image_over_the_pixel_limit_is_not_decoded(tmp_path: Path, limit: int) -> None:
    previous = Image.MAX_IMAGE_PIXELS

    # 48 × 32 = 1536 píxeles: con 1000, Pillow solo avisa; con 100 (menos de la mitad), falla
    with pytest.raises((Image.DecompressionBombWarning, Image.DecompressionBombError)):
        _convert(tmp_path, "PNG", grid_file("PNG"), max_image_pixels=limit)

    assert previous == Image.MAX_IMAGE_PIXELS


@pytest.mark.parametrize("size", [(MAX_JPEG_DIMENSION + 1, 1), (1, MAX_JPEG_DIMENSION + 1)])
def test_a_side_over_the_jpeg_limit_raises_a_value_error_not_a_broken_stream(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    # Pillow lo informaría como `OSError: broken data stream`, que el Conversor clasifica como
    # archivo dañado
    with pytest.raises(ValueError, match="65500"):
        _convert(tmp_path, "PNG", _flat(*size, mode="L"))


@pytest.mark.req("5.10")
def test_the_limit_applies_to_the_displayed_size(tmp_path: Path) -> None:
    # 1 × 65501 almacenado con orientación 6 se muestra como 65501 × 1
    im = Image.new("L", (1, MAX_JPEG_DIMENSION + 1))
    exif = Image.Exif()
    exif[0x0112] = 6

    with pytest.raises(ValueError, match="65501 × 1 "):
        _convert(tmp_path, "PNG", _save(im, "PNG", exif=exif))
