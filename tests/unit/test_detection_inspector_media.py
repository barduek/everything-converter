"""Pruebas del inspector `media` en src/converter/detection/inspectors/media.py (Req 9.6, 10.4,
10.8, 13.7, 16.2).

El análisis de la salida JSON de ffprobe usa documentos escritos a mano con la forma de
`ffprobe -print_format json -show_format -show_streams`. Las pruebas del inspector y del worker
ponen un `ffprobe` de prueba, un script de shell que registra sus argumentos y escribe una salida
fija, al principio del `PATH`, así corren sin FFmpeg. Las que necesitan el `ffprobe` real usan la
marca `requires_tool` y corren dentro de la imagen.
"""

from __future__ import annotations

import json
import os
import shlex
import struct
import subprocess
import sys
import wave
from collections.abc import Mapping
from pathlib import Path

import pytest

from converter.converters.media_rules import default_audio_track, layout_channels
from converter.detection.inspectors import InspectorInput, get_inspector, registered_inspectors
from converter.detection.inspectors.media import (
    UNKNOWN_CODEC,
    FfprobeError,
    MediaInfo,
    MediaStream,
    ffprobe_command,
    has_decodable_stream,
    inspect_media,
    parse_ffprobe,
)
from converter.detection.source_info import (
    SourceInfo,
    SourceRejection,
    dump_inspector_output,
    load_inspector_output,
    registered_source_infos,
)
from converter.eventlog import Tool
from converter.messages_es import FailureReason
from converter.sandbox.limits import SandboxTool
from converter.workers.inspect import MODULE

# --- Salidas de ffprobe ---

MP4_TWO_AUDIO_TRACKS: dict[str, object] = {
    "streams": [
        {
            "index": 0,
            "codec_name": "h264",
            "codec_type": "video",
            "width": 321,
            "height": 240,
            "duration": "2.000000",
            "disposition": {"default": 1, "attached_pic": 0},
        },
        {
            "index": 1,
            "codec_name": "aac",
            "codec_type": "audio",
            "sample_rate": "44100",
            "channels": 2,
            "channel_layout": "stereo",
            "duration": "2.023311",
            "disposition": {"default": 0, "attached_pic": 0},
        },
        {
            "index": 2,
            "codec_name": "aac",
            "codec_type": "audio",
            "sample_rate": "48000",
            "channels": 6,
            "channel_layout": "5.1",
            "duration": "2.005333",
            "disposition": {"default": 1, "attached_pic": 0},
        },
    ],
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "2.023311"},
}

MP3_WITH_COVER: dict[str, object] = {
    "streams": [
        {
            "index": 0,
            "codec_name": "mp3",
            "codec_type": "audio",
            "sample_rate": "44100",
            "channels": 2,
            "channel_layout": "stereo",
            "duration": "187.763265",
            "disposition": {"default": 0, "attached_pic": 0},
        },
        {
            "index": 1,
            "codec_name": "mjpeg",
            "codec_type": "video",
            "width": 500,
            "height": 500,
            "disposition": {"default": 0, "attached_pic": 1},
        },
    ],
    "format": {"format_name": "mp3", "duration": "187.763265"},
}


def audio_stream(**override: object) -> dict[str, object]:
    stream: dict[str, object] = {
        "index": 0,
        "codec_name": "pcm_s16le",
        "codec_type": "audio",
        "sample_rate": "8000",
        "channels": 1,
        "channel_layout": "mono",
        "duration": "1.000000",
        "disposition": {"default": 0},
    }
    return {**stream, **override}


def media_stream(**override: object) -> MediaStream:
    values: dict[str, object] = {
        "index": 0,
        "kind": "audio",
        "codec": "mp3",
        "decodable": True,
        "is_default": False,
        "channels": 2,
        "channel_layout": "stereo",
        "sample_rate": 44100,
        "width": None,
        "height": None,
        "duration_ms": 1000,
    }
    return MediaStream(**{**values, **override})  # type: ignore[arg-type]


# --- Registro ---


def test_media_inspector_is_registered_with_the_catalog_name() -> None:
    assert "media" in registered_inspectors()
    spec = get_inspector("media")
    assert spec.function is inspect_media
    assert spec.info_type is MediaInfo
    assert (spec.tool, spec.sandbox_tool) == (Tool.FFPROBE, SandboxTool.FFMPEG)
    assert registered_source_infos()["media"] is MediaInfo


# --- MediaInfo y MediaStream ---


@pytest.mark.req("16.2")
def test_media_info_travels_as_json() -> None:
    info = MediaInfo(
        duration_ms=2023,
        streams=(
            media_stream(
                index=0,
                kind="video",
                codec="h264",
                channels=None,
                channel_layout=None,
                sample_rate=None,
                width=321,
                height=240,
                is_default=True,
            ),
            media_stream(index=1, channel_layout=None, duration_ms=None),
        ),
    )

    assert MediaInfo.from_json(info.to_json()) == info
    assert load_inspector_output(dump_inspector_output(info)) == info
    data = info.to_json()["data"]
    assert isinstance(data, dict)
    assert set(data) == {"duration_ms", "streams"}
    streams = data["streams"]
    assert isinstance(streams, list)
    first = streams[0]
    assert isinstance(first, dict)
    assert set(first) == {
        "index",
        "kind",
        "codec",
        "decodable",
        "is_default",
        "channels",
        "channel_layout",
        "sample_rate",
        "width",
        "height",
        "duration_ms",
    }


@pytest.mark.parametrize(
    "override",
    [
        {"index": -1},
        {"index": True},
        {"kind": "subtitle"},
        {"codec": ""},
        {"codec": 3},
        {"decodable": 1},
        {"is_default": "si"},
        {"channels": 0},
        {"sample_rate": -44100},
        {"width": 0},
        {"height": True},
        {"duration_ms": -1},
        {"channel_layout": ""},
    ],
)
def test_media_stream_rejects_invalid_values(override: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        media_stream(**override)


@pytest.mark.parametrize(
    ("duration", "streams"),
    [(-1, ()), (True, ()), (1.5, ()), (0, [media_stream()]), (0, (object(),))],
)
def test_media_info_rejects_invalid_values(duration: object, streams: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        MediaInfo(duration_ms=duration, streams=streams)  # type: ignore[arg-type]


def test_json_with_invalid_streams_is_rejected() -> None:
    valid = media_stream()
    document = MediaInfo(duration_ms=1, streams=(valid,)).to_json()
    data = document["data"]
    assert isinstance(data, dict)
    streams = data["streams"]
    assert isinstance(streams, list)
    stream = streams[0]
    assert isinstance(stream, dict)

    stream["kind"] = "subtitle"
    with pytest.raises(ValueError, match="kind"):
        MediaInfo.from_json(document)
    stream["kind"] = "audio"
    del stream["codec"]
    with pytest.raises(ValueError, match="codec"):
        MediaInfo.from_json(document)


# --- Análisis de la salida de ffprobe ---


@pytest.mark.req("10.4", "10.8")
def test_parse_reads_tracks_dispositions_and_duration_of_an_mp4() -> None:
    info = parse_ffprobe(MP4_TWO_AUDIO_TRACKS)

    assert info == MediaInfo(
        duration_ms=2023,
        streams=(
            MediaStream(
                index=0,
                kind="video",
                codec="h264",
                decodable=True,
                is_default=True,
                channels=None,
                channel_layout=None,
                sample_rate=None,
                width=321,
                height=240,
                duration_ms=2000,
            ),
            MediaStream(
                index=1,
                kind="audio",
                codec="aac",
                decodable=True,
                is_default=False,
                channels=2,
                channel_layout="stereo",
                sample_rate=44100,
                width=None,
                height=None,
                duration_ms=2023,
            ),
            MediaStream(
                index=2,
                kind="audio",
                codec="aac",
                decodable=True,
                is_default=True,
                channels=6,
                channel_layout="5.1",
                sample_rate=48000,
                width=None,
                height=None,
                duration_ms=2005,
            ),
        ),
    )
    # `MediaStream` cumple lo que `default_audio_track` necesita de un flujo (Req 10.4)
    track = default_audio_track(info.streams)
    assert track is not None
    assert track.index == 2
    # La distribución que informó ffprobe es la que lee `layout_channels` (Req 9.3, 10.9)
    assert layout_channels(track.channel_layout, track.channels or 1) == (
        "FL",
        "FR",
        "FC",
        "LFE",
        "BL",
        "BR",
    )


def test_the_stored_size_is_the_one_ffprobe_reports() -> None:
    # `-noautorotate` conserva el ancho y el alto almacenados, que son los que se informan
    document = {
        "streams": [
            {
                "index": 0,
                "codec_name": "h264",
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}],
            }
        ]
    }

    (video,) = parse_ffprobe(document).streams

    assert (video.width, video.height) == (1920, 1080)


def test_a_cover_picture_is_not_a_video_track() -> None:
    info = parse_ffprobe(MP3_WITH_COVER)

    audio, cover = info.streams
    assert (audio.kind, cover.kind) == ("audio", "other")
    assert (cover.codec, cover.decodable) == ("mjpeg", True)
    assert (cover.width, cover.height) == (None, None)
    assert has_decodable_stream(info)


@pytest.mark.parametrize(
    ("container", "streams", "expected"),
    [
        pytest.param({"duration": "1.5"}, [audio_stream(duration="9")], 1500, id="contenedor"),
        pytest.param(
            {"duration": "N/A"},
            [audio_stream(duration="1.2345"), audio_stream(index=1, duration="3.0004")],
            3000,
            id="mayor_de_los_flujos",
        ),
        pytest.param({}, [audio_stream(duration=2)], 2000, id="sin_contenedor"),
        pytest.param({"duration": "N/A"}, [audio_stream(duration="N/A")], 0, id="desconocida"),
        pytest.param({"duration": "1e3"}, [audio_stream(duration=None)], 0, id="invalida"),
        pytest.param({"duration": "1.0006"}, [audio_stream()], 1001, id="redondeo"),
    ],
)
def test_container_duration_falls_back_to_the_longest_stream(
    container: dict[str, object], streams: list[dict[str, object]], expected: int
) -> None:
    info = parse_ffprobe({"streams": streams, "format": container})

    assert info.duration_ms == expected


def test_stream_duration_is_none_when_ffprobe_does_not_report_it() -> None:
    (stream,) = parse_ffprobe({"streams": [audio_stream(duration="N/A")]}).streams
    assert stream.duration_ms is None
    (stream,) = parse_ffprobe({"streams": [{**audio_stream(), "duration": None}]}).streams
    assert stream.duration_ms is None


@pytest.mark.parametrize(
    "stream",
    [
        pytest.param(audio_stream(codec_name="unknown"), id="codec_desconocido"),
        pytest.param(audio_stream(codec_name="none"), id="codec_none"),
        pytest.param({**audio_stream(), "codec_name": None}, id="codec_nulo"),
        pytest.param(audio_stream(codec_name="no valido!"), id="codec_con_caracteres_raros"),
        pytest.param(audio_stream(sample_rate="0"), id="sin_frecuencia"),
        pytest.param(audio_stream(channels=0), id="sin_canales"),
        pytest.param({**audio_stream(), "sample_rate": None}, id="frecuencia_ausente"),
    ],
)
def test_an_audio_stream_without_codec_or_parameters_is_not_decodable(
    stream: dict[str, object],
) -> None:
    (parsed,) = parse_ffprobe({"streams": [stream]}).streams

    assert parsed.kind == "audio"
    assert parsed.decodable is False
    assert not has_decodable_stream(parse_ffprobe({"streams": [stream]}))


def test_an_unidentified_codec_is_reported_as_unknown() -> None:
    (parsed,) = parse_ffprobe({"streams": [audio_stream(codec_name="none")]}).streams
    assert parsed.codec == UNKNOWN_CODEC


def test_a_video_stream_without_size_is_not_decodable() -> None:
    video = {"index": 0, "codec_name": "h264", "codec_type": "video", "width": 0, "height": 240}

    (parsed,) = parse_ffprobe({"streams": [video]}).streams

    assert (parsed.kind, parsed.codec, parsed.decodable) == ("video", "h264", False)
    assert (parsed.width, parsed.height) == (None, 240)


def test_other_streams_are_decodable_when_the_codec_is_identified() -> None:
    subtitle = {"index": 3, "codec_name": "mov_text", "codec_type": "subtitle"}
    timer = {"index": 4, "codec_name": "none", "codec_type": "data"}

    info = parse_ffprobe({"streams": [subtitle, timer]})

    assert [(s.kind, s.codec, s.decodable) for s in info.streams] == [
        ("other", "mov_text", True),
        ("other", UNKNOWN_CODEC, False),
    ]
    # Subtítulos y datos no bastan: un contenedor necesita audio o video (Req 16.2)
    assert not has_decodable_stream(info)


def test_a_channel_layout_with_control_characters_is_dropped() -> None:
    clean = audio_stream(channel_layout="2 channels (FL@izquierda+FR)")
    dirty = audio_stream(channel_layout="FL\x00FR")
    long = audio_stream(channel_layout="x" * 513)

    layouts = [
        parse_ffprobe({"streams": [s]}).streams[0].channel_layout for s in (clean, dirty, long)
    ]

    assert layouts == ["2 channels (FL@izquierda+FR)", None, None]


def test_dispositions_other_than_one_do_not_mark_the_default() -> None:
    marks = [1, 2, 0, True, None, "si"]

    parsed = [
        parse_ffprobe({"streams": [audio_stream(disposition={"default": mark})]})
        .streams[0]
        .is_default
        for mark in marks
    ]

    assert parsed == [True, False, False, False, False, False]
    assert (
        parse_ffprobe({"streams": [audio_stream(disposition=None)]}).streams[0].is_default is False
    )


@pytest.mark.parametrize(
    "document",
    [
        pytest.param([], id="lista"),
        pytest.param("texto", id="texto"),
        pytest.param({"streams": {}}, id="flujos_no_lista"),
        pytest.param({"streams": ["audio"]}, id="flujo_no_objeto"),
        pytest.param({"streams": [{"codec_type": "audio"}]}, id="flujo_sin_indice"),
        pytest.param({"streams": [{"index": -1}]}, id="indice_negativo"),
        pytest.param({"streams": [{"index": True}]}, id="indice_booleano"),
        pytest.param({"streams": [], "format": []}, id="contenedor_no_objeto"),
    ],
)
def test_a_document_that_is_not_ffprobe_output_raises(document: object) -> None:
    with pytest.raises(FfprobeError):
        parse_ffprobe(document)


def test_a_document_without_streams_has_no_decodable_stream() -> None:
    info = parse_ffprobe({})

    assert info == MediaInfo(duration_ms=0, streams=())
    assert not has_decodable_stream(info)


# --- Comando de ffprobe ---


@pytest.mark.req("13.7")
@pytest.mark.parametrize(
    ("real_type", "demuxer"),
    [("MP3", "mp3"), ("WAV", "wav"), ("OGG", "ogg"), ("MP4", "mp4"), ("WEBM", "matroska")],
)
def test_ffprobe_forces_the_demuxer_and_only_the_file_protocol(
    real_type: str, demuxer: str
) -> None:
    command = ffprobe_command(Path("/ws/source/original"), real_type)

    assert command[0] == "ffprobe"
    assert command[-6:] == [
        "-protocol_whitelist",
        "file",
        "-f",
        demuxer,
        "-i",
        "/ws/source/original",
    ]
    assert command[1:-6] == [
        "-hide_banner",
        "-loglevel",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
    ]


@pytest.mark.req("13.7")
@pytest.mark.parametrize("real_type", [None, "", "PNG", "mp4", "TXT"])
def test_ffprobe_never_guesses_the_format(real_type: str | None) -> None:
    with pytest.raises(ValueError, match="demuxer"):
        ffprobe_command(Path("/ws/source/original"), real_type)


# --- Inspector con un ffprobe de prueba ---


def install_ffprobe(
    bin_dir: Path, *, stdout: str = "", stderr: str = "", exit_code: int = 0, kill: bool = False
) -> Path:
    """Crea `bin_dir/ffprobe`, un script que registra sus argumentos y escribe una salida fija.

    Devuelve el archivo donde queda un argumento por línea. Con `kill` el script termina con
    SIGKILL, como un ffprobe que el sistema detuvo.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
    (bin_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    arguments = bin_dir / "arguments.txt"
    ending = "kill -KILL $$" if kill else f"exit {exit_code}"
    script = bin_dir / "ffprobe"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > {shlex.quote(str(arguments))}\n'
        f"cat {shlex.quote(str(bin_dir / 'stdout.txt'))}\n"
        f"cat {shlex.quote(str(bin_dir / 'stderr.txt'))} >&2\n"
        f"{ending}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return arguments


def path_with(bin_dir: Path) -> str:
    return os.pathsep.join([str(bin_dir), os.environ.get("PATH", os.defpath)])


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "ws" / "source" / "original"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"contenido que el ffprobe de prueba no lee")
    return path


def run_inspector(source: Path, real_type: str | None = "MP4") -> SourceInfo | SourceRejection:
    return inspect_media(InspectorInput(path=source, real_type=real_type, max_image_pixels=1))


def ffprobe_json(document: Mapping[str, object]) -> str:
    return json.dumps(document)


@pytest.mark.req("10.4", "16.2")
def test_inspector_runs_ffprobe_with_the_forced_demuxer_and_returns_the_media_info(
    tmp_path: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = install_ffprobe(tmp_path / "bin", stdout=ffprobe_json(MP4_TWO_AUDIO_TRACKS))
    monkeypatch.setenv("PATH", path_with(tmp_path / "bin"))

    result = run_inspector(source, "WEBM")

    assert result == parse_ffprobe(MP4_TWO_AUDIO_TRACKS)
    assert (
        arguments.read_text(encoding="utf-8").split("\n")[:-1]
        == ffprobe_command(source, "WEBM")[1:]
    )


@pytest.mark.req("16.2")
@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"streams": [], "format": {"duration": "1.0"}}, id="sin_flujos"),
        pytest.param({"format": {}}, id="sin_lista_de_flujos"),
        pytest.param({"streams": [audio_stream(codec_name="unknown")]}, id="codec_desconocido"),
        pytest.param(
            {"streams": [{"index": 0, "codec_name": "mov_text", "codec_type": "subtitle"}]},
            id="solo_subtitulos",
        ),
        pytest.param(
            {
                "streams": [
                    {
                        "index": 0,
                        "codec_name": "mjpeg",
                        "codec_type": "video",
                        "width": 5,
                        "height": 5,
                        "disposition": {"attached_pic": 1},
                    }
                ]
            },
            id="solo_caratula",
        ),
    ],
)
def test_a_container_without_decodable_audio_or_video_is_damaged(
    tmp_path: Path, source: Path, monkeypatch: pytest.MonkeyPatch, document: dict[str, object]
) -> None:
    install_ffprobe(tmp_path / "bin", stdout=ffprobe_json(document))
    monkeypatch.setenv("PATH", path_with(tmp_path / "bin"))

    assert run_inspector(source) == SourceRejection(FailureReason.DAMAGED)


@pytest.mark.req("16.2")
@pytest.mark.parametrize(
    "stderr",
    [
        "[mov,mp4,m4a,3gp,3g2,mj2 @ 0x55] moov atom not found\n"
        "/ws/source/original: Invalid data found when processing input\n",
        "[matroska,webm @ 0x55] EBML header parsing failed\n",
        "error de ffprobe que no está en la tabla de patrones\n",
        "",
    ],
)
def test_ffprobe_failing_without_streams_is_a_damaged_file(
    tmp_path: Path, source: Path, monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    install_ffprobe(tmp_path / "bin", stderr=stderr, exit_code=1)
    monkeypatch.setenv("PATH", path_with(tmp_path / "bin"))

    result = run_inspector(source)

    assert isinstance(result, SourceRejection)
    assert result.reason is FailureReason.DAMAGED
    assert result.params == {}


@pytest.mark.parametrize(
    ("kwargs", "stdout"),
    [
        pytest.param({"kill": True}, "", id="senal"),
        pytest.param({"stderr": "No space left on device\n", "exit_code": 1}, "", id="sin_espacio"),
        pytest.param({}, "esto no es JSON", id="salida_no_json"),
        pytest.param({}, "", id="salida_vacia"),
        pytest.param({}, "[1, 2]", id="json_que_no_es_ffprobe"),
    ],
)
def test_ffprobe_failures_that_are_not_the_file_raise(
    tmp_path: Path,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, object],
    stdout: str,
) -> None:
    install_ffprobe(tmp_path / "bin", stdout=stdout, **kwargs)  # type: ignore[arg-type]
    monkeypatch.setenv("PATH", path_with(tmp_path / "bin"))

    with pytest.raises(FfprobeError) as error:
        run_inspector(source)

    # El mensaje no cita la salida de ffprobe, que tiene datos del archivo
    assert "esto no es JSON" not in str(error.value)
    assert "No space left" not in str(error.value)


def test_inspector_without_ffprobe_raises_instead_of_rejecting(
    tmp_path: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    with pytest.raises(FileNotFoundError):
        run_inspector(source)


@pytest.mark.req("13.7")
@pytest.mark.parametrize("real_type", [None, "PDF"])
def test_inspector_without_a_demuxer_does_not_run_ffprobe(
    tmp_path: Path, source: Path, monkeypatch: pytest.MonkeyPatch, real_type: str | None
) -> None:
    arguments = install_ffprobe(tmp_path / "bin", stdout=ffprobe_json(MP3_WITH_COVER))
    monkeypatch.setenv("PATH", path_with(tmp_path / "bin"))

    with pytest.raises(ValueError, match="demuxer"):
        run_inspector(source, real_type)

    assert not arguments.exists()


# --- Worker ---


def run_worker(
    source: Path, bin_dir: Path, real_type: str = "MP4"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-m", MODULE, "--real-type", real_type, "--", "media", str(source)],
        env={"PATH": path_with(bin_dir)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.req("10.4", "16.2")
def test_worker_writes_the_result_of_the_media_inspector(tmp_path: Path, source: Path) -> None:
    bin_dir = tmp_path / "bin"
    install_ffprobe(bin_dir, stdout=ffprobe_json(MP4_TWO_AUDIO_TRACKS))

    completed = run_worker(source, bin_dir)

    assert completed.returncode == 0, completed.stderr
    assert load_inspector_output(completed.stdout) == parse_ffprobe(MP4_TWO_AUDIO_TRACKS)

    install_ffprobe(bin_dir, stderr="moov atom not found\n", exit_code=1)
    damaged = run_worker(source, bin_dir)
    assert damaged.returncode == 0, damaged.stderr
    assert load_inspector_output(damaged.stdout) == SourceRejection(FailureReason.DAMAGED)


def test_worker_exits_with_the_traceback_when_ffprobe_is_killed(
    tmp_path: Path, source: Path
) -> None:
    install_ffprobe(tmp_path / "bin", kill=True)

    completed = run_worker(source, tmp_path / "bin")

    # El proceso principal clasifica esta salida como `error_interno`
    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "FfprobeError" in completed.stderr


# --- ffprobe real ---


def write_wav(path: Path, *, frames: int = 4000, channels: int = 2, rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(struct.pack("<h", 0) * frames * channels)


@pytest.mark.requires_tool("ffprobe")
@pytest.mark.req("9.4", "10.4", "13.7")
def test_real_ffprobe_reads_a_wav(tmp_path: Path) -> None:
    path = tmp_path / "original"
    write_wav(path, frames=4000, channels=2, rate=8000)

    result = run_inspector(path, "WAV")

    assert isinstance(result, MediaInfo)
    assert result.duration_ms == 500
    (stream,) = result.streams
    assert (stream.index, stream.kind, stream.codec) == (0, "audio", "pcm_s16le")
    assert (stream.decodable, stream.channels, stream.sample_rate) == (True, 2, 8000)
    assert (stream.width, stream.height) == (None, None)


@pytest.mark.requires_tool("ffprobe")
@pytest.mark.req("16.2")
@pytest.mark.parametrize("real_type", ["WAV", "OGG", "MP4", "WEBM"])
def test_real_ffprobe_rejects_bytes_that_are_not_the_format(tmp_path: Path, real_type: str) -> None:
    path = tmp_path / "original"
    path.write_bytes(bytes(range(256)) * 8)

    assert run_inspector(path, real_type) == SourceRejection(FailureReason.DAMAGED)


@pytest.mark.requires_tool("ffprobe")
@pytest.mark.req("13.7")
def test_real_ffprobe_does_not_follow_a_playlist_to_other_files(tmp_path: Path) -> None:
    # Con el demuxer de WAV forzado, una lista `concat` no se interpreta como tal
    wav = tmp_path / "secret.wav"
    write_wav(wav)
    path = tmp_path / "original"
    path.write_text(f"ffconcat version 1.0\nfile {wav}\n", encoding="utf-8")

    assert run_inspector(path, "WAV") == SourceRejection(FailureReason.DAMAGED)
