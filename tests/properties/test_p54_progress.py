"""Propiedad 54: el Porcentaje_de_Avance es acotado y no decrece (Req 14.12).

Cada ejemplo aplica una secuencia de operaciones a un `ProgressReporter` nuevo. Las fracciones
crudas incluyen NaN, infinitos, negativos y valores mayores que 1, y van al reporter o a franjas
y subfranjas creadas al azar, como las de una Tarea de varios pasos. Después de cada operación
se lee el Porcentaje_de_Avance publicado, que debe ser un entero entre 0 y 100 y no menor que la
lectura anterior.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from converter.tasks.progress import ProgressBand, ProgressReporter

# Fracciones en los bordes de [0, 1] y fuera del rango
_EDGE_FRACTIONS: tuple[float, ...] = (
    math.nan,
    -math.nan,
    math.inf,
    -math.inf,
    -0.0,
    0.0,
    5e-324,  # menor subnormal positivo
    -5e-324,
    math.nextafter(1.0, 0.0),
    1.0,
    math.nextafter(1.0, math.inf),
    -1.0,
    2.0,
    1e308,
)

# Límites de franja: los extremos, valores como los de MP4 a TXT y cualquiera en [0, 1]
_BOUNDS = st.one_of(st.sampled_from((0.0, 0.1, 0.5, 1.0)), st.floats(0.0, 1.0))

# Índice del destino de una operación, tomado módulo la cantidad de destinos que hay en ese momento
_SINK_INDEXES = st.integers(0, 15)


@dataclass(frozen=True)
class _Report:
    """Envía `fraction` al destino `sink`."""

    sink: int
    fraction: float


@dataclass(frozen=True)
class _NewBand:
    """Crea la franja [start, end] del destino `parent` y la agrega a los destinos."""

    parent: int
    start: float
    end: float


type _Operation = _Report | _NewBand


def _raw_fractions() -> st.SearchStrategy[float]:
    """Fracciones dentro de [0, 1], cualquier float (NaN e infinitos incluidos) o bordes."""
    return st.one_of(
        st.floats(0.0, 1.0),
        st.floats(allow_nan=True, allow_infinity=True),
        st.sampled_from(_EDGE_FRACTIONS),
    )


@st.composite
def _new_bands(draw: st.DrawFn) -> _NewBand:
    """Franjas con `0 <= start <= end <= 1`; una de cada 5 tiene ancho 0."""
    first = draw(_BOUNDS)
    second = first if draw(st.integers(0, 4)) == 0 else draw(_BOUNDS)
    return _NewBand(draw(_SINK_INDEXES), min(first, second), max(first, second))


@st.composite
def _operations(draw: st.DrawFn) -> Sequence[_Operation]:
    """Secuencias de operaciones; en la mitad de los ejemplos todas van directo al reporter."""
    if draw(st.booleans()):
        return draw(st.lists(st.builds(_Report, st.just(0), _raw_fractions()), max_size=60))
    reports = st.builds(_Report, _SINK_INDEXES, _raw_fractions())
    return draw(st.lists(st.one_of(reports, _new_bands()), max_size=60))


def _check_published(percent: int, previous: int | None) -> None:
    # Entero entre 0 y 100 (bool es subclase de int y no vale) que no baja de la lectura anterior
    assert type(percent) is int, type(percent)
    assert 0 <= percent <= 100, percent
    if previous is not None:
        assert percent >= previous, (previous, percent)


# Feature: file-converter, Property 54: El Porcentaje_de_Avance es acotado y no decrece
@pytest.mark.req("14.12")
@given(operations=_operations())
def test_progress_percent_is_bounded_and_never_decreases(
    operations: Sequence[_Operation],
) -> None:
    reporter = ProgressReporter()
    sinks: list[ProgressReporter | ProgressBand] = [reporter]
    depths = [0]  # franjas que hay entre cada destino y el reporter
    previous = reporter.percent
    _check_published(previous, None)
    for operation in operations:
        if isinstance(operation, _NewBand):
            index = operation.parent % len(sinks)
            sinks.append(sinks[index].band(operation.start, operation.end))
            depths.append(depths[index] + 1)
        else:
            sinks[operation.sink % len(sinks)].report(operation.fraction)
        percent = reporter.percent
        _check_published(percent, previous)
        previous = percent

    fractions = [op.fraction for op in operations if isinstance(op, _Report)]
    if any(math.isnan(fraction) for fraction in fractions):
        event("con NaN")
    if any(math.isinf(fraction) for fraction in fractions):
        event("con infinitos")
    if any(fraction < 0.0 for fraction in fractions):
        event("con negativos")
    if any(fraction > 1.0 for fraction in fractions):
        event("con mayores que 1")
    if max(depths) >= 2:
        event("con subfranjas")
    elif max(depths) == 1:
        event("con franjas")
    final = "100" if previous == 100 else "0" if previous == 0 else "intermedio"
    event(f"porcentaje final: {final}")
