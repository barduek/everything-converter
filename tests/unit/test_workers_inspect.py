"""Pruebas de src/converter/workers/inspect.py: argumentos, ejecución del inspector, escritura del
resultado en líneas acotadas y salida del módulo (Req 2.11, 16.9).

El recorrido completo en un subproceso, a través del Ejecutor aislado, está en
`test_detection_inspect_runner.py`.
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from converter.detection.inspectors import get_inspector
from converter.detection.source_info import dump_inspector_output, load_inspector_output
from converter.sandbox.runner import MAX_LINE_BYTES
from converter.workers.inspect import (
    DEFAULT_MAX_IMAGE_PIXELS,
    EXIT_USAGE,
    MODULE,
    OUTPUT_LINE_CHARS,
    WorkerArgs,
    format_args,
    parse_args,
    run_inspector,
    write_output,
)
from tests.inspector_fakes import FAKE, INFO, FakeInfo, isolate_registries, register_fakes


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate_registries(monkeypatch)
    register_fakes()


def test_parse_args_takes_name_and_path_with_defaults() -> None:
    assert parse_args(["image", "foto.png"]) == WorkerArgs(name="image", path=Path("foto.png"))
    assert DEFAULT_MAX_IMAGE_PIXELS == 100_000_000  # predeterminado de CONVERTER_MAX_IMAGE_PIXELS
    args = parse_args(["--real-type", "MP4", "--max-image-pixels", "0042", "media", "/v.mp4"])
    assert args == WorkerArgs(
        name="media", path=Path("/v.mp4"), real_type="MP4", max_image_pixels=42
    )


@pytest.mark.parametrize(
    "args",
    [
        WorkerArgs(name="image", path=Path("/ws/source/original")),
        WorkerArgs(name="media", path=Path("-raro"), real_type="WEBM", max_image_pixels=1),
    ],
)
def test_format_args_round_trips(args: WorkerArgs) -> None:
    formatted = format_args(args)
    # Los posicionales van después de "--": una ruta que empieza con "-" no es una opción
    assert formatted[-3:] == ["--", args.name, str(args.path)]
    assert parse_args(formatted) == args


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["image"],
        ["--max-image-pixels", "0", "image", "x"],
        ["--max-image-pixels", "-5", "image", "x"],
        ["--max-image-pixels", "1e3", "image", "x"],
        ["--max-image-pixels", "\u0661", "image", "x"],  # dígito no ASCII
        ["--max-image", "5", "image", "x"],  # sin abreviaturas
        ["--otra", "image", "x"],
    ],
)
def test_parse_args_rejects_invalid_arguments(args: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        parse_args(args)
    assert error.value.code == EXIT_USAGE


def test_run_inspector_passes_the_worker_arguments(fakes: None, tmp_path: Path) -> None:
    source = tmp_path / "original"
    source.write_text(INFO, encoding="ascii")
    args = WorkerArgs(name=FAKE, path=source, real_type="PNG", max_image_pixels=7)
    assert run_inspector(get_inspector(FAKE), args) == FakeInfo(
        command=INFO, path=str(source), real_type="PNG", max_image_pixels=7
    )


@pytest.mark.req("2.11")
@pytest.mark.parametrize("size", [0, 10, OUTPUT_LINE_CHARS, OUTPUT_LINE_CHARS + 1, 100_000])
def test_write_output_splits_long_results_into_bounded_lines(size: int) -> None:
    text = "".join(chr(ord("a") + i % 26) for i in range(size))
    stream = io.StringIO()
    write_output(text, stream)
    lines = stream.getvalue().splitlines()
    # El Ejecutor aislado entrega completas las líneas de hasta MAX_LINE_BYTES bytes
    assert OUTPUT_LINE_CHARS <= MAX_LINE_BYTES
    assert all(len(line) <= OUTPUT_LINE_CHARS for line in lines)
    assert len(lines) == -(-size // OUTPUT_LINE_CHARS)
    assert "".join(lines) == text
    assert stream.getvalue().endswith("\n") or size == 0


def test_written_lines_join_back_into_the_inspector_output(fakes: None) -> None:
    info = FakeInfo(
        command="big", path="/x", real_type=None, max_image_pixels=1, values=tuple(range(30_000))
    )
    stream = io.StringIO()
    write_output(dump_inspector_output(info), stream)
    assert len(stream.getvalue().splitlines()) > 1
    assert load_inspector_output("".join(stream.getvalue().splitlines())) == info


@pytest.mark.req("16.9")
def test_module_exits_with_usage_error_for_an_unknown_inspector(tmp_path: Path) -> None:
    source = tmp_path / "original"
    source.write_bytes(b"\x89PNG")
    result = subprocess.run(
        [sys.executable, "-I", "-m", MODULE, "no_existe", str(source)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == EXIT_USAGE
    assert result.stdout == ""  # sin resultado
    assert "inspector no registrado: 'no_existe'" in result.stderr
