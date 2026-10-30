"""Worker de imagen a PDF: PNG, JPG y WEBP a un PDF de una página con Pillow (Req 5.4, 5.10, 4.11,
13.4).

Uso:

    python -m converter.workers.image_pdf [--max-image-pixels N] -- <origen> <ruta> <salida>

Convierte el archivo `<ruta>`, de Formato `<origen>` (`PNG`, `JPG` o `WEBP`), en un PDF de una
página y lo escribe en `<salida>`. `--max-image-pixels` es el Límite_de_Píxeles; si se omite,
vale el predeterminado de `CONVERTER_MAX_IMAGE_PIXELS`. El convertidor `image.pdf`
(`converters/image_pdf.py`) lo lanza a través del Ejecutor aislado.

Pasos, en este orden. Los pasos 1 a 3 son los de `workers/image.py` (design.md, "Imágenes"):

1. Se abre con Pillow restringido al Formato de origen y se decodifican los píxeles.
2. `apply_orientation` rota o refleja los píxeles según la Orientación_EXIF. El objeto de imagen
   del PDF tiene las Dimensiones_Mostradas del origen y el PDF no lleva la etiqueta (Req 5.4,
   5.10).
3. `normalize_mode` deja la imagen en `L`, `RGB` o `RGBA` de 8 bits por canal. Los PNG de 16 bits
   se reducen a 8 bits tomando el byte alto de cada muestra.
4. Una imagen con transparencia se compone sobre blanco con `flatten_on_white`.
5. Pillow la guarda con `save(format="PDF")`: una página, del tamaño de la imagen a 96 ppp
   (0,75 pt por píxel), escalada de manera uniforme si el lado mayor supera 14 400 pt, el límite
   de PDF sin `UserUnit`. La imagen ocupa la página completa.

## Simplificación del MVP (tarea 15.6)

El diseño pide un escritor propio (`pdf_image_writer`) que inserta el JPEG original sin
recodificar y guarda el resto en `/FlateDecode`, con `/SMask` para la transparencia y 16 bits por
canal en los PNG de 16 bits. Ese escritor queda para después (tasks.md, "Fuera del alcance del
MVP"). Este worker usa el escritor de PDF de Pillow, que cambia el contenido en cuatro casos. La
tarea 15.7 debe tenerlos en cuenta al registrar los pares PNG, JPG y WEBP a PDF:

- **Todo se recodifica como JPEG.** Pillow escribe las imágenes `L` y `RGB` como `/DCTDecode`
  (JPEG), nunca como Flate. El worker pide calidad 95 y submuestreo de color 4:4:4 (`JPEG_QUALITY`
  y `JPEG_SUBSAMPLING`), pero igual cambia los valores de los píxeles. Esto afecta a los tres
  pares, no solo a JPG a PDF: en JPG a PDF el JPEG original se decodifica y se vuelve a
  comprimir, y en PNG y WEBP a PDF se introduce una compresión con pérdida que el origen no
  tenía. Ningún par cumple Req 4.11 (Sin_Pérdida), así que los tres son Con_Pérdida, y por eso el
  Conversor pide la confirmación del usuario antes de ejecutarlos (Req 3.3).
- **La transparencia se reemplaza por blanco.** Pillow 11.3 sí puede escribir `RGBA` y `LA`, con
  `/JPXDecode` y `/SMaskInData`, pero no es fiable: en una prueba con PDFium (pypdfium2), un píxel
  `(200, 30, 90)` con opacidad 128 se dibujó como `(240, 198, 213)` y no como la mezcla con
  blanco, `(227, 142, 172)`. Para que el resultado no dependa del visor se compone sobre blanco
  con la misma fórmula que JPG (Req 5.2) y el PDF no tiene `/SMask`. La descripción de pérdida
  del par debe decirlo.
- **Los PNG de 16 bits se reducen a 8 bits.** El convertidor agrega el aviso `bits_reducidos`.
  La descripción de pérdida debe decirlo, o esos PNG pasarían por Sin_Pérdida.
- **Sin `/ID`, y con la versión de Pillow en el archivo.** Pillow escribe `%PDF-1.4` y un
  comentario `created by Pillow <versión> PDF driver`, y no escribe `/ID`; el diseño pide PDF 1.7
  y un `/ID` derivado del contenido. Ninguno de los dos afecta a Req 5.4 ni a Req 5.10.

Límite conocido: libjpeg admite como máximo 65500 píxeles de ancho o alto, así que un origen con
Dimensiones_Mostradas mayores (posible con el Límite_de_Píxeles predeterminado, por ejemplo
100000 × 1000) no se puede guardar. Pillow lo informa como `OSError: broken data stream`, que el
Conversor clasifica como `archivo_danado`; por eso `convert_file` lo comprueba antes y lanza un
`ValueError`, que la Tarea informa como `error_interno`. Con el escritor propio no existe ese
límite.

## Determinismo (Req 13.4)

Todas las opciones de Pillow son fijas. Sin ellas Pillow escribe la fecha actual en
`/CreationDate` y `/ModDate` y el nombre del archivo en `/Title`, y dos conversiones del mismo
origen difieren. Aquí se pasan `creationDate=None`, `modDate=None` y `title=None`, con lo que el
PDF no tiene diccionario `/Info`. Con el mismo origen, la misma versión de Pillow y libjpeg, el
resultado es idéntico byte a byte.

Códigos de salida: 0 si escribió el resultado; 2 si los argumentos son inválidos; 1 si Pillow o el
sistema de archivos lanzaron una excepción, con el traceback en stderr. El proceso principal
sanea esa salida antes de registrarla (puede contener datos del archivo).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from converter.converters.orientation import apply_orientation, read_orientation

# Funciones de `workers/image.py` que este worker comparte con `image.raster`: así los pasos 1 a 4
# son los mismos en los dos y no se duplican. `_pixel_limit` y `_positive_int` son privadas de ese
# módulo; si otro worker las necesita, conviene hacerlas públicas allí
from converter.workers.image import (
    DEFAULT_MAX_IMAGE_PIXELS,
    PILLOW_FORMATS,
    _pixel_limit,
    _positive_int,
    flatten_on_white,
    normalize_mode,
    read_png_header,
)

MODULE = "converter.workers.image_pdf"  # para `python -m`

EXIT_OK = 0
EXIT_USAGE = 2  # el código de argparse para argumentos inválidos

_MAX_IMAGE_PIXELS = "--max-image-pixels"
_SEPARATOR = "--"

# Opciones de guardado de la imagen dentro del PDF: Pillow la comprime como JPEG
JPEG_QUALITY = 95
JPEG_SUBSAMPLING = 0  # 4:4:4, sin submuestreo de color

MAX_JPEG_DIMENSION = 65500  # píxeles por lado que admite libjpeg

# Tamaño de página (design.md, "Imágenes"): 96 ppp son 0,75 pt por píxel, y 14 400 pt es el
# máximo de una página PDF sin `UserUnit`
PAGE_PPI = 96.0
POINTS_PER_INCH = 72.0
MAX_PAGE_POINTS = 14_400.0


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerArgs:
    """Argumentos de una invocación del worker."""

    source_format: str  # Formato del Archivo_de_Origen: "PNG", "JPG" o "WEBP"
    source: Path  # Archivo_de_Origen
    destination: Path  # Archivo_Resultado (PDF)
    max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS  # Límite_de_Píxeles


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m {MODULE}",
        description="Convierte una imagen PNG, JPG o WEBP en un PDF de una página.",
        allow_abbrev=False,
    )
    parser.add_argument(
        _MAX_IMAGE_PIXELS,
        dest="max_image_pixels",
        type=_positive_int,
        default=DEFAULT_MAX_IMAGE_PIXELS,
        help="Límite_de_Píxeles",
    )
    parser.add_argument("source_format", choices=sorted(PILLOW_FORMATS), help="Formato de origen")
    parser.add_argument("source", type=Path, help="Archivo_de_Origen")
    parser.add_argument("destination", type=Path, help="Archivo_Resultado")
    return parser


def parse_args(args: Sequence[str]) -> WorkerArgs:
    """Interpreta los argumentos del worker. Lanza `SystemExit(2)` si son inválidos."""
    parsed = _parser().parse_args(list(args))
    return WorkerArgs(
        source_format=parsed.source_format,
        source=parsed.source,
        destination=parsed.destination,
        max_image_pixels=parsed.max_image_pixels,
    )


def format_args(args: WorkerArgs) -> list[str]:
    """Argumentos del worker para `args`; `parse_args(format_args(args)) == args`.

    Los argumentos posicionales van después de "--", así una ruta que empiece con "-" no se toma
    como una opción.
    """
    return [
        _MAX_IMAGE_PIXELS,
        str(args.max_image_pixels),
        _SEPARATOR,
        args.source_format,
        str(args.source),
        str(args.destination),
    ]


# --------------------------------------------------------------------------------------------
# Página e imagen


def page_resolution(width: int, height: int) -> float:
    """Resolución en ppp con la que Pillow escribe una página para una imagen de `width` × `height`.

    Pillow da a la página `píxeles × 72 / resolución` puntos por lado. Con 96 ppp son 0,75 pt por
    píxel. Si el lado mayor pasaría de 14 400 pt, la resolución sube en la proporción justa para
    que mida 14 400 pt, y los dos lados se escalan igual: la relación entre el ancho y el alto de
    la página es la de las Dimensiones_Mostradas (Req 5.4).
    """
    longest = max(width, height) * POINTS_PER_INCH / PAGE_PPI
    if longest <= MAX_PAGE_POINTS:
        return PAGE_PPI
    return PAGE_PPI * longest / MAX_PAGE_POINTS


def prepare_for_pdf(im: Image.Image) -> Image.Image:
    """Imagen lista para el escritor de PDF de Pillow, a partir de una de `normalize_mode`.

    Pillow escribe `L` y `RGB` como JPEG. Una imagen `RGBA` se compone sobre blanco con
    `flatten_on_white`: ver "La transparencia se reemplaza por blanco" en el docstring del módulo.
    """
    if im.mode == "RGBA":
        return flatten_on_white(im)
    if im.mode in ("L", "RGB"):
        return im
    raise ValueError(f"prepare_for_pdf necesita una imagen L, RGB o RGBA: {im.mode}")


def pdf_save_options(width: int, height: int) -> dict[str, object]:
    """Opciones de `Image.save(format="PDF")` para una imagen de `width` × `height` (Req 13.4).

    - `resolution`: el tamaño de página de `page_resolution`.
    - `quality` y `subsampling`: la compresión JPEG de la imagen.
    - `creationDate`, `modDate` y `title` en `None`: sin ellos Pillow escribe la fecha actual y el
      nombre del archivo, y el resultado cambiaría en cada conversión.

    No se pasan `exif`, `xmp` ni `icc_profile`.
    """
    return {
        "resolution": page_resolution(width, height),
        "quality": JPEG_QUALITY,
        "subsampling": JPEG_SUBSAMPLING,
        "creationDate": None,
        "modDate": None,
        "title": None,
    }


# --------------------------------------------------------------------------------------------
# Conversión


def convert_file(args: WorkerArgs) -> None:
    """Convierte `args.source` y escribe el PDF en `args.destination`.

    Lanza las excepciones de Pillow y del sistema de archivos: `UnidentifiedImageError` si el
    contenido no es del Formato de origen, `OSError` si está truncado o no se puede escribir y
    `ValueError` si el lado mayor de las Dimensiones_Mostradas pasa de 65500 píxeles (límite de
    libjpeg).
    """
    header = read_png_header(args.source) if args.source_format == "PNG" else None
    with (
        _pixel_limit(args.max_image_pixels),
        Image.open(args.source, formats=[PILLOW_FORMATS[args.source_format]]) as opened,
    ):
        opened.load()
        oriented = apply_orientation(opened, read_orientation(opened))
        if max(oriented.size) > MAX_JPEG_DIMENSION:
            raise ValueError(
                f"la imagen mide {oriented.width} × {oriented.height} píxeles y el JPEG de "
                f"un PDF admite como máximo {MAX_JPEG_DIMENSION} por lado"
            )
        result = prepare_for_pdf(normalize_mode(oriented, header))
        result.info = {}  # sin EXIF, XMP, perfil ICC ni comentarios en el resultado
        result.save(args.destination, format="PDF", **pdf_save_options(*result.size))


def main(argv: Sequence[str] | None = None) -> int:
    """Ejecuta el worker y devuelve su código de salida.

    Una excepción de la conversión se propaga: el intérprete escribe el traceback en stderr y
    termina con el código 1.
    """
    convert_file(parse_args(sys.argv[1:] if argv is None else argv))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
