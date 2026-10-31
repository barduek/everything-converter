"""Pruebas de audio.ffmpeg y de la extracción de audio de un MP4 (Req 9.2 a 9.6, 10.3, 10.4, 10.9).

El ejecutor es un doble: no hace falta FFmpeg. Comprueban el comando, los avisos y los rechazos.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from converter.config import Config, load_config
from converter.converters.audio import AudioFfmpegConverter, plan_audio
from converter.converters.base import (
    ConversionContext,
    ConversionFailure,
    ConversionRequest,
    NoticeCollector,
)
from converter.converters.ffmpeg_tools import set_decoders
from converter.converters.video_audio import VideoExtractAudioConverter
from converter.detection.inspectors.media import MediaInfo, MediaStream
from converter.messages_es import FailureReason, NoticeCode, RejectionCode
from converter.models import Category, LossClass
from converter.registry.model import ConversionPair
from converter.sandbox.runner import CompletedRun, SandboxRunner
from converter.storage.workspace import Workspace, WorkspaceManager
from converter.tasks.progress import ProgressReporter

TASK_ID = "6f1c2c9e-8a0b-4d7e-9a35-2f0d4b8c1e77"


class FakeRunner:
    """Ejecutor que registra el comando y, si termina bien, crea el archivo de salida."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.commands: list[list[str]] = []

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        limits: object,
        env: object,
        on_stdout_line: object = None,
        on_stderr_line: object = None,
    ) -> CompletedRun:
        command = list(argv)
        self.commands.append(command)
        if self.returncode == 0:
            Path(command[-1]).write_bytes(b"audio")
        return CompletedRun(self.returncode, ())


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    space = WorkspaceManager(tmp_path).create()
    space.source_file.write_bytes(b"audio")
    return space


@pytest.fixture
def decoders() -> object:
    set_decoders(["mp3", "pcm_s16le", "vorbis", "aac"])
    yield
    set_decoders(None)


def _config() -> Config:
    config = load_config({})
    assert isinstance(config, Config)
    return config


def _stream(**overrides: object) -> MediaStream:
    values: dict[str, object] = {
        "index": 0,
        "kind": "audio",
        "codec": "pcm_s16le",
        "decodable": True,
        "is_default": True,
        "channels": 2,
        "channel_layout": "stereo",
        "sample_rate": 44100,
        "width": None,
        "height": None,
        "duration_ms": 1000,
    }
    values.update(overrides)
    return MediaStream(**values)  # type: ignore[arg-type]


def _info(*streams: MediaStream, duration_ms: int = 1000) -> MediaInfo:
    return MediaInfo(duration_ms=duration_ms, streams=streams)


def _context(
    workspace: Workspace,
    info: MediaInfo,
    runner: FakeRunner,
    *,
    source: str,
    target: str,
    category: Category,
    converter_id: str,
) -> ConversionContext:
    return ConversionContext(
        task_id=TASK_ID,
        pair=ConversionPair(
            source=source,
            target=target,
            category=category,
            loss_class=LossClass.LOSSY,
            loss_description="nota",
            converter_id=converter_id,
        ),
        request=ConversionRequest(target),
        source=info,
        workspace=workspace,
        config=_config(),
        runner=cast(SandboxRunner, runner),
        progress=ProgressReporter(),
        notices=NoticeCollector(),
    )


@pytest.mark.req("9.2", "9.5")
@pytest.mark.asyncio
async def test_wav_to_mp3_keeps_rate_and_stereo(workspace: Workspace, decoders: object) -> None:
    runner = FakeRunner()
    ctx = _context(
        workspace,
        _info(_stream()),
        runner,
        source="WAV",
        target="MP3",
        category=Category.AUDIO,
        converter_id="audio.ffmpeg",
    )

    outcome = await AudioFfmpegConverter().run(ctx)

    command = runner.commands[0]
    assert command[0] == "ffmpeg"
    assert "-protocol_whitelist" in command and "file" in command
    assert "-f" in command and command[command.index("-f") + 1] == "wav"
    assert "-map" in command and "0:0" in command
    assert "-c:a" in command and command[command.index("-c:a") + 1] == "libmp3lame"
    assert "-q:a" in command and "-ac" not in command and "-ar" not in command
    assert outcome.result_path.is_file()
    assert ctx.notices.notices == ()


@pytest.mark.req("9.3", "9.4")
@pytest.mark.asyncio
async def test_many_channels_and_a_foreign_rate_are_reported(
    workspace: Workspace, decoders: object
) -> None:
    runner = FakeRunner()
    ctx = _context(
        workspace,
        _info(_stream(channels=6, channel_layout="5.1", sample_rate=7350)),
        runner,
        source="WAV",
        target="MP3",
        category=Category.AUDIO,
        converter_id="audio.ffmpeg",
    )

    await AudioFfmpegConverter().run(ctx)

    command = runner.commands[0]
    assert command[command.index("-ac") + 1] == "2"
    assert command[command.index("-ar") + 1] == "8000"
    codes = [notice.code for notice in ctx.notices.notices]
    assert codes == [NoticeCode.SAMPLE_RATE_CHANGED, NoticeCode.CHANNELS_MIXED]
    assert ctx.notices.notices[1].params["canales_origen"] == 6
    assert ctx.notices.notices[1].params["canales_resultado"] == 2


@pytest.mark.req("9.6")
@pytest.mark.asyncio
async def test_an_unknown_codec_fails_before_ffmpeg(workspace: Workspace, decoders: object) -> None:
    runner = FakeRunner()
    ctx = _context(
        workspace,
        _info(_stream(codec="amr_nb")),
        runner,
        source="WAV",
        target="MP3",
        category=Category.AUDIO,
        converter_id="audio.ffmpeg",
    )

    with pytest.raises(ConversionFailure) as error:
        await AudioFfmpegConverter().run(ctx)

    assert error.value.failure.reason is FailureReason.UNSUPPORTED_CODEC
    assert error.value.failure.params["codec"] == "amr_nb"
    assert runner.commands == []


@pytest.mark.req("9.6")
@pytest.mark.asyncio
async def test_ffmpeg_failing_fails_the_conversion(workspace: Workspace, decoders: object) -> None:
    runner = FakeRunner(returncode=1)
    ctx = _context(
        workspace,
        _info(_stream()),
        runner,
        source="WAV",
        target="OGG",
        category=Category.AUDIO,
        converter_id="audio.ffmpeg",
    )

    with pytest.raises(ConversionFailure):
        await AudioFfmpegConverter().run(ctx)

    assert "-c:a" in runner.commands[0]
    assert runner.commands[0][runner.commands[0].index("-c:a") + 1] == "libvorbis"


@pytest.mark.req("9.4")
def test_plan_audio_keeps_a_wav_rate(decoders: object) -> None:
    plan = plan_audio(_stream(sample_rate=48000, channels=1), "WAV", _config())

    assert plan.rate_changed is False
    assert plan.mixed is False
    assert plan.options == ("-c:a", "pcm_s16le")


@pytest.mark.req("10.3")
def test_extract_preflight_rejects_a_video_without_audio() -> None:
    info = _info(
        _stream(
            kind="video",
            codec="h264",
            channels=None,
            sample_rate=None,
            width=320,
            height=240,
        )
    )
    rejection = VideoExtractAudioConverter().preflight(info, ConversionRequest("MP3"), _config())

    assert rejection is not None
    assert rejection.code is RejectionCode.VIDEO_WITHOUT_AUDIO


@pytest.mark.req("10.4", "10.9")
@pytest.mark.asyncio
async def test_extract_uses_the_default_track_and_reports_the_rest(
    workspace: Workspace, decoders: object
) -> None:
    runner = FakeRunner()
    info = _info(
        _stream(index=1, is_default=False, codec="aac"),
        _stream(index=2, is_default=True, codec="aac", channels=6, channel_layout="5.1"),
    )
    ctx = _context(
        workspace,
        info,
        runner,
        source="MP4",
        target="MP3",
        category=Category.VIDEO,
        converter_id="video.extract_audio",
    )

    await VideoExtractAudioConverter().run(ctx)

    command = runner.commands[0]
    assert command[command.index("-f") + 1] == "mp4"
    assert "0:2" in command
    assert "-ac" in command
    assert [notice.code for notice in ctx.notices.notices] == [
        NoticeCode.UNUSED_TRACKS,
        NoticeCode.CHANNELS_MIXED,
    ]
    assert ctx.notices.notices[0].params["cantidad"] == 1
