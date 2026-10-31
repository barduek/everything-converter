"""Herramientas comunes de FFmpeg para los convertidores de audio y video y para el inspector
`media` (Req 9.6, 10.8, 13.7, 14.5, 16.2).

Los convertidores `audio.ffmpeg`, `video.ffmpeg`, `video.extract_audio` y `transcription.whisper`
arman sus comandos y leen el avance y los Códecs con lo que hay aquí:

- **Demuxer forzado** (Req 13.7). `DEMUXERS` da el demuxer de cada Tipo_Real (`-f mp4`,
  `-f matroska`, `-f mp3`, `-f wav`, `-f ogg`). FFmpeg y ffprobe nunca adivinan el formato del
  Archivo_de_Origen, así que un archivo no puede interpretarse como una lista HLS o `concat` que
  apunta a otros archivos. `demuxer_for` lanza `ValueError` para un Tipo_Real sin demuxer.
- **Argumentos comunes** (Req 13.7, 13.4). `source_args` es la parte de entrada, que comparten
  ffprobe y FFmpeg: `-protocol_whitelist file`, el demuxer forzado y `-i`. `ffmpeg_command` arma
  el comando completo de la sección "Audio" del diseño: sin banner ni lectura de stdin, con
  `-loglevel error`, `-progress pipe:1`, los metadatos del origen descartados
  (`-map_metadata -1`) y una cantidad fija de hilos, la de `effective_cpus()`. Con esa cantidad
  fija, el resultado es reproducible en el mismo Equipo_de_Ejecución (Req 13.4). El
  convertidor agrega lo propio de su par (`-noautorotate`, `-map`, el Códec y sus opciones) en
  `input_options`, `stream_options` y `output_options`.
- **Progreso** (Req 14.5). `FfmpegProgress.feed` es el callback de líneas de stdout del Ejecutor
  aislado: lee `out_time_us` de `-progress pipe:1`, lo divide por la duración y lo informa a un
  `ProgressSink`. No lanza excepciones, porque una excepción de un callback detiene el
  subproceso.
- **Decodificadores** (Req 9.6, 10.8). Al arrancar, la función `load_decoders` (`@on_startup`)
  ejecuta `ffmpeg -decoders` una vez y guarda los Códecs que la build puede decodificar.
  `require_decoder` compara el Códec que informó ffprobe con esa lista y falla con
  `codec_no_soportado`, con el Códec y la pista. Si la consulta falló al arrancar, `decoders()`
  lanza `DecodersUnavailableError` (un `RuntimeError`) y el Gestor_de_Tareas termina la Tarea con
  `error_interno`.

**Lista de decodificadores.** `ffmpeg -decoders` imprime una línea por decodificador, con sus
marcas, su nombre y su descripción. El nombre del decodificador puede diferir del nombre del
Códec que informa ffprobe (`mp3float` decodifica `mp3`, `amrnb` decodifica `amr_nb`), y entonces
FFmpeg termina la descripción con `(codec <nombre del Códec>)`. La lista guarda los dos nombres
de cada línea, así que `codec in decoders()` responde si hay algún decodificador para ese Códec.

Importar este módulo no hace E/S ni lanza subprocesos (ver `converters/__init__.py`): la consulta
a FFmpeg corre en `load_decoders`.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from converter.converters.base import ConversionFailure, StartupContext, on_startup
from converter.converters.errors import classify_failure
from converter.eventlog import Tool, sanitize_tool_output
from converter.messages_es import CodecTrack, FailureReason
from converter.models import Stage
from converter.sandbox.cpus import effective_cpus
from converter.sandbox.limits import SandboxTool, tool_limits
from converter.sandbox.runner import CompletedRun, minimal_env

if TYPE_CHECKING:
    from converter.tasks.progress import ProgressSink

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# Demuxer de libavformat de cada Tipo_Real (design.md, "SSRF y lectura de archivos locales")
DEMUXERS: Mapping[str, str] = MappingProxyType(
    {"MP3": "mp3", "WAV": "wav", "OGG": "ogg", "MP4": "mp4", "WEBM": "matroska"}
)

# Solo el protocolo `file`: ningún flujo del origen puede abrir una conexión de red (Req 13.7)
PROTOCOL_WHITELIST = "file"

# Opciones que abren todo comando de FFmpeg: sin banner, sin leer stdin, solo errores y el
# avance en stdout
_FFMPEG_PREFIX = ("-hide_banner", "-nostdin", "-loglevel", "error", "-progress", "pipe:1")
# Descarta los metadatos del origen (etiquetas, carátulas, GPS) del Archivo_Resultado
_DROP_METADATA = ("-map_metadata", "-1")


# --------------------------------------------------------------------------------------------
# Comandos


def demuxer_for(real_type: str | None) -> str:
    """Demuxer que se fuerza para el Tipo_Real `real_type`.

    Lanza `ValueError` si `real_type` es None o no es un Formato con demuxer: nunca se deja que
    FFmpeg adivine el formato (Req 13.7).
    """
    demuxer = DEMUXERS.get(real_type) if isinstance(real_type, str) else None
    if demuxer is None:
        raise ValueError(f"no hay un demuxer para el Tipo_Real {real_type!r}")
    return demuxer


def source_args(real_type: str | None, source: Path) -> list[str]:
    """Opciones de entrada de ffprobe y FFmpeg: `-protocol_whitelist file -f <demuxer> -i <ruta>`.

    `source` se pasa como ruta absoluta, así nunca se toma por una opción ni por una URL.
    """
    return [
        "-protocol_whitelist",
        PROTOCOL_WHITELIST,
        "-f",
        demuxer_for(real_type),
        "-i",
        str(source.absolute()),
    ]


def thread_count() -> int:
    """Hilos de FFmpeg: las CPU efectivas del Contenedor, fijas durante su ciclo de vida."""
    return effective_cpus()


def ffmpeg_command(
    real_type: str | None,
    source: Path,
    output: Path,
    *,
    input_options: Sequence[str] = (),
    stream_options: Sequence[str] = (),
    output_options: Sequence[str] = (),
    threads: int | None = None,
    program: str = FFMPEG,
) -> list[str]:
    """Comando de FFmpeg con los argumentos comunes de la sección "Audio" del diseño:

        ffmpeg -hide_banner -nostdin -loglevel error -progress pipe:1 [input_options]
               -protocol_whitelist file -f <demuxer> -i <source>
               [stream_options] -map_metadata -1 -threads <n> [output_options] <output>

    - `input_options` afectan al origen y van antes de `-i`, como `-noautorotate`.
    - `stream_options` eligen los flujos: `-map 0:a:0 -vn -sn -dn` en audio.
    - `output_options` son del Archivo_Resultado: `-ar`, `-af`, `-c:a`, `-q:a`, `-movflags`.
    - `threads` son los hilos del codificador; si se omite, `thread_count()`.

    Lanza `ValueError` si `real_type` no tiene demuxer o `threads` es menor que 1.
    """
    count = thread_count() if threads is None else threads
    if isinstance(count, bool) or count < 1:
        raise ValueError(f"threads debe ser un entero positivo: {count!r}")
    return [
        program,
        *_FFMPEG_PREFIX,
        *input_options,
        *source_args(real_type, source),
        *stream_options,
        *_DROP_METADATA,
        "-threads",
        str(count),
        *output_options,
        str(output.absolute()),
    ]


# --------------------------------------------------------------------------------------------
# Progreso

_OUT_TIME_KEY = "out_time_us"
# Un entero de 64 bits con signo tiene hasta 19 dígitos: más largo no es un `out_time_us`
_MAX_DIGITS = 19
_MICROSECONDS_PER_MILLISECOND = 1000


def parse_out_time_us(line: str) -> int | None:
    """Microsegundos de una línea `out_time_us=<n>` de `-progress pipe:1`, o None.

    Devuelve None para cualquier otra línea (`frame=…`, `progress=continue`), para `N/A`, que
    FFmpeg escribe hasta que codifica el primer cuadro, y para un valor negativo o que no es un
    entero decimal.
    """
    key, separator, value = line.partition("=")
    if separator != "=" or key.strip() != _OUT_TIME_KEY:
        return None
    value = value.strip()
    # Solo dígitos ASCII: `int()` aceptaría además signos, "_" y dígitos no ASCII
    if not (value.isascii() and value.isdigit()) or len(value) > _MAX_DIGITS:
        return None
    return int(value)


class FfmpegProgress:
    """Convierte `out_time_us` de `-progress pipe:1` en una fracción de avance (Req 14.5).

    `feed` es el callback `on_stdout_line` del Ejecutor aislado. Cada `out_time_us` se divide
    por la duración en microsegundos y se informa al `sink`, que acota la fracción a [0, 1] y
    no retrocede (ver `tasks/progress.py`). Con una duración desconocida (0), no informa nada.
    """

    __slots__ = ("_sink", "_total_us")

    def __init__(self, duration_ms: int, sink: ProgressSink) -> None:
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
            raise ValueError(f"duration_ms debe ser un entero no negativo: {duration_ms!r}")
        self._total_us = duration_ms * _MICROSECONDS_PER_MILLISECOND
        self._sink = sink

    def feed(self, line: str) -> None:
        """Procesa una línea de stdout de FFmpeg. Ignora las que no son `out_time_us`."""
        if self._total_us == 0:
            return
        elapsed = parse_out_time_us(line)
        if elapsed is not None:
            self._sink.report(elapsed / self._total_us)


# --------------------------------------------------------------------------------------------
# Decodificadores

# Una línea de `ffmpeg -decoders`: seis marcas (tipo, hilos por cuadro, hilos por segmento,
# experimental, `draw_horiz_band` y renderizado directo), el nombre y la descripción. Las líneas
# de la leyenda (" V..... = Video") tienen "=" en el lugar del nombre y no coinciden.
_DECODER_LINE = re.compile(r"\s*[A-Z?.]{6}\s+([A-Za-z0-9][\w.+-]*)(?:\s+(.*?))?\s*")
# Cuando el decodificador y el Códec tienen nombres distintos, la descripción termina con esto
_CODEC_SUFFIX = re.compile(r"\(codec ([A-Za-z0-9][\w.+-]*)\)$")

DECODERS_TIMEOUT_SECONDS = 10

_decoders: frozenset[str] | None = None


class DecodersUnavailableError(RuntimeError):
    """La lista de decodificadores no se cargó al arrancar (ver `load_decoders`)."""

    def __init__(self) -> None:
        super().__init__("la lista de decodificadores de FFmpeg no está cargada")


class DecoderQueryError(RuntimeError):
    """`ffmpeg -decoders` no produjo una lista de decodificadores."""


def parse_decoders(lines: Iterable[str]) -> frozenset[str]:
    """Códecs que la build puede decodificar, según las líneas de `ffmpeg -decoders`.

    Por cada decodificador guarda su nombre y, si la descripción termina con `(codec <nombre>)`,
    el nombre de ese Códec. Ignora la leyenda y toda línea que no tiene la forma de un
    decodificador.
    """
    names: set[str] = set()
    for line in lines:
        match = _DECODER_LINE.fullmatch(line)
        if match is None:
            continue
        names.add(match.group(1))
        suffix = _CODEC_SUFFIX.search(match.group(2) or "")
        if suffix is not None:
            names.add(suffix.group(1))
    return frozenset(names)


def set_decoders(names: Iterable[str] | None) -> None:
    """Guarda la lista de decodificadores; None la borra. Es lo que hace `load_decoders`."""
    global _decoders
    _decoders = None if names is None else frozenset(names)


def decoders() -> frozenset[str]:
    """Códecs que la build puede decodificar. Lanza `DecodersUnavailableError` si no se cargaron."""
    if _decoders is None:
        raise DecodersUnavailableError
    return _decoders


@on_startup
async def load_decoders(ctx: StartupContext) -> None:
    """Función de arranque: ejecuta `ffmpeg -decoders` una vez y guarda la lista (Req 9.6, 10.8).

    Corre en un Espacio_de_Trabajo propio, con los límites de FFmpeg y el entorno mínimo, y lo
    elimina al terminar. Lanza `DecoderQueryError` si FFmpeg termina con error o no lista
    ningún decodificador, y `TimeoutError` si tarda más de `DECODERS_TIMEOUT_SECONDS`.
    `run_startup_hooks` registra el fallo y sigue: mientras la lista no esté cargada,
    `decoders()` falla y la Tarea que la necesite termina con `error_interno`.
    """
    lines: list[str] = []
    workspace = ctx.storage.create()
    try:
        async with asyncio.timeout(DECODERS_TIMEOUT_SECONDS):
            completed = await ctx.runner.run(
                [FFMPEG, "-hide_banner", "-decoders"],
                cwd=workspace.tmp_dir,
                limits=tool_limits(SandboxTool.FFMPEG, timeout_seconds=DECODERS_TIMEOUT_SECONDS),
                env=minimal_env(workspace.tmp_dir),
                on_stdout_line=lines.append,
            )
    finally:
        # Un Espacio_de_Trabajo que no se pudo borrar lo elimina la purga del próximo arranque
        with contextlib.suppress(OSError):
            ctx.storage.remove(workspace)
    if completed.returncode != 0:
        raise DecoderQueryError("ffmpeg -decoders terminó con error")
    names = parse_decoders(lines)
    if not names:
        raise DecoderQueryError("ffmpeg -decoders no listó ningún decodificador")
    set_decoders(names)


# --------------------------------------------------------------------------------------------
# Comprobación del Códec


class DecodableStream(Protocol):
    """Lo que `require_decoder` necesita de un flujo; `MediaStream` lo cumple."""

    @property
    def codec(self) -> str:  # nombre que declara el archivo, p. ej. "h264"
        ...

    @property
    def decodable(self) -> bool:  # el inspector identificó su Códec y sus parámetros
        ...


def require_decoder(
    stream: DecodableStream,
    track: CodecTrack,
    *,
    available: AbstractSet[str] | None = None,
    stage: Stage = Stage.CONVERSION,
) -> None:
    """Comprueba que la build de FFmpeg puede decodificar el flujo `stream` que usa un par.

    `track` es la pista a la que corresponde el flujo, y `available`, la lista de Códecs
    decodificables; si se omite, la de `decoders()`. `stage` es la etapa en la que corre la
    comprobación: conversión o transcripción.

    - Si el Códec no está en la lista, lanza `ConversionFailure` con `codec_no_soportado`, el
      Códec y la pista (Req 9.6, 10.8).
    - Si el inspector no pudo identificar el Códec ni los parámetros del flujo
      (`decodable` falso), lanza `ConversionFailure` con `archivo_danado`: no hay un nombre de
      Códec que informar.

    Lanza `DecodersUnavailableError` si no se pasó `available` y la lista no se cargó.
    """
    if not stream.decodable:
        raise ConversionFailure(FailureReason.DAMAGED, stage=stage)
    codecs = decoders() if available is None else available
    if stream.codec not in codecs:
        raise ConversionFailure(
            FailureReason.UNSUPPORTED_CODEC,
            stage=stage,
            params={"codec": stream.codec, "pista": CodecTrack(track).value},
        )


# Motivos con los que puede terminar FFmpeg. Un Códec no soportado se decide antes, con
# `require_decoder`, y no a partir de la salida del proceso.
_FFMPEG_REASONS = frozenset(
    {FailureReason.DAMAGED, FailureReason.NO_SPACE, FailureReason.INTERNAL_ERROR}
)


def failure_from_ffmpeg(completed: CompletedRun) -> ConversionFailure:
    """Fallo de un FFmpeg que terminó con error o sin escribir su resultado (Req 16.9, 16.10)."""
    detail = sanitize_tool_output(
        Tool.FFMPEG, completed.stderr_lines, returncode=completed.returncode
    )
    reason = classify_failure(detail=detail)
    if reason not in _FFMPEG_REASONS:
        reason = FailureReason.INTERNAL_ERROR
    return ConversionFailure(reason, detail=detail)
