"""Inspector `media`: MP3, WAV, OGG, MP4 y WEBM con ffprobe (Req 9.6, 10.3, 10.4, 10.8, 13.7, 16.2).

Ejecuta `ffprobe` sobre el Archivo_de_Origen con el demuxer forzado según el Tipo_Real y
devuelve un `MediaInfo` con lo que necesitan los convertidores de audio, video y transcripción,
o un Rechazo_de_Origen. Corre en el subproceso `converter.workers.inspect`, con los límites de
FFmpeg del Ejecutor aislado, y ffprobe hereda esos límites y el entorno mínimo:

    ffprobe -hide_banner -loglevel error -print_format json -show_format -show_streams
            -protocol_whitelist file -f <demuxer> -i <ruta>

El demuxer sale de `converter.converters.ffmpeg_tools.DEMUXERS` (`-f mp4`, `-f matroska`, `-f mp3`,
`-f wav`, `-f ogg`), así que un archivo no puede interpretarse como una lista HLS o `concat` que
apunta a otros archivos (Req 13.7). Sin Tipo_Real, o con uno que no tiene demuxer, el inspector
lanza `ValueError`: nunca deja que ffprobe adivine el formato.

Datos de `MediaInfo` y de cada `MediaStream`:

- `MediaInfo.duration_ms` es la duración que declara el contenedor, en milisegundos. Si el
  contenedor no la declara (por ejemplo, un WEBM grabado por un navegador), es la mayor de las
  duraciones de sus flujos y, si ninguno la declara, 0: la duración se desconoce y el avance de
  una conversión no se puede estimar. `MediaStream.duration_ms` es la del flujo, o None.
- `index` es el índice del flujo en el archivo, el que FFmpeg usa en `-map 0:<índice>`.
- `kind` es "audio", "video" u "other". Un flujo de video con la marca `attached_pic` es la
  carátula de un MP3 o un MP4, no una Pista_de_Video (glosario): es "other", igual que los
  subtítulos y los flujos de datos.
- `codec` es el nombre que declara el archivo (glosario, Códec), como ffprobe lo escribe:
  "h264", "aac", "pcm_s16le". Si ffprobe no identifica el Códec es "unknown" y el flujo no es
  decodificable.
- `decodable` es verdadero si ffprobe identificó el Códec y, en audio, la frecuencia de muestreo
  y los canales, o, en video, el ancho y el alto. No consulta los decodificadores de la build,
  que conoce el proceso principal (`ffmpeg_tools.require_decoder`, Req 9.6, 10.8).
- `is_default` es la marca `disposition.default` de ffprobe, de la que
  `media_rules.default_audio_track` elige la Pista_de_Audio_Predeterminada (Req 10.4).
- `channels`, `channel_layout` y `sample_rate` son de los flujos de audio (la distribución como
  la escribe ffprobe, que lee `media_rules.layout_channels`), y `width` y `height`, de los de
  video: el tamaño almacenado, sin aplicar la rotación (Req 10.5). Los demás campos son None.

Rechazos, en el orden de `INSPECTION_REJECTIONS`:

- `archivo_danado` (Req 16.2): ffprobe termina con un código distinto de 0 y la clasificación de
  su salida de error (`classify_failure` con `no_streams`) lo da por dañado, por ejemplo con
  `moov atom not found`, `EBML header parsing failed` o `Invalid data found when processing
  input`; o ffprobe termina bien, pero el contenedor no tiene ningún flujo de audio o de video
  decodificable. Un archivo con la cabecera íntegra y los datos truncados pasa: la conversión lo
  detecta (Req 16.2, "desde en_proceso").

Cualquier otro fallo de ffprobe (una señal, la falta de memoria, una salida que no es JSON) lanza
`FfprobeError`: el worker termina con el traceback en stderr y el proceso principal lo informa
como `error_interno`. El error nunca cita la salida de ffprobe, que contiene datos del archivo.

Este módulo no importa nada pesado: ffprobe es un programa externo.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from converter.converters.errors import classify_failure
from converter.converters.ffmpeg_tools import FFPROBE, source_args
from converter.detection.inspectors.base import InspectorInput, register_inspector
from converter.detection.source_info import SourceInfo, SourceRejection, register_source_info
from converter.eventlog import Tool, sanitize_tool_output
from converter.messages_es import FailureReason
from converter.sandbox.limits import SandboxTool

type StreamKind = Literal["audio", "video", "other"]

UNKNOWN_CODEC = "unknown"  # `MediaStream.codec` de un Códec que ffprobe no identifica
_KINDS = ("audio", "video", "other")

# Un nombre de Códec de ffprobe: letras, dígitos, ".", "_", "+" y "-"; como el de `messages_es`
_CODEC_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")  # se usa con `fullmatch`
# ffprobe escribe "none" para un flujo sin Códec (por ejemplo, un temporizador de MP4)
_UNIDENTIFIED_CODECS = frozenset({"unknown", "none"})
_MAX_LAYOUT_CHARS = 512

# Los campos enteros de libavformat caben en 32 bits; ninguna duración razonable pasa de 2**53 ms
_MAX_INTEGER = 2**31 - 1
_MAX_DIGITS = 10
_MAX_SECONDS_DIGITS = 20
_MAX_DURATION_MS = 2**53
_MILLISECONDS_PER_SECOND = 1000
_SECONDS = re.compile(r"[0-9]+(?:\.[0-9]+)?")  # se usa con `fullmatch`


def _check_int(name: str, value: object, minimum: int, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} debe ser un entero")
    if value < minimum:
        raise ValueError(f"{name} debe ser {minimum} o mayor")


@dataclass(frozen=True, slots=True)
class MediaStream:
    """Un flujo del contenedor, con lo que informa ffprobe."""

    index: int  # índice del flujo en el archivo (`-map 0:<índice>`)
    kind: StreamKind
    codec: str  # nombre que declara el archivo; "unknown" si ffprobe no lo identifica
    decodable: bool  # ffprobe identificó su Códec y sus parámetros
    is_default: bool  # `disposition.default` = 1
    channels: int | None
    channel_layout: str | None
    sample_rate: int | None
    width: int | None
    height: int | None
    duration_ms: int | None

    def __post_init__(self) -> None:
        _check_int("index", self.index, 0)
        if self.kind not in _KINDS:
            raise ValueError("kind debe ser audio, video u other")
        if not isinstance(self.codec, str) or not self.codec:
            raise ValueError("el Códec del flujo no puede estar vacío")
        for name in ("decodable", "is_default"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} debe ser un booleano")
        for name in ("channels", "sample_rate", "width", "height"):
            _check_int(name, getattr(self, name), 1, optional=True)
        _check_int("duration_ms", self.duration_ms, 0, optional=True)
        if self.channel_layout is not None and (
            not isinstance(self.channel_layout, str) or not self.channel_layout
        ):
            raise ValueError("la distribución de canales, si está, no puede estar vacía")


@register_source_info("media")
@dataclass(frozen=True, slots=True)
class MediaInfo(SourceInfo):
    """Información de un Archivo_de_Origen MP3, WAV, OGG, MP4 o WEBM, que produce el inspector
    `media`."""

    duration_ms: int  # duración del contenedor; 0 si no la declara
    streams: tuple[MediaStream, ...]  # en el orden del archivo

    def __post_init__(self) -> None:
        _check_int("duration_ms", self.duration_ms, 0)
        if not isinstance(self.streams, tuple) or not all(
            isinstance(stream, MediaStream) for stream in self.streams
        ):
            raise TypeError("streams debe ser una tupla de MediaStream")


class FfprobeError(RuntimeError):
    """ffprobe no produjo un resultado utilizable. El mensaje no cita su salida."""


# --------------------------------------------------------------------------------------------
# Salida JSON de ffprobe


def _integer(value: object, minimum: int = 1) -> int | None:
    """Entero de ffprobe: un número JSON o un texto de dígitos; None si falta o no es válido."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        # Solo dígitos ASCII: `int()` aceptaría además signos, "_" y dígitos no ASCII
        if not (value.isascii() and value.isdigit()) or len(value) > _MAX_DIGITS:
            return None
        value = int(value)
    if isinstance(value, int) and minimum <= value <= _MAX_INTEGER:
        return value
    return None


def _milliseconds(value: object) -> int | None:
    """Milisegundos de una duración en segundos de ffprobe ("1.044898"); None si no se conoce.

    ffprobe escribe `N/A`, o no escribe el campo, cuando el contenedor no declara la duración.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if _SECONDS.fullmatch(value) is None or len(value) > _MAX_SECONDS_DIGITS:
            return None
        value = float(value)
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    milliseconds = round(value * _MILLISECONDS_PER_SECOND)
    return milliseconds if milliseconds <= _MAX_DURATION_MS else None


def _codec(value: object) -> str:
    if (
        not isinstance(value, str)
        or _CODEC_NAME.fullmatch(value) is None
        or value.lower() in _UNIDENTIFIED_CODECS
    ):
        return UNKNOWN_CODEC
    return value


def _layout(value: object) -> str | None:
    # La distribución puede traer nombres de canal que escribió el archivo ("FL@nombre")
    if not isinstance(value, str) or not value or len(value) > _MAX_LAYOUT_CHARS:
        return None
    return value if value.isprintable() else None


def _disposition(raw: Mapping[str, object], key: str) -> bool:
    disposition = raw.get("disposition")
    return isinstance(disposition, Mapping) and _integer(disposition.get(key), 0) == 1


def _kind(raw: Mapping[str, object]) -> StreamKind:
    codec_type = raw.get("codec_type")
    if codec_type == "audio":
        return "audio"
    # Una carátula (`attached_pic`) no es una Pista_de_Video
    if codec_type == "video" and not _disposition(raw, "attached_pic"):
        return "video"
    return "other"


def _stream(raw: object) -> MediaStream:
    if not isinstance(raw, Mapping):
        raise FfprobeError("un flujo de la salida de ffprobe no es un objeto")
    index = _integer(raw.get("index"), 0)
    if index is None:
        raise FfprobeError("un flujo de la salida de ffprobe no tiene índice")
    kind = _kind(raw)
    codec = _codec(raw.get("codec_name"))
    audio = kind == "audio"
    video = kind == "video"
    channels = _integer(raw.get("channels")) if audio else None
    sample_rate = _integer(raw.get("sample_rate")) if audio else None
    width = _integer(raw.get("width")) if video else None
    height = _integer(raw.get("height")) if video else None
    if audio:
        parameters = channels is not None and sample_rate is not None
    elif video:
        parameters = width is not None and height is not None
    else:
        parameters = True
    return MediaStream(
        index=index,
        kind=kind,
        codec=codec,
        decodable=codec != UNKNOWN_CODEC and parameters,
        is_default=_disposition(raw, "default"),
        channels=channels,
        channel_layout=_layout(raw.get("channel_layout")) if audio else None,
        sample_rate=sample_rate,
        width=width,
        height=height,
        duration_ms=_milliseconds(raw.get("duration")),
    )


def parse_ffprobe(document: object) -> MediaInfo:
    """`MediaInfo` desde el JSON de `ffprobe -show_format -show_streams`, ya decodificado.

    Lanza `FfprobeError` si el documento no tiene la forma de la salida de ffprobe. Un
    contenedor sin flujos decodificables no es un error de este análisis: lo decide el inspector.
    """
    if not isinstance(document, Mapping):
        raise FfprobeError("la salida de ffprobe no es un objeto")
    raw_streams = document.get("streams", [])
    if not isinstance(raw_streams, list):
        raise FfprobeError("los flujos de la salida de ffprobe no son una lista")
    streams = tuple(_stream(raw) for raw in raw_streams)
    container = document.get("format", {})
    if not isinstance(container, Mapping):
        raise FfprobeError("el contenedor de la salida de ffprobe no es un objeto")
    duration = _milliseconds(container.get("duration"))
    if duration is None:
        duration = max((s.duration_ms for s in streams if s.duration_ms is not None), default=0)
    return MediaInfo(duration_ms=duration, streams=streams)


def has_decodable_stream(info: MediaInfo) -> bool:
    """Si el contenedor tiene algún flujo de audio o de video decodificable."""
    return any(s.decodable and s.kind in ("audio", "video") for s in info.streams)


# --------------------------------------------------------------------------------------------
# Inspector


def ffprobe_command(source: Path, real_type: str | None, *, program: str = FFPROBE) -> list[str]:
    """Comando de ffprobe para `source`, con el demuxer forzado según `real_type`.

    Lanza `ValueError` si `real_type` es None o no tiene demuxer.
    """
    return [
        program,
        "-hide_banner",
        "-loglevel",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        *source_args(real_type, source),
    ]


def _failed(completed: subprocess.CompletedProcess[bytes]) -> SourceRejection:
    """Rechazo de un ffprobe que terminó con un código distinto de 0 o por una señal."""
    lines = completed.stderr.decode("utf-8", errors="replace").splitlines()
    detail = sanitize_tool_output(Tool.FFPROBE, lines, returncode=completed.returncode)
    # Sin flujos: ffprobe no pudo abrir el archivo como el formato del Tipo_Real
    if classify_failure(detail=detail, no_streams=True) is FailureReason.DAMAGED:
        return SourceRejection(FailureReason.DAMAGED, detail=detail)
    raise FfprobeError("ffprobe terminó con error")


@register_inspector(
    "media", info_type=MediaInfo, tool=Tool.FFPROBE, sandbox_tool=SandboxTool.FFMPEG
)
def inspect_media(source: InspectorInput) -> MediaInfo | SourceRejection:
    """Inspecciona el MP3, WAV, OGG, MP4 o WEBM de `source.path` con ffprobe."""
    command = ffprobe_command(source.path, source.real_type)
    completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, check=False)
    if completed.returncode != 0:
        return _failed(completed)
    try:
        document = json.loads(completed.stdout)
    except (ValueError, RecursionError):
        # `UnicodeDecodeError` y `JSONDecodeError` son `ValueError`
        raise FfprobeError("la salida de ffprobe no es JSON válido") from None
    info = parse_ffprobe(document)
    if not has_decodable_stream(info):
        return SourceRejection(FailureReason.DAMAGED)
    return info
