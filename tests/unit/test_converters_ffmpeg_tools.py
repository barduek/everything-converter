"""Pruebas de src/converter/converters/ffmpeg_tools.py: demuxer forzado, argumentos comunes de
FFmpeg, parser de `-progress pipe:1`, lista de decodificadores y comprobación de Códec (Req 9.6,
10.8, 13.7, 14.5, 16.2).

Nada de esto necesita FFmpeg: el comando se compara con el de la sección "Audio" del diseño, el
parser y la lista de decodificadores se prueban con salidas escritas a mano y la función de
arranque corre con un ejecutor de prueba que entrega la salida de `ffmpeg -decoders` sin lanzar
procesos. La prueba que consulta el FFmpeg real usa la marca `requires_tool`.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from converter.config import Config, load_config
from converter.converters import base
from converter.converters.base import (
    ConversionFailure,
    StartupContext,
    run_startup_hooks,
    startup_hooks,
)
from converter.converters.ffmpeg_tools import (
    DECODERS_TIMEOUT_SECONDS,
    DEMUXERS,
    DecoderQueryError,
    DecodersUnavailableError,
    FfmpegProgress,
    decoders,
    demuxer_for,
    ffmpeg_command,
    load_decoders,
    parse_decoders,
    parse_out_time_us,
    require_decoder,
    set_decoders,
    source_args,
    thread_count,
)
from converter.detection.inspectors.media import MediaStream
from converter.eventlog import EventLog
from converter.messages_es import CodecTrack, FailureReason, render_failure
from converter.models import Stage
from converter.sandbox.cpus import effective_cpus
from converter.sandbox.limits import Limits, SandboxTool, tool_limits
from converter.sandbox.runner import CompletedRun, SandboxRunner
from converter.storage.workspace import WorkspaceManager
from converter.tasks.progress import ProgressReporter

# Salida de `ffmpeg -hide_banner -decoders`: leyenda, separador y decodificadores. Los de
# nombre distinto al del Códec terminan con "(codec <nombre>)"
DECODERS_OUTPUT = """\
Decoders:
 V..... = Video
 A..... = Audio
 S..... = Subtitle
 .F.... = Frame-level multithreading
 ..S... = Slice-level multithreading
 ...X.. = Codec is experimental
 ....B. = Supports draw_horiz_band
 .....D = Supports direct rendering method 1
 ------
 V....D 012v                 Uncompressed 4:2:2 10-bit
 VFS..D h264                 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10
 V.S.BD flv                  FLV / Sorenson Spark / Sorenson H.263 (Flash Video) (codec flv1)
 VFS..D vp9                  Google VP9
 V....D libvpx-vp9           libvpx VP9 (codec vp9)
 V.S.BD mpeg4                MPEG-4 part 2
 V....D mjpeg                MJPEG (Motion JPEG)
 A....D aac                  AAC (Advanced Audio Coding)
 A....D amrnb                AMR-NB (Adaptive Multi-Rate NarrowBand) (codec amr_nb)
 A....D mp3                  MP3 (MPEG audio layer 3)
 A....D mp3float             MP3 (MPEG audio layer 3) (codec mp3)
 A....D opus                 Opus
 A....D libopus              libopus Opus (codec opus)
 A....D pcm_s16le            PCM signed 16-bit little-endian
 A....D vorbis               Vorbis
 S..... mov_text             MOV text
"""


def lines_of(text: str) -> list[str]:
    return text.splitlines()


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Cada prueba parte sin lista de decodificadores y con los registros de arranque aislados."""
    monkeypatch.setattr(base, "_STARTUP_HOOKS", list(base._STARTUP_HOOKS))
    set_decoders(None)
    yield
    set_decoders(None)


def stream(**override: object) -> MediaStream:
    values: dict[str, object] = {
        "index": 1,
        "kind": "audio",
        "codec": "aac",
        "decodable": True,
        "is_default": True,
        "channels": 2,
        "channel_layout": "stereo",
        "sample_rate": 44100,
        "width": None,
        "height": None,
        "duration_ms": 1000,
    }
    return MediaStream(**{**values, **override})  # type: ignore[arg-type]


# --- Demuxer forzado y comando ---


@pytest.mark.req("13.7")
def test_every_media_format_has_its_forced_demuxer() -> None:
    assert dict(DEMUXERS) == {
        "MP3": "mp3",
        "WAV": "wav",
        "OGG": "ogg",
        "MP4": "mp4",
        "WEBM": "matroska",
    }
    assert [demuxer_for(name) for name in DEMUXERS] == list(DEMUXERS.values())


@pytest.mark.req("13.7")
@pytest.mark.parametrize("real_type", [None, "", "PNG", "mp4", "MKV", 3])
def test_no_demuxer_is_guessed_for_other_types(real_type: object) -> None:
    with pytest.raises(ValueError, match="demuxer"):
        demuxer_for(real_type)  # type: ignore[arg-type]


@pytest.mark.req("13.7")
def test_source_args_allow_only_the_file_protocol_and_force_the_demuxer() -> None:
    assert source_args("WEBM", Path("/ws/source/original")) == [
        "-protocol_whitelist",
        "file",
        "-f",
        "matroska",
        "-i",
        "/ws/source/original",
    ]


def test_source_args_make_the_path_absolute() -> None:
    # Una ruta relativa que empieza con "-" no puede tomarse por una opción
    args = source_args("MP3", Path("-i"))

    assert Path(args[-1]).is_absolute()
    assert args[-1].endswith("/-i")


@pytest.mark.req("9.6", "13.7", "13.4")
def test_audio_command_follows_the_design() -> None:
    command = ffmpeg_command(
        "MP3",
        Path("/ws/source/original"),
        Path("/ws/work/output.wav"),
        stream_options=["-map", "0:a:0", "-vn", "-sn", "-dn"],
        output_options=["-ar", "44100", "-c:a", "pcm_s16le"],
        threads=3,
    )

    assert command == [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-protocol_whitelist",
        "file",
        "-f",
        "mp3",
        "-i",
        "/ws/source/original",
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-threads",
        "3",
        "-ar",
        "44100",
        "-c:a",
        "pcm_s16le",
        "/ws/work/output.wav",
    ]


@pytest.mark.req("13.7")
@pytest.mark.parametrize("real_type", list(DEMUXERS))
def test_the_source_is_opened_only_with_the_file_protocol_and_the_forced_demuxer(
    real_type: str,
) -> None:
    command = ffmpeg_command(real_type, Path("/ws/source/original"), Path("/ws/work/output"))

    position = command.index("-i")
    assert command[position - 4 : position + 2] == [
        "-protocol_whitelist",
        "file",
        "-f",
        DEMUXERS[real_type],
        "-i",
        "/ws/source/original",
    ]
    assert command.count("-protocol_whitelist") == command.count("-i") == 1
    assert command[command.index("-map_metadata") + 1] == "-1"


def test_input_options_go_before_the_input() -> None:
    command = ffmpeg_command(
        "MP4",
        Path("/ws/source/original"),
        Path("/ws/work/output.webm"),
        input_options=["-noautorotate"],
        stream_options=["-map", "0:v:0"],
        output_options=["-fps_mode", "passthrough"],
        threads=2,
    )

    assert command.index("-noautorotate") < command.index("-i") < command.index("-map")
    assert command.index("-map") < command.index("-map_metadata") < command.index("-threads")
    assert command.index("-threads") < command.index("-fps_mode")
    assert command[-1] == "/ws/work/output.webm"
    assert command[command.index("-threads") + 1] == "2"


@pytest.mark.req("13.4")
def test_threads_default_to_the_effective_cpus() -> None:
    command = ffmpeg_command("WAV", Path("/a"), Path("/b"))

    assert thread_count() == effective_cpus()
    assert command[command.index("-threads") + 1] == str(effective_cpus())


@pytest.mark.parametrize("threads", [0, -1, True])
def test_invalid_threads_are_rejected(threads: int) -> None:
    with pytest.raises(ValueError, match="threads"):
        ffmpeg_command("WAV", Path("/a"), Path("/b"), threads=threads)


def test_the_command_needs_a_demuxer() -> None:
    with pytest.raises(ValueError, match="demuxer"):
        ffmpeg_command(None, Path("/a"), Path("/b"))


# --- Progreso ---


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("out_time_us=0", 0),
        ("out_time_us=1500000", 1_500_000),
        ("out_time_us = 42 ", 42),
        ("out_time_us=9223372036854775807", 9_223_372_036_854_775_807),
        ("out_time_us=N/A", None),
        ("out_time_us=", None),
        ("out_time_us=-5", None),
        ("out_time_us=1.5", None),
        ("out_time_us=+5", None),
        ("out_time_us=٣", None),
        ("out_time_us=1_000", None),
        ("out_time_us=" + "9" * 20, None),
        ("out_time_us=" + "9" * 6000, None),
        ("out_time_ms=1500000", None),
        ("out_time=00:00:01.500000", None),
        ("progress=continue", None),
        ("frame=12", None),
        ("", None),
        ("sin signo igual", None),
    ],
)
def test_parse_out_time_us(line: str, expected: int | None) -> None:
    assert parse_out_time_us(line) == expected


class RecordingSink:
    def __init__(self) -> None:
        self.fractions: list[float] = []

    def report(self, fraction: float) -> None:
        self.fractions.append(fraction)


PROGRESS_BLOCKS = """\
frame=0
fps=0.00
stream_0_0_q=-1.0
bitrate=N/A
total_size=0
out_time_us=N/A
out_time_ms=N/A
out_time=N/A
dup_frames=0
drop_frames=0
speed=N/A
progress=continue
frame=125
fps=0.00
bitrate=  64.0kbits/s
total_size=40960
out_time_us=2500000
out_time_ms=2500000
out_time=00:00:02.500000
speed=47.6x
progress=continue
frame=250
bitrate=  64.0kbits/s
total_size=81920
out_time_us=5000000
out_time_ms=5000000
out_time=00:00:05.000000
speed=51.2x
progress=end
"""


@pytest.mark.req("14.5")
def test_progress_is_out_time_over_the_duration() -> None:
    sink = RecordingSink()
    progress = FfmpegProgress(10_000, sink)

    for line in lines_of(PROGRESS_BLOCKS):
        progress.feed(line)

    assert sink.fractions == [0.25, 0.5]


@pytest.mark.req("14.5")
def test_progress_feeds_the_percentage_without_going_back() -> None:
    reporter = ProgressReporter()
    progress = FfmpegProgress(4_000, reporter)
    seen: list[int] = []

    for line in ("out_time_us=1000000", "out_time_us=400000", "out_time_us=9000000"):
        progress.feed(line)
        seen.append(reporter.percent)

    # Un `out_time_us` por encima de la duración llega a 100 %; uno menor no retrocede
    assert seen == [25, 25, 100]


def test_progress_without_a_known_duration_reports_nothing() -> None:
    sink = RecordingSink()
    progress = FfmpegProgress(0, sink)

    progress.feed("out_time_us=1000000")

    assert sink.fractions == []


@pytest.mark.parametrize("duration", [-1, True, 1.5, "10"])
def test_progress_rejects_an_invalid_duration(duration: object) -> None:
    with pytest.raises(ValueError, match="duration_ms"):
        FfmpegProgress(duration, RecordingSink())  # type: ignore[arg-type]


def test_progress_feed_never_raises_on_garbage() -> None:
    sink = RecordingSink()
    progress = FfmpegProgress(1000, sink)

    for line in ("", "=", "out_time_us", "out_time_us=" + "9" * 70_000, "\x00\xff", "progress=end"):
        progress.feed(line)

    assert sink.fractions == []


# --- Lista de decodificadores ---


def test_parse_decoders_reads_names_and_the_codecs_they_decode() -> None:
    names = parse_decoders(lines_of(DECODERS_OUTPUT))

    assert names == {
        "012v",
        "h264",
        "flv",
        "flv1",
        "vp9",
        "libvpx-vp9",
        "mpeg4",
        "mjpeg",
        "aac",
        "amrnb",
        "amr_nb",
        "mp3",
        "mp3float",
        "opus",
        "libopus",
        "pcm_s16le",
        "vorbis",
        "mov_text",
    }


@pytest.mark.parametrize(
    "lines",
    [
        [],
        [""],
        ["Decoders:", " ------"],
        [" V..... = Video", " .F.... = Frame-level multithreading"],
        ["ffmpeg: command not found"],
        [" V....D"],
    ],
)
def test_parse_decoders_ignores_everything_but_decoder_lines(lines: list[str]) -> None:
    assert parse_decoders(lines) == frozenset()


def test_decoders_are_unavailable_until_they_are_stored() -> None:
    with pytest.raises(DecodersUnavailableError):
        decoders()

    set_decoders(["aac", "mp3"])
    assert decoders() == {"aac", "mp3"}
    assert isinstance(decoders(), frozenset)

    set_decoders(None)
    with pytest.raises(DecodersUnavailableError):
        decoders()


def test_missing_decoders_fail_with_a_runtime_error() -> None:
    # El Gestor_de_Tareas termina con `error_interno` cualquier excepción que no sea
    # `ConversionFailure`
    assert issubclass(DecodersUnavailableError, RuntimeError)
    assert not issubclass(DecodersUnavailableError, ConversionFailure)


# --- Función de arranque ---


class DecodersRunner(SandboxRunner):
    """Ejecutor sin subprocesos: registra la llamada y entrega la salida de `ffmpeg -decoders`."""

    def __init__(self, stdout: Sequence[str], *, returncode: int = 0) -> None:
        super().__init__()
        self.stdout = tuple(stdout)
        self.returncode = returncode
        self.calls: list[tuple[list[str], Path, Limits, dict[str, str]]] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        limits: Limits,
        env: Mapping[str, str],
        on_stdout_line: Callable[[str], None] | None = None,
        on_stderr_line: Callable[[str], None] | None = None,
    ) -> CompletedRun:
        self.calls.append((list(argv), cwd, limits, dict(env)))
        assert cwd.is_dir()  # el Espacio_de_Trabajo existe mientras corre la consulta
        assert on_stdout_line is not None
        for line in self.stdout:
            on_stdout_line(line)
        return CompletedRun(returncode=self.returncode, stderr_lines=())


def startup_context(
    tmp_path: Path, runner: SandboxRunner, out: io.StringIO | None = None
) -> StartupContext:
    config = load_config({})
    assert isinstance(config, Config)
    return StartupContext(
        config=config,
        runner=runner,
        storage=WorkspaceManager(tmp_path),
        event_log=EventLog(io.StringIO() if out is None else out),
    )


def workspaces(tmp_path: Path) -> list[Path]:
    return [path for path in tmp_path.iterdir() if path.name.startswith("ws-")]


def test_load_decoders_is_a_registered_startup_hook() -> None:
    assert load_decoders in startup_hooks()


@pytest.mark.asyncio
@pytest.mark.req("9.6", "10.8")
async def test_load_decoders_stores_the_list_and_removes_its_workspace(tmp_path: Path) -> None:
    runner = DecodersRunner(lines_of(DECODERS_OUTPUT))

    await load_decoders(startup_context(tmp_path, runner))

    assert decoders() == parse_decoders(lines_of(DECODERS_OUTPUT))
    ((argv, cwd, limits, env),) = runner.calls
    assert argv == ["ffmpeg", "-hide_banner", "-decoders"]
    assert cwd.name == "tmp"
    assert limits == tool_limits(SandboxTool.FFMPEG, timeout_seconds=DECODERS_TIMEOUT_SECONDS)
    assert env["HOME"] == env["TMPDIR"] == str(cwd)
    assert workspaces(tmp_path) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [
        pytest.param(lines_of(DECODERS_OUTPUT), 1, id="codigo_de_salida"),
        pytest.param([], 0, id="salida_vacia"),
        pytest.param(["Decoders:", " ------"], 0, id="sin_decodificadores"),
    ],
)
async def test_load_decoders_fails_without_storing_a_list(
    tmp_path: Path, stdout: list[str], returncode: int
) -> None:
    runner = DecodersRunner(stdout, returncode=returncode)

    with pytest.raises(DecoderQueryError):
        await load_decoders(startup_context(tmp_path, runner))

    with pytest.raises(DecodersUnavailableError):
        decoders()
    assert workspaces(tmp_path) == []


@pytest.mark.asyncio
async def test_a_failed_query_is_logged_and_does_not_stop_the_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Solo esta función de arranque: las que registren otras familias no se ejecutan
    monkeypatch.setattr(base, "_STARTUP_HOOKS", [load_decoders])
    out = io.StringIO()

    await run_startup_hooks(startup_context(tmp_path, DecodersRunner([], returncode=1), out))

    (event,) = (json.loads(line) for line in out.getvalue().splitlines())
    assert event["event"] == "startup_hook_failed"
    assert event["hook"].endswith("ffmpeg_tools.load_decoders")
    assert event["exception"].endswith("DecoderQueryError")
    with pytest.raises(DecodersUnavailableError):
        decoders()


@pytest.mark.requires_tool("ffmpeg")
@pytest.mark.asyncio
@pytest.mark.req("9.6", "10.8")
async def test_real_ffmpeg_lists_the_decoders_of_the_build(tmp_path: Path) -> None:
    await load_decoders(startup_context(tmp_path, SandboxRunner()))

    available = decoders()
    assert {"pcm_s16le", "aac", "mp3", "vorbis"} <= available
    assert workspaces(tmp_path) == []


# --- Comprobación del Códec ---


@pytest.mark.req("9.6", "10.8")
def test_a_codec_with_a_decoder_passes() -> None:
    set_decoders(parse_decoders(lines_of(DECODERS_OUTPUT)))

    require_decoder(stream(codec="aac"), CodecTrack.DEFAULT_AUDIO)
    # Un decodificador con otro nombre que el del Códec (`mp3float` decodifica `mp3`)
    require_decoder(stream(codec="amr_nb"), CodecTrack.AUDIO)
    require_decoder(stream(kind="video", codec="flv1"), CodecTrack.VIDEO)


@pytest.mark.req("9.6", "10.8")
@pytest.mark.parametrize(
    ("track", "text"),
    [
        (CodecTrack.AUDIO, "del audio"),
        (CodecTrack.DEFAULT_AUDIO, "de la pista de audio predeterminada"),
        (CodecTrack.VIDEO, "de la pista de video"),
    ],
)
def test_a_codec_without_a_decoder_fails_with_the_codec_and_the_track(
    track: CodecTrack, text: str
) -> None:
    set_decoders(parse_decoders(lines_of(DECODERS_OUTPUT)))

    with pytest.raises(ConversionFailure) as error:
        require_decoder(stream(kind="video", codec="hevc"), track)

    failure = error.value.failure
    assert failure.reason is FailureReason.UNSUPPORTED_CODEC
    assert failure.stage is Stage.CONVERSION
    assert dict(failure.params) == {"codec": "hevc", "pista": track.value}
    message = render_failure(failure)
    assert "«hevc»" in message.text
    assert text in message.text


@pytest.mark.req("11.6", "9.6")
def test_the_check_can_run_in_the_transcription_stage() -> None:
    with pytest.raises(ConversionFailure) as error:
        require_decoder(
            stream(codec="wmapro"),
            CodecTrack.DEFAULT_AUDIO,
            available={"aac"},
            stage=Stage.TRANSCRIPTION,
        )

    assert error.value.failure.stage is Stage.TRANSCRIPTION
    assert error.value.failure.reason is FailureReason.UNSUPPORTED_CODEC


def test_the_check_takes_an_explicit_list_before_the_stored_one() -> None:
    set_decoders(["aac"])

    require_decoder(stream(codec="mp3"), CodecTrack.AUDIO, available={"mp3"})
    with pytest.raises(ConversionFailure):
        require_decoder(stream(codec="mp3"), CodecTrack.AUDIO)


def test_the_check_needs_the_stored_list_when_none_is_given() -> None:
    with pytest.raises(DecodersUnavailableError):
        require_decoder(stream(), CodecTrack.AUDIO)


@pytest.mark.req("16.2")
def test_a_stream_the_inspector_could_not_identify_is_a_damaged_file() -> None:
    unidentified = stream(codec="unknown", decodable=False)

    with pytest.raises(ConversionFailure) as error:
        require_decoder(unidentified, CodecTrack.AUDIO, available={"unknown"})

    assert error.value.failure.reason is FailureReason.DAMAGED
    assert dict(error.value.failure.params) == {}


def test_the_check_rejects_an_invalid_track() -> None:
    with pytest.raises(ValueError):
        require_decoder(stream(codec="x264"), "pista_inventada", available={"aac"})  # type: ignore[arg-type]
