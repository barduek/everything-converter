"""Log_de_Eventos y saneamiento del detalle técnico (Req 16.7, 16.8, 4.5–4.7, 4.12, 4.13, 17.9).

El Log_de_Eventos es una secuencia de objetos JSON, uno por línea, escrita en la salida estándar
del proceso; es lo que muestra `docker logs`. Cada objeto empieza con tres campos:

- `ts`: fecha y hora en UTC con milisegundos, por ejemplo "2025-01-15T10:32:07.412Z";
- `level`: "info", "warning" o "error";
- `event`: el nombre del evento, por ejemplo "task_failed".

`EventLog` escribe los eventos. La salida y el reloj se inyectan para las pruebas; sin salida
inyectada usa `sys.stdout` en el momento de cada escritura. Cada línea se escribe y se vacía
bajo un lock compartido, así dos hilos nunca mezclan líneas. Si la escritura falla (salida
cerrada o tubería rota), el evento se descarta sin propagar el error, como hace `logging`: un
fallo del log no debe hacer fallar una Tarea.

Qué no entra nunca al Log_de_Eventos (Req 16.8):

- Nombres_Originales: ningún evento tiene un campo para ellos.
- Contenido de los archivos: la salida de las herramientas pasa por `sanitize_tool_output`, que
  la compara con un conjunto cerrado de patrones (`PATTERNS`) y devuelve solo el identificador
  y el texto fijo del patrón reconocido, el código de salida, la señal y la cantidad de líneas
  no reconocidas. Las líneas se cuentan pero no se copian, así no llegan al log los metadatos,
  el texto extraído ni el texto que imprime `whisper-cli`.
- Mensajes de excepción: `describe_exception` guarda la clase, el errno y la ubicación, nunca
  el mensaje, porque un `UnicodeDecodeError` cita bytes del archivo y un `KeyError` puede citar
  texto extraído.

`emit` acepta eventos que no son de este módulo (por ejemplo, los de arranque). Quien lo llama
no debe pasarle Nombres_Originales ni contenido de los archivos.
"""

from __future__ import annotations

import contextlib
import errno as errno_codes
import json
import os
import signal as signals
import sys
import threading
import traceback
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TextIO

from converter.models import Stage
from converter.sandbox import launch

type JsonValue = str | int | float | bool | None | Sequence[JsonValue] | Mapping[str, JsonValue]

# Campos que `emit` agrega a todo evento; un evento no puede redefinirlos
_RESERVED_FIELDS = frozenset({"ts", "level", "event"})

# Un solo lock para todas las instancias: comparten la salida estándar del proceso
_WRITE_LOCK = threading.Lock()


class Level(StrEnum):
    """Nivel de un evento del Log_de_Eventos."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Event(StrEnum):
    """Eventos que define este módulo."""

    TASK_FAILED = "task_failed"  # Req 16.7
    REGISTRY_REJECTED = "registry_rejected"  # Req 4.5, 4.6, 4.7, 4.12, 4.13
    CLEANUP_FAILED = "cleanup_failed"  # Req 17.9


class RegistryEntryKind(StrEnum):
    """Tipo de entrada del catálogo que el Registro_de_Conversiones rechazó."""

    FORMAT = "formato"
    PAIR = "par"


class Tool(StrEnum):
    """Herramienta cuya salida se sanea; elige el conjunto de patrones de `PATTERNS`."""

    FFMPEG = "ffmpeg"
    FFPROBE = "ffprobe"
    PILLOW = "pillow"  # subproceso Python que usa Pillow
    PYPDFIUM2 = "pypdfium2"  # subproceso Python que usa pypdfium2
    LIBREOFFICE = "libreoffice"
    WHISPER_CPP = "whisper.cpp"  # whisper-cli


# --------------------------------------------------------------------------------------------
# Patrones de la salida de las herramientas


@dataclass(frozen=True, slots=True)
class ToolPattern:
    """Patrón conocido de la salida de una herramienta.

    Una línea coincide si contiene `text` tal cual. Al log va `text`, nunca la línea.
    """

    id: str  # identificador estable, por ejemplo "invalid_data"
    text: str  # texto fijo que se busca y que se registra


def _patterns(*pairs: tuple[str, str]) -> tuple[ToolPattern, ...]:
    return tuple(ToolPattern(pattern_id, text) for pattern_id, text in pairs)


# Falta de espacio (`sin_espacio_proceso`, en cualquier herramienta). Van primero: si una
# escritura falla por ENOSPC, los errores que la herramienta informa después son consecuencia
_NO_SPACE = _patterns(
    ("no_space", "No space left on device"),  # strerror(ENOSPC)
    ("enospc", "ENOSPC"),
)

# Mensajes de `converter.sandbox.launch`, que inicia todos los subprocesos
_LAUNCHER = _patterns(
    ("launch_invalid_args", f"{launch.MODULE}: argumentos inválidos"),
    ("launch_limits_failed", f"{launch.MODULE}: no se pudieron fijar los límites"),
    ("launch_not_found", f"{launch.MODULE}: no se encontró el programa"),
    ("launch_cannot_execute", f"{launch.MODULE}: no se pudo ejecutar"),
)

# Límites de recursos alcanzados (RLIMIT_AS, RLIMIT_FSIZE). Van al final: suelen acompañar a
# otro error más específico
_RESOURCES = _patterns(
    ("out_of_memory", "Cannot allocate memory"),  # strerror(ENOMEM)
    ("file_too_large", "File too large"),  # strerror(EFBIG)
)

# Demuxers de libavformat: los imprimen FFmpeg y ffprobe con `-loglevel error`
_LIBAV_INPUT = _patterns(
    ("invalid_data", "Invalid data found when processing input"),  # AVERROR_INVALIDDATA
    ("moov_atom_not_found", "moov atom not found"),  # MP4 truncado o sin índice
    ("ebml_header_failed", "EBML header parsing failed"),  # WEBM dañado
    ("partial_file", "partial file"),  # MP4 truncado
    ("codec_parameters_not_found", "Could not find codec parameters"),
    ("header_missing", "Header missing"),  # cuadro MP3 dañado
)

_FFMPEG_ONLY = _patterns(
    ("no_decoder", "no decoder found for"),  # FFmpeg 7
    ("decoder_not_found", "not found for input stream"),  # FFmpeg 5 y 6: "Decoder (codec x) …"
    ("unknown_encoder", "Unknown encoder"),
    ("error_while_decoding", "Error while decoding stream"),
    ("no_output_streams", "does not contain any stream"),
    ("conversion_failed", "Conversion failed!"),  # línea final genérica
)

# Excepciones de Pillow en el subproceso Python (mensajes de PIL/Image.py e ImageFile.py)
_PILLOW = _patterns(
    ("decompression_bomb", "DecompressionBombError"),
    ("unidentified_image", "cannot identify image file"),  # UnidentifiedImageError
    ("truncated_image", "image file is truncated"),
    ("truncated_png", "truncated PNG file"),
    ("truncated_read", "Truncated File Read"),
    # Estados del decodificador (`Image.core.getcodecstatus`)
    ("decoder_error", "broken data stream"),
    ("unrecognized_data", "unrecognized data stream contents"),
    ("buffer_overrun", "buffer overrun"),
    ("codec_configuration_error", "codec configuration error"),
    ("codec_out_of_memory", "out of memory"),
    ("decoder_failed", "decoder error"),  # estado sin descripción: "decoder error -N"
    ("encoder_failed", "encoder error"),
    ("memory_error", "MemoryError"),
)

# `PdfiumError` de pypdfium2 en el subproceso Python. Los códigos FPDF_ERR_* aparecen como
# "Failed to load document (PDFium: <texto>)."
_PYPDFIUM2 = _patterns(
    ("pdf_password_error", "PDFium: Incorrect password error"),  # FPDF_ERR_PASSWORD
    ("pdf_security_error", "PDFium: Unsupported security scheme error"),  # FPDF_ERR_SECURITY
    ("pdf_format_error", "PDFium: Data format error"),  # FPDF_ERR_FORMAT
    ("pdf_file_error", "PDFium: File access error"),  # FPDF_ERR_FILE
    ("pdf_page_error", "PDFium: Page not found or content error"),  # FPDF_ERR_PAGE
    ("pdf_unknown_error", "PDFium: Unknown error"),  # FPDF_ERR_UNKNOWN
    ("pdf_load_failed", "Failed to load document"),
    ("pdf_page_load_failed", "Failed to load page"),
    ("pdf_text_page_load_failed", "Failed to load text page"),
    ("memory_error", "MemoryError"),
)

# `soffice --headless --convert-to pdf`
_LIBREOFFICE = _patterns(
    ("source_not_loaded", "Error: source file could not be loaded"),
    ("no_export_filter", "Error: no export filter"),
    ("user_installation_failed", "User installation could not be completed"),
    ("fatal_exception", "Fatal exception"),  # "Fatal exception: Signal 11"
    ("bad_alloc", "std::bad_alloc"),
)

# `whisper-cli`: los errores van a stderr; el texto transcripto, a stdout y a archivos
_WHISPER_CPP = _patterns(
    ("model_load_failed", "failed to load model"),
    ("context_init_failed", "failed to initialize whisper context"),
    ("invalid_model_data", "invalid model data"),  # "invalid model data (bad magic)"
    ("input_not_found", "input file not found"),
    ("audio_read_failed", "failed to read audio"),
    ("audio_process_failed", "failed to process audio"),
    ("unknown_language", "unknown language"),
    ("allocation_failed", "failed to allocate"),
    ("bad_alloc", "std::bad_alloc"),
)


def _tool_patterns(*specific: tuple[ToolPattern, ...]) -> tuple[ToolPattern, ...]:
    combined = (*_NO_SPACE, *_LAUNCHER, *(p for group in specific for p in group), *_RESOURCES)
    ids = [pattern.id for pattern in combined]
    if len(ids) != len(set(ids)):
        raise ValueError(f"identificadores de patrón repetidos: {ids}")
    return combined


# Conjunto cerrado de patrones por herramienta, en orden de prioridad: si la salida contiene
# varios, `sanitize_tool_output` informa el primero de esta lista
PATTERNS: Mapping[Tool, tuple[ToolPattern, ...]] = MappingProxyType(
    {
        Tool.FFMPEG: _tool_patterns(_LIBAV_INPUT, _FFMPEG_ONLY),
        Tool.FFPROBE: _tool_patterns(_LIBAV_INPUT),
        Tool.PILLOW: _tool_patterns(_PILLOW),
        Tool.PYPDFIUM2: _tool_patterns(_PYPDFIUM2),
        Tool.LIBREOFFICE: _tool_patterns(_LIBREOFFICE),
        Tool.WHISPER_CPP: _tool_patterns(_WHISPER_CPP),
    }
)


def _signal_name(signum: int | None) -> str | None:
    if signum is None:
        return None
    try:
        return signals.Signals(signum).name
    except ValueError:
        return str(signum)


@dataclass(frozen=True, slots=True)
class ToolDetail:
    """Detalle técnico saneado de la ejecución de una herramienta."""

    tool: Tool
    exit_code: int | None  # None si terminó por una señal o si no se conoce
    signal: int | None  # número de la señal que la terminó, o None
    pattern: ToolPattern | None  # patrón reconocido de mayor prioridad, o None
    unrecognized_lines: int  # líneas que no coinciden con ningún patrón; no se copian

    def as_json(self) -> dict[str, JsonValue]:
        """Campos del detalle para el Log_de_Eventos; la señal va por nombre ("SIGKILL")."""
        return {
            "tool": self.tool.value,
            "exit_code": self.exit_code,
            "signal": _signal_name(self.signal),
            "classification": self.pattern.id if self.pattern is not None else None,
            "matched": self.pattern.text if self.pattern is not None else None,
            "unrecognized_lines": self.unrecognized_lines,
        }


def sanitize_tool_output(
    tool: Tool, stderr_lines: Iterable[str], *, returncode: int | None = None
) -> ToolDetail:
    """Reduce la salida de `tool` a datos fijos, sin copiar ninguna línea (Req 16.8).

    Cada línea se compara con los patrones de `PATTERNS[tool]`: coincide con un patrón si
    contiene su texto. El resultado guarda el patrón reconocido de mayor prioridad (el primero
    en la lista, con independencia del orden de las líneas) y cuenta las líneas que no coinciden
    con ninguno. `returncode` sigue la convención de `subprocess` y `asyncio`: un valor negativo
    `-N` indica que la herramienta terminó por la señal N.
    """
    tool = Tool(tool)  # admite también el valor como texto ("ffmpeg"); otro valor da ValueError
    patterns = PATTERNS[tool]
    best: int | None = None  # posición en `patterns` del patrón de mayor prioridad visto
    unrecognized = 0
    for line in stderr_lines:
        for index, pattern in enumerate(patterns):
            if pattern.text in line:
                if best is None or index < best:
                    best = index
                break
        else:
            unrecognized += 1
    exit_code = signum = None
    if returncode is not None:
        if returncode < 0:
            signum = -returncode
        else:
            exit_code = returncode
    return ToolDetail(
        tool=tool,
        exit_code=exit_code,
        signal=signum,
        pattern=patterns[best] if best is not None else None,
        unrecognized_lines=unrecognized,
    )


# --------------------------------------------------------------------------------------------
# Excepciones


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    """Detalle técnico de una excepción, sin su mensaje."""

    exception: str  # clase, con su módulo salvo las predefinidas: "PermissionError"
    errno: str | None  # nombre del errno de un OSError: "EACCES"
    where: str | None  # archivo y línea del cuadro más interno del traceback: "shutil.py:12"

    def as_json(self) -> dict[str, JsonValue]:
        """Campos del detalle para el Log_de_Eventos."""
        return {"exception": self.exception, "errno": self.errno, "where": self.where}


def describe_exception(exc: BaseException) -> ErrorDetail:
    """Clase, errno y ubicación de `exc`; nunca su mensaje ni sus argumentos (Req 16.8)."""
    cls = type(exc)
    name = cls.__qualname__
    if cls.__module__ != "builtins":
        name = f"{cls.__module__}.{name}"
    code: str | None = None
    if isinstance(exc, OSError) and isinstance(exc.errno, int):
        code = errno_codes.errorcode.get(exc.errno, str(exc.errno))
    where: str | None = None
    for frame, lineno in traceback.walk_tb(exc.__traceback__):
        where = f"{os.path.basename(frame.f_code.co_filename)}:{lineno}"
    return ErrorDetail(exception=name, errno=code, where=where)


# --------------------------------------------------------------------------------------------
# Escritura


def pair_label(source: str, target: str) -> str:
    """Nombre de un Par_de_Conversión en el log: "PNG→JPG"."""
    return f"{source}→{target}"


def format_ts(moment: datetime) -> str:
    """Fecha y hora en UTC con milisegundos: "2025-01-15T10:32:07.412Z".

    `moment` debe tener zona horaria; una fecha sin zona es ambigua y lanza `ValueError`.
    """
    if moment.utcoffset() is None:
        raise ValueError("la fecha y hora del evento debe tener zona horaria")
    utc = moment.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _to_builtin(value: object) -> object:
    # `json` solo serializa dict, list y tuple; los demás Mapping y Sequence se convierten
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return list(value)
    raise TypeError(f"valor no serializable en el Log_de_Eventos: {type(value).__name__}")


class EventLog:
    """Escribe eventos del Log_de_Eventos como objetos JSON, uno por línea."""

    __slots__ = ("_clock", "_stream")

    def __init__(
        self, stream: TextIO | None = None, *, clock: Callable[[], datetime] = _utc_now
    ) -> None:
        self._stream = stream  # None: `sys.stdout` en el momento de cada escritura
        self._clock = clock  # fecha y hora con zona horaria

    def emit(self, level: Level, event: str, fields: Mapping[str, JsonValue] | None = None) -> None:
        """Escribe un evento con `ts`, `level`, `event` y después `fields`, en ese orden.

        `fields` no puede redefinir `ts`, `level` ni `event` (`ValueError`), y sus valores
        deben ser serializables como JSON sin NaN ni infinitos (`TypeError`, `ValueError`).
        """
        record: dict[str, JsonValue] = {
            "ts": format_ts(self._clock()),
            "level": str(level),
            "event": str(event),
        }
        if fields:
            reserved = sorted(_RESERVED_FIELDS & fields.keys())
            if reserved:
                raise ValueError(f"campos reservados del Log_de_Eventos: {reserved}")
            record.update(fields)
        # `json` escapa los saltos de línea dentro de los valores: el evento ocupa una línea
        line = json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
            default=_to_builtin,
        )
        stream = self._stream if self._stream is not None else sys.stdout
        with _WRITE_LOCK, contextlib.suppress(OSError, ValueError):
            stream.write(line + "\n")
            stream.flush()

    def task_failed(
        self,
        *,
        task_id: str,
        stage: Stage,
        reason: str,
        real_type: str | None = None,
        pair: tuple[str, str] | None = None,
        detail: ToolDetail | ErrorDetail | None = None,
    ) -> None:
        """Registra que una Tarea pasó a fallida (Req 16.7).

        Se llama al aplicar el cambio de estado, así `ts` es la fecha y hora del cambio.
        `reason` es el código del Motivo_de_Fallo; `real_type` y `pair` (origen y destino) son
        None si el Tipo_Real no se determinó o si el usuario no eligió un Formato_de_Destino.
        """
        self.emit(
            Level.WARNING,
            Event.TASK_FAILED,
            {
                "task_id": task_id,
                "real_type": real_type,
                "pair": pair_label(*pair) if pair is not None else None,
                "stage": str(stage),
                "reason": str(reason),
                "detail": detail.as_json() if detail is not None else None,
            },
        )

    def registry_rejected(
        self,
        *,
        entry_kind: RegistryEntryKind,
        entry: str | None,
        error: str,
        details: Mapping[str, JsonValue] | None = None,
    ) -> None:
        """Registra que el Registro_de_Conversiones rechazó un Formato o un Par_de_Conversión.

        `entry` identifica la entrada (nombre del Formato o `pair_label` del par), o es None si
        no tiene. `error` es el código devuelto (por ejemplo "par_duplicado") y `details`, lo
        que nombra el error: el atributo inválido, el Formato en conflicto, el par existente o
        los Formatos faltantes (Req 4.5, 4.6, 4.7, 4.12, 4.13).
        """
        self.emit(
            Level.WARNING,
            Event.REGISTRY_REJECTED,
            {
                "entry_kind": str(entry_kind),
                "entry": entry,
                "error": str(error),
                "details": details if details is not None else {},
            },
        )

    def cleanup_failed(
        self,
        *,
        task_id: str | None,
        workspace: str,
        error: BaseException,
        attempt: int,
        will_retry: bool,
    ) -> None:
        """Registra que falló la eliminación de un Espacio_de_Trabajo o de sus archivos (Req 17.9).

        `task_id` es None si el Espacio_de_Trabajo es de una recepción que no creó Tarea.
        `attempt` cuenta desde 1. El motivo es `describe_exception(error)`. El nivel es
        "warning" si quedan reintentos y "error" en el último intento.
        """
        if attempt < 1:
            raise ValueError(f"attempt debe ser 1 o mayor: {attempt}")
        self.emit(
            Level.WARNING if will_retry else Level.ERROR,
            Event.CLEANUP_FAILED,
            {
                "task_id": task_id,
                "workspace": workspace,
                "attempt": attempt,
                "will_retry": will_retry,
                "detail": describe_exception(error).as_json(),
            },
        )
