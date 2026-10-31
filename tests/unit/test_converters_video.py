"""Pruebas de video.ffmpeg: comando, relleno a dimensiones pares y pista de audio (Req 10.5, 10.8,
10.10, 10.11). El ejecutor es un doble.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from converter.config import Config, load_config
from converter.converters.base import (
    ConversionContext,
    ConversionFailure,
    ConversionRequest,
    NoticeCollector,
)
from converter.converters.ffmpeg_tools import set_decoders
from converter.converters.video import PAD_FILTER, VideoFfmpegConverter, h264_bitrate, h264_encoder
from converter.detection.inspectors.media import MediaInfo, MediaStream
from converter.messages_es import FailureReason, NoticeCode
from converter.models import Category, LossClass
from converter.registry.model import ConversionPair
from converter.sandbox.runner import CompletedRun, SandboxRunner
from converter.storage.workspace import Workspace, WorkspaceManager
from converter.tasks.progress import ProgressReporter

TASK_ID = "6f1c2c9e-8a0b-4d7e-9a35-2f0d4b8c1e77"


class FakeRunner:
    def __init__(self) -> None:
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
        Path(command[-1]).write_bytes(b"video")
        return CompletedRun(0, ())


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    space = WorkspaceManager(tmp_path).create()
    space.source_file.write_bytes(b"video")
    return space


@pytest.fixture
def decoders() -> object:
    set_decoders(["h264", "vp9", "opus", "aac", "libopenh264"])
    yield
    set_decoders(None)


def _config() -> Config:
    config = load_config({})
    assert isinstance(config, Config)
    return config


def _video(**overrides: object) -> MediaStream:
    values: dict[str, object] = {
        "index": 0,
        "kind": "video",
        "codec": "h264",
        "decodable": True,
        "is_default": False,
        "channels": None,
        "channel_layout": None,
        "sample_rate": None,
        "width": 320,
        "height": 240,
        "duration_ms": 1000,
    }
    values.update(overrides)
    return MediaStream(**values)  # type: ignore[arg-type]


def _audio(**overrides: object) -> MediaStream:
    values: dict[str, object] = {
        "index": 1,
        "kind": "audio",
        "codec": "aac",
        "decodable": True,
        "is_default": True,
        "channels": 2,
        "channel_layout": "stereo",
        "sample_rate": 48000,
        "width": None,
        "height": None,
        "duration_ms": 1000,
    }
    values.update(overrides)
    return MediaStream(**values)  # type: ignore[arg-type]


def _context(
    workspace: Workspace, info: MediaInfo, runner: FakeRunner, *, source: str, target: str
) -> ConversionContext:
    return ConversionContext(
        task_id=TASK_ID,
        pair=ConversionPair(
            source=source,
            target=target,
            category=Category.VIDEO,
            loss_class=LossClass.LOSSY,
            loss_description="nota",
            converter_id="video.ffmpeg",
        ),
        request=ConversionRequest(target),
        source=info,
        workspace=workspace,
        config=_config(),
        runner=cast(SandboxRunner, runner),
        progress=ProgressReporter(),
        notices=NoticeCollector(),
    )


def test_h264_bitrate_follows_the_frame_size() -> None:
    assert h264_bitrate(320, 240) == 320 * 240 * 3
    assert h264_bitrate(1920, 1080) == 1920 * 1080 * 3
    assert h264_bitrate(64, 64) == 200_000


def test_h264_encoder_prefers_openh264() -> None:
    assert h264_encoder({"libopenh264", "h264"}) == "libopenh264"
    assert h264_encoder({"h264"}) == "libx264"


@pytest.mark.req("10.5", "10.7")
@pytest.mark.asyncio
async def test_mp4_to_webm_maps_video_and_the_default_audio(
    workspace: Workspace, decoders: object
) -> None:
    runner = FakeRunner()
    info = MediaInfo(
        1000,
        (
            _video(codec="h264"),
            _audio(index=2, codec="aac"),
            _audio(index=3, is_default=False),
        ),
    )
    ctx = _context(workspace, info, runner, source="MP4", target="WEBM")

    await VideoFfmpegConverter().run(ctx)

    command = runner.commands[0]
    assert command[command.index("-f") + 1] == "mp4"
    assert "-noautorotate" in command
    assert "-fps_mode" in command and command[command.index("-fps_mode") + 1] == "passthrough"
    assert "0:v:0" in command and "0:2" in command
    assert command[command.index("-c:v") + 1] == "libvpx-vp9"
    assert command[command.index("-c:a") + 1] == "libopus"
    assert PAD_FILTER not in command
    assert ctx.notices.notices[0].code is NoticeCode.UNUSED_TRACKS
    assert ctx.notices.notices[0].params["cantidad"] == 1


@pytest.mark.req("10.10")
@pytest.mark.asyncio
async def test_a_video_without_audio_has_no_audio_codec(
    workspace: Workspace, decoders: object
) -> None:
    runner = FakeRunner()
    ctx = _context(workspace, MediaInfo(1000, (_video(),)), runner, source="MP4", target="WEBM")

    await VideoFfmpegConverter().run(ctx)

    assert "-c:a" not in runner.commands[0]
    assert ctx.notices.notices == ()


@pytest.mark.req("10.11")
@pytest.mark.asyncio
async def test_odd_webm_to_mp4_pads_and_uses_openh264(
    workspace: Workspace, decoders: object
) -> None:
    runner = FakeRunner()
    info = MediaInfo(1000, (_video(codec="vp9", width=321, height=240), _audio(codec="opus")))
    ctx = _context(workspace, info, runner, source="WEBM", target="MP4")

    await VideoFfmpegConverter().run(ctx)

    command = runner.commands[0]
    assert command[command.index("-c:v") + 1] == "libopenh264"
    assert PAD_FILTER in command
    assert "+faststart" in command
    notice = ctx.notices.notices[0]
    assert notice.code is NoticeCode.DIMENSIONS_PADDED
    assert notice.params["ancho_origen"] == 321
    assert notice.params["ancho_resultado"] == 322
    assert notice.params["alto_resultado"] == 240


@pytest.mark.req("10.11")
@pytest.mark.asyncio
async def test_even_webm_to_mp4_does_not_pad(workspace: Workspace, decoders: object) -> None:
    runner = FakeRunner()
    info = MediaInfo(1000, (_video(codec="vp9", width=320, height=240),))
    ctx = _context(workspace, info, runner, source="WEBM", target="MP4")

    await VideoFfmpegConverter().run(ctx)

    assert PAD_FILTER not in runner.commands[0]
    assert ctx.notices.notices == ()


@pytest.mark.req("10.8")
@pytest.mark.asyncio
async def test_an_undecodable_video_codec_fails_before_ffmpeg(
    workspace: Workspace, decoders: object
) -> None:
    runner = FakeRunner()
    ctx = _context(
        workspace, MediaInfo(1000, (_video(codec="av1"),)), runner, source="MP4", target="WEBM"
    )

    with pytest.raises(ConversionFailure) as error:
        await VideoFfmpegConverter().run(ctx)

    assert error.value.failure.reason is FailureReason.UNSUPPORTED_CODEC
    assert error.value.failure.params["pista"] == "video"
    assert runner.commands == []
