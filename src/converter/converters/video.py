"""Convertidor `video.ffmpeg`: MP4 a WEBM y WEBM a MP4 (Req 10.4 a 10.8, 10.10, 10.11).

FFmpeg corre con `-noautorotate`, `-fps_mode passthrough`, `-map 0:v:0` y, si hay audio, la
Pista_de_Audio_Predeterminada. Un video sin audio produce un resultado sin audio (Req 10.10).
Si hay más de una pista de audio, el aviso `pistas_no_usadas` informa las que no se usan.

- **MP4 a WEBM.** `libvpx-vp9` y `libopus` a 48 kHz. VP9 admite dimensiones impares, así que no
  se rellena.
- **WEBM a MP4.** H.264 con `libopenh264` si la build lo tiene y, si no, con `libx264` (el
  FFmpeg de Debian del MVP está compilado con GPL). Audio en AAC y `-movflags +faststart`.
  Si el ancho o el alto es impar, el filtro `pad` agrega píxeles negros a la derecha o abajo
  y el aviso `dimensiones_rellenadas` informa los dos tamaños (Req 10.11).

La tasa de H.264 sale del ancho y del alto. El inspector no informa la cadencia, así que se
asumen 30 cuadros por segundo: `ancho × alto × 3` bits/s, entre 200 kbit/s y 8 Mbit/s.

Los Códecs de las pistas usadas se comprueban con `require_decoder` antes de lanzar FFmpeg
(Req 10.8).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from converter.converters.audio import media_of, run_ffmpeg
from converter.converters.base import (
    ConversionContext,
    ConversionFailure,
    ConversionOutcome,
    ConversionRequest,
    register_converter,
)
from converter.converters.ffmpeg_tools import decoders, require_decoder
from converter.converters.media_rules import default_audio_track, padded_size, unused_audio_tracks
from converter.detection.inspectors.media import MediaInfo, MediaStream
from converter.detection.source_info import SourceInfo
from converter.messages_es import CodecTrack, FailureReason, NoticeCode
from converter.models import DestinationRejection

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet

    from converter.config import Config

CONVERTER_ID = "video.ffmpeg"
_ASSUMED_FPS = 30
_MIN_BITRATE = 200_000
_MAX_BITRATE = 8_000_000
# 0,1 bit por píxel y cuadro: ancho × alto × cuadros / 10

EXTENSIONS = {"MP4": ".mp4", "WEBM": ".webm"}
PAD_FILTER = "pad=w=ceil(iw/2)*2:h=ceil(ih/2)*2:x=0:y=0:color=black"


def h264_bitrate(width: int, height: int) -> int:
    """Tasa de H.264 en bits/s para un video de `width` × `height`, a 30 cuadros por segundo."""
    if width < 1 or height < 1:
        raise ValueError(f"width y height deben ser 1 o mayores: {width}, {height}")
    raw = width * height * _ASSUMED_FPS // 10
    return min(_MAX_BITRATE, max(_MIN_BITRATE, raw))


def h264_encoder(available: AbstractSet[str] | None = None) -> str:
    """Codificador H.264 de la build: `libopenh264` si está y, si no, `libx264` (tarea 20.1).

    La lista es la de decodificadores cargada al arrancar: FFmpeg nombra `libopenh264` tanto
    al decodificador como al codificador. Lanza `DecodersUnavailableError` si no se cargó y
    no se pasó `available`.
    """
    names = decoders() if available is None else available
    return "libopenh264" if "libopenh264" in names else "libx264"


def video_track(info: MediaInfo) -> MediaStream:
    """Primera Pista_de_Video. Lanza `ConversionFailure` si el contenedor no tiene una."""
    for stream in info.streams:
        if stream.kind == "video":
            return stream
    raise ConversionFailure(FailureReason.DAMAGED)


@register_converter(CONVERTER_ID)
class VideoFfmpegConverter:
    """Convierte MP4 a WEBM y WEBM a MP4 con FFmpeg."""

    def preflight(
        self, source: SourceInfo, request: ConversionRequest, config: Config
    ) -> DestinationRejection | None:
        """Un video sin audio igual se convierte (Req 10.10).

        Lanza `TypeError` si `source` no es un medio.
        """
        media_of(source)
        return None

    async def run(self, ctx: ConversionContext) -> ConversionOutcome:
        """Recodifica el video y deja el Archivo_Resultado en `work/`."""
        info = media_of(ctx.source)
        video = video_track(info)
        require_decoder(video, CodecTrack.VIDEO)
        audio = default_audio_track(info.streams)
        if audio is not None:
            require_decoder(audio, CodecTrack.DEFAULT_AUDIO)
        if video.width is None or video.height is None:
            raise ConversionFailure(FailureReason.DAMAGED)

        target = ctx.pair.target
        output = (ctx.workspace.work_dir / f"output{EXTENSIONS[target]}").absolute()
        padded = padded_size(video.width, video.height)
        needs_pad = target == "MP4" and padded != (video.width, video.height)
        output_options = _output_options(
            target, video.width, video.height, has_audio=audio is not None, pad=needs_pad
        )
        maps = ["-map", "0:v:0"]
        if audio is not None:
            maps.extend(("-map", f"0:{audio.index}"))
        await run_ffmpeg(
            ctx,
            input_options=("-noautorotate",),
            stream_options=(*maps, "-fps_mode", "passthrough"),
            output_options=output_options,
            output=output,
            duration_ms=video.duration_ms if video.duration_ms is not None else info.duration_ms,
            timeout_seconds=ctx.config.timeout_video_seconds,
        )
        unused = unused_audio_tracks(info.streams)
        if unused:
            ctx.notices.add(NoticeCode.UNUSED_TRACKS, {"cantidad": unused})
        if needs_pad:
            ctx.notices.add(
                NoticeCode.DIMENSIONS_PADDED,
                {
                    "ancho_origen": video.width,
                    "alto_origen": video.height,
                    "ancho_resultado": padded[0],
                    "alto_resultado": padded[1],
                },
            )
        return ConversionOutcome(result_path=output, result_kind="single")


def _output_options(
    target: str, width: int, height: int, *, has_audio: bool, pad: bool
) -> tuple[str, ...]:
    """Códecs y, en WEBM a MP4, el relleno y `+faststart`."""
    options: list[str] = []
    if pad:
        options.extend(("-vf", PAD_FILTER))
    if target == "WEBM":
        options.extend(
            (
                "-c:v",
                "libvpx-vp9",
                "-crf",
                "32",
                "-b:v",
                "0",
                "-row-mt",
                "1",
                "-deadline",
                "good",
                "-cpu-used",
                "4",
                "-pix_fmt",
                "yuv420p",
            )
        )
        if has_audio:
            options.extend(("-c:a", "libopus", "-b:a", "128k", "-ar", "48000"))
        return tuple(options)
    options.extend(
        (
            "-c:v",
            h264_encoder(),
            "-b:v",
            str(h264_bitrate(width, height)),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        )
    )
    if has_audio:
        options.extend(("-c:a", "aac", "-b:a", "160k"))
    return tuple(options)
