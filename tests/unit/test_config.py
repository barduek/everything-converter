"""Pruebas de src/converter/config.py: valores predeterminados, bordes de cada rango, rangos
dependientes y errores informados juntos (Req 20.5, 20.6).

Los rangos y los predeterminados esperados se copian de la tabla "Configuración" de design.md en
`_DOCUMENTED`, en lugar de tomarlos de `VARIABLES`, para que un valor mal escrito en config.py
haga fallar las pruebas.
"""

from __future__ import annotations

import dataclasses
from typing import NamedTuple

import pytest

from converter.config import VARIABLES, Config, ConfigError, load_config

_UPLOAD = "CONVERTER_MAX_UPLOAD_MB"
_OGG_MIN = "CONVERTER_OGG_MIN_SAMPLE_RATE_HZ"
_OGG_MAX = "CONVERTER_OGG_MAX_SAMPLE_RATE_HZ"
_CATEGORY_LIMITS = (
    "CONVERTER_MAX_IMAGE_MB",
    "CONVERTER_MAX_DOCUMENT_MB",
    "CONVERTER_MAX_AUDIO_MB",
    "CONVERTER_MAX_VIDEO_MB",
)

# Problemas que `load_config` escribe al inicio del motivo
_EMPTY = "está vacía"
_NOT_INTEGER = "no es un entero en base 10 sin signo ni espacios"
_OUT_OF_RANGE = "está fuera de rango"


class _Documented(NamedTuple):
    """Una fila de la tabla "Configuración" de design.md.

    En una variable dependiente, `minimum` y `maximum` son los extremos de su rango más amplio:
    los que rigen cuando la variable de referencia vale su máximo (Límites_de_Tamaño por
    Categoría) o su mínimo (máximo de OGG), y también cuando la referencia es inválida.
    """

    name: str
    minimum: int
    maximum: int
    default: int
    minimum_from: str | None = None
    maximum_from: str | None = None


# En el orden de la tabla del diseño, que es el orden en que se informan los errores
_DOCUMENTED: tuple[_Documented, ...] = (
    _Documented("CONVERTER_PORT", 1, 65_535, 8080),
    _Documented(_UPLOAD, 1, 100_000, 500),
    _Documented("CONVERTER_MAX_IMAGE_MB", 1, 100_000, 25, maximum_from=_UPLOAD),
    _Documented("CONVERTER_MAX_DOCUMENT_MB", 1, 100_000, 25, maximum_from=_UPLOAD),
    _Documented("CONVERTER_MAX_AUDIO_MB", 1, 100_000, 200, maximum_from=_UPLOAD),
    _Documented("CONVERTER_MAX_VIDEO_MB", 1, 100_000, 500, maximum_from=_UPLOAD),
    _Documented("CONVERTER_MAX_IMAGE_PIXELS", 1, 1_000_000_000, 100_000_000),
    _Documented("CONVERTER_MAX_TRANSCRIPTION_MINUTES", 1, 5999, 60),
    _Documented("CONVERTER_TIMEOUT_IMAGE_MINUTES", 1, 1440, 2),
    _Documented("CONVERTER_TIMEOUT_DOCUMENT_MINUTES", 1, 1440, 2),
    _Documented("CONVERTER_TIMEOUT_AUDIO_MINUTES", 1, 1440, 10),
    _Documented("CONVERTER_TIMEOUT_VIDEO_MINUTES", 1, 1440, 30),
    _Documented("CONVERTER_TIMEOUT_TRANSCRIPTION_MINUTES", 1, 1440, 120),
    _Documented("CONVERTER_RETENTION_MINUTES", 1, 10_080, 60),
    _Documented("CONVERTER_CONCURRENCY", 1, 64, 2),
    _Documented("CONVERTER_RASTER_DPI", 36, 600, 150),
    _Documented("CONVERTER_MIN_FREE_SPACE_MB", 0, 1_000_000, 1000),
    _Documented("CONVERTER_PROGRESS_INTERVAL_SECONDS", 1, 60, 5),
    _Documented(_OGG_MIN, 8000, 192_000, 8000),
    _Documented(_OGG_MAX, 8000, 192_000, 192_000, minimum_from=_OGG_MIN),
)


def _field(name: str) -> str:
    # Campo de `Config`: "CONVERTER_MAX_UPLOAD_MB" da "max_upload_mb"
    return name.removeprefix("CONVERTER_").lower()


_DEFAULTS = {_field(var.name): var.default for var in _DOCUMENTED}


def _config(env: dict[str, str]) -> Config:
    result = load_config(env)
    assert isinstance(result, Config), result
    return result


def _errors(env: dict[str, str]) -> list[ConfigError]:
    result = load_config(env)
    assert isinstance(result, list), result
    return result


def _reason(problem: str, low: object, high: object) -> str:
    return f"{problem}; debe ser un entero entre {low} y {high}"


def _ref(name: str, value: int) -> str:
    # Extremo que depende del valor efectivo de otra variable
    return f"el valor de {name} ({value})"


def _widest(var: _Documented) -> dict[str, str]:
    """Define la referencia de `var`, si tiene, en el valor que da su rango más amplio."""
    context: dict[str, str] = {}
    if var.maximum_from is not None:
        context[var.maximum_from] = str(var.maximum)
    if var.minimum_from is not None:
        context[var.minimum_from] = str(var.minimum)
    return context


def _widest_reason(problem: str, var: _Documented) -> str:
    """Motivo de un error de `var` con su referencia definida según `_widest`."""
    low = str(var.minimum) if var.minimum_from is None else _ref(var.minimum_from, var.minimum)
    high = str(var.maximum) if var.maximum_from is None else _ref(var.maximum_from, var.maximum)
    return _reason(problem, low, high)


def _ids(var: _Documented) -> str:
    return var.name


def test_table_matches_config_fields_and_is_consistent() -> None:
    names = [spec.name for spec in VARIABLES]
    assert len(names) == 20
    assert len(set(names)) == 20
    assert [spec.field for spec in VARIABLES] == [f.name for f in dataclasses.fields(Config)]
    by_name = {spec.name: spec for spec in VARIABLES}
    for index, spec in enumerate(VARIABLES):
        assert spec.minimum <= spec.default <= spec.maximum
        for source in (spec.minimum_from, spec.maximum_from):
            if source is not None:
                # La referencia se valida antes que la variable que depende de ella
                assert names.index(source) < index
        # El rango fijo de una variable dependiente es el más amplio que admite su referencia
        if spec.maximum_from is not None:
            assert spec.maximum == by_name[spec.maximum_from].maximum
        if spec.minimum_from is not None:
            assert spec.minimum == by_name[spec.minimum_from].minimum


# Variables no definidas


@pytest.mark.req("20.5")
def test_undefined_variables_take_defaults() -> None:
    config = _config({})
    assert dataclasses.asdict(config) == _DEFAULTS
    assert config.max_upload_bytes == 500_000_000
    assert config.max_image_bytes == 25_000_000
    assert config.min_free_space_bytes == 1_000_000_000
    assert config.timeout_transcription_seconds == 120 * 60
    assert config.retention_seconds == 3600


@pytest.mark.req("20.5")
def test_defined_values_override_defaults_and_derive_units() -> None:
    config = _config(
        {
            "CONVERTER_PORT": "0080",
            "CONVERTER_MAX_UPLOAD_MB": "1000",
            "CONVERTER_MAX_VIDEO_MB": "1000",
            "CONVERTER_MIN_FREE_SPACE_MB": "0",
            "CONVERTER_MAX_TRANSCRIPTION_MINUTES": "5999",
            "CONVERTER_TIMEOUT_AUDIO_MINUTES": "1",
            "CONVERTER_OTRA_VARIABLE": "no se valida",
        }
    )
    assert config.port == 80
    assert config.max_upload_bytes == 1_000_000_000
    assert config.max_video_bytes == 1_000_000_000
    assert config.min_free_space_bytes == 0
    assert config.max_transcription_seconds == 5999 * 60
    assert config.timeout_audio_seconds == 60
    # Las no definidas siguen con su predeterminado
    assert config.max_image_mb == 25


def test_config_is_immutable() -> None:
    config = _config({})
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.port = 9000  # type: ignore[misc]


# Bordes de cada rango


@pytest.mark.req("20.5")
@pytest.mark.parametrize("border", ["mínimo", "máximo"])
@pytest.mark.parametrize("var", _DOCUMENTED, ids=_ids)
def test_value_at_each_range_border_is_taken(var: _Documented, border: str) -> None:
    value = var.minimum if border == "mínimo" else var.maximum
    context = _widest(var)
    config = _config(context | {var.name: str(value)})
    # La variable toma el valor del borde y las demás no definidas, su predeterminado
    expected = _DEFAULTS | {_field(name): int(raw) for name, raw in context.items()}
    assert dataclasses.asdict(config) == expected | {_field(var.name): value}


@pytest.mark.req("20.6")
@pytest.mark.parametrize("side", ["bajo el mínimo", "sobre el máximo"])
@pytest.mark.parametrize("var", _DOCUMENTED, ids=_ids)
def test_value_just_outside_each_range_is_rejected(var: _Documented, side: str) -> None:
    if side == "sobre el máximo":
        raw, problem = str(var.maximum + 1), _OUT_OF_RANGE
    elif var.minimum == 0:
        # Por debajo de 0 solo se escribe con signo, y el signo ya vuelve inválido el valor
        raw, problem = "-1", _NOT_INTEGER
    else:
        raw, problem = str(var.minimum - 1), _OUT_OF_RANGE
    assert _errors(_widest(var) | {var.name: raw}) == [
        ConfigError(var.name, _widest_reason(problem, var))
    ]


@pytest.mark.req("20.6")
@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ("", _EMPTY),
        ("abc", _NOT_INTEGER),
        ("+80", _NOT_INTEGER),
        ("-1", _NOT_INTEGER),
        (" 80", _NOT_INTEGER),
        ("80\n", _NOT_INTEGER),
        ("8_080", _NOT_INTEGER),
        ("80.0", _NOT_INTEGER),
        ("\u0668\u0660", _NOT_INTEGER),
        ("0", _OUT_OF_RANGE),
        ("65536", _OUT_OF_RANGE),
        ("9" * 5000, _OUT_OF_RANGE),
    ],
)
def test_invalid_value_names_the_variable_and_reason(raw: str, problem: str) -> None:
    assert _errors({"CONVERTER_PORT": raw}) == [
        ConfigError("CONVERTER_PORT", _reason(problem, 1, 65535))
    ]


# Rangos dependientes


@pytest.mark.req("20.6")
@pytest.mark.parametrize(
    "upload", [None, "1", "300", "100000"], ids=["predeterminado", "1", "300", "100000"]
)
@pytest.mark.parametrize("variable", _CATEGORY_LIMITS)
def test_category_limit_admits_at_most_effective_global_limit(
    variable: str, upload: str | None
) -> None:
    env = {} if upload is None else {_UPLOAD: upload}
    limit = 500 if upload is None else int(upload)
    assert getattr(_config(env | {variable: str(limit)}), _field(variable)) == limit
    assert _errors(env | {variable: str(limit + 1)}) == [
        ConfigError(variable, _reason(_OUT_OF_RANGE, 1, _ref(_UPLOAD, limit)))
    ]


@pytest.mark.req("20.5")
def test_undefined_category_limits_keep_defaults_under_smaller_global_limit() -> None:
    config = _config({"CONVERTER_MAX_UPLOAD_MB": "10"})
    assert config.max_upload_mb == 10
    assert config.max_video_mb == 500


@pytest.mark.req("20.6")
@pytest.mark.parametrize(
    "minimum", [None, "8000", "44100", "192000"], ids=["predeterminado", "8000", "44100", "192000"]
)
def test_ogg_max_sample_rate_admits_from_effective_minimum(minimum: str | None) -> None:
    env = {} if minimum is None else {_OGG_MIN: minimum}
    low = 8000 if minimum is None else int(minimum)
    for valid in (low, 192_000):
        assert _config(env | {_OGG_MAX: str(valid)}).ogg_max_sample_rate_hz == valid
    reason = _reason(_OUT_OF_RANGE, _ref(_OGG_MIN, low), 192000)
    for invalid in (low - 1, 192_001):
        # El error nombra al máximo, no al mínimo que lo supera
        assert _errors(env | {_OGG_MAX: str(invalid)}) == [ConfigError(_OGG_MAX, reason)]


@pytest.mark.req("20.6")
def test_dependent_variable_uses_fixed_range_when_reference_is_invalid() -> None:
    # La referencia inválida no arrastra a la dependiente, que se juzga con su rango fijo
    env = {"CONVERTER_MAX_UPLOAD_MB": "abc", "CONVERTER_MAX_IMAGE_MB": "600"}
    assert [error.variable for error in _errors(env)] == ["CONVERTER_MAX_UPLOAD_MB"]
    env["CONVERTER_MAX_IMAGE_MB"] = "100001"
    assert _errors(env)[1] == ConfigError(
        "CONVERTER_MAX_IMAGE_MB", _reason(_OUT_OF_RANGE, 1, 100000)
    )
    env = {"CONVERTER_OGG_MIN_SAMPLE_RATE_HZ": "", "CONVERTER_OGG_MAX_SAMPLE_RATE_HZ": "8000"}
    assert [error.variable for error in _errors(env)] == ["CONVERTER_OGG_MIN_SAMPLE_RATE_HZ"]


# Varios errores informados juntos


@pytest.mark.req("20.6")
def test_errors_of_each_kind_are_reported_together_in_table_order() -> None:
    env = {
        _OGG_MAX: "44099",
        _OGG_MIN: "44100",
        "CONVERTER_MIN_FREE_SPACE_MB": "-1",
        "CONVERTER_RASTER_DPI": "35",
        "CONVERTER_CONCURRENCY": "dos",
        "CONVERTER_MAX_VIDEO_MB": "101",
        _UPLOAD: "100",
        "CONVERTER_PORT": "",
        # Válidas: no generan errores
        "CONVERTER_MAX_IMAGE_MB": "100",
        "CONVERTER_RETENTION_MINUTES": "60",
    }
    assert _errors(env) == [
        ConfigError("CONVERTER_PORT", _reason(_EMPTY, 1, 65535)),
        ConfigError("CONVERTER_MAX_VIDEO_MB", _reason(_OUT_OF_RANGE, 1, _ref(_UPLOAD, 100))),
        ConfigError("CONVERTER_CONCURRENCY", _reason(_NOT_INTEGER, 1, 64)),
        ConfigError("CONVERTER_RASTER_DPI", _reason(_OUT_OF_RANGE, 36, 600)),
        ConfigError("CONVERTER_MIN_FREE_SPACE_MB", _reason(_NOT_INTEGER, 0, 1000000)),
        ConfigError(_OGG_MAX, _reason(_OUT_OF_RANGE, _ref(_OGG_MIN, 44100), 192000)),
    ]


@pytest.mark.req("20.6")
def test_all_category_limits_above_global_limit_are_reported_together() -> None:
    env = {_UPLOAD: "100"} | {name: "101" for name in _CATEGORY_LIMITS}
    assert _errors(env) == [
        ConfigError(name, _reason(_OUT_OF_RANGE, 1, _ref(_UPLOAD, 100)))
        for name in _CATEGORY_LIMITS
    ]


@pytest.mark.req("20.6")
def test_every_variable_invalid_gives_one_error_per_variable() -> None:
    # Con las referencias inválidas, las dependientes se juzgan con su rango fijo
    env = {var.name: "" for var in _DOCUMENTED}
    assert _errors(env) == [
        ConfigError(var.name, _reason(_EMPTY, var.minimum, var.maximum)) for var in _DOCUMENTED
    ]
