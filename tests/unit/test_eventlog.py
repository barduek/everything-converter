"""Pruebas de src/converter/eventlog.py: formato de los eventos del Log_de_Eventos y saneamiento
del detalle técnico (Req 16.7, 16.8, 4.5, 4.6, 17.9).

Los mensajes de Pillow, pypdfium2 y del lanzador se obtienen ejecutando esas bibliotecas y el
lanzador real. FFmpeg, ffprobe, LibreOffice y whisper-cli no están fuera del contenedor, así que
sus líneas se copian de la salida de esas herramientas.
"""

from __future__ import annotations

import errno
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import traceback
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

# pypdfium2 no publica anotaciones de tipos; `pdfium` queda como Any
import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pytest
from PIL import Image

from converter.eventlog import (
    PATTERNS,
    ErrorDetail,
    EventLog,
    Level,
    RegistryEntryKind,
    Tool,
    describe_exception,
    format_ts,
    sanitize_tool_output,
)
from converter.messages_es import FailureReason
from converter.models import Stage
from converter.sandbox import launch

SECRET = "contenido-privado-7f3a"  # fragmento de un archivo que no debe llegar al log
FIXED_TS = datetime(2025, 1, 15, 10, 32, 7, 412987, tzinfo=UTC)


def _log() -> tuple[EventLog, io.StringIO]:
    stream = io.StringIO()
    return EventLog(stream, clock=lambda: FIXED_TS), stream


def _events(stream: io.StringIO) -> list[dict[str, Any]]:
    text = stream.getvalue()
    assert text.endswith("\n")
    return [json.loads(line) for line in text.splitlines()]


def _traceback_lines(action: Callable[[], object]) -> list[str]:
    """Líneas que imprime un subproceso Python al terminar por la excepción de `action`."""
    try:
        action()
    except Exception as exc:
        return "".join(traceback.format_exception(exc)).splitlines()
    pytest.fail("la acción no lanzó una excepción")


# --------------------------------------------------------------------------------------------
# Formato del evento


@pytest.mark.req("16.7")
def test_ts_is_utc_with_milliseconds_and_z() -> None:
    moment = datetime(2025, 1, 15, 7, 32, 7, 412987, tzinfo=timezone(timedelta(hours=-3)))
    assert format_ts(moment) == "2025-01-15T10:32:07.412Z"
    with pytest.raises(ValueError, match="zona horaria"):
        format_ts(datetime(2025, 1, 15, 10, 32, 7))


def test_each_event_is_one_json_object_per_line_starting_with_ts_level_event() -> None:
    log, stream = _log()
    log.emit(Level.INFO, "startup", {"note": "a\nb"})
    log.emit(Level.ERROR, "other")
    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert list(first) == ["ts", "level", "event", "note"]
    assert first == {
        "ts": "2025-01-15T10:32:07.412Z",
        "level": "info",
        "event": "startup",
        "note": "a\nb",
    }
    assert json.loads(lines[1])["level"] == "error"


def test_fields_cannot_redefine_ts_level_or_event() -> None:
    log, stream = _log()
    with pytest.raises(ValueError, match="reservados"):
        log.emit(Level.INFO, "startup", {"level": "error"})
    assert stream.getvalue() == ""


def test_default_stream_is_the_standard_output(capsys: pytest.CaptureFixture[str]) -> None:
    EventLog(clock=lambda: FIXED_TS).emit(Level.INFO, "startup")
    out = capsys.readouterr().out
    assert json.loads(out) == {
        "ts": "2025-01-15T10:32:07.412Z",
        "level": "info",
        "event": "startup",
    }


def test_a_failing_stream_does_not_raise() -> None:
    stream = io.StringIO()
    stream.close()
    EventLog(stream, clock=lambda: FIXED_TS).emit(Level.INFO, "startup")


@pytest.mark.req("16.7")
def test_task_failed_has_the_fields_of_the_design_example() -> None:
    log, stream = _log()
    detail = sanitize_tool_output(
        Tool.PILLOW, ["OSError: broken data stream when reading image file"], returncode=1
    )
    log.task_failed(
        task_id="6f1c0d2e",
        real_type="PNG",
        pair=("PNG", "JPG"),
        stage=Stage.CONVERSION,
        reason="archivo_danado",
        detail=detail,
    )
    assert "PNG→JPG" in stream.getvalue()  # sin escapar, como en el ejemplo del diseño
    assert _events(stream) == [
        {
            "ts": "2025-01-15T10:32:07.412Z",
            "level": "warning",
            "event": "task_failed",
            "task_id": "6f1c0d2e",
            "real_type": "PNG",
            "pair": "PNG→JPG",
            "stage": "conversión",
            "reason": "archivo_danado",
            "detail": {
                "tool": "pillow",
                "exit_code": 1,
                "signal": None,
                "classification": "decoder_error",
                "matched": "broken data stream",
                "unrecognized_lines": 0,
            },
        }
    ]


@pytest.mark.req("16.7")
def test_task_failed_without_real_type_pair_or_detail_uses_null() -> None:
    log, stream = _log()
    log.task_failed(task_id="ab12", stage=Stage.DETECTION, reason="formato_no_reconocido")
    (event,) = _events(stream)
    assert event["real_type"] is None
    assert event["pair"] is None
    assert event["detail"] is None
    assert event["stage"] == "detección"


def _write_result_without_space() -> None:
    path = f"/var/lib/converter/storage/ws-1/result/{SECRET}.zip"
    raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), path)


@pytest.mark.req("16.7", "16.8")
def test_task_failed_in_each_stage_with_its_reason_and_detail() -> None:
    log, stream = _log()
    log.task_failed(
        task_id="a1",
        real_type="PDF",
        stage=Stage.DETECTION,
        reason=FailureReason.PROTECTED_PDF,
        detail=sanitize_tool_output(
            Tool.PYPDFIUM2,
            [
                "pypdfium2._helpers.misc.PdfiumError: Failed to load document "
                "(PDFium: Incorrect password error)."
            ],
            returncode=1,
        ),
    )
    log.task_failed(
        task_id="b2",
        real_type="MP3",
        pair=("MP3", "TXT"),
        stage=Stage.TRANSCRIPTION,
        reason=FailureReason.TIMED_OUT,
        detail=sanitize_tool_output(
            Tool.WHISPER_CPP,
            [f"[00:00.000 --> 00:02.000] {SECRET}"],
            returncode=-signal.SIGKILL,
        ),
    )
    with pytest.raises(OSError) as info:
        _write_result_without_space()
    log.task_failed(
        task_id="c3",
        real_type="PDF",
        pair=("PDF", "PNG"),
        stage=Stage.DELIVERY,
        reason=FailureReason.NO_SPACE,
        detail=describe_exception(info.value),
    )
    assert SECRET not in stream.getvalue()
    detection, transcription, delivery = _events(stream)
    fields = ["ts", "level", "event", "task_id", "real_type", "pair", "stage", "reason", "detail"]
    for event in (detection, transcription, delivery):
        assert list(event) == fields  # el orden del ejemplo del diseño
        assert (event["level"], event["event"]) == ("warning", "task_failed")

    assert (detection["pair"], detection["stage"], detection["reason"]) == (
        None,
        "detección",
        "pdf_protegido",
    )
    assert detection["detail"]["classification"] == "pdf_password_error"

    assert (transcription["pair"], transcription["stage"], transcription["reason"]) == (
        "MP3→TXT",
        "transcripción",
        "tiempo_excedido",
    )
    assert transcription["detail"] == {
        "tool": "whisper.cpp",
        "exit_code": None,
        "signal": "SIGKILL",
        "classification": None,
        "matched": None,
        "unrecognized_lines": 1,
    }

    assert (delivery["pair"], delivery["stage"], delivery["reason"]) == (
        "PDF→PNG",
        "entrega",
        "sin_espacio_proceso",
    )
    assert set(delivery["detail"]) == {"exception", "errno", "where"}
    assert (delivery["detail"]["exception"], delivery["detail"]["errno"]) == ("OSError", "ENOSPC")
    assert delivery["detail"]["where"].startswith("test_eventlog.py:")


@pytest.mark.req("4.5", "4.6")
def test_registry_rejected_names_the_entry_and_the_error() -> None:
    log, stream = _log()
    log.registry_rejected(
        entry_kind=RegistryEntryKind.PAIR,
        entry="PNG→JPG",
        error="par_duplicado",
        details=MappingProxyType({"existing": "PNG→JPG"}),
    )
    log.registry_rejected(
        entry_kind=RegistryEntryKind.PAIR,
        entry="XYZ→ABC",
        error="formato_faltante",
        details={"missing": ("XYZ", "ABC")},
    )
    first, second = _events(stream)
    assert first["event"] == "registry_rejected"
    assert first["level"] == "warning"
    assert (first["entry_kind"], first["entry"], first["error"]) == (
        "par",
        "PNG→JPG",
        "par_duplicado",
    )
    assert first["details"] == {"existing": "PNG→JPG"}
    assert second["details"] == {"missing": ["XYZ", "ABC"]}


@pytest.mark.req("17.9")
def test_cleanup_failed_logs_task_and_reason_without_the_path(tmp_path: Path) -> None:
    log, stream = _log()
    with pytest.raises(FileNotFoundError) as info:
        shutil.rmtree(tmp_path / SECRET)
    log.cleanup_failed(
        task_id="ab12", workspace="ws-" + "0" * 32, error=info.value, attempt=1, will_retry=True
    )
    log.cleanup_failed(
        task_id=None, workspace="ws-" + "0" * 32, error=info.value, attempt=4, will_retry=False
    )
    assert SECRET not in stream.getvalue()
    retry, final = _events(stream)
    assert retry["event"] == "cleanup_failed"
    assert (retry["level"], final["level"]) == ("warning", "error")
    assert (retry["task_id"], final["task_id"]) == ("ab12", None)
    assert retry["attempt"] == 1
    assert retry["detail"]["exception"] == "FileNotFoundError"
    assert retry["detail"]["errno"] == "ENOENT"
    with pytest.raises(ValueError, match="attempt"):
        log.cleanup_failed(
            task_id="ab12", workspace="ws-x", error=info.value, attempt=0, will_retry=True
        )


@pytest.mark.req("16.8")
def test_describe_exception_keeps_class_and_location_but_not_the_message() -> None:
    with pytest.raises(UnicodeDecodeError) as info:
        (SECRET.encode() + b"\xff").decode("utf-8")
    detail = describe_exception(info.value)
    assert detail.exception == "UnicodeDecodeError"
    assert detail.errno is None
    assert detail.where is not None and detail.where.startswith("test_eventlog.py:")
    assert SECRET not in json.dumps(detail.as_json())
    assert describe_exception(Image.DecompressionBombError("x")) == ErrorDetail(
        exception="PIL.Image.DecompressionBombError", errno=None, where=None
    )


# --------------------------------------------------------------------------------------------
# Saneamiento de la salida de las herramientas


def test_every_tool_has_a_closed_set_of_patterns_with_unique_ids() -> None:
    assert set(PATTERNS) == set(Tool)
    for patterns in PATTERNS.values():
        ids = [pattern.id for pattern in patterns]
        assert len(ids) == len(set(ids))
        assert all(pattern.text for pattern in patterns)
        assert ids[0] == "no_space"  # la falta de espacio tiene prioridad en toda herramienta


# Una línea real por patrón de la tabla "Clasificación de errores de herramientas"
_KNOWN_LINES = [
    (
        Tool.FFMPEG,
        "[in#0 @ 0x6000] Error opening input: Invalid data found when processing input",
        "invalid_data",
    ),
    (Tool.FFPROBE, "[mov,mp4,m4a,3gp,3g2,mj2 @ 0x5f2c] moov atom not found", "moov_atom_not_found"),
    (Tool.FFPROBE, "[matroska,webm @ 0x55d0] EBML header parsing failed", "ebml_header_failed"),
    (Tool.FFMPEG, "Decoding requested, but no decoder found for: hevc", "no_decoder"),
    (Tool.LIBREOFFICE, "Error: source file could not be loaded", "source_not_loaded"),
    (
        Tool.WHISPER_CPP,
        "whisper_init_with_params_no_state: failed to load model",
        "model_load_failed",
    ),
    (Tool.WHISPER_CPP, "error: failed to initialize whisper context", "context_init_failed"),
    (
        Tool.PYPDFIUM2,
        "PdfiumError: Failed to load document (PDFium: Incorrect password error).",
        "pdf_password_error",
    ),
    (
        Tool.PILLOW,
        "PIL.Image.DecompressionBombError: Image size (2000000000 pixels) exceeds "
        "limit of 178956970 pixels, could be decompression bomb DOS attack.",
        "decompression_bomb",
    ),
    (Tool.FFMPEG, "av_interleaved_write_frame(): No space left on device", "no_space"),
]


@pytest.mark.req("16.8")
@pytest.mark.parametrize(("tool", "line", "pattern_id"), _KNOWN_LINES)
def test_known_lines_are_recognized(tool: Tool, line: str, pattern_id: str) -> None:
    detail = sanitize_tool_output(tool, [line], returncode=1)
    assert detail.pattern is not None
    assert detail.pattern.id == pattern_id
    assert detail.pattern.text in line
    assert detail.unrecognized_lines == 0


@pytest.mark.req("16.8")
def test_only_fixed_text_is_kept_and_other_lines_are_counted() -> None:
    lines = [
        f"Error opening input file /var/lib/converter/storage/ws-1/source/{SECRET}.",
        f"    title           : {SECRET}",
        f"Error opening input files: Invalid data found when processing input {SECRET}",
        "",
    ]
    detail = sanitize_tool_output(Tool.FFMPEG, lines, returncode=1)
    assert detail.as_json() == {
        "tool": "ffmpeg",
        "exit_code": 1,
        "signal": None,
        "classification": "invalid_data",
        "matched": "Invalid data found when processing input",
        "unrecognized_lines": 3,
    }
    log, stream = _log()
    log.task_failed(task_id="ab12", stage=Stage.CONVERSION, reason="archivo_danado", detail=detail)
    assert SECRET not in stream.getvalue()


def test_highest_priority_pattern_wins_regardless_of_line_order() -> None:
    lines = [
        "Error while decoding stream #0:0: Invalid data found when processing input",
        "av_interleaved_write_frame(): No space left on device",
    ]
    for ordered in (lines, lines[::-1]):
        detail = sanitize_tool_output(Tool.FFMPEG, ordered, returncode=1)
        assert detail.pattern is not None and detail.pattern.id == "no_space"


def test_unrecognized_output_keeps_only_counts_and_signal() -> None:
    detail = sanitize_tool_output(
        Tool.WHISPER_CPP, [f"[00:00.000 --> 00:02.000] {SECRET}"] * 3, returncode=-9
    )
    assert detail.as_json() == {
        "tool": "whisper.cpp",
        "exit_code": None,
        "signal": "SIGKILL",
        "classification": None,
        "matched": None,
        "unrecognized_lines": 3,
    }
    assert sanitize_tool_output(Tool.FFPROBE, []).as_json()["exit_code"] is None
    with pytest.raises(ValueError):
        sanitize_tool_output("weasyprint", [])  # type: ignore[arg-type]


@pytest.mark.req("16.8")
@pytest.mark.parametrize(
    ("tool", "action", "pattern_id"),
    [
        (Tool.PILLOW, lambda: Image.open(io.BytesIO(SECRET.encode())), "unidentified_image"),
        (
            Tool.PYPDFIUM2,
            lambda: pdfium.PdfDocument(b"%PDF-1.4\n" + SECRET.encode()),
            "pdf_format_error",
        ),
    ],
)
def test_python_worker_tracebacks_are_recognized(
    tool: Tool, action: Callable[[], object], pattern_id: str
) -> None:
    detail = sanitize_tool_output(tool, _traceback_lines(action), returncode=1)
    assert detail.pattern is not None and detail.pattern.id == pattern_id
    assert detail.unrecognized_lines > 0


def test_launcher_messages_are_recognized() -> None:
    program = f"no-such-program-{SECRET}"
    argv = [
        sys.executable,
        "-m",
        launch.MODULE,
        *launch.format_args(
            launch.LaunchSpec(
                address_space_mb=4000, cpu_seconds=10, file_size_mb=10, argv=(program,)
            )
        ),
    ]
    run = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    detail = sanitize_tool_output(Tool.FFMPEG, run.stderr.splitlines(), returncode=run.returncode)
    assert detail.exit_code == launch.EXIT_NOT_FOUND
    assert detail.pattern is not None and detail.pattern.id == "launch_not_found"
    assert SECRET not in json.dumps(detail.as_json())
