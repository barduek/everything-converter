"""Propiedad 64: `load_config` acepta los valores válidos y nombra los inválidos (Req 20.5, 20.6).

El oráculo usa su propia copia de la tabla "Configuración" de design.md, no `VARIABLES` de
config.py, para que un nombre, rango o predeterminado mal escrito en config.py haga fallar la
prueba.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass

import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from converter.config import Config, ConfigError, load_config


@dataclass(frozen=True)
class _Variable:
    """Una fila de la tabla "Configuración" de design.md."""

    name: str
    minimum: int
    maximum: int
    default: int
    # Variable cuyo valor efectivo reemplaza al extremo fijo del rango
    minimum_from: str | None = None
    maximum_from: str | None = None


_UPLOAD = "CONVERTER_MAX_UPLOAD_MB"
_OGG_MIN = "CONVERTER_OGG_MIN_SAMPLE_RATE_HZ"

# En el orden del diseño, que pone cada referencia antes de las variables que dependen de ella.
# El extremo fijo de una variable dependiente es el que se aplica cuando su referencia es
# inválida, como documenta config.py: el más amplio que la referencia puede admitir.
_TABLE: tuple[_Variable, ...] = (
    _Variable("CONVERTER_PORT", 1, 65_535, 8080),
    _Variable(_UPLOAD, 1, 100_000, 500),
    _Variable("CONVERTER_MAX_IMAGE_MB", 1, 100_000, 25, maximum_from=_UPLOAD),
    _Variable("CONVERTER_MAX_DOCUMENT_MB", 1, 100_000, 25, maximum_from=_UPLOAD),
    _Variable("CONVERTER_MAX_AUDIO_MB", 1, 100_000, 200, maximum_from=_UPLOAD),
    _Variable("CONVERTER_MAX_VIDEO_MB", 1, 100_000, 500, maximum_from=_UPLOAD),
    _Variable("CONVERTER_MAX_IMAGE_PIXELS", 1, 1_000_000_000, 100_000_000),
    _Variable("CONVERTER_MAX_TRANSCRIPTION_MINUTES", 1, 5999, 60),
    _Variable("CONVERTER_TIMEOUT_IMAGE_MINUTES", 1, 1440, 2),
    _Variable("CONVERTER_TIMEOUT_DOCUMENT_MINUTES", 1, 1440, 2),
    _Variable("CONVERTER_TIMEOUT_AUDIO_MINUTES", 1, 1440, 10),
    _Variable("CONVERTER_TIMEOUT_VIDEO_MINUTES", 1, 1440, 30),
    _Variable("CONVERTER_TIMEOUT_TRANSCRIPTION_MINUTES", 1, 1440, 120),
    _Variable("CONVERTER_RETENTION_MINUTES", 1, 10_080, 60),
    _Variable("CONVERTER_CONCURRENCY", 1, 64, 2),
    _Variable("CONVERTER_RASTER_DPI", 36, 600, 150),
    _Variable("CONVERTER_MIN_FREE_SPACE_MB", 0, 1_000_000, 1000),
    _Variable("CONVERTER_PROGRESS_INTERVAL_SECONDS", 1, 60, 5),
    _Variable(_OGG_MIN, 8000, 192_000, 8000),
    _Variable("CONVERTER_OGG_MAX_SAMPLE_RATE_HZ", 8000, 192_000, 192_000, minimum_from=_OGG_MIN),
)

# Claves que no son Variables_de_Configuración y que `load_config` debe ignorar
_UNRELATED = (
    "PATH",
    "HOME",
    "CONVERTER_OTRA_VARIABLE",
    "CONVERTER_PORT_EXTRA",
    "converter_port",
    " CONVERTER_PORT",
)

_WHITESPACE = (" ", "\t", "\n", "\r", "\v", "\f", "\u00a0", "\u2003", "\u3000")
# Ceros de dígitos decimales de otras escrituras, que `int()` acepta y la configuración no:
# árabe-índicos, persas, devanagari y de ancho completo
_FOREIGN_ZEROS = (0x0660, 0x06F0, 0x0966, 0xFF10)
_NOT_INTEGERS = ("abc", "ochenta", "8_080", "80.0", "1e3", "0x50", "1,000", "None", "true")


def _field(name: str) -> str:
    # Campo de `Config`: "CONVERTER_MAX_UPLOAD_MB" da "max_upload_mb"
    return name.removeprefix("CONVERTER_").lower()


def _is_ascii_decimal(raw: str) -> bool:
    # Entero en base 10 escrito solo con los dígitos ASCII del 0 al 9; "" no lo es
    return raw.isascii() and raw.isdigit()


def _bound(fixed: int, source: str | None, effective: Mapping[str, int]) -> int:
    # Si la referencia es inválida no tiene valor efectivo y rige el extremo fijo
    if source is not None and source in effective:
        return effective[source]
    return fixed


def _admitted(var: _Variable, effective: Mapping[str, int]) -> tuple[int, int]:
    """Rango admitido de `var` según los valores efectivos de las variables anteriores."""
    return (
        _bound(var.minimum, var.minimum_from, effective),
        _bound(var.maximum, var.maximum_from, effective),
    )


def _is_valid(raw: str, low: int, high: int) -> bool:
    return _is_ascii_decimal(raw) and low <= int(raw) <= high


def _expected(env: Mapping[str, str]) -> tuple[dict[str, int], list[str]]:
    """Valores efectivos de las variables válidas o no definidas, y nombres de las inválidas."""
    effective: dict[str, int] = {}
    invalid: list[str] = []
    for var in _TABLE:
        raw = env.get(var.name)
        if raw is None:
            effective[var.name] = var.default
        elif _is_valid(raw, *_admitted(var, effective)):
            effective[var.name] = int(raw)
        else:
            invalid.append(var.name)
    return effective, invalid


def _decimal(value: int, zeros: int) -> str:
    # Los ceros a la izquierda no cambian el valor
    return "0" * zeros + str(value)


def _signed(sign: str, digits: str) -> str:
    return sign + digits


def _with_whitespace(digits: str, space: str, where: str) -> str:
    if where == "inicio":
        return space + digits
    if where == "fin":
        return digits + space
    middle = max(1, len(digits) // 2)
    return digits[:middle] + space + digits[middle:]


def _foreign_digits(value: int, zero: int) -> str:
    return "".join(chr(zero + int(digit)) for digit in str(value))


@st.composite
def _integers(draw: st.DrawFn, var: _Variable, low: int, high: int) -> int:
    """Enteros dentro, en el borde y fuera del rango fijo y del rango admitido `low` a `high`.

    En una variable dependiente con referencia válida el rango admitido puede ser más estrecho
    que el fijo. Los enteros que quedan entre ambos solo violan el rango dependiente y salen en
    la mitad de los casos.
    """
    narrower: list[st.SearchStrategy[int]] = []
    if high < var.maximum:
        narrower.append(st.integers(high + 1, var.maximum))
    if low > var.minimum:
        narrower.append(st.integers(var.minimum, low - 1))
    if len(narrower) > 0 and draw(st.booleans()):
        return draw(st.one_of(narrower))
    edges = {var.minimum - 1, var.minimum, var.maximum, var.maximum + 1}
    edges |= {low - 1, low, high, high + 1}
    options = [
        st.integers(var.minimum, var.maximum),
        st.sampled_from(sorted(n for n in edges if n >= 0)),
        # Por debajo del mínimo fijo; con mínimo 0 solo queda el 0, que es válido
        st.integers(0, max(var.minimum - 1, 0)),
        # Por encima del máximo fijo, hasta números de 40 cifras
        st.integers(var.maximum + 1, 10**40),
    ]
    return draw(st.one_of(options))


def _in_range(low: int, high: int) -> st.SearchStrategy[int]:
    # Los extremos aparecen en cerca de la mitad de los ejemplos
    return st.one_of(st.sampled_from([low, high]), st.integers(low, high))


@st.composite
def _arbitrary_values(draw: st.DrawFn, var: _Variable, low: int, high: int) -> str:
    """Valores válidos o inválidos: enteros, vacíos, signos, espacios, otros dígitos y texto.

    La mitad de los valores son enteros en base 10; la otra mitad nunca lo es. Los signos, los
    espacios y los dígitos de otras escrituras se aplican a enteros del rango admitido `low` a
    `high`, para que solo la forma de escribirlos los vuelva inválidos.
    """
    # Con `decimals | st.one_of(...)` Hypothesis aplana las opciones y los enteros serían 1 de 7
    if draw(st.booleans()):
        return _decimal(draw(_integers(var, low, high)), draw(st.integers(0, 3)))
    admitted = st.builds(_decimal, _in_range(low, high), st.integers(0, 3))
    not_decimals: list[st.SearchStrategy[str]] = [
        st.just(""),
        st.builds(_signed, st.sampled_from(["+", "-"]), admitted),
        st.builds(
            _with_whitespace,
            admitted,
            st.sampled_from(_WHITESPACE),
            st.sampled_from(["inicio", "medio", "fin"]),
        ),
        st.builds(_foreign_digits, _in_range(low, high), st.sampled_from(_FOREIGN_ZEROS)),
        st.sampled_from(_NOT_INTEGERS),
        st.text(max_size=20).filter(lambda raw: not _is_ascii_decimal(raw)),
    ]
    return draw(st.one_of(not_decimals))


# Qué hace el generador con cada variable. Una variable "libre" toma cualquier valor, válido o no.
_ONLY_VALID: tuple[str, ...] = ("no definida", "válida")
_WITH_FREE: tuple[tuple[str, ...], ...] = (
    ("no definida", "válida", "libre"),  # una de cada 3 variables es libre
    ("no definida",) * 4 + ("válida",) * 4 + ("libre",),  # una de cada 9
)


@st.composite
def env_mappings(draw: st.DrawFn) -> dict[str, str]:
    """Entornos con Variables_de_Configuración no definidas, válidas e inválidas, y claves ajenas.

    En cerca de la mitad de los ejemplos todas las variables definidas son válidas. El
    generador sigue los valores efectivos en el orden de la tabla para conocer el rango admitido
    de cada variable dependiente y elegir valores en sus bordes o entre ese rango y el fijo.
    """
    kinds = _ONLY_VALID if draw(st.booleans()) else draw(st.sampled_from(_WITH_FREE))
    effective: dict[str, int] = {}
    env: dict[str, str] = {}
    for var in _TABLE:
        kind = draw(st.sampled_from(kinds))
        if kind == "no definida":
            effective[var.name] = var.default
            continue
        low, high = _admitted(var, effective)
        if kind == "válida":
            raw = _decimal(draw(_in_range(low, high)), draw(st.integers(0, 3)))
        else:
            raw = draw(_arbitrary_values(var, low, high))
        env[var.name] = raw
        if _is_valid(raw, low, high):
            effective[var.name] = int(raw)
    unrelated = draw(st.permutations(_UNRELATED))[: draw(st.integers(0, len(_UNRELATED)))]
    for key in unrelated:
        env[key] = draw(st.text(max_size=10))
    return env


# Feature: file-converter, Property 64: La configuración acepta los valores válidos y nombra los inválidos  # noqa: E501
@pytest.mark.req("20.5", "20.6")
@given(env=env_mappings())
def test_load_config_accepts_valid_values_and_names_invalid_ones(env: dict[str, str]) -> None:
    effective, invalid = _expected(env)
    result = load_config(env)
    if invalid:
        event("con variables inválidas")
        assert isinstance(result, list), result
        assert all(isinstance(error, ConfigError) and error.reason for error in result), result
        # Un error por variable inválida y ninguno por las válidas, las no definidas o las ajenas
        assert sorted(error.variable for error in result) == sorted(invalid)
    else:
        event("todas válidas")
        assert isinstance(result, Config), result
        # Los valores definidos y, para las no definidas, los predeterminados de la tabla
        expected = {_field(name): value for name, value in effective.items()}
        assert dataclasses.asdict(result) == expected
