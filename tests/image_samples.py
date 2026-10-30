"""Imágenes pequeñas y deterministas para las pruebas de los convertidores de imagen.

- `grid_file(fmt, tag)` es una cuadrícula de 3 × 2 bloques de 16 × 16 píxeles con seis colores
  saturados distintos, en PNG, JPG o WEBP, con la etiqueta Orientation de EXIF que se pida. Los
  bloques coinciden con los de la compresión de JPG, así que el centro de cada bloque conserva su
  color con un error pequeño. Sin pérdida (PNG, WEBP sin pérdida y JPG de calidad 100 sin
  submuestreo de color) para que el color esperado de cada bloque sea el de `GRID_COLOURS`.
- `block_centres(im)` lee el color del centro de cada bloque de una imagen en cuadrícula, y
  `expected_blocks(tag)` da los que deben quedar tras aplicar la Orientación_EXIF `tag`, con
  `tests.strategies.images.oriented_pixels` como referencia.
- `png_bytes` escribe un PNG con pypng, que admite las profundidades de 1 a 16 bits y el color
  clave de transparencia (`tRNS`) que Pillow no escribe.
"""

from __future__ import annotations

import io
from collections.abc import Sequence

import png  # type: ignore[import-untyped]
from PIL import Image, PngImagePlugin

from tests.strategies.images import exif_block, normalized_orientation, oriented_pixels

type Colour = tuple[int, ...]

BLOCK = 16  # lado de cada bloque de la cuadrícula, en píxeles
GRID_COLUMNS = 3
GRID_ROWS = 2
# Colores de los bloques, por filas de izquierda a derecha
GRID_COLOURS: tuple[Colour, ...] = (
    (255, 0, 0),
    (0, 255, 0),
    (0, 0, 255),
    (255, 255, 0),
    (0, 255, 255),
    (255, 0, 255),
)
XMP = (
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
    b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description '
    b'xmlns:tiff="http://ns.adobe.com/tiff/1.0/" tiff:Orientation="6"/></rdf:RDF></x:xmpmeta>'
)


def grid_image() -> Image.Image:
    """Cuadrícula de `GRID_COLUMNS` × `GRID_ROWS` bloques, en `RGB`."""
    im = Image.new("RGB", (GRID_COLUMNS * BLOCK, GRID_ROWS * BLOCK))
    for index, colour in enumerate(GRID_COLOURS):
        row, column = divmod(index, GRID_COLUMNS)
        left, top = column * BLOCK, row * BLOCK
        im.paste(colour, (left, top, left + BLOCK, top + BLOCK))
    return im


def grid_file(fmt: str, tag: int | None = None, *, xmp: bool = False) -> bytes:
    """Cuadrícula en `fmt` ("PNG", "JPG" o "WEBP") con la etiqueta Orientation `tag`.

    Sin `tag` el archivo no tiene EXIF. Con `xmp`, además lleva un bloque XMP.
    """
    metadata: dict[str, object] = {}
    if tag is not None:
        metadata["exif"] = exif_block(tag, big_endian=False, other_tag=False)
    out = io.BytesIO()
    im = grid_image()
    if fmt == "PNG":
        info = PngImagePlugin.PngInfo()
        if xmp:
            info.add_itxt("XML:com.adobe.xmp", XMP.decode())
        im.save(out, "PNG", pnginfo=info, **metadata)
    elif fmt == "JPG":
        im.save(
            out, "JPEG", quality=100, subsampling=0, **metadata, **({"xmp": XMP} if xmp else {})
        )
    elif fmt == "WEBP":
        im.save(out, "WEBP", lossless=True, exact=True, **metadata, **({"xmp": XMP} if xmp else {}))
    else:
        raise ValueError(f"formato de cuadrícula desconocido: {fmt}")
    return out.getvalue()


def block_centres(im: Image.Image) -> list[Colour]:
    """Color del centro de cada bloque de `im`, por filas de izquierda a derecha."""
    rgb = im.convert("RGB")
    centres: list[Colour] = []
    for row in range(rgb.height // BLOCK):
        for column in range(rgb.width // BLOCK):
            pixel = rgb.getpixel((column * BLOCK + BLOCK // 2, row * BLOCK + BLOCK // 2))
            assert isinstance(pixel, tuple)
            centres.append(pixel)
    return centres


def expected_blocks(tag: int | None) -> tuple[int, int, list[Colour]]:
    """Columnas, filas y colores de los bloques que quedan tras aplicar la etiqueta `tag`."""
    return oriented_pixels(GRID_COLOURS, GRID_COLUMNS, GRID_ROWS, normalized_orientation(tag))


def close(actual: Sequence[int], expected: Sequence[int], tolerance: int) -> bool:
    """Cada canal de `actual` difiere del de `expected` en `tolerance` o menos."""
    return len(actual) == len(expected) and all(
        abs(a - b) <= tolerance for a, b in zip(actual, expected, strict=True)
    )


def png_bytes(
    width: int,
    height: int,
    rows: Sequence[Sequence[int]],
    *,
    bitdepth: int,
    greyscale: bool = False,
    alpha: bool = False,
    transparent: tuple[int, ...] | None = None,
) -> bytes:
    """PNG de `bitdepth` bits por canal escrito con pypng.

    `rows` tiene una lista de muestras por fila, con los canales de cada píxel seguidos. Los
    valores van de 0 a 2 ** bitdepth - 1. `transparent` es el color clave (`tRNS`) de gris o RGB.
    """
    out = io.BytesIO()
    writer = png.Writer(
        width,
        height,
        greyscale=greyscale,
        alpha=alpha,
        bitdepth=bitdepth,
        transparent=transparent,
    )
    writer.write(out, rows)
    return out.getvalue()
