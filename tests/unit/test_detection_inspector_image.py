"""Pruebas del inspector `image` de src/converter/detection/inspectors/image.py (Req 5.6, 5.9,
16.1, 16.2).

Los archivos se construyen en la prueba con Pillow y pypng: un PNG, un JPG o un WEBP por
caso, con los chunks, los segmentos y los metadatos que pide cada uno. El inspector corre en el
proceso de la prueba; las últimas pruebas lo ejecutan también en el worker real y a través del
Ejecutor aislado. La Orientación_EXIF se compara con la que lee `read_orientation` sobre la
imagen ya decodificada, que es la que ven los convertidores.
"""

from __future__ import annotations

import io
import struct
import subprocess
import sys
import warnings
from pathlib import Path

import png  # type: ignore[import-untyped]
import pytest
from PIL import Image, ImageFile, PngImagePlugin, WebPImagePlugin

from converter.converters.orientation import displayed_size, read_orientation
from converter.detection.detector import InspectionRequest
from converter.detection.inspect_runner import InspectRunner
from converter.detection.inspectors import InspectorInput, get_inspector
from converter.detection.inspectors.image import BITS_PER_CHANNEL, ImageInfo
from converter.detection.source_info import (
    InspectorOutputError,
    SourceInfo,
    SourceRejection,
    dump_inspector_output,
    load_inspector_output,
    registered_source_infos,
)
from converter.eventlog import Tool
from converter.messages_es import FailureReason
from converter.models import Stage
from converter.sandbox.limits import SandboxTool
from converter.sandbox.runner import SandboxRunner
from converter.storage.workspace import Workspace, WorkspaceManager
from converter.workers.inspect import MODULE
from tests.strategies.images import exif_block

LIMIT = 100_000_000  # Límite_de_Píxeles predeterminado
_EXIF_PREFIX = b"Exif\x00\x00"
_FORMATS = ("PNG", "JPEG", "WEBP")
_REAL_TYPES = {"PNG": "PNG", "JPEG": "JPG", "WEBP": "WEBP"}
_TIMEOUT_S = 60

_XMP_ORIENTATION_6 = (
    '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
    'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description '
    'xmlns:tiff="http://ns.adobe.com/tiff/1.0/" tiff:Orientation="6"/></rdf:RDF></x:xmpmeta>'
)


# --- Construcción de archivos ---


def _picture(width: int, height: int, mode: str = "RGB") -> Image.Image:
    """Imagen con valores distintos en cada píxel y canal."""
    channels = len(mode)
    data = bytes(
        (31 * x + 17 * y + 53 * c) % 256
        for y in range(height)
        for x in range(width)
        for c in range(channels)
    )
    return Image.frombytes(mode, (width, height), data)


def _save(im: Image.Image, fmt: str, **params: object) -> bytes:
    out = io.BytesIO()
    im.save(out, fmt, **params)
    return out.getvalue()


def _with_png_chunks(data: bytes, chunks: list[tuple[bytes, bytes]], *, after_idat: bool) -> bytes:
    """Agrega `chunks` a un PNG antes del primer IDAT o, con `after_idat`, antes de IEND."""
    existing = list(png.Reader(bytes=data).chunks())
    kinds = [kind for kind, _ in existing]
    position = kinds.index(b"IEND") if after_idat else kinds.index(b"IDAT")
    existing[position:position] = chunks
    out = io.BytesIO()
    png.write_chunks(out, existing)
    return out.getvalue()


def _exif(tag: int | None) -> bytes | None:
    return None if tag is None else exif_block(tag, big_endian=False, other_tag=False)


def _file_with_tag(
    fmt: str, tag: int | None, size: tuple[int, int] = (8, 6), *, after_idat: bool = False
) -> bytes:
    """Imagen de `fmt` con la etiqueta Orientation igual a `tag`, o sin EXIF si es None."""
    picture = _picture(*size)
    exif = _exif(tag)
    if fmt == "PNG":
        data = _save(picture, "PNG")
        if exif is None:
            return data
        chunk = (b"eXIf", exif.removeprefix(_EXIF_PREFIX))
        return _with_png_chunks(data, [chunk], after_idat=after_idat)
    params: dict[str, object] = {} if exif is None else {"exif": exif}
    if fmt == "WEBP":
        params["lossless"] = True
    return _save(picture, fmt, **params)


def _png16(kind: str, width: int = 4, height: int = 3) -> bytes:
    """PNG de 16 bits por canal escrito con pypng; `kind` es L, LA, RGB o RGBA."""
    channels = len(kind)
    writer = png.Writer(
        width,
        height,
        greyscale=kind in ("L", "LA"),
        alpha=kind in ("LA", "RGBA"),
        bitdepth=16,
    )
    rows = [
        [(997 * x + 13 * y + c) % 65536 for x in range(width) for c in range(channels)]
        for y in range(height)
    ]
    out = io.BytesIO()
    writer.write(out, rows)
    return out.getvalue()


def _single_frame_apng(width: int = 4, height: int = 3) -> bytes:
    """APNG con un único cuadro: acTL con un cuadro y fcTL antes de IDAT."""
    data = _save(_picture(width, height), "PNG")
    actl = struct.pack(">II", 1, 0)  # un cuadro, repetición infinita
    fctl = struct.pack(">IIIIIHHBB", 0, width, height, 0, 0, 0, 1, 0, 0)
    return _with_png_chunks(data, [(b"acTL", actl), (b"fcTL", fctl)], after_idat=False)


def _animated(fmt: str) -> bytes:
    first, second = _picture(8, 6), _picture(8, 6).transpose(Image.Transpose.ROTATE_180)
    return _save(first, fmt, save_all=True, append_images=[second])


_JPEG = _save(_picture(8, 6), "JPEG")
_PNG = _save(_picture(8, 6), "PNG")
# Con la Orientación_EXIF 6 en un chunk eXIf posterior a IDAT. Se arma al importar el módulo:
# `no_decoding` también impide que Pillow guarde imágenes
_PNG_EXIF_AT_END = _file_with_tag("PNG", 6, after_idat=True)


def _decoded_orientation(data: bytes) -> int:
    """Orientación_EXIF que ve un convertidor: la de `read_orientation` con la imagen cargada."""
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        return read_orientation(im)


# --- Ejecución del inspector ---


def _inspect(
    tmp_path: Path, data: bytes, real_type: str | None = "PNG", limit: int = LIMIT
) -> SourceInfo | SourceRejection:
    source = tmp_path / "original"
    source.write_bytes(data)
    inspector = get_inspector("image")
    return inspector.function(
        InspectorInput(path=source, real_type=real_type, max_image_pixels=limit)
    )


def _info(
    tmp_path: Path, data: bytes, real_type: str | None = "PNG", limit: int = LIMIT
) -> ImageInfo:
    result = _inspect(tmp_path, data, real_type, limit)
    assert isinstance(result, ImageInfo), result
    return result


def _rejection(
    tmp_path: Path, data: bytes, real_type: str | None = "PNG", limit: int = LIMIT
) -> SourceRejection:
    result = _inspect(tmp_path, data, real_type, limit)
    assert isinstance(result, SourceRejection), result
    return result


@pytest.fixture
def no_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hace fallar toda carga de píxeles de Pillow."""

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("el inspector decodificó los píxeles")

    for cls in (Image.Image, ImageFile.ImageFile, WebPImagePlugin.WebPImageFile):
        monkeypatch.setattr(cls, "load", fail)


# --- ImageInfo y registro ---


def test_image_inspector_is_registered_with_its_source_info() -> None:
    spec = get_inspector("image")
    assert spec.info_type is ImageInfo
    assert (spec.tool, spec.sandbox_tool) == (Tool.PILLOW, SandboxTool.PYTHON)
    assert registered_source_infos()["image"] is ImageInfo


@pytest.mark.req("5.9")
@pytest.mark.parametrize(("orientation", "swapped"), [(o, o >= 5) for o in range(1, 9)])
def test_displayed_size_swaps_the_dimensions_for_orientations_5_to_8(
    orientation: int, swapped: bool
) -> None:
    info = ImageInfo(8, 6, "RGB", 8, False, 1, orientation)
    assert info.displayed_size == ((6, 8) if swapped else (8, 6))
    assert info.displayed_size == displayed_size(8, 6, orientation)


@pytest.mark.req("5.9")
def test_image_info_travels_as_json() -> None:
    info = ImageInfo(8, 6, "RGBA", 16, True, 1, 6)
    assert ImageInfo.from_json(info.to_json()) == info
    assert load_inspector_output(dump_inspector_output(info)) == info
    # `displayed_size` es una propiedad: no es un campo de la forma JSON
    data = info.to_json()["data"]
    assert isinstance(data, dict)
    assert set(data) == {
        "width",
        "height",
        "mode",
        "bits_per_channel",
        "has_alpha",
        "frames",
        "exif_orientation",
    }


@pytest.mark.parametrize(
    "override",
    [
        {"width": 0},
        {"height": -1},
        {"width": True},
        {"mode": ""},
        {"mode": 3},
        {"bits_per_channel": 3},
        {"bits_per_channel": 32},
        {"bits_per_channel": True},
        {"has_alpha": 1},
        {"frames": 0},
        {"exif_orientation": 0},
        {"exif_orientation": 9},
    ],
)
def test_image_info_rejects_invalid_values(override: dict[str, object]) -> None:
    values: dict[str, object] = {
        "width": 8,
        "height": 6,
        "mode": "RGB",
        "bits_per_channel": 8,
        "has_alpha": False,
        "frames": 1,
        "exif_orientation": 1,
    }
    with pytest.raises((TypeError, ValueError)):
        ImageInfo(**{**values, **override})  # type: ignore[arg-type]
    # La salida de un worker con esos valores no se acepta
    with pytest.raises(InspectorOutputError):
        ImageInfo.from_json({"type": "image", "data": {**values, **override}})


# --- Datos de la cabecera ---


@pytest.mark.parametrize(
    ("mode", "bits", "alpha"),
    [
        ("1", 1, False),
        ("L", 8, False),
        ("LA", 8, True),
        ("RGB", 8, False),
        ("RGBA", 8, True),
    ],
)
def test_png_modes_bits_and_alpha(tmp_path: Path, mode: str, bits: int, alpha: bool) -> None:
    picture = _picture(8, 6, "L").convert("1") if mode == "1" else _picture(8, 6, mode)
    info = _info(tmp_path, _save(picture, "PNG"))
    assert info == ImageInfo(8, 6, mode, bits, alpha, 1, 1)


@pytest.mark.parametrize(("colors", "bits"), [(2, 1), (4, 2), (16, 4), (256, 8)])
@pytest.mark.parametrize("transparency", [None, 0, b"\x00\x80"])
def test_png_palette_bits_come_from_ihdr(
    tmp_path: Path, colors: int, bits: int, transparency: int | bytes | None
) -> None:
    picture = Image.frombytes("P", (8, 6), bytes(i % colors for i in range(48)))
    picture.putpalette([value for i in range(colors) for value in (i % 256, 255 - i % 256, 7)])
    params: dict[str, object] = {} if transparency is None else {"transparency": transparency}
    data = _save(picture, "PNG", **params)
    assert data[24] == bits  # profundidad de IHDR
    info = _info(tmp_path, data)
    assert (info.mode, info.bits_per_channel) == ("P", bits)
    assert info.has_alpha is (transparency is not None)


@pytest.mark.parametrize(
    ("mode", "transparency"), [("L", 7), ("RGB", (10, 20, 30))], ids=["gris", "color"]
)
def test_png_color_key_transparency_counts_as_alpha(
    tmp_path: Path, mode: str, transparency: int | tuple[int, int, int]
) -> None:
    data = _save(_picture(8, 6, mode), "PNG", transparency=transparency)
    info = _info(tmp_path, data)
    assert (info.mode, info.has_alpha) == (mode, True)


@pytest.mark.parametrize("kind", ["L", "LA", "RGB", "RGBA"])
def test_16_bit_png_reports_its_bit_depth(tmp_path: Path, kind: str) -> None:
    info = _info(tmp_path, _png16(kind))
    assert (info.width, info.height) == (4, 3)
    assert info.bits_per_channel == 16
    assert info.has_alpha is kind.endswith("A")


@pytest.mark.parametrize("mode", ["L", "RGB", "CMYK"])
def test_jpg_has_8_bits_and_no_alpha(tmp_path: Path, mode: str) -> None:
    info = _info(tmp_path, _save(_picture(8, 6, mode), "JPEG"), "JPG")
    assert info == ImageInfo(8, 6, mode, 8, False, 1, 1)


@pytest.mark.parametrize("lossless", [True, False])
@pytest.mark.parametrize(("mode", "alpha"), [("RGB", False), ("RGBA", True)])
def test_webp_has_8_bits_and_alpha_from_its_mode(
    tmp_path: Path, mode: str, alpha: bool, lossless: bool
) -> None:
    data = _save(_picture(8, 6, mode), "WEBP", lossless=lossless)
    assert _info(tmp_path, data, "WEBP") == ImageInfo(8, 6, mode, 8, alpha, 1, 1)


def test_real_type_selects_the_format_and_none_accepts_any(tmp_path: Path) -> None:
    jpg = _save(_picture(8, 6), "JPEG")
    # Con el Tipo_Real indicado, el contenido tiene que ser de ese Formato
    assert _rejection(tmp_path, jpg, "PNG").reason is FailureReason.DAMAGED
    assert _rejection(tmp_path, jpg, "WEBP").reason is FailureReason.DAMAGED
    assert _rejection(tmp_path, _save(_picture(8, 6), "PNG"), "JPG").reason is FailureReason.DAMAGED
    # Sin Tipo_Real, o con uno que no es de imagen, se prueban los tres
    assert _info(tmp_path, jpg, None).width == 8
    assert _info(tmp_path, jpg, "MP4").width == 8


# --- Orientación_EXIF y Dimensiones_Mostradas ---


@pytest.mark.req("5.9")
@pytest.mark.parametrize("orientation", range(1, 9))
@pytest.mark.parametrize("fmt", _FORMATS)
def test_exif_orientation_gives_the_displayed_size(
    tmp_path: Path, fmt: str, orientation: int
) -> None:
    info = _info(tmp_path, _file_with_tag(fmt, orientation), _REAL_TYPES[fmt])
    assert (info.width, info.height) == (8, 6)
    assert info.exif_orientation == orientation
    assert info.displayed_size == ((6, 8) if orientation >= 5 else (8, 6))


@pytest.mark.req("5.9")
@pytest.mark.parametrize("after_idat", [False, True], ids=["antes de IDAT", "después de IDAT"])
@pytest.mark.parametrize("orientation", [3, 6, 8])
def test_png_exif_chunk_is_found_before_and_after_idat(
    tmp_path: Path, orientation: int, after_idat: bool
) -> None:
    data = _file_with_tag("PNG", orientation, after_idat=after_idat)
    assert _info(tmp_path, data).exif_orientation == orientation


@pytest.mark.req("5.9")
@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize("tag", [None, 0, 9, 255, 0xFFFF, 0x10003], ids=str)
def test_missing_or_out_of_range_orientation_is_normal(
    tmp_path: Path, fmt: str, tag: int | None
) -> None:
    assert _info(tmp_path, _file_with_tag(fmt, tag), _REAL_TYPES[fmt]).exif_orientation == 1


def _png_with_raw_profile(orientation: int) -> bytes:
    """PNG con el EXIF en el chunk de texto "Raw profile type exif" de ImageMagick."""
    exif = exif_block(orientation, big_endian=False, other_tag=False).removeprefix(_EXIF_PREFIX)
    text = f"\nexif\n{len(exif):>8}\n{exif.hex()}\n"
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Raw profile type exif", text)
    return _save(_picture(8, 6), "PNG", pnginfo=metadata)


def _png_with_xmp_orientation() -> bytes:
    metadata = PngImagePlugin.PngInfo()
    metadata.add_itxt("XML:com.adobe.xmp", _XMP_ORIENTATION_6)
    return _save(_picture(8, 6), "PNG", pnginfo=metadata)


@pytest.mark.req("5.9")
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param(_file_with_tag("PNG", None), 1, id="sin EXIF"),
        pytest.param(_file_with_tag("PNG", 6), 6, id="eXIf antes de IDAT"),
        pytest.param(_file_with_tag("PNG", 8, after_idat=True), 8, id="eXIf después de IDAT"),
        pytest.param(_png_with_raw_profile(5), 5, id="perfil de ImageMagick"),
        # Solo cuenta el EXIF (glosario): el XMP no define la Orientación_EXIF
        pytest.param(_png_with_xmp_orientation(), 1, id="solo XMP"),
        pytest.param(_png16("RGBA"), 1, id="16 bits"),
    ],
)
def test_png_orientation_matches_the_one_a_converter_reads_after_decoding(
    tmp_path: Path, data: bytes, expected: int
) -> None:
    assert _decoded_orientation(data) == expected
    assert _info(tmp_path, data).exif_orientation == expected


# --- Sin decodificar los píxeles ---


@pytest.mark.req("5.9")
@pytest.mark.parametrize(
    ("data", "real_type"),
    [
        pytest.param(_file_with_tag("PNG", None), "PNG", id="PNG sin EXIF"),
        pytest.param(_file_with_tag("PNG", 6, after_idat=True), "PNG", id="PNG con eXIf al final"),
        pytest.param(_png_with_raw_profile(6), "PNG", id="PNG con perfil de texto"),
        pytest.param(_png16("RGB"), "PNG", id="PNG de 16 bits"),
        pytest.param(_single_frame_apng(), "PNG", id="APNG de un cuadro"),
        pytest.param(_file_with_tag("JPEG", 6), "JPG", id="JPG"),
        pytest.param(_file_with_tag("WEBP", 6), "WEBP", id="WEBP"),
        pytest.param(_animated("PNG"), "PNG", id="APNG animado"),
        pytest.param(_animated("WEBP"), "WEBP", id="WEBP animado"),
    ],
)
def test_inspection_never_decodes_pixels(
    tmp_path: Path, no_decoding: None, data: bytes, real_type: str
) -> None:
    # Si decodificara, `load` fallaría con AssertionError y la prueba también
    assert isinstance(_inspect(tmp_path, data, real_type), (ImageInfo, SourceRejection))


@pytest.mark.req("5.9")
def test_read_orientation_alone_decodes_a_png_without_exif(no_decoding: None) -> None:
    # Por eso el inspector no la llama con el PNG abierto
    with Image.open(io.BytesIO(_PNG)) as im, pytest.raises(AssertionError):
        read_orientation(im)


@pytest.mark.req("16.2")
def test_pixel_data_truncated_after_a_whole_header_is_left_to_the_conversion(
    tmp_path: Path,
) -> None:
    data = _save(_picture(64, 48), "PNG")
    truncated = data[: len(data) // 2]
    with pytest.raises(OSError, match="truncated"), Image.open(io.BytesIO(truncated)) as im:
        im.load()
    info = _info(tmp_path, truncated)
    assert (info.width, info.height, info.bits_per_channel) == (64, 48, 8)


# --- Rechazos ---


@pytest.mark.req("5.6", "16.1")
@pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
def test_animated_image_is_rejected(tmp_path: Path, fmt: str) -> None:
    rejection = _rejection(tmp_path, _animated(fmt), fmt)
    assert rejection == SourceRejection(FailureReason.ANIMATED_IMAGE)
    assert rejection.failure.stage is Stage.DETECTION


@pytest.mark.req("5.6")
def test_png_and_webp_with_a_single_frame_are_accepted(tmp_path: Path) -> None:
    apng = _info(tmp_path, _single_frame_apng(5, 4))
    assert (apng.width, apng.height, apng.frames) == (5, 4, 1)
    webp = _save(_picture(5, 4), "WEBP", save_all=True, append_images=[])
    assert _info(tmp_path, webp, "WEBP").frames == 1


@pytest.mark.req("5.6")
def test_jpg_with_several_images_is_not_an_animated_image(tmp_path: Path) -> None:
    mpo = _save(_picture(8, 6), "MPO", save_all=True, append_images=[_picture(8, 6)])
    with Image.open(io.BytesIO(mpo)) as im:
        assert im.n_frames == 2
    assert _info(tmp_path, mpo, "JPG") == ImageInfo(8, 6, "RGB", 8, False, 1, 1)


@pytest.mark.req("5.9", "16.1")
def test_image_at_the_pixel_limit_is_accepted_and_one_pixel_over_is_rejected(
    tmp_path: Path,
) -> None:
    data = _file_with_tag("PNG", None)  # 8 × 6 = 48 píxeles
    assert _info(tmp_path, data, limit=48).width == 8
    rejection = _rejection(tmp_path, data, limit=47)
    assert rejection == SourceRejection(
        FailureReason.PIXEL_LIMIT, {"ancho": 8, "alto": 6, "limite_pixeles": 47}
    )
    assert rejection.failure.stage is Stage.DETECTION


@pytest.mark.req("5.9")
@pytest.mark.parametrize("orientation", range(1, 9))
@pytest.mark.parametrize(
    "limit", [47, 20], ids=["más del límite", "más del doble del límite (Pillow falla)"]
)
@pytest.mark.parametrize("fmt", _FORMATS)
def test_pixel_limit_rejection_reports_the_displayed_size(
    tmp_path: Path, fmt: str, limit: int, orientation: int
) -> None:
    # En PNG, el eXIf va al final: solo se lee sin decodificar los píxeles
    data = _file_with_tag(fmt, orientation, after_idat=True)
    rejection = _rejection(tmp_path, data, _REAL_TYPES[fmt], limit)
    width, height = (6, 8) if orientation >= 5 else (8, 6)
    assert rejection == SourceRejection(
        FailureReason.PIXEL_LIMIT, {"ancho": width, "alto": height, "limite_pixeles": limit}
    )


@pytest.mark.req("5.9")
@pytest.mark.parametrize("limit", [47, 20])
def test_pixel_limit_rejection_does_not_decode_pixels(
    tmp_path: Path, no_decoding: None, limit: int
) -> None:
    assert _rejection(tmp_path, _PNG_EXIF_AT_END, limit=limit).params["ancho"] == 6


@pytest.mark.req("5.9", "5.6")
def test_animation_is_reported_before_the_pixel_limit(tmp_path: Path) -> None:
    assert _rejection(tmp_path, _animated("PNG"), limit=1).reason is FailureReason.ANIMATED_IMAGE
    assert _rejection(tmp_path, _animated("WEBP"), "WEBP", 1).reason is FailureReason.ANIMATED_IMAGE


@pytest.mark.req("5.9")
@pytest.mark.parametrize(("limit", "opened_with"), [(47, [47]), (20, [20, None])])
def test_pillow_pixel_limit_is_the_configured_one_and_is_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
    opened_with: list[int | None],
) -> None:
    seen: list[int | None] = []
    real_open = Image.open

    def spy(*args: object, **kwargs: object) -> ImageFile.ImageFile:
        seen.append(Image.MAX_IMAGE_PIXELS)
        return real_open(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Image, "open", spy)
    before = Image.MAX_IMAGE_PIXELS
    _rejection(tmp_path, _file_with_tag("JPEG", None), "JPG", limit)
    # Con más del doble del límite, Pillow no devuelve la imagen: se vuelve a leer la cabecera
    # sin tope para informar sus dimensiones
    assert seen == opened_with
    assert before == Image.MAX_IMAGE_PIXELS


@pytest.mark.req("5.9")
def test_pillow_warnings_do_not_reach_stderr(tmp_path: Path) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # Entre el límite y su doble Pillow avisa en lugar de fallar
        _rejection(tmp_path, _file_with_tag("PNG", None), limit=40)
    assert caught == []


def _bad_ihdr_depth() -> bytes:
    data = bytearray(_PNG)
    data[24] = 3  # profundidad que PNG no admite
    return bytes(data)


@pytest.mark.req("16.1", "16.2")
@pytest.mark.parametrize(
    ("data", "real_type"),
    [
        pytest.param(b"", "PNG", id="vacío"),
        pytest.param(b"no es una imagen", "PNG", id="texto"),
        pytest.param(_PNG[:8], "PNG", id="solo la firma de PNG"),
        pytest.param(_PNG[:20], "PNG", id="IHDR cortado"),
        pytest.param(_PNG[:40], "PNG", id="chunk cortado"),
        pytest.param(_bad_ihdr_depth(), "PNG", id="IHDR inválido"),
        pytest.param(_JPEG[:3], "JPG", id="solo la firma de JPG"),
        pytest.param(_JPEG[:20], "JPG", id="JPG sin SOF"),
        pytest.param(b"RIFF\x00\x00\x00\x00WEBP", "WEBP", id="WEBP sin chunks"),
        pytest.param(_JPEG, "PNG", id="JPG declarado como PNG"),
        pytest.param(_PNG, "JPG", id="PNG declarado como JPG"),
        pytest.param(_PNG, "WEBP", id="PNG declarado como WEBP"),
    ],
)
def test_damaged_or_unidentifiable_file_is_rejected(
    tmp_path: Path, data: bytes, real_type: str
) -> None:
    rejection = _rejection(tmp_path, data, real_type)
    assert rejection == SourceRejection(FailureReason.DAMAGED)
    assert rejection.failure.stage is Stage.DETECTION


@pytest.mark.req("16.2")
def test_file_system_errors_are_not_a_damaged_file(tmp_path: Path) -> None:
    # Leer el origen falla por un motivo del sistema, no del archivo: error interno. Pillow
    # tampoco queda con el límite de píxeles del inspector
    before = Image.MAX_IMAGE_PIXELS
    inspector = get_inspector("image")
    with pytest.raises(IsADirectoryError):
        inspector.function(InspectorInput(path=tmp_path, real_type="PNG", max_image_pixels=7))
    assert before == Image.MAX_IMAGE_PIXELS


def test_bits_per_channel_values_are_the_ones_png_allows() -> None:
    assert BITS_PER_CHANNEL == (1, 2, 4, 8, 16)


# --- Worker y Ejecutor aislado ---


@pytest.mark.req("5.9", "16.2")
def test_worker_writes_the_info_and_the_rejections_as_json(tmp_path: Path) -> None:
    source = tmp_path / "original"
    source.write_bytes(_file_with_tag("PNG", 6, after_idat=True))

    def run(limit: int) -> SourceInfo | SourceRejection:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                MODULE,
                *("--real-type", "PNG", "--max-image-pixels", str(limit)),
                *("--", "image", str(source)),
            ],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=True,
        )
        assert result.stderr == ""
        return load_inspector_output("".join(result.stdout.splitlines()))

    assert run(LIMIT) == ImageInfo(8, 6, "RGB", 8, False, 1, 6)
    assert run(47) == SourceRejection(
        FailureReason.PIXEL_LIMIT, {"ancho": 6, "alto": 8, "limite_pixeles": 47}
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    storage = tmp_path / "storage"
    storage.mkdir()
    return WorkspaceManager(storage).create()


_PIXEL_LIMIT_6X8 = SourceRejection(
    FailureReason.PIXEL_LIMIT, {"ancho": 6, "alto": 8, "limite_pixeles": 10}
)


@pytest.mark.asyncio
@pytest.mark.req("2.11", "5.9", "16.2")
@pytest.mark.parametrize(
    ("data", "limit", "expected"),
    [
        pytest.param(
            _file_with_tag("PNG", 8),
            LIMIT,
            ImageInfo(8, 6, "RGB", 8, False, 1, 8),
            id="información",
        ),
        pytest.param(_file_with_tag("PNG", 8), 10, _PIXEL_LIMIT_6X8, id="límite de píxeles"),
        pytest.param(
            _animated("PNG"), LIMIT, SourceRejection(FailureReason.ANIMATED_IMAGE), id="animada"
        ),
        pytest.param(_PNG[:40], LIMIT, SourceRejection(FailureReason.DAMAGED), id="dañada"),
    ],
)
async def test_runner_executes_the_image_inspector_in_the_sandbox(
    workspace: Workspace, data: bytes, limit: int, expected: SourceInfo | SourceRejection
) -> None:
    workspace.source_file.write_bytes(data)
    inspect = InspectRunner(SandboxRunner(), max_image_pixels=limit, python=sys.executable)
    request = InspectionRequest(inspector="image", real_type="PNG", workspace=workspace)
    assert await inspect(request) == expected
