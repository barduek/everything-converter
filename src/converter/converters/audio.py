"""Convertidor `audio.ffmpeg`: MP3, WAV y OGG entre sí (Req 9.1 a 9.6, 13.4, 14.5).

Los seis pares de audio del Mapa_de_Conversión_Inicial los ejecuta FFmpeg a través del Ejecutor
aislado, con el demuxer forzado del Tipo_Real y `-protocol_whitelist file` (Req 13.7). Este
módulo decide la frecuencia, los canales y el Códec; `ffmpeg_tools` arma el comando común.

- **Frecuencia** (Req 9.4, 9.5). `choose_sample_rate` deja la del origen si el destino la admite
  y, si no, elige la admitida más cercana. MP3 admite `MP3_SAMPLE_RATES`, OGG el intervalo de la
  configuración y WAV cualquier valor. Si cambia, el aviso `frecuencia_cambiada` cita los dos
  valores en Hz.
- **Canales** (Req 9.2, 9.3). Se conservan si no superan el máximo del destino. Si lo superan,
  el MVP mezcla con `-ac N` de FFmpeg en lugar del filtro `pan` y de `build_downmix_matrix`
  (tasks.md, tarea 19.6). Para MP3, N es 2. El aviso `canales_mezclados` informa las dos
  cantidades. Con distribuciones que FFmpeg no reconoce, algún canal puede no llegar a la mezcla.
- **Códec** (Req 9.6). Antes de lanzar FFmpeg, `require_decoder` compara el Códec que informó
  ffprobe con la lista cargada al arrancar. Si no está, la Tarea falla con `codec_no_soportado`.
- **Progreso** (Req 14.5). `out_time_us` de `-progress pipe:1` se divide por la duración del flujo.

MP3 usa `libmp3lame -q:a 2`, OGG `libvorbis -q:a 5` y WAV `pcm_s16le`. El mapa es el índice de la
Pista_de_Audio_Predeterminada, que en un archivo de audio es su único flujo de audio.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from converter.converters.base import (
    ConversionContext,
    ConversionFailure,
    ConversionOutcome,
    ConversionRequest,
    NoticeCollector,
    register_converter,
)
from converter.converters.ffmpeg_tools import (
    FfmpegProgress,
    failure_from_ffmpeg,
    ffmpeg_command,
    require_decoder,
)
from converter.converters.media_rules import (
    MAX_CHANNELS,
    MP3_SAMPLE_RATES,
    WAV_SAMPLE_RATES,
    SampleRateRange,
    SampleRates,
    choose_sample_rate,
    default_audio_track,
)
from converter.detection.inspectors.media import MediaInfo, MediaStream
from converter.detection.source_info import SourceInfo
from converter.messages_es import CodecTrack, FailureReason, NoticeCode
from converter.models import DestinationRejection
from converter.sandbox.limits import SandboxTool, tool_limits
from converter.sandbox.runner import minimal_env

if TYPE_CHECKING:
    from converter.config import Config

CONVERTER_ID = "audio.ffmpeg"

# Códec y opciones de cada Formato_de_Destino (design.md, "Audio")
CODEC_OPTIONS: dict[str, tuple[str, ...]] = {
    "MP3": ("-c:a", "libmp3lame", "-q:a", "2"),
    "OGG": ("-c:a", "libvorbis", "-q:a", "5"),
    "WAV": ("-c:a", "pcm_s16le"),
}
EXTENSIONS = {"MP3": ".mp3", "OGG": ".ogg", "WAV": ".wav"}


@dataclass(frozen=True, slots=True)
class AudioPlan:
    """Codificación de un flujo de audio y los avisos que le corresponden."""

    options: tuple[str, ...]
    sample_rate: int
    channels: int
    rate_changed: bool
    mixed: bool


def admitted_rates(target: str, config: Config) -> SampleRates:
    """Frecuencias_de_Muestreo_Admitidas del Formato_de_Destino `target`."""
    if target == "MP3":
        return MP3_SAMPLE_RATES
    if target == "OGG":
        return SampleRateRange(config.ogg_min_sample_rate_hz, config.ogg_max_sample_rate_hz)
    if target == "WAV":
        return WAV_SAMPLE_RATES
    raise ValueError(f"no hay frecuencias admitidas para {target}")


def plan_audio(stream: MediaStream, target: str, config: Config) -> AudioPlan:
    """Opciones de FFmpeg para pasar `stream` al Formato `target` (Req 9.2 a 9.5).

    Lanza `ConversionFailure` con `archivo_danado` si al flujo le faltan la frecuencia o los
    canales: el inspector lo marca como no decodificable, y aquí es la misma evidencia.
    """
    if stream.sample_rate is None or stream.channels is None:
        raise ConversionFailure(FailureReason.DAMAGED)
    maximum = MAX_CHANNELS[target]
    rate = choose_sample_rate(stream.sample_rate, admitted_rates(target, config))
    mixed = stream.channels > maximum
    channels = maximum if mixed else stream.channels
    options: list[str] = []
    if rate != stream.sample_rate:
        options.extend(("-ar", str(rate)))
    if mixed:
        # MVP: `-ac N` en lugar del filtro `pan` (tarea 19.6). En MP3, N es 2.
        options.extend(("-ac", str(maximum)))
    options.extend(CODEC_OPTIONS[target])
    return AudioPlan(
        options=tuple(options),
        sample_rate=rate,
        channels=channels,
        rate_changed=rate != stream.sample_rate,
        mixed=mixed,
    )


def add_audio_notices(
    notices: NoticeCollector, plan: AudioPlan, source_rate: int, source_channels: int
) -> None:
    """Avisos de frecuencia y de mezcla, en ese orden (Req 9.3, 9.4)."""
    if plan.rate_changed:
        notices.add(
            NoticeCode.SAMPLE_RATE_CHANGED,
            {"origen_hz": source_rate, "resultado_hz": plan.sample_rate},
        )
    if plan.mixed:
        notices.add(
            NoticeCode.CHANNELS_MIXED,
            {"canales_origen": source_channels, "canales_resultado": plan.channels},
        )


def media_of(source: SourceInfo) -> MediaInfo:
    """`source` como información de un medio. Lanza `TypeError` si es de otro Formato."""
    if not isinstance(source, MediaInfo):
        raise TypeError(f"el convertidor de audio o video necesita MediaInfo: {type(source)}")
    return source


def audio_track(info: MediaInfo) -> MediaStream | None:
    """Pista_de_Audio_Predeterminada de `info`, o None si el contenedor no tiene audio."""
    return default_audio_track(info.streams)


def output_file(work_dir: Path, target: str) -> Path:
    """Ruta del Archivo_Resultado dentro de `work/`."""
    return (work_dir / f"output{EXTENSIONS[target]}").absolute()


async def run_ffmpeg(
    ctx: ConversionContext,
    *,
    input_options: Sequence[str] = (),
    stream_options: Sequence[str] = (),
    output_options: Sequence[str] = (),
    output: Path,
    duration_ms: int,
    timeout_seconds: int,
) -> None:
    """Ejecuta FFmpeg y exige que `output` exista. Informa el avance y al final el 100 %.

    Lanza `ConversionFailure` si FFmpeg termina con error o no escribe el archivo.
    """
    progress = FfmpegProgress(duration_ms, ctx.progress)
    completed = await ctx.runner.run(
        ffmpeg_command(
            ctx.pair.source,
            ctx.workspace.source_file,
            output,
            input_options=input_options,
            stream_options=stream_options,
            output_options=output_options,
        ),
        cwd=ctx.workspace.tmp_dir,
        limits=tool_limits(SandboxTool.FFMPEG, timeout_seconds=timeout_seconds),
        env=minimal_env(ctx.workspace.tmp_dir),
        on_stdout_line=progress.feed,
    )
    if completed.returncode != 0 or not output.is_file():
        raise failure_from_ffmpeg(completed)
    ctx.progress.report(1.0)


def prepare_audio(info: MediaInfo, target: str, config: Config) -> tuple[MediaStream, AudioPlan]:
    """Flujo de audio que usa el par y su plan de codificación.

    Lanza `ConversionFailure` con `archivo_danado` si no hay audio y con `codec_no_soportado`
    si la build no decodifica su Códec (Req 9.6).
    """
    stream = audio_track(info)
    if stream is None:
        raise ConversionFailure(FailureReason.DAMAGED)
    require_decoder(stream, CodecTrack.AUDIO)
    return stream, plan_audio(stream, target, config)


@register_converter(CONVERTER_ID)
class AudioFfmpegConverter:
    """Convierte MP3, WAV y OGG entre sí con FFmpeg."""

    def preflight(
        self, source: SourceInfo, request: ConversionRequest, config: Config
    ) -> DestinationRejection | None:
        """Los pares de audio no tienen Rechazos_de_Destino propios (Req 16.12).

        Lanza `TypeError` si `source` no es la información de un medio.
        """
        media_of(source)
        return None

    async def run(self, ctx: ConversionContext) -> ConversionOutcome:
        """Codifica el audio y deja el Archivo_Resultado en `work/`."""
        info = media_of(ctx.source)
        stream, plan = prepare_audio(info, ctx.pair.target, ctx.config)
        output = output_file(ctx.workspace.work_dir, ctx.pair.target)
        await run_ffmpeg(
            ctx,
            stream_options=("-map", f"0:{stream.index}", "-vn", "-sn", "-dn"),
            output_options=plan.options,
            output=output,
            duration_ms=stream.duration_ms if stream.duration_ms is not None else info.duration_ms,
            timeout_seconds=ctx.config.timeout_audio_seconds,
        )
        if stream.sample_rate is not None and stream.channels is not None:
            add_audio_notices(ctx.notices, plan, stream.sample_rate, stream.channels)
        return ConversionOutcome(result_path=output, result_kind="single")
