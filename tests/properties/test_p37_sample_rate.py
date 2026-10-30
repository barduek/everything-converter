"""Propiedad 37: la frecuencia elegida es la admitida más cercana (Req 9.4, 9.5).

Cada ejemplo da una frecuencia de origen y unas Frecuencias_de_Muestreo_Admitidas de uno de
estos tipos:

- las de MP3, el conjunto fijo del glosario, con orígenes en los puntos medios entre frecuencias
  vecinas (empates exactos donde la suma de las dos es par) y junto a cada frecuencia;
- un conjunto cualquiera de enteros positivos, en la mitad de los ejemplos con un empate
  plantado: dos frecuencias a la misma distancia del origen y ninguna más cerca;
- un intervalo de OGG dentro de los rangos de la configuración (8000 a 192 000 Hz, con el
  mínimo menor o igual que el máximo), con un solo valor en uno de cada 5 ejemplos;
- el intervalo de WAV, que admite cualquier valor que cabe en la cabecera;
- un intervalo cualquiera.

El oráculo usa su propia copia del glosario y de la configuración, no las constantes de
media_rules.py. Para un conjunto recorre todas las frecuencias; para un intervalo distingue si
el origen está dentro, debajo del mínimo o encima del máximo.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise

import pytest
from hypothesis import event, example, given
from hypothesis import strategies as st

from converter.converters.media_rules import (
    MP3_SAMPLE_RATES,
    WAV_SAMPLE_RATES,
    SampleRateRange,
    SampleRates,
    choose_sample_rate,
)

# Frecuencias_de_Muestreo_Admitidas de MP3, copiadas del glosario
_MP3_RATES: frozenset[int] = frozenset(
    {8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000}
)
_MP3_SORTED: tuple[int, ...] = tuple(sorted(_MP3_RATES))
# Puntos medios enteros entre frecuencias vecinas de MP3, donde las dos quedan a igual distancia
_MP3_TIES: tuple[int, ...] = tuple(
    (low + high) // 2 for low, high in pairwise(_MP3_SORTED) if (low + high) % 2 == 0
)
# Enteros alrededor de cada punto medio: los dos más cercanos y uno más a cada lado
_MP3_NEAR_TIES: tuple[int, ...] = tuple(
    sorted(
        {
            (low + high) // 2 + delta
            for low, high in pairwise(_MP3_SORTED)
            for delta in (-1, 0, 1, 2)
        }
    )
)
# Cada frecuencia de MP3 y sus vecinas a 1 Hz
_MP3_EDGES: tuple[int, ...] = tuple(
    sorted({rate + delta for rate in _MP3_RATES for delta in (-1, 0, 1)})
)

# Rangos de CONVERTER_OGG_MIN_SAMPLE_RATE_HZ y CONVERTER_OGG_MAX_SAMPLE_RATE_HZ (tabla de
# configuración del diseño)
_OGG_LOWEST_HZ = 8000
_OGG_HIGHEST_HZ = 192_000
# WAV admite cualquier valor; la cabecera lo guarda en un entero de 32 bits sin signo
_WAV_HIGHEST_HZ = 0xFFFF_FFFF

# Frecuencias de origen que aparecen en archivos reales, de 4 kHz a 768 kHz
_COMMON_RATES: tuple[int, ...] = (
    4000, 6000, 7350, 8000, 11025, 12000, 16000, 22050, 24000, 32000, 37800, 44056, 44100,
    47250, 48000, 50000, 50400, 64000, 88200, 96000, 176400, 192000, 352800, 384000, 705600,
    768000,
)  # fmt: skip
# Extremos de intervalo de OGG con más peso: los bordes de la configuración y valores habituales
_OGG_EDGES: tuple[int, ...] = (
    _OGG_LOWEST_HZ, _OGG_LOWEST_HZ + 1, 11025, 16000, 22050, 32000, 44100, 48000, 96000,
    _OGG_HIGHEST_HZ - 1, _OGG_HIGHEST_HZ,
)  # fmt: skip

# Valor máximo de las frecuencias de un conjunto cualquiera: valores chicos para que haya
# frecuencias vecinas y empates, valores de audio y cualquier valor de la cabecera WAV
_SET_SCALES: tuple[int, ...] = (64, 200_000, _WAV_HIGHEST_HZ)

type _Reference = frozenset[int] | tuple[int, int]


@dataclass(frozen=True)
class _Case:
    """Una frecuencia de origen y las Frecuencias_de_Muestreo_Admitidas del destino.

    `admitted` es lo que recibe `choose_sample_rate`. `reference` es lo mismo en la forma del
    oráculo: un conjunto o un intervalo cerrado `(mínimo, máximo)`.
    """

    source: int
    admitted: SampleRates
    reference: _Reference
    kind: str


def _is_admitted(rate: int, reference: _Reference) -> bool:
    if isinstance(reference, tuple):
        minimum, maximum = reference
        return minimum <= rate <= maximum
    return rate in reference


def _expected(source: int, reference: _Reference) -> int:
    # Req 9.5: una frecuencia admitida se conserva
    if _is_admitted(source, reference):
        return source
    # Req 9.4: la admitida más cercana. En un intervalo es el extremo del lado del origen
    if isinstance(reference, tuple):
        minimum, maximum = reference
        return minimum if source < minimum else maximum
    # En un conjunto se recorren todas; a igual distancia, la mayor
    distance = min(abs(rate - source) for rate in reference)
    return max(rate for rate in reference if abs(rate - source) == distance)


def _mp3_cases() -> st.SearchStrategy[_Case]:
    sources = st.one_of(
        st.sampled_from(_MP3_TIES),
        st.sampled_from(_MP3_NEAR_TIES),
        st.sampled_from(_MP3_EDGES),
        st.sampled_from(_COMMON_RATES),
        st.integers(1, 200_000),
        st.integers(1, _WAV_HIGHEST_HZ),
    )
    return sources.map(lambda source: _Case(source, MP3_SAMPLE_RATES, _MP3_RATES, "MP3"))


def _set_sources(rates: Sequence[int], upper: int) -> st.SearchStrategy[int]:
    """Orígenes admitidos, a 1 Hz de una admitida, en el punto medio de dos vecinas o cualquiera."""
    neighbours = sorted({max(rate + delta, 1) for rate in rates for delta in (-1, 1)})
    midpoints = sorted(
        {(low + high) // 2 + delta for low, high in pairwise(rates) for delta in (0, 1)}
    )
    return st.one_of(
        st.sampled_from(rates),
        st.sampled_from(neighbours),
        st.sampled_from(midpoints) if midpoints else st.nothing(),
        st.integers(1, upper + 64),
    )


@st.composite
def _set_cases(draw: st.DrawFn) -> _Case:
    upper = draw(st.sampled_from(_SET_SCALES))
    rates = set(draw(st.frozensets(st.integers(1, upper), min_size=1, max_size=12)))
    if draw(st.booleans()):
        # Empate plantado: `source ± half` están en el conjunto y se quita todo lo más cercano
        source = draw(st.integers(2, upper))
        half = draw(st.integers(1, source - 1))
        rates = {rate for rate in rates if abs(rate - source) >= half}
        rates |= {source - half, source + half}
    else:
        source = draw(_set_sources(sorted(rates), upper))
    frozen = frozenset(rates)
    return _Case(source, frozen, frozen, "conjunto")


def _range_sources(minimum: int, maximum: int, upper: int) -> st.SearchStrategy[int]:
    """Orígenes en los extremos y a 1 Hz, habituales, dentro del intervalo o cualquiera."""
    edges = sorted({max(end + delta, 1) for end in (minimum, maximum) for delta in (-1, 0, 1)})
    return st.one_of(
        st.sampled_from(edges),
        st.sampled_from(_COMMON_RATES),
        st.integers(minimum, maximum),
        st.integers(1, upper),
    )


def _range_cases(ends: st.SearchStrategy[int], upper: int, kind: str) -> st.SearchStrategy[_Case]:
    """Intervalos con extremos de `ends` y orígenes hasta `upper`; uno de cada 5, de un valor."""

    @st.composite
    def cases(draw: st.DrawFn) -> _Case:
        first = draw(ends)
        second = first if draw(st.integers(0, 4)) == 0 else draw(ends)
        minimum, maximum = min(first, second), max(first, second)
        source = draw(_range_sources(minimum, maximum, upper))
        return _Case(source, SampleRateRange(minimum, maximum), (minimum, maximum), kind)

    return cases()


def _wav_cases() -> st.SearchStrategy[_Case]:
    sources = st.one_of(
        st.sampled_from((1, 2, _WAV_HIGHEST_HZ - 1, _WAV_HIGHEST_HZ)),
        st.sampled_from(_COMMON_RATES),
        st.integers(1, _WAV_HIGHEST_HZ),
    )
    return sources.map(lambda source: _Case(source, WAV_SAMPLE_RATES, (1, _WAV_HIGHEST_HZ), "WAV"))


def _cases() -> st.SearchStrategy[_Case]:
    ogg_ends = st.one_of(st.sampled_from(_OGG_EDGES), st.integers(_OGG_LOWEST_HZ, _OGG_HIGHEST_HZ))
    any_ends = st.one_of(st.integers(1, 64), st.integers(1, _WAV_HIGHEST_HZ))
    return st.one_of(
        _mp3_cases(),
        _set_cases(),
        _range_cases(ogg_ends, 2 * _OGG_HIGHEST_HZ, "OGG"),
        _wav_cases(),
        _range_cases(any_ends, 2 * _WAV_HIGHEST_HZ, "intervalo"),
    )


def _with_mp3_ties[T: Callable[..., None]](test: T) -> T:
    """Agrega como ejemplos explícitos los empates exactos entre frecuencias vecinas de MP3."""
    for tie in _MP3_TIES:
        test = example(case=_Case(tie, MP3_SAMPLE_RATES, _MP3_RATES, "MP3"))(test)
    return test


# Feature: file-converter, Property 37: La frecuencia elegida es la admitida más cercana
@pytest.mark.req("9.4", "9.5")
@given(case=_cases())
@_with_mp3_ties
def test_chosen_rate_is_the_nearest_admitted(case: _Case) -> None:
    result = choose_sample_rate(case.source, case.admitted)

    assert result == _expected(case.source, case.reference), (case, result)
    assert _is_admitted(result, case.reference), (case, result)

    event(f"admitidas: {case.kind}")
    if _is_admitted(case.source, case.reference):
        event("origen admitido, se conserva")
        return
    event("origen no admitido, se remuestrea")
    if isinstance(case.reference, tuple):
        minimum, maximum = case.reference
        if minimum == maximum:
            event("intervalo de un solo valor")
        event("debajo del mínimo" if case.source < minimum else "encima del máximo")
    else:
        distance = abs(result - case.source)
        if case.source - distance in case.reference and case.source + distance in case.reference:
            event("empate")
