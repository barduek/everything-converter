"""Pruebas de src/converter/converters/orientation.py: Orientación_EXIF y Dimensiones_Mostradas
(D15; Req 5.1, 5.4, 5.5, 5.9, 5.10).

Los resultados esperados de las ocho orientaciones se escriben a mano a partir de la definición
de cada una sobre una imagen de 3 × 2, no a partir de la tabla del módulo. `orient_samples` se
compara además con `Image.transpose` de Pillow en imágenes de 1 a 4 canales.
"""

from __future__ import annotations

import io
import struct
from array import array

import pytest
from PIL import Image, PngImagePlugin

from converter.converters.orientation import (
    apply_orientation,
    displayed_size,
    orient_samples,
    read_orientation,
)

_FORMATS = ("PNG", "JPEG", "WEBP")

# Imagen de 3 × 2 con las muestras 1 a 6:
#   1 2 3
#   4 5 6
_GRID = [1, 2, 3, 4, 5, 6]
# Filas de la imagen mostrada para cada Orientación_EXIF
_EXPECTED_ROWS: dict[int, list[list[int]]] = {
    1: [[1, 2, 3], [4, 5, 6]],
    2: [[3, 2, 1], [6, 5, 4]],  # reflejo horizontal
    3: [[6, 5, 4], [3, 2, 1]],  # 180°
    4: [[4, 5, 6], [1, 2, 3]],  # reflejo vertical
    5: [[1, 4], [2, 5], [3, 6]],  # reflejo sobre la diagonal principal
    6: [[4, 1], [5, 2], [6, 3]],  # 90° en sentido horario
    7: [[6, 3], [5, 2], [4, 1]],  # reflejo sobre la diagonal secundaria
    8: [[3, 6], [2, 5], [1, 4]],  # 90° en sentido antihorario
}

_XMP = (
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
    b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description '
    b'xmlns:tiff="http://ns.adobe.com/tiff/1.0/" tiff:Orientation="6"/></rdf:RDF></x:xmpmeta>'
)


def _exif(orientation: int) -> Image.Exif:
    exif = Image.Exif()
    exif[0x0112] = orientation
    return exif


def _encode(fmt: str, **params: object) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (3, 2), (200, 30, 90)).save(buffer, fmt, **params)
    return buffer.getvalue()


def _orientation_of(data: bytes) -> int:
    with Image.open(io.BytesIO(data)) as im:
        return read_orientation(im)


def _tiff_with_orientation_entry(field_type: int, count: int, value: bytes) -> bytes:
    """EXIF con una sola entrada Orientation del tipo TIFF indicado (little endian)."""
    entry = struct.pack("<HHI", 0x0112, field_type, count) + value.ljust(4, b"\x00")
    return b"Exif\x00\x00II*\x00\x08\x00\x00\x00\x01\x00" + entry + b"\x00\x00\x00\x00"


def _png_text(key: str, value: str, *, itxt: bool = False) -> PngImagePlugin.PngInfo:
    info = PngImagePlugin.PngInfo()
    if itxt:
        info.add_itxt(key, value)
    else:
        info.add_text(key, value)
    return info


def _png_chunks(png: bytes) -> list[bytes]:
    chunks, pos = [], 8
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        chunks.append(png[pos : pos + 12 + length])
        pos += 12 + length
    return chunks


def _rows(samples: bytes | array[int], width: int) -> list[list[int]]:
    values = list(samples)
    return [values[i : i + width] for i in range(0, len(values), width)]


# --- read_orientation ---


@pytest.mark.req("5.1", "5.10")
@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize("orientation", range(1, 9))
def test_reads_the_exif_orientation(fmt: str, orientation: int) -> None:
    assert _orientation_of(_encode(fmt, exif=_exif(orientation))) == orientation


@pytest.mark.req("5.10")
@pytest.mark.parametrize("fmt", _FORMATS)
def test_missing_exif_is_normal(fmt: str) -> None:
    assert _orientation_of(_encode(fmt)) == 1


@pytest.mark.req("5.10")
@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize("value", [0, 9, 65535])
def test_out_of_range_value_is_normal(fmt: str, value: int) -> None:
    assert _orientation_of(_encode(fmt, exif=_exif(value))) == 1


@pytest.mark.req("5.10")
def test_non_integer_value_is_normal() -> None:
    # Tipo ASCII con el texto "6": no es un valor de 1 a 8
    exif = _tiff_with_orientation_entry(field_type=2, count=2, value=b"6\x00")
    assert _orientation_of(_encode("PNG", exif=exif)) == 1


@pytest.mark.req("5.10")
@pytest.mark.parametrize(
    ("fmt", "params"),
    [
        ("PNG", {"exif": b"Exif\x00\x00garbage!!"}),  # cabecera TIFF inválida
        ("WEBP", {"exif": b"Exif\x00\x00garbage!!"}),
        ("PNG", {"exif": b"Exif\x00\x00II*\x00"}),  # cabecera TIFF truncada
        # Perfil EXIF en un chunk de texto que no es hexadecimal
        ("PNG", {"pnginfo": _png_text("Raw profile type exif", "\nexif\n   2\nzz")}),
    ],
)
def test_damaged_exif_is_normal(fmt: str, params: dict[str, object]) -> None:
    assert _orientation_of(_encode(fmt, **params)) == 1


@pytest.mark.req("5.10")
@pytest.mark.parametrize("fmt", ["JPEG", "WEBP", "PNG"])
def test_xmp_orientation_is_not_exif(fmt: str) -> None:
    if fmt == "PNG":
        data = _encode(fmt, pnginfo=_png_text("XML:com.adobe.xmp", _XMP.decode(), itxt=True))
    else:
        data = _encode(fmt, xmp=_XMP)
    with Image.open(io.BytesIO(data)) as im:
        assert im.getexif().get(0x0112) == 6  # Pillow toma la etiqueta del XMP
    assert _orientation_of(data) == 1


@pytest.mark.req("5.10")
def test_exif_orientation_wins_over_xmp() -> None:
    assert _orientation_of(_encode("JPEG", exif=_exif(3), xmp=_XMP)) == 3


@pytest.mark.req("5.10")
def test_png_raw_exif_profile_is_read_with_xmp_present() -> None:
    # Perfil EXIF con la orientación 8 en un chunk de texto, junto a un XMP con 6
    profile = _exif(8).tobytes().hex()
    info = _png_text("Raw profile type exif", f"\nexif\n   {len(profile) // 2}\n{profile}")
    info.add_itxt("XML:com.adobe.xmp", _XMP.decode())
    assert _orientation_of(_encode("PNG", pnginfo=info)) == 8


@pytest.mark.req("5.10")
def test_png_exif_chunk_after_idat_is_read() -> None:
    chunks = _png_chunks(_encode("PNG", exif=_exif(6)))
    exif_chunks = [c for c in chunks if c[4:8] == b"eXIf"]
    others = [c for c in chunks if c[4:8] != b"eXIf"]
    assert len(exif_chunks) == 1 and others[-1][4:8] == b"IEND"
    data = b"\x89PNG\r\n\x1a\n" + b"".join([*others[:-1], *exif_chunks, others[-1]])
    with Image.open(io.BytesIO(data)) as im:
        assert "exif" not in im.info  # no aparece en la cabecera
        assert read_orientation(im) == 6


# --- displayed_size ---


@pytest.mark.req("5.1", "5.4", "5.5", "5.9")
@pytest.mark.parametrize("orientation", range(1, 9))
def test_displayed_size_swaps_only_from_5_to_8(orientation: int) -> None:
    expected = (40, 30) if orientation >= 5 else (30, 40)
    assert displayed_size(30, 40, orientation) == expected


@pytest.mark.parametrize("orientation", [0, 9, -1])
def test_displayed_size_rejects_unnormalized_values(orientation: int) -> None:
    with pytest.raises(ValueError, match="orientation"):
        displayed_size(3, 2, orientation)


# --- apply_orientation ---


@pytest.mark.req("5.1", "5.10")
@pytest.mark.parametrize("orientation", range(1, 9))
def test_apply_orientation_matches_the_definition(orientation: int) -> None:
    im = Image.frombytes("L", (3, 2), bytes(_GRID))
    oriented = apply_orientation(im, orientation)
    assert oriented.size == displayed_size(3, 2, orientation)
    assert _rows(oriented.tobytes(), oriented.width) == _EXPECTED_ROWS[orientation]


def test_apply_orientation_normal_returns_the_same_image() -> None:
    im = Image.frombytes("L", (3, 2), bytes(_GRID))
    assert apply_orientation(im, 1) is im


@pytest.mark.parametrize("orientation", [0, 9])
def test_apply_orientation_rejects_unnormalized_values(orientation: int) -> None:
    with pytest.raises(ValueError, match="orientation"):
        apply_orientation(Image.new("L", (3, 2)), orientation)


# --- orient_samples ---


@pytest.mark.req("5.4", "5.10")
@pytest.mark.parametrize("orientation", range(1, 9))
def test_orient_samples_matches_the_definition_in_16_bits(orientation: int) -> None:
    samples = array("H", [1000 * v for v in _GRID])  # valores que no caben en 8 bits
    oriented = orient_samples(samples, 3, 2, 1, orientation)
    out_width, _ = displayed_size(3, 2, orientation)
    expected = [[1000 * v for v in row] for row in _EXPECTED_ROWS[orientation]]
    assert oriented.typecode == "H"
    assert _rows(oriented, out_width) == expected


@pytest.mark.req("5.4", "5.10")
@pytest.mark.parametrize("orientation", range(1, 9))
@pytest.mark.parametrize("mode", ["L", "LA", "RGB", "RGBA"])
# Más columnas que filas, más filas que columnas, cuadrada y de un píxel de ancho o de alto,
# para recorrer las filas y las columnas de salida
@pytest.mark.parametrize("size", [(4, 3), (3, 4), (2, 2), (1, 1), (1, 5), (5, 1)])
@pytest.mark.parametrize("typecode", ["B", "H"])
def test_orient_samples_matches_pillow(
    orientation: int, mode: str, size: tuple[int, int], typecode: str
) -> None:
    width, height = size
    channels = len(mode)
    # Muestras distintas entre sí (7 y 256 son coprimos), para detectar cualquier reordenamiento
    data = bytes((7 * i + 3) % 256 for i in range(width * height * channels))
    expected = apply_orientation(Image.frombytes(mode, size, data), orientation).tobytes()

    def widen(values: bytes) -> list[int]:
        # En 16 bits, una correspondencia uno a uno con valores mayores que 255
        return [256 * v + (255 - v) if typecode == "H" else v for v in values]

    samples = array(typecode, widen(data))
    oriented = orient_samples(samples, width, height, channels, orientation)
    assert oriented.typecode == typecode
    assert list(oriented) == widen(expected)


def test_orient_samples_normal_returns_the_same_array() -> None:
    samples = array("B", _GRID)
    assert orient_samples(samples, 3, 2, 1, 1) is samples


@pytest.mark.parametrize(
    ("length", "width", "height", "channels", "orientation", "match"),
    [
        (6, 3, 2, 1, 0, "orientation"),
        (6, 3, 2, 1, 9, "orientation"),
        (5, 3, 2, 1, 6, "muestras"),
        (6, 3, 2, 2, 6, "muestras"),
        (0, 0, 2, 1, 6, "mayores"),
        (0, 3, 2, 0, 6, "mayores"),
    ],
)
def test_orient_samples_rejects_invalid_input(
    length: int, width: int, height: int, channels: int, orientation: int, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        orient_samples(array("B", bytes(length)), width, height, channels, orientation)
