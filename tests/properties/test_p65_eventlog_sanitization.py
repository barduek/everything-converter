"""Propiedad 65: el Log_de_Eventos no contiene contenido de los archivos (Req 16.8).

Cada ejemplo genera un fragmento del contenido de un archivo (marcas de contenido privado, texto
extraído, metadatos, texto transcripto, Unicode, saltos de línea, comillas, barras y caracteres
de control) y lo pone donde el contenido llega en la práctica al detalle técnico de un error:

- En las líneas de stderr de una herramienta: solo, en una línea que también tiene un patrón de
  `PATTERNS`, dentro de una ruta, como metadato o texto transcripto y citado en una traza de
  Python. Las líneas se arman como las entrega el ejecutor: el texto se corta en cada "\\n" y
  se quita un "\\r" final, así un fragmento con saltos de línea queda en varias líneas. La
  salida estándar no se prueba: el ejecutor solo la entrega a los callbacks de progreso.
- En excepciones: OSError de una llamada real con la ruta del archivo, OSError con dos rutas,
  UnicodeDecodeError sobre los bytes del contenido, UnicodeEncodeError, KeyError y ValueError
  con el texto extraído, con causas y contextos encadenados y con notas (PEP 678).
- En los errores de limpieza de un Espacio_de_Trabajo, con rutas que incluyen el fragmento.

El detalle se arma con `sanitize_tool_output` y `describe_exception` y se registra con
`task_failed`, `registry_rejected` y `cleanup_failed`. La prueba verifica que cada línea del log
es un único objeto JSON que empieza con ts, level y event, y que ni el fragmento ni cada una de
sus líneas aparecen en el texto del log: tal cual, escapados como JSON (con y sin
`ensure_ascii`) o como `repr` de Python, que es como `str()` cita rutas y claves en OSError y
KeyError.

Falsos positivos. El log copia textos legítimos: los de `PATTERNS`, nombres de campos, Etapas,
Motivos_de_Fallo, nombres de errno y de señales, identificadores y números. Hypothesis además
propone textos tomados de las constantes del código, como un texto de patrón. Un fragmento que
es parte de esos textos no es una filtración. Para descartarlos, cada ejemplo arma también un
log de control: el mismo escenario con el fragmento reemplazado por "x", byte por byte, salvo
los saltos de línea, los NUL, las barras y los textos de patrón que contiene. Así las líneas,
los patrones reconocidos y los errores son los mismos, y solo se buscan las formas del fragmento
que no aparecen en el log de control ni en los textos fijos que pueden cambiar con el fragmento
(textos e identificadores de patrón, nombres de errno, de señales y de excepciones). El control
descarta también las formas que coinciden con un tramo que cruza varios campos del JSON, como
`null,"`. Los detalles no se comparan con los del control: si la implementación copiara
contenido, serían distintos justamente en los casos que la prueba debe detectar.
"""

from __future__ import annotations

import builtins
import errno
import io
import json
import os
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import NoReturn

import pytest
from hypothesis import assume, event, given
from hypothesis import strategies as st

from converter.eventlog import (
    PATTERNS,
    ErrorDetail,
    Event,
    EventLog,
    Level,
    RegistryEntryKind,
    Tool,
    ToolDetail,
    ToolPattern,
    describe_exception,
    pair_label,
    sanitize_tool_output,
)
from converter.messages_es import FailureReason
from converter.models import Stage
from converter.registry.registry import RegistryErrorCode

_NOW = datetime(2025, 1, 15, 10, 32, 7, 412987, tzinfo=UTC)
_TS = "2025-01-15T10:32:07.412Z"
_LEVELS = frozenset(level.value for level in Level)

# Almacenamiento_Temporal que no existe: las llamadas reales con rutas fallan sin tocar archivos
_STORAGE = "/p65-almacenamiento-inexistente"

_FORMATS = (
    "PNG", "JPG", "WEBP", "PDF", "TXT", "MD", "HTML", "DOCX", "MP3", "WAV", "OGG", "MP4", "WEBM",
    "SRT",
)  # fmt: skip

# Textos fijos de los patrones de todas las herramientas
_PATTERN_TEXTS = tuple(sorted({pattern.text for group in PATTERNS.values() for pattern in group}))

# Textos fijos del log que pueden cambiar con el fragmento: el patrón reconocido, el errno, la
# señal y la clase de la excepción. El log de control los repite, salvo que el reemplazo del
# fragmento cambiara alguno; por eso también se descartan las formas que son parte de ellos
_FIXED_TEXTS = frozenset(
    {
        *_PATTERN_TEXTS,
        *(pattern.id for group in PATTERNS.values() for pattern in group),
        *errno.errorcode.values(),
        *(signum.name for signum in signal.Signals),
        *(
            name
            for name, value in vars(builtins).items()
            if isinstance(value, type) and issubclass(value, BaseException)
        ),
    }
)

# --------------------------------------------------------------------------------------------
# Fragmentos de contenido

_CONTENT_SAMPLES = (
    "Informe trimestral confidencial: ventas 2024",  # texto extraído de un documento
    "Autor: Juana Pérez <juana@example.com>",  # metadatos
    "hola, ¿me escuchás bien?",  # texto transcripto
    "Ñandú «comillas» “tipográficas” — 😀",
    'clave="valor" y una barra \\ invertida',
    "línea uno\nlínea dos\r\nlínea tres",
    "\u202eotxet invertido\u202c",
    '{"ts":"falso","level":"info"}',  # JSON dentro del contenido
    "\x1b[31mrojo\x1b[0m\x00fin",
    "C:\\Users\\juana\\Documentos\\privado.docx",
    "../../etc/passwd",
)

_SEPARATORS = (
    "\n", "\r\n", "\r", '"', "'", "\\", "\t", "\x00", "\x85", "\u2028", "/", ": ", "{", "}",
)  # fmt: skip

# Marcas de contenido privado, fáciles de reconocer en un contraejemplo
_SECRET_MARKERS = st.text("0123456789abcdef", min_size=8, max_size=16).map(
    lambda digits: f"SECRETO-{digits}"
)


def _fragments() -> st.SearchStrategy[str]:
    """Marcas, muestras de contenido, combinaciones y texto Unicode arbitrario.

    Las combinaciones mezclan marcas, muestras, separadores (saltos de línea, comillas, barras,
    NUL), textos de patrón y texto arbitrario: una línea que solo tiene contenido del archivo
    puede contener un patrón conocido.
    """
    parts = st.one_of(
        _SECRET_MARKERS,
        st.sampled_from(_CONTENT_SAMPLES),
        st.sampled_from(_SEPARATORS),
        st.sampled_from(_PATTERN_TEXTS),
        st.text(max_size=10),
    )
    return st.one_of(
        _SECRET_MARKERS,
        st.sampled_from(_CONTENT_SAMPLES),
        st.lists(parts, min_size=2, max_size=6).map("".join),
        st.text(min_size=3, max_size=80),
    )


# Caracteres que el log de control conserva: definen las líneas y las rutas
_KEPT = frozenset("\n\r\x00/")


def _placeholder(fragment: str) -> str:
    """El fragmento con cada carácter reemplazado por tantas "x" como bytes tiene en UTF-8.

    Conserva `_KEPT` y los textos de patrón que contiene, así las líneas de stderr reconocen
    los mismos patrones y las rutas tienen los mismos componentes y longitudes en bytes, que
    deciden el errno.
    """
    keep = [char in _KEPT for char in fragment]
    for text in _PATTERN_TEXTS:
        start = fragment.find(text)
        while start != -1:
            keep[start : start + len(text)] = [True] * len(text)
            start = fragment.find(text, start + 1)
    return "".join(
        char if kept else "x" * len(char.encode())
        for char, kept in zip(fragment, keep, strict=True)
    )


def _forms(text: str) -> set[str]:
    """`text` tal cual, escapado como JSON con y sin `ensure_ascii` y como `repr` de Python."""
    return {
        text,
        json.dumps(text, ensure_ascii=False)[1:-1],
        json.dumps(text)[1:-1],
        repr(text)[1:-1],
    }


def _needles(fragment: str) -> set[str]:
    """Formas del fragmento y de cada una de sus líneas, que también son contenido."""
    parts = {fragment, *(piece.removesuffix("\r") for piece in fragment.split("\n"))}
    return {form for part in parts for form in _forms(part) if form}


# --------------------------------------------------------------------------------------------
# Salida de las herramientas


class _LineKind(StrEnum):
    """Dónde aparece el fragmento en una línea de stderr."""

    ALONE = "solo el fragmento"
    PATTERN_FIRST = "patrón y fragmento"
    PATTERN_LAST = "fragmento y patrón"
    PATH_WITH_PATTERN = "ruta con patrón"
    INPUT_PATH = "ruta de entrada"
    METADATA = "metadato"
    TRANSCRIPT = "transcripción"
    TRACEBACK = "traza de Python"
    PATTERN_ONLY = "solo un patrón"  # sin el fragmento
    NOISE = "línea sin contenido"  # sin el fragmento ni un patrón


_WITH_PATTERN = frozenset(
    {_LineKind.PATTERN_FIRST, _LineKind.PATTERN_LAST, _LineKind.PATH_WITH_PATTERN,
     _LineKind.PATTERN_ONLY}
)  # fmt: skip
_WITH_FRAGMENT = tuple(
    kind for kind in _LineKind if kind not in {_LineKind.PATTERN_ONLY, _LineKind.NOISE}
)

# Plantillas con la forma de la salida de FFmpeg, Pillow, whisper-cli y las trazas de Python
_TEMPLATES: Mapping[_LineKind, str] = {
    _LineKind.ALONE: "{fragment}",
    _LineKind.PATTERN_FIRST: "{pattern}: {fragment}",
    _LineKind.PATTERN_LAST: "{fragment}: {pattern}",
    _LineKind.PATH_WITH_PATTERN: "{pattern} '{work}/{fragment}'",
    _LineKind.INPUT_PATH: "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from '{work}/{fragment}':",
    _LineKind.METADATA: "    title           : {fragment}",
    _LineKind.TRANSCRIPT: "[00:00:00.000 --> 00:00:02.480]  {fragment}",
    _LineKind.TRACEBACK: "KeyError: {quoted}",
    _LineKind.PATTERN_ONLY: "{pattern}",
    _LineKind.NOISE: "  Duration: 00:00:02.00, start: 0.000000, bitrate: 128 kb/s",
}


@dataclass(frozen=True)
class _Line:
    kind: _LineKind
    pattern: ToolPattern | None  # el patrón de las líneas de `_WITH_PATTERN`

    def render(self, fragment: str, work_dir: str) -> str:
        return _TEMPLATES[self.kind].format(
            fragment=fragment,
            quoted=repr(fragment),
            pattern=self.pattern.text if self.pattern is not None else "",
            work=work_dir,
        )


@st.composite
def _lines(draw: st.DrawFn, tool: Tool) -> tuple[_Line, ...]:
    """De 1 a 6 líneas de stderr de `tool`; al menos una tiene el fragmento."""
    patterns = st.sampled_from(PATTERNS[tool])

    def line(kinds: Sequence[_LineKind]) -> _Line:
        kind = draw(st.sampled_from(kinds))
        return _Line(kind, draw(patterns) if kind in _WITH_PATTERN else None)

    lines = [line(tuple(_LineKind)) for _ in range(draw(st.integers(0, 5)))]
    lines.insert(draw(st.integers(0, len(lines))), line(_WITH_FRAGMENT))
    return tuple(lines)


# Códigos de salida, señales que terminan una herramienta (-N) o código desconocido
_RETURNCODES = st.one_of(st.none(), st.integers(0, 255), st.integers(-64, -1))

# --------------------------------------------------------------------------------------------
# Excepciones


class _Source(StrEnum):
    """Excepción que lleva el fragmento en su mensaje, sus argumentos o sus atributos."""

    PATH = "OSError con la ruta"  # llamada real a os.stat
    TWO_PATHS = "OSError con dos rutas"
    DECODE = "UnicodeDecodeError"
    ENCODE = "UnicodeEncodeError"
    KEY = "KeyError"
    VALUE = "ValueError"


class _Chain(StrEnum):
    NONE = "sin encadenar"
    CAUSE = "con causa"  # raise ... from
    CONTEXT = "con contexto"  # raise dentro de except


# errno de una E/S que falla con dos rutas, como `os.replace` o una limpieza
_ERRNOS = (
    errno.EACCES, errno.EPERM, errno.ENOTEMPTY, errno.EBUSY, errno.EROFS, errno.EIO, errno.ENOSPC,
)  # fmt: skip


@dataclass(frozen=True)
class _Failure:
    source: _Source
    chain: _Chain
    note: bool  # agrega el fragmento como nota (PEP 678)
    code: int  # errno de TWO_PATHS
    codec: str  # códec de DECODE


def _raise_source(failure: _Failure, fragment: str, work_dir: str) -> NoReturn:
    # Concatenación: con `/` de pathlib, un fragmento absoluto reemplazaría la carpeta
    path = f"{work_dir}/{fragment}"
    match failure.source:
        case _Source.PATH:
            # FileNotFoundError, OSError ENAMETOOLONG o ValueError si el fragmento tiene un NUL
            os.stat(path)
        case _Source.TWO_PATHS:
            raise OSError(failure.code, os.strerror(failure.code), path, None, f"{path}.tmp")
        case _Source.DECODE:
            # b"\xff" no es válido en UTF-8 ni en ASCII; `object` guarda los bytes del contenido
            (fragment.encode() + b"\xff").decode(failure.codec)
        case _Source.ENCODE:
            (fragment + "\u4e00").encode("latin-1")
        case _Source.KEY:
            fonts: dict[str, str] = {}
            _ = fonts[fragment]
        case _Source.VALUE:
            raise ValueError(f"texto no válido: {fragment}")
    # `pytest.fail` no es una Exception: `_capture` no la atrapa
    pytest.fail(f"{failure.source} no lanzó la excepción; ¿existe {_STORAGE}?")


def _capture(failure: _Failure, fragment: str, work_dir: str) -> Exception:
    """La excepción de `failure` con su traceback, como la atrapa quien registra el fallo."""
    try:
        if failure.chain is _Chain.NONE:
            _raise_source(failure, fragment, work_dir)
        try:
            _raise_source(failure, fragment, work_dir)
        except Exception as inner:
            if failure.chain is _Chain.CAUSE:
                raise RuntimeError(f"no se pudo convertir: {fragment}") from inner
            raise LookupError(fragment)  # noqa: B904  # la anterior queda en __context__
    except Exception as exc:
        if failure.note:
            exc.add_note(f"contenido: {fragment}")
        return exc


def _failures(sources: Sequence[_Source], chains: Sequence[_Chain]) -> st.SearchStrategy[_Failure]:
    return st.builds(
        _Failure,
        source=st.sampled_from(sources),
        chain=st.sampled_from(chains),
        note=st.booleans(),
        code=st.sampled_from(_ERRNOS),
        codec=st.sampled_from(("utf-8", "ascii")),
    )


@dataclass(frozen=True)
class _Cleanup:
    failure: _Failure
    attempt: int  # de 1 a 4; el 4 es el último (3 reintentos)


# --------------------------------------------------------------------------------------------
# Escenario


@dataclass(frozen=True)
class _Scenario:
    task_id: str
    workspace: str  # "ws-" y 32 dígitos hexadecimales
    stage: Stage
    reason: FailureReason
    real_type: str | None
    pair: tuple[str, str] | None
    tool: Tool
    lines: tuple[_Line, ...]
    returncode: int | None
    failures: tuple[_Failure, ...]
    registry_error: RegistryErrorCode
    cleanups: tuple[_Cleanup, ...]

    @property
    def work_dir(self) -> str:
        return f"{_STORAGE}/{self.workspace}/work"

    def expected_events(self) -> list[str]:
        return [
            *[Event.TASK_FAILED.value] * (1 + len(self.failures)),
            Event.REGISTRY_REJECTED.value,
            *[Event.CLEANUP_FAILED.value] * len(self.cleanups),
        ]


@st.composite
def _scenarios(draw: st.DrawFn) -> _Scenario:
    """Una Tarea fallida con la salida de una herramienta, excepciones y limpiezas fallidas."""
    real_type = draw(st.none() | st.sampled_from(_FORMATS))
    pair = None
    if real_type is not None and draw(st.booleans()):
        pair = (real_type, draw(st.sampled_from(_FORMATS)))
    tool = draw(st.sampled_from(Tool))
    cleanup_failures = _failures((_Source.PATH, _Source.TWO_PATHS), (_Chain.NONE,))
    return _Scenario(
        task_id=draw(st.uuids()).hex,
        workspace=f"ws-{draw(st.uuids()).hex}",
        stage=draw(st.sampled_from(Stage)),
        reason=draw(st.sampled_from(FailureReason)),
        real_type=real_type,
        pair=pair,
        tool=tool,
        lines=draw(_lines(tool)),
        returncode=draw(_RETURNCODES),
        failures=tuple(
            draw(st.lists(_failures(tuple(_Source), tuple(_Chain)), min_size=1, max_size=4))
        ),
        registry_error=draw(st.sampled_from(RegistryErrorCode)),
        cleanups=tuple(
            draw(
                st.lists(
                    st.builds(_Cleanup, cleanup_failures, st.integers(1, 4)),
                    min_size=1,
                    max_size=2,
                )
            )
        ),
    )


@dataclass(frozen=True)
class _Run:
    text: str  # el Log_de_Eventos escrito
    details: tuple[ToolDetail | ErrorDetail, ...]  # en el orden de los eventos


def _run(scenario: _Scenario, fragment: str) -> _Run:
    """Registra los fallos de `scenario` con `fragment` en el contenido del archivo."""
    stream = io.StringIO()
    log = EventLog(stream, clock=lambda: _NOW)
    work_dir = scenario.work_dir
    # Como las entrega el ejecutor: cortadas en cada "\n" y sin un "\r" final
    stderr = "\n".join(line.render(fragment, work_dir) for line in scenario.lines)
    stderr_lines = [line.removesuffix("\r") for line in stderr.split("\n")]
    tool_detail = sanitize_tool_output(scenario.tool, stderr_lines, returncode=scenario.returncode)
    details: list[ToolDetail | ErrorDetail] = [tool_detail]
    details += (describe_exception(_capture(f, fragment, work_dir)) for f in scenario.failures)
    for detail in details:
        log.task_failed(
            task_id=scenario.task_id,
            stage=scenario.stage,
            reason=scenario.reason,
            real_type=scenario.real_type,
            pair=scenario.pair,
            detail=detail,
        )
    # Un rechazo del registro cuyo detalle es el de la herramienta y el de una excepción
    log.registry_rejected(
        entry_kind=RegistryEntryKind.PAIR if scenario.pair else RegistryEntryKind.FORMAT,
        entry=pair_label(*scenario.pair) if scenario.pair else scenario.real_type,
        error=scenario.registry_error,
        details={"tool": tool_detail.as_json(), "exception": details[1].as_json()},
    )
    for cleanup in scenario.cleanups:
        error = _capture(cleanup.failure, fragment, work_dir)
        details.append(describe_exception(error))
        log.cleanup_failed(
            task_id=scenario.task_id,
            workspace=scenario.workspace,
            error=error,
            attempt=cleanup.attempt,
            will_retry=cleanup.attempt < 4,
        )
    return _Run(stream.getvalue(), tuple(details))


def _reject_constant(name: str) -> NoReturn:
    raise AssertionError(f"el Log_de_Eventos tiene {name}, que no es JSON válido")


def _check_lines(text: str, expected_events: Sequence[str]) -> None:
    """Cada evento es una línea con un único objeto JSON que empieza con ts, level y event."""
    assert text.endswith("\n"), text
    events: list[str] = []
    for line in text.split("\n")[:-1]:
        # `json.loads` rechaza una línea con dos objetos ("Extra data") o con controles crudos
        record = json.loads(line, parse_constant=_reject_constant)
        assert isinstance(record, dict), line
        assert list(record)[:3] == ["ts", "level", "event"], line
        assert record["ts"] == _TS, line
        assert record["level"] in _LEVELS, line
        events.append(record["event"])
    assert events == list(expected_events), events


def _record_case(fragment: str, scenario: _Scenario, run: _Run) -> None:
    event(f"herramienta: {scenario.tool}")
    for kind in sorted({line.kind for line in scenario.lines}):
        event(f"línea: {kind}")
    for failure in scenario.failures:
        event(f"excepción: {failure.source}, {failure.chain}")
    if "\n" in fragment:
        event("fragmento con saltos de línea")
    if any(quote in fragment for quote in "\"'\\"):
        event("fragmento con comillas o barras invertidas")
    if not fragment.isascii():
        event("fragmento no ASCII")
    if any(text in fragment for text in _PATTERN_TEXTS):
        event("fragmento con un texto de patrón")
    tool_detail = run.details[0]
    assert isinstance(tool_detail, ToolDetail)
    event("patrón reconocido" if tool_detail.pattern is not None else "sin patrón reconocido")


# Feature: file-converter, Property 65: El Log_de_Eventos no contiene contenido de los archivos
@pytest.mark.req("16.8")
@given(fragment=_fragments(), scenario=_scenarios())
def test_event_log_never_contains_file_content(fragment: str, scenario: _Scenario) -> None:
    run = _run(scenario, fragment)
    _check_lines(run.text, scenario.expected_events())

    # Solo se buscan las formas que no son texto legítimo del log
    control = _run(scenario, _placeholder(fragment))
    needles = sorted(
        needle
        for needle in _needles(fragment)
        if needle not in control.text and not any(needle in text for text in _FIXED_TEXTS)
    )
    assume(needles)

    for needle in needles:
        assert needle not in run.text, (needle, run.text)
    _record_case(fragment, scenario, run)
