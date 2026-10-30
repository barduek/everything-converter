"""Imágenes, arreglos de píxeles y referencia de la Orientación_EXIF, que usan las propiedades 10
a 16.

Referencia, escrita a partir de la definición de cada valor de la etiqueta Orientation de EXIF
(en qué lado de la imagen mostrada quedan la fila 0 y la columna 0 almacenadas) y sin usar
`converter.converters.orientation` ni `Image.transpose`, para que un error del módulo haga
fallar las pruebas en lugar de pasar al valor esperado:

- `normalized_orientation(tag)`: la Orientación_EXIF según el glosario, que es el valor de la
  etiqueta si va de 1 a 8 y 1 si falta o está fuera de rango.
- `oriented_pixels(pixels, width, height, orientation)`: los píxeles mostrados y su ancho y alto,
  que son las Dimensiones_Mostradas.
- `INVERSE_ORIENTATION`: la orientación que deshace cada una.

Generadores:

- `orientation_tags()`: valores de la etiqueta Orientation: de 1 a 8, fuera de rango (0, de 9
  a 65535 y valores de tipo LONG mayores que 65535, incluidos los que tienen de 1 a 8 en sus 16
  bits bajos) o ausente (`None`).
- `pixel_arrays()`: arreglos planos de muestras de 8 o 16 bits con 1 a 4 canales y anchos y
  altos de 1 a 64, como los que recibe `orient_samples`: muestras elegidas una por una, todas
  distintas, al azar o solo con valores de borde (0, 1, el medio, el máximo menos 1 y el
  máximo).
- `images()`: archivos PNG, JPG y WEBP. PNG en los modos `1`, `L`, `LA`, `P` (sin
  transparencia, con un índice transparente o con una tabla de opacidad por índice), `RGB` y
  `RGBA` escritos con Pillow, y PNG de 16 bits por canal en escala de grises, escala de grises
  con alfa, RGB y RGBA escritos con pypng; JPG en los modos `1`, `L` y `RGB`; WEBP en `RGB` y
  `RGBA`, con y sin pérdida. La etiqueta se escribe en un bloque EXIF propio, en orden de bytes
  `II` o `MM`: en el segmento APP1 de JPG, en el chunk `EXIF` de WEBP y en el chunk `eXIf` de
  PNG, antes o después de los IDAT. Una imagen sin etiqueta puede no tener EXIF o tener un EXIF
  con otra etiqueta.

El contenido de `images()` es asimétrico: un gradiente con pesos distintos en x e y, que da un
valor distinto en cada esquina, y un bloque marcador en la esquina superior izquierda, más ancho
que alto. Con ancho y alto de 3 o más, las ocho orientaciones dan imágenes distintas: en las
imágenes cuadradas las ocho transformaciones del cuadrado llevan las cuatro esquinas o el
bloque a lugares distintos, y en las demás las orientaciones 1 a 4 se distinguen entre sí por
las esquinas o el bloque, igual que 5 a 8, y unas de otras por el tamaño. Las imágenes con
transparencia tienen píxeles opacos, parcialmente transparentes y totalmente transparentes con
valores de color distintos de 0. En los archivos sin pérdida, una parte de los ejemplos agrega
ruido al azar de 0 a 15 a los canales de color, que no cambia el orden de los valores de las
esquinas.
"""

from __future__ import annotations

import io
import random
import struct
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import png  # type: ignore[import-untyped]
from hypothesis import strategies as st
from PIL import Image

# --- Referencia de la Orientación_EXIF ---

ORIENTATIONS = range(1, 9)

# Orientación que deshace cada una: 6 muestra los píxeles rotados 90° en sentido horario y 8 en
# sentido antihorario; las demás son reflejos o la rotación de 180°, que se deshacen solas
INVERSE_ORIENTATION: dict[int, int] = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 8, 7: 7, 8: 6}


def normalized_orientation(tag: int | None) -> int:
    """Orientación_EXIF según el glosario: el valor si va de 1 a 8 y 1 si falta o está fuera."""
    return tag if tag is not None and 1 <= tag <= 8 else 1


def oriented_pixels[T](
    pixels: Sequence[T], width: int, height: int, orientation: int
) -> tuple[int, int, list[T]]:
    """Ancho, alto y píxeles de la imagen mostrada según `orientation`, de 1 a 8.

    `pixels` tiene un elemento por píxel almacenado, por filas de arriba hacia abajo y cada fila
    de izquierda a derecha; el resultado tiene la misma disposición.
    """
    if orientation not in ORIENTATIONS:
        raise ValueError(f"orientación fuera de 1 a 8: {orientation}")
    if len(pixels) != width * height:
        raise ValueError(f"se esperaban {width * height} píxeles: {len(pixels)}")
    last_x, last_y = width - 1, height - 1
    # Píxel almacenado (columna, fila) que se muestra en (x, y). Cada valor de EXIF dice en qué
    # lado de la imagen mostrada quedan la fila 0 y la columna 0 almacenadas
    sources: dict[int, Callable[[int, int], tuple[int, int]]] = {
        1: lambda x, y: (x, y),  # fila 0 arriba, columna 0 a la izquierda
        2: lambda x, y: (last_x - x, y),  # fila 0 arriba, columna 0 a la derecha
        3: lambda x, y: (last_x - x, last_y - y),  # fila 0 abajo, columna 0 a la derecha
        4: lambda x, y: (x, last_y - y),  # fila 0 abajo, columna 0 a la izquierda
        # De 5 a 8 cada fila almacenada se muestra como una columna
        5: lambda x, y: (y, x),  # fila 0 a la izquierda, columna 0 arriba
        6: lambda x, y: (y, last_y - x),  # fila 0 a la derecha, columna 0 arriba
        7: lambda x, y: (last_x - y, last_y - x),  # fila 0 a la derecha, columna 0 abajo
        8: lambda x, y: (last_x - y, x),  # fila 0 a la izquierda, columna 0 abajo
    }
    source = sources[orientation]
    # Dimensiones_Mostradas: las filas almacenadas pasan a ser columnas de 5 a 8
    shown_width, shown_height = (height, width) if orientation >= 5 else (width, height)
    shown: list[T] = []
    for y in range(shown_height):
        for x in range(shown_width):
            column, row = source(x, y)
            shown.append(pixels[row * width + column])
    return shown_width, shown_height, shown


# --- Etiqueta Orientation y bloque EXIF ---

_ORIENTATION_TAG = 0x0112
_RESOLUTION_UNIT_TAG = 0x0128  # otra etiqueta SHORT, para un EXIF sin Orientation
_SHORT, _LONG = 3, 4  # tipos de dato TIFF
_EXIF_PREFIX = b"Exif\x00\x00"  # inicio del segmento APP1 de JPG; PNG y WEBP no lo llevan

_OUT_OF_RANGE_TAGS = st.one_of(
    st.sampled_from((0, 9, 255, 0xFFFF)),
    st.integers(10, 0xFFFF),
    # De tipo LONG: con 1 a 8 en los 16 bits bajos, un lector que tome solo 16 bits vería un
    # valor válido
    st.integers(1, 8).map(lambda value: 0x10000 + value),
    st.integers(0x10000, 0xFFFF_FFFF),
)


# La mitad de los ejemplos tiene un valor de 1 a 8, para que cada orientación aparezca seguido.
# Se elige primero el caso: `st.one_of` aplanaría los cuatro generadores de los valores fuera de
# rango y les daría dos tercios de los ejemplos
_TAG_KINDS = ("de 1 a 8", "de 1 a 8", "fuera de rango", "ausente")


@st.composite
def _orientation_tags(draw: st.DrawFn) -> int | None:
    kind = draw(st.sampled_from(_TAG_KINDS))
    if kind == "de 1 a 8":
        return draw(st.sampled_from(ORIENTATIONS))
    if kind == "fuera de rango":
        return draw(_OUT_OF_RANGE_TAGS)
    return None


def orientation_tags() -> st.SearchStrategy[int | None]:
    """Valor de la etiqueta Orientation: de 1 a 8, fuera de rango o `None` si no está."""
    return _orientation_tags()


def exif_block(tag: int | None, *, big_endian: bool, other_tag: bool) -> bytes:
    """Bloque EXIF de JPG ("Exif\\0\\0" y un TIFF con un único IFD).

    El IFD tiene la etiqueta Orientation con el valor `tag` (tipo SHORT hasta 65535 y LONG desde
    65536) si `tag` no es `None`, y ResolutionUnit si `other_tag` es verdadero.
    """
    order = ">" if big_endian else "<"
    entries: list[bytes] = []
    if tag is not None:
        if tag <= 0xFFFF:
            entries.append(_ifd_entry(order, _ORIENTATION_TAG, _SHORT, tag))
        else:
            entries.append(_ifd_entry(order, _ORIENTATION_TAG, _LONG, tag))
    if other_tag:
        entries.append(_ifd_entry(order, _RESOLUTION_UNIT_TAG, _SHORT, 2))  # 2: pulgadas
    # Cabecera TIFF: orden de bytes, 42 y el desplazamiento del IFD; el IFD termina con un
    # desplazamiento 0 al siguiente
    header = (b"MM" if big_endian else b"II") + struct.pack(f"{order}HI", 42, 8)
    ifd = struct.pack(f"{order}H", len(entries)) + b"".join(entries) + struct.pack(f"{order}I", 0)
    return _EXIF_PREFIX + header + ifd


def _ifd_entry(order: str, tag: int, field_type: int, value: int) -> bytes:
    # Un único valor, guardado en los 4 bytes de la entrada y alineado a la izquierda
    packed = (
        struct.pack(f"{order}H", value) if field_type == _SHORT else struct.pack(f"{order}I", value)
    )
    return struct.pack(f"{order}HHI", tag, field_type, 1) + packed.ljust(4, b"\x00")


# --- pixel_arrays() ---

_TYPECODES: dict[int, str] = {8: "B", 16: "H"}
# Más muestras que esto se generan a partir de una semilla y no una por una
_MAX_DRAWN_SAMPLES = 256


@dataclass(frozen=True)
class PixelArray:
    """Arreglo plano de muestras, como lo recibe `orient_samples`.

    Las filas van de arriba hacia abajo, cada fila con sus píxeles de izquierda a derecha y cada
    píxel con sus `channels` muestras seguidas. `samples` es de tipo "B" con 8 bits y "H" con 16.
    """

    samples: array[int]
    width: int
    height: int
    channels: int

    @property
    def bits(self) -> int:
        return 8 if self.samples.typecode == "B" else 16

    def pixels(self) -> list[tuple[int, ...]]:
        """Las muestras agrupadas por píxel."""
        step = self.channels
        return [tuple(self.samples[i : i + step]) for i in range(0, len(self.samples), step)]


@st.composite
def _pixel_arrays(
    draw: st.DrawFn, bits: Sequence[int], channels: Sequence[int], max_side: int
) -> PixelArray:
    depth = draw(st.sampled_from(bits))
    count = draw(st.sampled_from(channels))
    width = draw(st.integers(1, max_side))
    height = draw(st.integers(1, max_side))
    total = width * height * count
    top = (1 << depth) - 1
    typecode = _TYPECODES[depth]

    kinds = ["distintas", "al azar", "bordes"]
    if total <= _MAX_DRAWN_SAMPLES:
        kinds.append("elegidas")
    kind = draw(st.sampled_from(kinds))
    if kind == "elegidas":
        values = draw(st.lists(st.integers(0, top), min_size=total, max_size=total))
        samples = array(typecode, values)
    elif kind == "distintas":
        # a·i + b módulo 2^bits con a impar: sin repetidos en cada tramo de 2^bits muestras
        a = 2 * draw(st.integers(0, top // 2)) + 1
        b = draw(st.integers(0, top))
        samples = array(typecode, [(a * i + b) & top for i in range(total)])
    else:
        rng = random.Random(draw(st.integers(0, 2**32 - 1)))
        if kind == "al azar":
            samples = array(typecode)
            samples.frombytes(rng.randbytes(total * samples.itemsize))
        else:
            edges = (0, 1, top // 2, top - 1, top)
            samples = array(typecode, [rng.choice(edges) for _ in range(total)])
    return PixelArray(samples, width, height, count)


def pixel_arrays(
    *,
    bits: Sequence[int] = (8, 16),
    channels: Sequence[int] = (1, 2, 3, 4),
    max_side: int = 64,
) -> st.SearchStrategy[PixelArray]:
    """Arreglos de muestras de `bits` bits con `channels` canales y lados de 1 a `max_side`."""
    if not bits or any(depth not in _TYPECODES for depth in bits):
        raise ValueError(f"bits debe tener valores 8 o 16: {bits}")
    if not channels or any(count not in range(1, 5) for count in channels):
        raise ValueError(f"channels debe tener valores de 1 a 4: {channels}")
    return _pixel_arrays(bits, channels, max_side)


# --- images() ---


@dataclass(frozen=True)
class SampleImage:
    """Archivo de imagen generado y lo que se sabe de él."""

    data: bytes
    format: str  # nombre de Pillow: "PNG", "JPEG" o "WEBP"
    # Modo de los píxeles que se escribieron: "1", "L", "LA", "P", "RGB" o "RGBA". JPG guarda
    # "1" en escala de grises de 8 bits, y WEBP siempre guarda RGB o RGBA
    mode: str
    bits: int  # bits por muestra del archivo: los de IHDR en PNG (1, 2, 4, 8 o 16); 8 en JPG y WEBP
    width: int  # almacenados
    height: int
    orientation_tag: int | None  # valor de la etiqueta Orientation; None si no está
    exif: bool  # tiene un bloque EXIF, con la etiqueta o sin ella
    exif_after_idat: bool  # PNG con el chunk eXIf después de los IDAT
    transparency: bool  # canal alfa o chunk tRNS
    lossless: bool  # guarda exactamente los píxeles escritos: PNG y WEBP sin pérdida

    @property
    def orientation(self) -> int:
        """Orientación_EXIF según el glosario."""
        return normalized_orientation(self.orientation_tag)

    @property
    def displayed_size(self) -> tuple[int, int]:
        """Dimensiones_Mostradas: las almacenadas con 1 a 4 e intercambiadas con 5 a 8."""
        if self.orientation >= 5:
            return self.height, self.width
        return self.width, self.height


_FORMATS = ("PNG", "JPEG", "WEBP")
# Modo y bits por canal de los píxeles que se escriben en cada formato
_MODES: dict[str, tuple[tuple[str, int], ...]] = {
    "PNG": (("1", 1), ("L", 8), ("LA", 8), ("P", 8), ("RGB", 8), ("RGBA", 8)),
    "JPEG": (("1", 1), ("L", 8), ("RGB", 8)),
    "WEBP": (("RGB", 8), ("RGBA", 8)),
}
_PNG_16_BIT_MODES: tuple[tuple[str, int], ...] = (("L", 16), ("LA", 16), ("RGB", 16), ("RGBA", 16))

# Pesos del gradiente en x e y. Distintos, para que las cuatro esquinas tengan valores distintos
# y separados por 51 o más en 8 bits
_WEIGHTS: tuple[tuple[int, int], ...] = ((3, 1), (1, 3), (2, 1), (1, 2), (3, 2), (2, 3))
# Papel de cada canal de color: gradiente, gradiente invertido y con los pesos cambiados, y
# bloque marcador
_COLOR_ROLES = ("gradiente", "invertido", "marcador")
# Cantidad de colores de la paleta de los PNG en modo P: Pillow los escribe con 2, 4 y 8 bits
_PALETTE_SIZES = (4, 16, 256)
_NOISE = 16


def _fraction(value: int, last: int) -> float:
    return value / last if last > 0 else 0.0


def _gradient(x: int, y: int, width: int, height: int, weights: tuple[int, int]) -> float:
    """De 0 en la esquina superior izquierda a 1 en la inferior derecha."""
    a, b = weights
    return (a * _fraction(x, width - 1) + b * _fraction(y, height - 1)) / (a + b)


def _marker_size(width: int, height: int) -> tuple[int, int]:
    """Ancho y alto del bloque marcador de la esquina superior izquierda.

    Es más ancho que alto en las imágenes cuadradas de lado 3 o más, y con lados de 2 o más no
    llega al borde derecho ni al inferior.
    """
    return max(1, min((width + 1) // 2, width - 1)), max(1, height // 3)


def _in_marker(x: int, y: int, width: int, height: int) -> bool:
    marker_width, marker_height = _marker_size(width, height)
    return x < marker_width and y < marker_height


def _alpha(x: int, y: int, width: int, height: int) -> int:
    """Opaco en el bloque marcador, totalmente transparente en un bloque del mismo tamaño en la
    esquina inferior derecha y parcialmente transparente (64 a 191) en el resto."""
    marker_width, marker_height = _marker_size(width, height)
    if _in_marker(x, y, width, height):
        return 255
    if x >= width - marker_width and y >= height - marker_height:
        return 0
    return round(64 + 127 * _fraction(y, height - 1))


@dataclass(frozen=True)
class _Content:
    width: int
    height: int
    weights: tuple[int, int]
    roles: tuple[str, ...]
    noise_seed: int | None

    def color(self, x: int, y: int, role: str) -> int:
        if role == "gradiente":
            return round(255 * _gradient(x, y, self.width, self.height, self.weights))
        if role == "invertido":
            swapped = (self.weights[1], self.weights[0])
            return round(255 * (1 - _gradient(x, y, self.width, self.height, swapped)))
        return 255 if _in_marker(x, y, self.width, self.height) else 0

    def pixels(self, mode: str) -> list[tuple[int, ...]]:
        """Píxeles de 8 bits por canal en los modos "1" (0 o 255), "L", "LA", "RGB" y "RGBA"."""
        rng = random.Random(self.noise_seed)
        out: list[tuple[int, ...]] = []
        for y in range(self.height):
            for x in range(self.width):
                if mode == "1":
                    out.append((self.color(x, y, "marcador"),))
                    continue
                roles = self.roles if mode in ("RGB", "RGBA") else ("gradiente",)
                values = [self.color(x, y, role) for role in roles]
                if self.noise_seed is not None:
                    values = [min(255, value + rng.randrange(_NOISE)) for value in values]
                if mode in ("LA", "RGBA"):
                    values.append(_alpha(x, y, self.width, self.height))
                out.append(tuple(values))
        return out

    def palette_indices(self, colors: int) -> bytes:
        return bytes(
            round((colors - 1) * _gradient(x, y, self.width, self.height, self.weights))
            for y in range(self.height)
            for x in range(self.width)
        )


def _palette(colors: int) -> list[int]:
    # Colores distintos entre sí: el rojo crece con el índice
    palette: list[int] = []
    for index in range(colors):
        level = round(255 * index / (colors - 1))
        palette += [level, 255 - level, (3 * level) % 256]
    return palette


def _widen(pixels: list[tuple[int, ...]], width: int, alpha: bool) -> list[tuple[int, ...]]:
    """Píxeles de 16 bits a partir de los de 8: el byte alto es el valor de 8 bits y el bajo
    varía con la posición y el canal, salvo en la opacidad total y en la transparencia total."""
    wide: list[tuple[int, ...]] = []
    for index, pixel in enumerate(pixels):
        y, x = divmod(index, width)
        values = [
            256 * value + (37 * x + 101 * y + 53 * channel) % 256
            for channel, value in enumerate(pixel)
        ]
        if alpha:
            values[-1] = {0: 0, 255: 0xFFFF}.get(pixel[-1], values[-1])
        wide.append(tuple(values))
    return wide


def _png_with_exif(data: bytes, exif: bytes | None, *, after_idat: bool) -> bytes:
    """Agrega a un PNG el chunk eXIf con el TIFF de `exif`, antes del primer IDAT o al final."""
    if exif is None:
        return data
    chunks = [(kind, body) for kind, body in png.Reader(bytes=data).chunks()]
    kinds = [kind for kind, _ in chunks]
    position = kinds.index(b"IEND") if after_idat else kinds.index(b"IDAT")
    chunks.insert(position, (b"eXIf", exif.removeprefix(_EXIF_PREFIX)))
    out = io.BytesIO()
    png.write_chunks(out, chunks)
    return out.getvalue()


def _png_16_bits(mode: str, width: int, height: int, pixels: list[tuple[int, ...]]) -> bytes:
    writer = png.Writer(
        width, height, greyscale=mode in ("L", "LA"), alpha=mode in ("LA", "RGBA"), bitdepth=16
    )
    flat = [sample for pixel in pixels for sample in pixel]
    row = width * len(mode)
    out = io.BytesIO()
    writer.write(out, [flat[start : start + row] for start in range(0, len(flat), row)])
    return out.getvalue()


def _save(im: Image.Image, fmt: str, **params: object) -> bytes:
    out = io.BytesIO()
    im.save(out, fmt, **params)
    return out.getvalue()


@st.composite
def _images(
    draw: st.DrawFn, formats: Sequence[str], sixteen_bit: bool, min_side: int, max_side: int
) -> SampleImage:
    fmt = draw(st.sampled_from(formats))
    choices = _MODES[fmt] + (_PNG_16_BIT_MODES if fmt == "PNG" and sixteen_bit else ())
    mode, depth = draw(st.sampled_from(choices))
    width = draw(st.integers(min_side, max_side))
    height = draw(st.integers(min_side, max_side))
    lossless = fmt == "PNG" or (fmt == "WEBP" and draw(st.booleans()))
    content = _Content(
        width,
        height,
        weights=draw(st.sampled_from(_WEIGHTS)),
        roles=tuple(draw(st.permutations(_COLOR_ROLES))),
        noise_seed=draw(st.none() | st.integers(0, 2**32 - 1)) if lossless else None,
    )

    tag = draw(orientation_tags())
    # Sin etiqueta: sin EXIF o con un EXIF que tiene otra etiqueta
    other_tag = draw(st.booleans())
    has_exif = tag is not None or other_tag
    exif = (
        exif_block(tag, big_endian=draw(st.booleans()), other_tag=other_tag) if has_exif else None
    )
    after_idat = fmt == "PNG" and has_exif and draw(st.booleans())

    transparency = mode in ("LA", "RGBA")
    if depth == 16:
        wide = _widen(content.pixels(mode), width, alpha=transparency)
        data = _png_16_bits(mode, width, height, wide)
    else:
        params: dict[str, object] = {}
        if mode == "P":
            colors = draw(st.sampled_from(_PALETTE_SIZES))
            indices = content.palette_indices(colors)
            im = Image.frombytes("P", (width, height), indices)
            im.putpalette(_palette(colors))
            kind = draw(st.sampled_from(("ninguna", "índice", "tabla")))
            transparency = kind != "ninguna"
            if kind == "índice":
                # Un índice que aparece en la imagen, para que haya píxeles transparentes
                params["transparency"] = draw(st.sampled_from(sorted(set(indices))))
            elif kind == "tabla":
                # Opacidad creciente con el índice: el 0 es totalmente transparente
                params["transparency"] = bytes(round(255 * i / (colors - 1)) for i in range(colors))
        else:
            flat = bytes(sample for pixel in content.pixels(mode) for sample in pixel)
            im = Image.frombytes("L" if mode == "1" else mode, (width, height), flat)
            if mode == "1":
                im = im.convert("1", dither=Image.Dither.NONE)
        if fmt == "JPEG":
            params["quality"] = 95
        elif fmt == "WEBP":
            params.update({"lossless": True, "exact": True} if lossless else {"quality": 95})
        if fmt != "PNG" and exif is not None:
            # Pillow escribe el segmento APP1 de JPG o el chunk EXIF de WEBP
            params["exif"] = exif
        data = _save(im, fmt, **params)
    if fmt == "PNG":
        data = _png_with_exif(data, exif, after_idat=after_idat)

    # Bits por muestra de IHDR, después de la firma, el largo, el tipo, el ancho y el alto
    bits = data[24] if fmt == "PNG" else 8
    return SampleImage(
        data=data,
        format=fmt,
        mode=mode,
        bits=bits,
        width=width,
        height=height,
        orientation_tag=tag,
        exif=has_exif,
        exif_after_idat=after_idat,
        transparency=transparency,
        lossless=lossless,
    )


def images(
    *,
    formats: Sequence[str] = _FORMATS,
    sixteen_bit: bool = True,
    min_side: int = 3,
    max_side: int = 16,
) -> st.SearchStrategy[SampleImage]:
    """Archivos de imagen de `formats` con lados de `min_side` a `max_side`.

    `sixteen_bit` incluye los PNG de 16 bits por canal. Con lados menores que 3 el contenido
    sigue siendo el mismo, pero algunas orientaciones pueden dar imágenes iguales.
    """
    if not formats or any(fmt not in _FORMATS for fmt in formats):
        raise ValueError(f"formats debe tener valores de {_FORMATS}: {formats}")
    if not 1 <= min_side <= max_side:
        raise ValueError(f"se necesita 1 <= min_side <= max_side: {min_side}, {max_side}")
    return _images(formats, sixteen_bit, min_side, max_side)
