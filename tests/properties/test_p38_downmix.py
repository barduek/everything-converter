"""Propiedad 38: la mezcla a estéreo no pierde canales salvo el LFE (Req 9.3, 10.9).

Las distribuciones tienen de 3 a 255 canales, el máximo de OGG, y salen de dos fuentes:

- secuencias de posiciones de FFmpeg, canales sin nombre (None) y nombres desconocidos, a veces
  con pocas posiciones repetidas muchas veces, como 255 canales FC, para forzar la normalización;
- los nombres estándar de distribución que escribe ffprobe, traducidos con `layout_channels`.

El oráculo toma como Canal_LFE las posiciones `LFE` y `LFE2` de FFmpeg y no supone cómo se
normaliza cada salida: solo exige que cada una sume 1 o menos. La suma se calcula en los dos
órdenes y con `math.fsum`, porque el filtro `pan` puede acumular los coeficientes en otro orden.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from converter.converters.media_rules import CHANNEL_NAMES, build_downmix_matrix, layout_channels

# Canal_LFE: las posiciones de efectos de baja frecuencia de FFmpeg (glosario)
_LFE_NAMES = frozenset({"LFE", "LFE2"})

_MIN_CHANNELS = 3  # Req 9.3: más de 2 canales
_MAX_CHANNELS = 255  # máximo de canales de OGG (Req 9.2)

_POSITIONS: tuple[str, ...] = tuple(sorted(CHANNEL_NAMES))
# Nombres que FFmpeg escribe para canales sin posición, y otros que no son posiciones
_UNKNOWN_NAMES: tuple[str, ...] = ("USR5", "UNK", "AMBI0", "NONE", "")

# Distribuciones estándar de FFmpeg 7.1 con más de 2 canales y su cantidad de canales
_STANDARD_LAYOUTS: tuple[tuple[str, int], ...] = (
    ("2.1", 3), ("3.0", 3), ("3.0(back)", 3),
    ("4.0", 4), ("quad", 4), ("quad(side)", 4), ("3.1", 4),
    ("5.0", 5), ("5.0(side)", 5), ("4.1", 5),
    ("5.1", 6), ("5.1(side)", 6), ("6.0", 6), ("6.0(front)", 6), ("3.1.2", 6), ("hexagonal", 6),
    ("6.1", 7), ("6.1(back)", 7), ("6.1(front)", 7), ("7.0", 7), ("7.0(front)", 7),
    ("7.1", 8), ("7.1(wide)", 8), ("7.1(wide-side)", 8), ("5.1.2", 8), ("octagonal", 8),
    ("cube", 8),
    ("5.1.4", 10), ("7.1.2", 10),
    ("7.1.4", 12), ("7.2.3", 12),
    ("9.1.4", 14),
    ("hexadecagonal", 16),
    ("22.2", 24),
)  # fmt: skip

type _Layout = tuple[str | None, ...]


def _channels() -> st.SearchStrategy[str | None]:
    """Un canal: una posición de FFmpeg, sin nombre (None) o con un nombre que no es posición."""
    return st.one_of(
        st.sampled_from(_POSITIONS),
        st.none(),
        st.sampled_from(_UNKNOWN_NAMES),
        st.text(max_size=8).filter(lambda name: name not in CHANNEL_NAMES),
    )


@st.composite
def _sequences(draw: st.DrawFn) -> _Layout:
    """Distribuciones de 3 a 255 canales; la mitad con 1 a 3 canales distintos repetidos."""
    length = draw(
        st.one_of(
            st.integers(_MIN_CHANNELS, _MAX_CHANNELS),
            st.sampled_from((_MIN_CHANNELS, _MAX_CHANNELS - 1, _MAX_CHANNELS)),
        )
    )
    channel = _channels()
    if draw(st.booleans()):
        # Con más peso para el Canal_LFE, para que aparezcan distribuciones de solo Canal_LFE
        lfe = st.sampled_from(sorted(_LFE_NAMES))
        repeated = st.one_of(_channels(), _channels(), _channels(), lfe)
        channel = st.sampled_from(draw(st.lists(repeated, min_size=1, max_size=3)))
    event("secuencia de canales")
    return tuple(draw(st.lists(channel, min_size=length, max_size=length)))


@st.composite
def _standard_layouts(draw: st.DrawFn) -> _Layout:
    """Posiciones de una distribución estándar, como las da `layout_channels` desde ffprobe."""
    name, channels = draw(st.sampled_from(_STANDARD_LAYOUTS))
    layout = layout_channels(name, channels)
    # Con una cantidad que no coincide, todos los canales quedarían sin nombre y el ejemplo no
    # probaría la distribución estándar
    assert len(layout) == channels and None not in layout, (name, channels, layout)
    event("distribución estándar")
    return layout


def _record_case(layout: _Layout) -> None:
    length = len(layout)
    if length == _MAX_CHANNELS:
        event("canales: 255")
    elif length > 24:
        event("canales: 25 a 254")
    else:
        event("canales: 3 a 24")
    lfe = sum(1 for name in layout if name in _LFE_NAMES)
    if lfe == length:
        event("solo Canal_LFE")
    elif lfe:
        event("con Canal_LFE")
    if None in layout:
        event("con canales sin nombre")
    if any(name is not None and name not in CHANNEL_NAMES for name in layout):
        event("con nombres que no son posiciones")


def _check_sum(side: str, coefficients: Sequence[float]) -> None:
    # Req 9.3: sin saturación, la suma de cada salida no supera 1 en ningún orden de acumulación
    forward = sum(coefficients)
    backward = sum(reversed(coefficients))
    exact = math.fsum(coefficients)
    assert forward <= 1.0, (side, forward)
    assert backward <= 1.0, (side, backward)
    assert exact <= 1.0, (side, exact)


# Feature: file-converter, Property 38: La mezcla a estéreo no pierde canales salvo el LFE
@pytest.mark.req("9.3", "10.9")
@given(layout=st.one_of(_sequences(), _standard_layouts()))
def test_downmix_keeps_every_channel_but_lfe(layout: _Layout) -> None:
    _record_case(layout)
    matrix = build_downmix_matrix(layout)

    # Un coeficiente por canal del origen en cada salida
    assert len(matrix.left) == len(layout), (len(layout), len(matrix.left))
    assert len(matrix.right) == len(layout), (len(layout), len(matrix.right))

    for index, (name, left, right) in enumerate(
        zip(layout, matrix.left, matrix.right, strict=True)
    ):
        # Ningún coeficiente es negativo ni NaN
        assert left >= 0.0 and right >= 0.0, (index, name, left, right)
        if name in _LFE_NAMES:
            # El Canal_LFE se descarta en las dos salidas
            assert left == 0.0 and right == 0.0, (index, name, left, right)
        else:
            # Req 9.3: el audio de cada canal que no es Canal_LFE está en al menos una salida
            assert left > 0.0 or right > 0.0, (index, name, left, right)

    _check_sum("izquierda", matrix.left)
    _check_sum("derecha", matrix.right)
