"""Worker de imágenes raster: PNG, JPG y WEBP entre sí (Req 5.1 a 5.3, 5.7, 5.8, 5.10, 13.4).

Uso:

    python -m converter.workers.image [--max-image-pixels N] -- <origen> <destino> <ruta> <salida>

Convierte el archivo `<ruta>`, de Formato `<origen>`, al Formato `<destino>` y lo escribe en
`<salida>`. Los Formatos son `PNG`, `JPG` o `WEBP` y deben ser distintos. `--max-image-pixels` es
el Límite_de_Píxeles; si se omite, vale el predeterminado de `CONVERTER_MAX_IMAGE_PIXELS`. El
convertidor `image.raster` lo lanza a través del Ejecutor aislado (`converters/images.py`).

Pasos, en este orden (design.md, "Imágenes"):

1. Se abre con Pillow restringido al Formato de origen y se decodifican los píxeles.
2. `apply_orientation` rota o refleja los píxeles según la Orientación_EXIF, antes de cualquier
   otro paso. El resultado tiene las Dimensiones_Mostradas del origen (Req 5.1, 5.10).
3. `normalize_mode` deja la imagen en `L`, `RGB` o `RGBA` de 8 bits por canal: la paleta, `LA`,
   `1` y la transparencia por color clave (`tRNS`) pasan a `L`, `RGB` o `RGBA`; `I;16` y los PNG
   de 16 bits se reducen a 8 bits tomando el byte alto de cada muestra (Req 5.3).
4. A JPG, `flatten_on_white` compone cada píxel sobre blanco según su opacidad (Req 5.2).
5. Se guarda con las opciones de `save_options`. Antes de guardar se vacía `info` y no se pasan
   `exif`, `xmp` ni `icc_profile`, así que el resultado no tiene EXIF ni XMP y, por lo tanto,
   tampoco la etiqueta de Orientación_EXIF. La preservación de metadatos está fuera del alcance.

Todas las opciones de Pillow son fijas, así que el mismo origen produce el mismo resultado en el
mismo Equipo_de_Ejecución (Req 13.4).

Códigos de salida: 0 si escribió el resultado; 2 si los argumentos son inválidos; 1 si Pillow o el
sistema de archivos lanzaron una excepción, con el traceback en stderr. El proceso principal
sanea esa salida antes de registrarla (puede contener datos del archivo).
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import sys
import warnings
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops

from converter.config import VARIABLES
from converter.converters.orientation import apply_orientation, read_orientation

MODULE = "converter.workers.image"  # para `python -m`

EXIT_OK = 0
EXIT_USAGE = 2  # el código de argparse para argumentos inválidos

_MAX_IMAGE_PIXELS = "--max-image-pixels"
_SEPARATOR = "--"

# Predeterminado de la Variable_de_Configuración del Límite_de_Píxeles
DEFAULT_MAX_IMAGE_PIXELS = {spec.name: spec.default for spec in VARIABLES}[
    "CONVERTER_MAX_IMAGE_PIXELS"
]

# Formatos del convertidor y su nombre en Pillow
PILLOW_FORMATS: dict[str, str] = {"PNG": "PNG", "JPG": "JPEG", "WEBP": "WEBP"}

# Opciones de guardado (design.md, "Imágenes")
JPEG_QUALITY = 90
WEBP_LOSSY_QUALITY = 90
PNG_COMPRESS_LEVEL = 6

WHITE = 255  # canal de un píxel blanco de 8 bits

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_IHDR_END = 29  # firma (8) + largo (4) + tipo (4) + datos de IHDR (13)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerArgs:
    """Argumentos de una invocación del worker."""

    source_format: str  # Formato del Archivo_de_Origen: "PNG", "JPG" o "WEBP"
    target_format: str  # Formato_de_Destino, distinto del de origen
    source: Path  # Archivo_de_Origen
    destination: Path  # Archivo_Resultado
    max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS  # Límite_de_Píxeles


@dataclass(frozen=True, slots=True)
class PngHeader:
    """Profundidad de bits y tipo de color de la cabecera `IHDR` de un PNG."""

    bit_depth: int  # bits por canal: 1, 2, 4, 8 o 16
    colour_type: int


def _positive_int(raw: str) -> int:
    # Solo dígitos ASCII: `int()` aceptaría además signos, espacios, "_" y dígitos no ASCII
    if not (raw.isascii() and raw.isdigit()) or int(raw) < 1:
        raise argparse.ArgumentTypeError(f"debe ser un entero positivo: {raw!r}")
    return int(raw)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m {MODULE}",
        description="Convierte una imagen entre PNG, JPG y WEBP.",
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
    parser.add_argument("target_format", choices=sorted(PILLOW_FORMATS), help="Formato de destino")
    parser.add_argument("source", type=Path, help="Archivo_de_Origen")
    parser.add_argument("destination", type=Path, help="Archivo_Resultado")
    return parser


def parse_args(args: Sequence[str]) -> WorkerArgs:
    """Interpreta los argumentos del worker. Lanza `SystemExit(2)` si son inválidos."""
    parser = _parser()
    parsed = parser.parse_args(list(args))
    if parsed.source_format == parsed.target_format:
        parser.error("el Formato de origen y el de destino deben ser distintos")
    return WorkerArgs(
        source_format=parsed.source_format,
        target_format=parsed.target_format,
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
        args.target_format,
        str(args.source),
        str(args.destination),
    ]


# --------------------------------------------------------------------------------------------
# Cabecera PNG


def read_png_header(path: Path) -> PngHeader | None:
    """Profundidad de bits y tipo de color de `IHDR`, o None si `path` no empieza con un PNG.

    Pillow entrega los PNG de 16 bits en 8 bits (RGB, RGBA, gris con alfa) o como `I;16` (gris),
    y los de 1, 2 y 4 bits en gris como `1` o `L`; la profundidad original solo está aquí.
    """
    with path.open("rb") as stream:
        head = stream.read(_IHDR_END)
    if len(head) < _IHDR_END or head[:8] != PNG_SIGNATURE or head[12:16] != b"IHDR":
        return None
    return PngHeader(bit_depth=head[24], colour_type=head[25])


# --------------------------------------------------------------------------------------------
# Modo y transparencia


def flatten_on_white(im: Image.Image) -> Image.Image:
    """Compone `im` sobre un fondo blanco según su opacidad (Req 5.2).

    `im` es `RGBA` (el resultado es `RGB`) o `LA` (el resultado es `L`), de 8 bits por canal. Cada
    canal de color `c` con opacidad `α` pasa a `c' = round(c·α/255 + 255·(1 − α/255))`: un píxel
    con α = 0 queda blanco, uno con α = 255 conserva su color y uno parcialmente transparente
    queda en la mezcla. El redondeo es exacto (`(c·α + 255·(255 − α) + 127) // 255`): Pillow
    mezcla con la misma aritmética entera al pegar con una máscara.
    """
    if im.mode == "RGBA":
        flattened = Image.new("RGB", im.size, (WHITE, WHITE, WHITE))
    elif im.mode == "LA":
        flattened = Image.new("L", im.size, WHITE)
    else:
        raise ValueError(f"flatten_on_white necesita una imagen RGBA o LA: {im.mode}")
    # Pillow pega los canales de color de `im`, sin su alfa, y usa el alfa como máscara
    flattened.paste(im, mask=im.getchannel("A"))
    return flattened


def _key_mask(bands: Sequence[Image.Image], key: Sequence[int]) -> Image.Image:
    """Máscara `L` de opacidad de un color clave: 0 donde todos los canales valen `key`, 255 en
    el resto. `bands` tiene un canal por valor de `key`."""
    matches = [
        band.point(lambda value, wanted=wanted: 255 if value == wanted else 0)
        for band, wanted in zip(bands, key, strict=True)
    ]
    return ImageChops.invert(functools.reduce(ImageChops.darker, matches))


def _gray_16_to_8(im: Image.Image) -> Image.Image:
    """Imagen `I;16` a `L` o, si tiene color clave de transparencia, a `RGBA`.

    `Image.convert` satura los valores mayores que 255, así que se toma el byte alto de cada
    muestra, igual que Pillow con las muestras de los PNG RGB y RGBA de 16 bits. El color clave
    (`tRNS`) se compara con las muestras completas de 16 bits.
    """
    data = im.tobytes()  # `I;16` es little endian: el byte alto es el segundo de cada muestra
    high = Image.frombytes("L", im.size, data[1::2])
    key = im.info.get("transparency")
    if not isinstance(key, int):
        return high
    low = Image.frombytes("L", im.size, data[0::2])
    del data
    rgba = high.convert("RGBA")
    rgba.putalpha(_key_mask((high, low), (key >> 8, key & 0xFF)))
    return rgba


def _colour_key(im: Image.Image, header: PngHeader | None) -> tuple[int, ...] | None:
    """Color clave de transparencia (`tRNS`) de una imagen `1`, `L` o `RGB`, en 8 bits por canal.

    Pillow entrega la clave con el valor de la muestra del PNG, pero los píxeles ya están
    escalados: la clave del gris de 1, 2 y 4 bits va de 0 a 1, 3 o 15 y los píxeles de 0 a 255, y
    la de RGB de 16 bits tiene 16 bits y los píxeles se reducen a 8. Se escala la clave igual que
    los píxeles. En RGB de 16 bits solo se conserva el byte alto, así que un píxel que difiere de
    la clave solo en el byte bajo también queda transparente.
    """
    key = im.info.get("transparency")
    depth = header.bit_depth if header is not None else 8
    if im.mode in ("1", "L") and isinstance(key, int):
        depth = 1 if im.mode == "1" else depth
        return (key * (255 // ((1 << depth) - 1)) if depth < 8 else key,)
    if im.mode == "RGB" and isinstance(key, tuple) and len(key) == 3:
        return tuple(channel >> 8 if depth == 16 else channel for channel in key)
    return None


def normalize_mode(im: Image.Image, header: PngHeader | None = None) -> Image.Image:
    """Imagen `L`, `RGB` o `RGBA` de 8 bits por canal con los mismos píxeles que `im`.

    - `I;16` (PNG en gris de 16 bits) se reduce a 8 bits con el byte alto de cada muestra.
    - `1` pasa a `L` (0 o 255). Una paleta pasa a `RGBA` si tiene transparencia y a `RGB` si no.
      `LA` pasa a `RGBA`. `CMYK` y los demás modos pasan a `RGB`.
    - Una imagen `L` o `RGB` con color clave de transparencia pasa a `RGBA`, con alfa 0 en los
      píxeles de ese color. `header` es la cabecera `IHDR` si el origen es un PNG.

    Devuelve `im` si ya es `L`, `RGB` o `RGBA` sin color clave. No modifica `im`.
    """
    if im.mode == "I;16":
        return _gray_16_to_8(im)
    if im.mode in ("1", "L", "RGB"):
        key = _colour_key(im, header)
        base = im.convert("L") if im.mode == "1" else im
        if key is None:
            return base
        rgba = base.convert("RGBA")
        rgba.putalpha(_key_mask(base.split(), key))
        return rgba
    if im.mode == "P":
        return im.convert("RGBA" if "transparency" in im.info else "RGB")
    if im.mode == "RGBA":
        return im
    if im.mode in ("LA", "PA", "La", "RGBa"):
        return im.convert("RGBA")
    return im.convert("RGB")


def prepare_for_target(im: Image.Image, target_format: str) -> Image.Image:
    """Imagen lista para guardar en `target_format`, a partir de una de `normalize_mode`.

    JPG no admite transparencia: se compone sobre blanco con `flatten_on_white`. WEBP guarda
    siempre `RGB` o `RGBA`, así que el gris pasa a `RGB`. PNG guarda los tres modos.
    """
    if target_format == "JPG":
        return flatten_on_white(im) if im.mode == "RGBA" else im
    if target_format == "WEBP" and im.mode == "L":
        return im.convert("RGB")
    return im


def save_options(source_format: str, target_format: str) -> dict[str, object]:
    """Opciones de `Image.save` para el par, sin `exif`, `xmp` ni `icc_profile` (Req 5.7, 5.10).

    - JPG: calidad 90 y submuestreo de color 4:2:0, sin optimización de tablas.
    - WEBP desde PNG: sin pérdida, con `exact=True` para conservar los valores RGB de los píxeles
      totalmente transparentes (Req 5.7). Desde JPG: con pérdida, calidad 90.
    - PNG: nivel de compresión 6.

    Lanza `ValueError` si el par no es PNG, JPG o WEBP entre sí.
    """
    if target_format == source_format or {source_format, target_format} - PILLOW_FORMATS.keys():
        raise ValueError(f"par de imágenes no admitido: {source_format} a {target_format}")
    if target_format == "JPG":
        return {"quality": JPEG_QUALITY, "subsampling": "4:2:0", "optimize": False}
    if target_format == "WEBP":
        if source_format == "PNG":
            return {"lossless": True, "quality": 100, "method": 4, "exact": True}
        return {"quality": WEBP_LOSSY_QUALITY}
    return {"compress_level": PNG_COMPRESS_LEVEL}


# --------------------------------------------------------------------------------------------
# Conversión


@contextlib.contextmanager
def _pixel_limit(max_image_pixels: int) -> Iterator[None]:
    """Fija el Límite_de_Píxeles de Pillow y convierte su aviso en error mientras dure.

    Pillow solo avisa cuando la imagen supera el límite y falla al superar el doble. Con el aviso
    como error, una imagen mayor que el límite no se decodifica.
    """
    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_image_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            yield
    finally:
        Image.MAX_IMAGE_PIXELS = previous


def convert_file(args: WorkerArgs) -> None:
    """Convierte `args.source` y escribe el resultado en `args.destination`.

    Lanza las excepciones de Pillow y del sistema de archivos: `UnidentifiedImageError` si el
    contenido no es del Formato de origen, `OSError` si está truncado o no se puede escribir.
    """
    options = save_options(args.source_format, args.target_format)
    header = read_png_header(args.source) if args.source_format == "PNG" else None
    with (
        _pixel_limit(args.max_image_pixels),
        Image.open(args.source, formats=[PILLOW_FORMATS[args.source_format]]) as opened,
    ):
        opened.load()
        oriented = apply_orientation(opened, read_orientation(opened))
        result = prepare_for_target(normalize_mode(oriented, header), args.target_format)
        result.info = {}  # sin EXIF, XMP, perfil ICC ni comentarios en el resultado
        result.save(args.destination, format=PILLOW_FORMATS[args.target_format], **options)


def main(argv: Sequence[str] | None = None) -> int:
    """Ejecuta el worker y devuelve su código de salida.

    Una excepción de la conversión se propaga: el intérprete escribe el traceback en stderr y
    termina con el código 1.
    """
    convert_file(parse_args(sys.argv[1:] if argv is None else argv))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
