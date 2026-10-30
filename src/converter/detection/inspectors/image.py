"""Inspector `image`: PNG, JPG y WEBP con Pillow, sin decodificar los píxeles (Req 5.6, 5.9,
16.1, 16.2).

Lee la cabecera del Archivo_de_Origen y devuelve un `ImageInfo` con lo que necesitan los
convertidores de imagen, o un Rechazo_de_Origen. Corre en el subproceso
`converter.workers.inspect`, con los límites del Ejecutor aislado.

Datos de `ImageInfo`:

- `width` y `height` son las dimensiones almacenadas; `displayed_size` da las
  Dimensiones_Mostradas con `orientation.displayed_size`, así que todo límite se calcula sobre
  las mismas dimensiones que usan los convertidores.
- `mode` es el modo de Pillow tras abrir la imagen.
- `bits_per_channel` sale de `IHDR` en PNG (1, 2, 4, 8 o 16): Pillow abre un PNG de 16 bits en
  color como `RGB` o `RGBA` y no lo informa. En JPG y WEBP vale 8.
- `has_alpha` es verdadero si el modo tiene canal alfa o la imagen declara transparencia (chunk
  `tRNS` de PNG). Se decide con la cabecera: no dice que haya un píxel con opacidad menor a la
  total.
- `frames` es la cantidad de cuadros de un PNG o un WEBP (Imagen_Animada, glosario). Un JPG
  cuenta un cuadro aunque sea un MPO con varias imágenes, como los que escriben algunas cámaras
  y teléfonos: se convierte la primera y el archivo no se anima.
- `exif_orientation` es la Orientación_EXIF que da `orientation.read_orientation`.

Rechazos, en el orden de `INSPECTION_REJECTIONS`:

1. `archivo_danado` (Req 16.2): Pillow no identifica la imagen con el formato del Tipo_Real, su
   cabecera está incompleta o es inválida (por ejemplo, un `IHDR` ilegible) o la lectura falla
   por un error de los datos. Un error de lectura del sistema (con `errno`) no es del archivo y
   se propaga: el proceso principal lo informa como `error_interno`. Como no se decodifican los
   píxeles, un archivo con la cabecera íntegra y los datos truncados pasa; la conversión lo
   detecta (Req 16.2, "desde en_proceso").
2. `imagen_animada` (Req 5.6): un PNG o un WEBP con más de un cuadro.
3. `excede_limite_pixeles` (Req 5.9): ancho × alto mayor que el Límite_de_Píxeles
   (`InspectorInput.max_image_pixels`). El producto es el mismo con cualquier orientación; el
   rechazo informa las Dimensiones_Mostradas.

Sin decodificar píxeles:

- Pillow abre la imagen de forma perezosa y esta función solo lee `size`, `mode`, `info`,
  `getbands()`, `n_frames` y el EXIF, que están en la cabecera.
- `Image.MAX_IMAGE_PIXELS` vale el Límite_de_Píxeles mientras se abre el archivo. Pillow avisa
  por encima del límite y falla por encima del doble. En ese segundo caso no devuelve la imagen
  y no se conocen sus dimensiones, así que se vuelve a leer la cabecera sin tope, solo para
  informarlas. Los avisos de Pillow se descartan: stderr queda para los errores.
- `read_orientation` carga los píxeles de un PNG cuando `Image.info` no tiene `exif`, porque el
  chunk `eXIf` puede venir después de `IDAT` y Pillow lo lee al decodificar. Eso pasa con todo
  PNG sin EXIF y con una imagen enorme llevaría al límite de memoria. Por eso en PNG se recorren
  los chunks con saltos (`_scan_png`), sin leer los datos de imagen, para obtener el último
  `eXIf`, y la Orientación_EXIF se lee de una imagen de 1 × 1 con los mismos metadatos.

Este módulo importa Pillow y `converter.converters.orientation` dentro de las funciones, porque
el proceso principal y cada worker importan todos los inspectores solo para conocer sus nombres
y sus tipos de `SourceInfo` (ver `base.py`).
"""

from __future__ import annotations

import os
import struct
import warnings
import zlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from converter.detection.inspectors.base import InspectorInput, register_inspector
from converter.detection.source_info import SourceInfo, SourceRejection, register_source_info
from converter.eventlog import Tool
from converter.messages_es import FailureReason

if TYPE_CHECKING:
    from PIL import Image

# Bits por canal que admite IHDR de PNG
BITS_PER_CHANNEL = (1, 2, 4, 8, 16)
# Valores de la etiqueta Orientation de EXIF (`orientation.ORIENTATIONS`, que importa Pillow)
_ORIENTATIONS = range(1, 9)

# Formatos de Pillow que admite cada Tipo_Real. Un MPO se abre con el lector de JPEG
_PILLOW_FORMATS: Mapping[str, tuple[str, ...]] = {
    "PNG": ("PNG",),
    "JPG": ("JPEG",),
    "WEBP": ("WEBP",),
}
_ANY_PILLOW_FORMAT = ("PNG", "JPEG", "WEBP")
# Formatos que pueden ser una Imagen_Animada
_ANIMATED_FORMATS = ("PNG", "WEBP")
_BITS_OUTSIDE_PNG = 8  # JPG y WEBP guardan 8 bits por canal

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_CHUNK_HEADER = struct.Struct(">I4s")  # largo de los datos y tipo
_PNG_CRC_SIZE = 4
_IHDR_SIZE = 13
_IHDR_BIT_DEPTH = 8  # posición del byte de profundidad: ancho (4) y alto (4) lo preceden
_EXIF_PREFIX = b"Exif\x00\x00"  # Pillow lo antepone a los datos del chunk eXIf de PNG


@register_source_info("image")
@dataclass(frozen=True, slots=True)
class ImageInfo(SourceInfo):
    """Información de un Archivo_de_Origen PNG, JPG o WEBP, que produce el inspector `image`."""

    width: int  # ancho almacenado
    height: int  # alto almacenado
    mode: str  # modo de Pillow tras abrir
    bits_per_channel: int  # 1, 2, 4, 8 o 16 (de IHDR en PNG)
    has_alpha: bool
    frames: int
    exif_orientation: int  # 1 a 8; 1 si falta la etiqueta o está fuera de rango

    def __post_init__(self) -> None:
        for name in ("width", "height", "bits_per_channel", "frames", "exif_orientation"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} debe ser un entero")
        if not isinstance(self.mode, str) or not self.mode:
            raise ValueError("el modo de la imagen no puede estar vacío")
        if not isinstance(self.has_alpha, bool):
            raise TypeError("has_alpha debe ser un booleano")
        if self.width < 1 or self.height < 1:
            raise ValueError("el ancho y el alto de la imagen deben ser 1 o mayores")
        if self.bits_per_channel not in BITS_PER_CHANNEL:
            raise ValueError("los bits por canal son 1, 2, 4, 8 o 16")
        if self.frames < 1:
            raise ValueError("la imagen tiene al menos un cuadro")
        if self.exif_orientation not in _ORIENTATIONS:
            raise ValueError("la Orientación_EXIF va de 1 a 8")

    @property
    def displayed_size(self) -> tuple[int, int]:
        """Dimensiones_Mostradas: `(ancho, alto)` tras aplicar la Orientación_EXIF."""
        # Pillow se importa al usarla, no al importar el módulo
        from converter.converters.orientation import displayed_size

        return displayed_size(self.width, self.height, self.exif_orientation)


class _Header(NamedTuple):
    """Campos de `ImageInfo`, en el mismo orden, leídos de la cabecera."""

    width: int
    height: int
    mode: str
    bits_per_channel: int
    has_alpha: bool
    frames: int
    exif_orientation: int


class _InvalidHeader(ValueError):
    """La cabecera del archivo es inválida aunque Pillow la haya aceptado."""


# Errores de Pillow y de zlib ante una cabecera incompleta o inválida: `UnidentifiedImageError`
# es un `OSError`; `SyntaxError`, `EOFError` y `ValueError` son los de los lectores de chunks y
# de segmentos
_DAMAGED_ERRORS = (OSError, SyntaxError, EOFError, ValueError, struct.error, zlib.error)


@register_inspector("image", info_type=ImageInfo, tool=Tool.PILLOW)
def inspect_image(source: InspectorInput) -> ImageInfo | SourceRejection:
    """Inspecciona el PNG, JPG o WEBP de `source.path` sin decodificar sus píxeles."""
    formats = _PILLOW_FORMATS.get(source.real_type or "", _ANY_PILLOW_FORMAT)
    limit = source.max_image_pixels
    with warnings.catch_warnings(), _max_image_pixels(limit):
        warnings.simplefilter("ignore")
        try:
            header = _read_header(source.path, formats)
        except _DAMAGED_ERRORS as exc:
            if isinstance(exc, OSError) and exc.errno is not None:
                raise  # un error del sistema de archivos no es del archivo
            return SourceRejection(FailureReason.DAMAGED)
    if header.frames > 1:
        return SourceRejection(FailureReason.ANIMATED_IMAGE)
    info = ImageInfo(*header)
    if info.width * info.height > limit:
        width, height = info.displayed_size
        return SourceRejection(
            FailureReason.PIXEL_LIMIT,
            {"ancho": width, "alto": height, "limite_pixeles": limit},
        )
    return info


@contextmanager
def _max_image_pixels(value: int | None) -> Iterator[None]:
    """Fija `Image.MAX_IMAGE_PIXELS` y restaura el valor anterior al salir."""
    from PIL import Image

    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = value
    try:
        yield
    finally:
        Image.MAX_IMAGE_PIXELS = previous


def _open(path: Path, formats: tuple[str, ...]) -> Image.Image:
    from PIL import Image

    try:
        return Image.open(path, formats=formats)
    except Image.DecompressionBombError:
        # Más del doble del Límite_de_Píxeles: Pillow no devuelve la imagen. Se lee otra vez la
        # cabecera sin tope para conocer sus dimensiones; los píxeles no se decodifican nunca
        with _max_image_pixels(None):
            return Image.open(path, formats=formats)


def _read_header(path: Path, formats: tuple[str, ...]) -> _Header:
    """Datos de la cabecera de `path`. Los errores de Pillow indican un archivo dañado."""
    from converter.converters.orientation import read_orientation

    with _open(path, formats) as im:
        width, height = im.size
        if width < 1 or height < 1:
            raise _InvalidHeader("la imagen no tiene píxeles")
        png = _scan_png(path) if im.format == "PNG" else None
        orientation = _png_orientation(im, png.exif) if png is not None else read_orientation(im)
        frames = _frame_count(im)
        return _Header(
            width=width,
            height=height,
            mode=im.mode,
            bits_per_channel=png.bit_depth if png is not None else _BITS_OUTSIDE_PNG,
            has_alpha="A" in im.getbands() or "transparency" in im.info,
            frames=frames,
            exif_orientation=orientation,
        )


def _frame_count(im: Image.Image) -> int:
    # Un MPO de JPG tiene varias imágenes, pero no es una Imagen_Animada (glosario)
    if im.format not in _ANIMATED_FORMATS:
        return 1
    return int(getattr(im, "n_frames", 1))


@dataclass(frozen=True, slots=True)
class _PngScan:
    """Lo que se obtiene de los chunks de un PNG sin leer los datos de imagen."""

    bit_depth: int  # IHDR
    exif: bytes | None  # datos del último chunk eXIf, antes o después de IDAT


def _scan_png(path: Path) -> _PngScan:
    """Lee `IHDR` y busca el chunk `eXIf` saltando los datos de los demás chunks.

    Lanza `_InvalidHeader` si el archivo no empieza con la firma de PNG y un `IHDR` de 13 bytes
    con una profundidad válida. Un archivo truncado después de `IHDR` termina el recorrido sin
    error: qué hay después de la cabecera lo decide la conversión.
    """
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        if stream.read(len(_PNG_SIGNATURE)) != _PNG_SIGNATURE:
            raise _InvalidHeader("firma de PNG inválida")
        header = stream.read(_PNG_CHUNK_HEADER.size)
        if len(header) != _PNG_CHUNK_HEADER.size:
            raise _InvalidHeader("IHDR ausente")
        length, kind = _PNG_CHUNK_HEADER.unpack(header)
        if kind != b"IHDR" or length != _IHDR_SIZE:
            raise _InvalidHeader("IHDR inválido")
        ihdr = stream.read(_IHDR_SIZE)
        if len(ihdr) != _IHDR_SIZE or ihdr[_IHDR_BIT_DEPTH] not in BITS_PER_CHANNEL:
            raise _InvalidHeader("IHDR inválido")
        bit_depth = ihdr[_IHDR_BIT_DEPTH]
        stream.seek(_PNG_CRC_SIZE, os.SEEK_CUR)

        exif: bytes | None = None
        while len(header := stream.read(_PNG_CHUNK_HEADER.size)) == _PNG_CHUNK_HEADER.size:
            length, kind = _PNG_CHUNK_HEADER.unpack(header)
            if kind == b"IEND":
                break
            if kind == b"eXIf" and stream.tell() + length <= size:
                exif = stream.read(length)
                length = 0
            stream.seek(length + _PNG_CRC_SIZE, os.SEEK_CUR)
    return _PngScan(bit_depth=bit_depth, exif=exif)


def _png_orientation(im: Image.Image, exif: bytes | None) -> int:
    """Orientación_EXIF de un PNG abierto con Pillow, sin decodificar sus píxeles.

    `exif` son los datos del último chunk `eXIf` del archivo. Los demás metadatos (texto, XMP y un
    `eXIf` anterior a IDAT) están en `im.info`. Con ellos se arma una imagen de 1 × 1 y se aplica
    `read_orientation`, que no carga píxeles en una imagen que no es un `PngImageFile`.
    """
    from PIL import Image

    from converter.converters.orientation import read_orientation

    carrier = Image.new("1", (1, 1))
    carrier.info.update(im.info)
    if exif is not None:
        carrier.info["exif"] = _EXIF_PREFIX + exif
    return read_orientation(carrier)
