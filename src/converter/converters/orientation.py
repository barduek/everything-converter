"""Orientación_EXIF y Dimensiones_Mostradas de las imágenes (D15; Req 5.1, 5.4, 5.5, 5.9, 5.10).

El inspector `image` y los convertidores de imagen usan estas funciones, así que los límites de
Req 5.5 y 5.9 y las comparaciones de Req 5.1 y 5.4 se calculan siempre sobre las mismas
Dimensiones_Mostradas.

- `read_orientation` lee la etiqueta Orientation (0x0112) del EXIF de una imagen PNG, JPG o WEBP
  abierta con Pillow y devuelve 1 si falta o si su valor está fuera de 1 a 8 (glosario).
- `displayed_size` da las Dimensiones_Mostradas: las almacenadas con los valores 1 a 4 e
  intercambiadas con los valores 5 a 8.
- `apply_orientation` rota o refleja una imagen de Pillow con `Image.transpose`.
- `orient_samples` hace lo mismo sobre un arreglo plano de muestras de 8 o 16 bits, para los PNG
  de 16 bits leídos con pypng, que Pillow no representa en color.
"""

from __future__ import annotations

import struct
from array import array

from PIL import Image, PngImagePlugin

ORIENTATION_TAG = 0x0112  # Orientation en EXIF (ExifTags.Base.Orientation)
NORMAL = 1
ORIENTATIONS = range(1, 9)

# Misma tabla que `ImageOps.exif_transpose`. Pillow rota en sentido antihorario, así que
# ROTATE_270 es 90° en sentido horario
_TRANSPOSE: dict[int, Image.Transpose] = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,  # reflejo horizontal
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,  # reflejo vertical
    5: Image.Transpose.TRANSPOSE,  # reflejo sobre la diagonal principal
    6: Image.Transpose.ROTATE_270,  # 90° en sentido horario
    7: Image.Transpose.TRANSVERSE,  # reflejo sobre la diagonal secundaria
    8: Image.Transpose.ROTATE_90,  # 90° en sentido antihorario
}

# Claves de `Image.info` con el XMP: iTXt de PNG, y JPG o WEBP
_XMP_KEYS = ("XML:com.adobe.xmp", "xmp")
# Perfil EXIF en hexadecimal dentro de un chunk de texto de PNG, que Pillow también lee
_RAW_EXIF_KEY = "Raw profile type exif"


def read_orientation(im: Image.Image) -> int:
    """Orientación_EXIF de `im`, de 1 a 8.

    Devuelve 1 si la imagen no tiene EXIF, si el EXIF no tiene la etiqueta, si el valor no es un
    entero de 1 a 8 o si el bloque EXIF está dañado y la etiqueta no se puede leer.

    Solo cuenta el EXIF, como dice el glosario: cuando el EXIF no tiene la etiqueta,
    `getexif()` la completa con `tiff:Orientation` del XMP, y ese valor se descarta.

    En PNG, si el chunk eXIf no aparece antes de IDAT, Pillow carga los píxeles para buscarlo en
    los chunks posteriores. Esa carga queda fuera del manejo de EXIF dañado, así que un error de
    los datos de imagen se propaga.
    """
    if isinstance(im, PngImagePlugin.PngImageFile) and "exif" not in im.info:
        # Lo mismo que hace `PngImageFile.getexif`, pero fuera del `try`
        im.load()
    try:
        exif = im.getexif()
        if any(key in im.info for key in _XMP_KEYS):
            exif = _exif_without_xmp(im)
    except (SyntaxError, struct.error, ValueError):
        # Cabecera TIFF inválida, entrada truncada o perfil hexadecimal mal formado: la etiqueta
        # no se puede leer, igual que si faltara
        return NORMAL
    value = exif.get(ORIENTATION_TAG)
    # Solo un entero: si la etiqueta tiene otro tipo de dato TIFF, Pillow devuelve texto, bytes
    # o números con decimales
    return value if type(value) is int and value in ORIENTATIONS else NORMAL


def _exif_without_xmp(im: Image.Image) -> Image.Exif:
    """Lee el EXIF de `im` de las mismas fuentes que `getexif()`, sin consultar el XMP."""
    exif = Image.Exif()
    raw = im.info.get("exif")
    if raw is None and isinstance(text := im.info.get(_RAW_EXIF_KEY), str):
        # Formato de ImageMagick: tres líneas de cabecera y el perfil en hexadecimal
        raw = bytes.fromhex("".join(text.split("\n")[3:]))
    if isinstance(raw, bytes):
        exif.load(raw)
    return exif


def displayed_size(width: int, height: int, orientation: int) -> tuple[int, int]:
    """Dimensiones_Mostradas: `(width, height)` con 1 a 4 y `(height, width)` con 5 a 8."""
    _check_orientation(orientation)
    return (height, width) if orientation >= 5 else (width, height)


def apply_orientation(im: Image.Image, orientation: int) -> Image.Image:
    """Rota o refleja los píxeles almacenados de `im` como indica `orientation` (Req 5.10).

    `orientation` es el valor ya normalizado por `read_orientation`. El resultado tiene las
    Dimensiones_Mostradas de `im`. Con la orientación 1 devuelve `im` sin copiarla.

    No toca `info`: el EXIF y el XMP del origen siguen ahí, y quien guarda el resultado debe
    descartarlos para que no lleve la etiqueta de Orientación_EXIF.
    """
    _check_orientation(orientation)
    method = _TRANSPOSE.get(orientation)
    return im if method is None else im.transpose(method)


def orient_samples(
    samples: array[int], width: int, height: int, channels: int, orientation: int
) -> array[int]:
    """Aplica `orientation` a un arreglo plano de muestras de 8 o 16 bits (Req 5.4, 5.10).

    `samples` tiene las filas de arriba hacia abajo, cada fila con sus píxeles de izquierda a
    derecha y cada píxel con sus `channels` muestras seguidas. El resultado tiene la misma
    disposición y el mismo tipo, con el ancho y el alto de las Dimensiones_Mostradas. Con la
    orientación 1 devuelve `samples` sin copiarlo.

    Cada paso copia una fila o una columna completa de la salida, canal por canal, con un corte
    de `array` de paso fijo. En las orientaciones 5 a 8, una fila de salida es una columna de
    origen y se toma con un corte de paso igual al largo de una fila de origen. Se recorre el
    eje de salida más corto, de modo que el trabajo en Python es proporcional a
    min(ancho, alto) × canales y no a la cantidad de píxeles.
    """
    _check_orientation(orientation)
    if width < 1 or height < 1 or channels < 1:
        raise ValueError(
            f"width, height y channels deben ser 1 o mayores: {width}, {height}, {channels}"
        )
    if len(samples) != width * height * channels:
        raise ValueError(
            f"samples debe tener width × height × channels = {width * height * channels} "
            f"muestras: {len(samples)}"
        )
    if orientation == NORMAL:
        return samples

    out_width, out_height = displayed_size(width, height, orientation)
    first, step_x, step_y = _source_steps(width, height, channels, orientation)
    out_row = out_width * channels
    # Una "línea" es una fila (si hay menos filas que columnas) o una columna de la salida.
    # line_*: desplazamiento entre líneas; along_*: desplazamiento entre píxeles de una línea
    if out_height <= out_width:
        lines, line_src, line_dst = out_height, step_y, out_row
        length, along_src, along_dst = out_width, step_x, channels
    else:
        lines, line_src, line_dst = out_width, step_x, channels
        length, along_src, along_dst = out_height, step_y, out_row

    # Arreglo del mismo tipo y largo que `samples`; cada muestra se sobrescribe abajo
    out = samples[:1] * len(samples)
    for line in range(lines):
        for channel in range(channels):
            src = first + line * line_src + channel
            dst = line * line_dst + channel
            stop = src + length * along_src
            # Con paso negativo, un límite negativo contaría desde el final del arreglo
            source = samples[src : stop if stop >= 0 else None : along_src]
            out[dst : dst + length * along_dst : along_dst] = source
    return out


def _source_steps(width: int, height: int, channels: int, orientation: int) -> tuple[int, int, int]:
    """Ubicación en el origen de los píxeles de salida.

    Devuelve el índice en `samples` de la primera muestra del píxel de salida (0, 0) y cuánto
    avanza ese índice al moverse en la salida un píxel a la derecha y una fila hacia abajo. El
    píxel de salida (x, y) empieza en `first + x * step_x + y * step_y`.
    """
    pixel = channels
    row = width * channels
    last_column = (width - 1) * pixel  # último píxel de la primera fila de origen
    last_row = (height - 1) * row  # primer píxel de la última fila de origen
    steps: dict[int, tuple[int, int, int]] = {
        1: (0, pixel, row),
        2: (last_column, -pixel, row),
        3: (last_row + last_column, -pixel, -row),
        4: (last_row, pixel, -row),
        # 5 a 8: cada fila de salida recorre una columna de origen
        5: (0, row, pixel),
        6: (last_row, -row, pixel),
        7: (last_row + last_column, -row, -pixel),
        8: (last_column, row, -pixel),
    }
    return steps[orientation]


def _check_orientation(orientation: int) -> None:
    if orientation not in ORIENTATIONS:
        raise ValueError(f"orientation debe estar entre 1 y 8: {orientation}")
