"""Convertidor `video.extract_audio`: MP4 a MP3 y MP4 a WAV (Req 10.1 a 10.4, 10.8, 10.9).

Usa la Pista_de_Audio_Predeterminada y las mismas reglas de canales y de frecuencia que
`audio.ffmpeg`. El preflight rechaza un video sin audio con `video_sin_audio` y deja la Tarea
en recibida (Req 10.3). `reject_video_without_audio` queda para los Pares_de_Transcripción,
que todavía no están.

Si el contenedor tiene más de una pista de audio, se usa solo la predeterminada y el aviso
`pistas_no_usadas` informa cuántas quedaron afuera (Req 10.4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from converter.converters.audio import (
    add_audio_notices,
    media_of,
    output_file,
    plan_audio,
    run_ffmpeg,
)
from converter.converters.base import (
    ConversionContext,
    ConversionFailure,
    ConversionOutcome,
    ConversionRequest,
    register_converter,
)
from converter.converters.ffmpeg_tools import require_decoder
from converter.converters.media_rules import default_audio_track, unused_audio_tracks
from converter.detection.source_info import SourceInfo
from converter.messages_es import CodecTrack, FailureReason, NoticeCode, RejectionCode
from converter.models import DestinationRejection

if TYPE_CHECKING:
    from converter.config import Config
    from converter.detection.inspectors.media import MediaInfo

CONVERTER_ID = "video.extract_audio"


def reject_video_without_audio(info: MediaInfo) -> DestinationRejection | None:
    """Rechazo `video_sin_audio` si `info` no tiene Pistas_de_Audio (Req 10.3).

    Lo usan este convertidor y, cuando existan, los Pares_de_Transcripción de un video.
    """
    if default_audio_track(info.streams) is None:
        return DestinationRejection(RejectionCode.VIDEO_WITHOUT_AUDIO, {})
    return None


@register_converter(CONVERTER_ID)
class VideoExtractAudioConverter:
    """Extrae la Pista_de_Audio_Predeterminada de un MP4 a MP3 o WAV."""

    def preflight(
        self, source: SourceInfo, request: ConversionRequest, config: Config
    ) -> DestinationRejection | None:
        """Rechaza un video sin audio (Req 10.3). Lanza `TypeError` si `source` no es un medio."""
        return reject_video_without_audio(media_of(source))

    async def run(self, ctx: ConversionContext) -> ConversionOutcome:
        """Codifica la pista predeterminada y deja el Archivo_Resultado en `work/`."""
        info = media_of(ctx.source)
        stream = default_audio_track(info.streams)
        if stream is None:
            # El preflight ya rechazó este caso; si llega igual, no hay audio que extraer.
            raise ConversionFailure(FailureReason.DAMAGED)
        require_decoder(stream, CodecTrack.DEFAULT_AUDIO)
        plan = plan_audio(stream, ctx.pair.target, ctx.config)
        output = output_file(ctx.workspace.work_dir, ctx.pair.target)
        await run_ffmpeg(
            ctx,
            stream_options=("-map", f"0:{stream.index}", "-vn", "-sn", "-dn"),
            output_options=plan.options,
            output=output,
            duration_ms=stream.duration_ms if stream.duration_ms is not None else info.duration_ms,
            timeout_seconds=ctx.config.timeout_video_seconds,
        )
        unused = unused_audio_tracks(info.streams)
        if unused:
            ctx.notices.add(NoticeCode.UNUSED_TRACKS, {"cantidad": unused})
        if stream.sample_rate is not None and stream.channels is not None:
            add_audio_notices(ctx.notices, plan, stream.sample_rate, stream.channels)
        return ConversionOutcome(result_path=output, result_kind="single")
