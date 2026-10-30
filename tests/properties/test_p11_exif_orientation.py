"""Propiedad 11: aplicar la Orientación_EXIF produce las Dimensiones_Mostradas (Req 5.1, 5.10).

El oráculo es la referencia de `tests.strategies.images`, escrita a partir de la definición de
cada valor de la etiqueta Orientation de EXIF con listas de Python, sin la tabla de
`converter.converters.orientation` ni `Image.transpose`.

Cada ejemplo recibe un archivo de `images()`, con la etiqueta de 1 a 8, fuera de rango o
ausente, y un arreglo de `pixel_arrays()`:

- `read_orientation` lee del archivo el valor de la etiqueta si va de 1 a 8 y 1 si falta o está
  fuera de rango.
- `apply_orientation` sobre la imagen decodificada, en los modos en que Pillow abre los PNG,
  JPG y WEBP (`1`, `L`, `LA`, `P`, `RGB`, `RGBA` e `I;16`), y sobre la imagen de Pillow con las
  muestras del arreglo cuando Pillow tiene un modo para ellas: 8 bits con 1 a 4 canales y 16
  bits con 1 canal (`I;16`). Pillow no tiene modos de 16 bits con varios canales, que es la razón
  de `orient_samples`.
- `orient_samples` sobre el arreglo, con 8 o 16 bits y 1 a 4 canales.

En los tres casos el resultado coincide con la referencia, su ancho y su alto son
`displayed_size` (que la referencia calcula como los almacenados con 1 a 4 e intercambiados con
5 a 8) y aplicar la orientación inversa devuelve los píxeles originales.

Cada ejemplo aplica las ocho orientaciones, y no solo la que lee `read_orientation`: con 100
ejemplos, las etiquetas fuera de rango o ausentes, que dan 1, y la tendencia de Hypothesis a
repetir valores dejan algunas orientaciones sin probar en muchas ejecuciones.
"""

from __future__ import annotations

import io
import sys
from array import array
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from hypothesis import event, given
from PIL import Image

from converter.converters.orientation import (
    apply_orientation,
    displayed_size,
    orient_samples,
    read_orientation,
)
from tests.strategies.images import (
    INVERSE_ORIENTATION,
    ORIENTATIONS,
    PixelArray,
    SampleImage,
    images,
    oriented_pixels,
    pixel_arrays,
)

# Modos de Pillow para muestras de 8 bits según la cantidad de canales
_MODES_8_BITS = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}


def _little_endian(samples: array[int]) -> bytes:
    # Pillow guarda "I;16" en little endian; `array` usa el orden de la máquina
    copy = array(samples.typecode, samples)
    if sys.byteorder == "big":
        copy.byteswap()
    return copy.tobytes()


def _pillow_image(pixels: PixelArray) -> Image.Image | None:
    """La imagen de Pillow con las muestras de `pixels`, o None si Pillow no tiene ese modo."""
    size = (pixels.width, pixels.height)
    if pixels.bits == 8:
        return Image.frombytes(_MODES_8_BITS[pixels.channels], size, pixels.samples.tobytes())
    if pixels.channels == 1:
        return Image.frombytes("I;16", size, _little_endian(pixels.samples))
    return None


def _samples_of(im: Image.Image) -> array[int]:
    """Las muestras de una imagen de Pillow de 8 bits o en "I;16", en un arreglo plano."""
    if im.mode == "I;16":
        samples = array("H", im.tobytes())  # en el orden de la máquina
        if sys.byteorder == "big":
            samples.byteswap()
        return samples
    return array("B", im.tobytes())


def _check_decoded_image(im: Image.Image, orientation: int) -> None:
    """`apply_orientation` sobre la imagen tal como la abre Pillow."""
    stored = list(im.getdata())
    width, height, expected = oriented_pixels(stored, im.width, im.height, orientation)
    assert displayed_size(im.width, im.height, orientation) == (width, height)

    oriented = apply_orientation(im, orientation)
    assert oriented.size == (width, height)
    assert oriented.mode == im.mode
    if im.mode == "P":
        assert oriented.getpalette() == im.getpalette()
    assert list(oriented.getdata()) == expected

    restored = apply_orientation(oriented, INVERSE_ORIENTATION[orientation])
    assert restored.size == im.size
    assert list(restored.getdata()) == stored


def _check_pixel_array(pixels: PixelArray, orientation: int) -> None:
    """`orient_samples` y, si Pillow tiene el modo, `apply_orientation` sobre las muestras."""
    original = array(pixels.samples.typecode, pixels.samples)
    width, height, shown = oriented_pixels(
        pixels.pixels(), pixels.width, pixels.height, orientation
    )
    expected = array(original.typecode, [sample for pixel in shown for sample in pixel])
    assert displayed_size(pixels.width, pixels.height, orientation) == (width, height)

    oriented = orient_samples(
        pixels.samples, pixels.width, pixels.height, pixels.channels, orientation
    )
    assert oriented.typecode == original.typecode
    assert oriented == expected
    inverse = INVERSE_ORIENTATION[orientation]
    assert orient_samples(oriented, width, height, pixels.channels, inverse) == original
    assert pixels.samples == original  # no modifica la entrada

    im = _pillow_image(pixels)
    if im is None:
        return
    oriented_im = apply_orientation(im, orientation)
    assert oriented_im.size == (width, height)
    assert _samples_of(oriented_im) == expected
    restored = apply_orientation(oriented_im, inverse)
    assert restored.size == im.size
    assert _samples_of(restored) == original


@contextmanager
def _noted(text: str) -> Iterator[None]:
    """Agrega `text` al informe de una aserción que falla dentro del bloque."""
    try:
        yield
    except AssertionError as error:
        error.add_note(text)
        raise


def _record_case(image: SampleImage, pixels: PixelArray) -> None:
    tag = image.orientation_tag
    if tag is None:
        event("sin etiqueta, con EXIF" if image.exif else "sin EXIF")
    elif 1 <= tag <= 8:
        event("etiqueta de 1 a 8")
    else:
        event("etiqueta fuera de rango")
    event(f"archivo {image.format} {image.mode} {image.bits} bits")
    if image.exif_after_idat:
        event("eXIf después de IDAT")
    event(f"arreglo de {pixels.bits} bits y {pixels.channels} canales")


# Feature: file-converter, Property 11: Aplicar la Orientación_EXIF produce las Dimensiones_Mostradas  # noqa: E501
@pytest.mark.req("5.1", "5.10")
@given(image=images(), pixels=pixel_arrays())
def test_applying_exif_orientation_gives_displayed_size(
    image: SampleImage, pixels: PixelArray
) -> None:
    _record_case(image, pixels)
    with Image.open(io.BytesIO(image.data)) as im:
        # Glosario: el valor de la etiqueta si va de 1 a 8; 1 si falta o está fuera de rango
        assert read_orientation(im) == image.orientation
        for orientation in ORIENTATIONS:
            with _noted(f"imagen decodificada en modo {im.mode}, orientación {orientation}"):
                _check_decoded_image(im, orientation)
    for orientation in ORIENTATIONS:
        with _noted(f"arreglo de muestras, orientación {orientation}"):
            _check_pixel_array(pixels, orientation)
