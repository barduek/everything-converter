"""Pruebas de src/converter/sandbox/launch.py: argumentos, límites que ve el programa, `exec`
sobre el mismo proceso, señales restauradas y códigos de salida propios (Req 13.7, 14.6).

Las pruebas que ejecutan el lanzador lo invocan como el Ejecutor aislado, con
`python -m converter.sandbox.launch`, y usan como programa un intérprete de Python que imprime
lo que observa, o una herramienta del sistema cuando hace falta un programa que no sea Python
(el intérprete vuelve a ignorar SIGPIPE y SIGXFSZ al iniciar).
"""

from __future__ import annotations

import json
import shutil
import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from converter.sandbox.launch import (
    DEFAULT_OPEN_FILES,
    EXIT_CANNOT_EXECUTE,
    EXIT_LAUNCH_ERROR,
    EXIT_NOT_FOUND,
    MODULE,
    LaunchSpec,
    UsageError,
    format_args,
    parse_args,
)

_MB = 1_000_000  # 1 MB = 1 000 000 bytes, como en las Variables_de_Configuración
_TIMEOUT_S = 60

# Imprime en JSON el PID y los límites blandos y duros del proceso
_PROBE = """\
import json, os, resource
names = ("RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NOFILE")
limits = {name: resource.getrlimit(getattr(resource, name)) for name in names}
print(json.dumps({"pid": os.getpid(), **limits}))
"""

_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="usa /proc o límites que solo Linux aplica"
)


def _launcher(args: Sequence[str]) -> list[str]:
    return [sys.executable, "-m", MODULE, *args]


def _run(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _launcher(args), cwd=cwd, capture_output=True, text=True, timeout=_TIMEOUT_S, check=False
    )


def _spec(*argv: str, as_mb: int = 4000, cpu_s: int = 30, fsize_mb: int = 5) -> LaunchSpec:
    return LaunchSpec(
        address_space_mb=as_mb, cpu_seconds=cpu_s, file_size_mb=fsize_mb, argv=tuple(argv)
    )


# --- Argumentos ---


@pytest.mark.req("13.7")
def test_parse_args_documented_form_uses_default_open_files() -> None:
    spec = parse_args(["--as-mb", "2000", "--cpu-s", "120", "--fsize-mb", "500", "--", "ffmpeg"])
    assert spec == LaunchSpec(
        address_space_mb=2000,
        cpu_seconds=120,
        file_size_mb=500,
        open_files=DEFAULT_OPEN_FILES,
        argv=("ffmpeg",),
    )
    assert DEFAULT_OPEN_FILES == 256


@pytest.mark.req("13.7")
def test_parse_args_accepts_any_order_and_keeps_program_args_verbatim() -> None:
    args = ["--nofile", "9", "--fsize-mb", "3", "--as-mb", "1", "--cpu-s", "2", "--"]
    program = ["prog", "--as-mb", "x", "--", "-v", ""]
    spec = parse_args([*args, *program])
    assert (spec.address_space_mb, spec.cpu_seconds, spec.file_size_mb) == (1, 2, 3)
    assert spec.open_files == 9
    assert spec.argv == tuple(program)


@pytest.mark.req("13.7")
def test_format_args_is_the_inverse_of_parse_args() -> None:
    spec = LaunchSpec(
        address_space_mb=3000, cpu_seconds=7, file_size_mb=40, open_files=64, argv=("a", "--")
    )
    assert parse_args(format_args(spec)) == spec


_REQUIRED = ["--as-mb", "1", "--cpu-s", "1", "--fsize-mb", "1"]


@pytest.mark.req("13.7")
@pytest.mark.parametrize(
    "args",
    [
        pytest.param([], id="vacío"),
        pytest.param([*_REQUIRED, "prog"], id="sin-separador"),
        pytest.param([*_REQUIRED, "--"], id="sin-programa"),
        pytest.param(["--as-mb", "1", "--cpu-s", "1", "--", "prog"], id="falta-fsize"),
        pytest.param([*_REQUIRED, "--mem", "1", "--", "prog"], id="opción-desconocida"),
        pytest.param([*_REQUIRED, "--cpu-s", "2", "--", "prog"], id="opción-repetida"),
        pytest.param([*_REQUIRED, "--nofile", "--", "prog"], id="sin-valor"),
        pytest.param([*_REQUIRED, "1", "--", "prog"], id="valor-suelto"),
    ],
)
def test_parse_args_rejects_malformed_args(args: list[str]) -> None:
    with pytest.raises(UsageError):
        parse_args(args)


@pytest.mark.req("13.7")
@pytest.mark.parametrize("value", ["0", "-1", "+1", " 1", "1 ", "1.5", "1_0", "0x10", "١", ""])
def test_parse_args_rejects_values_that_are_not_positive_ascii_integers(value: str) -> None:
    with pytest.raises(UsageError):
        parse_args(["--as-mb", "1", "--cpu-s", value, "--fsize-mb", "1", "--", "prog"])


# --- Ejecución ---


@pytest.mark.req("13.7", "14.6")
def test_program_replaces_launcher_and_sees_limits(tmp_path: Path) -> None:
    spec = LaunchSpec(
        address_space_mb=4000,
        cpu_seconds=30,
        file_size_mb=5,
        open_files=64,
        argv=(sys.executable, "-c", _PROBE),
    )
    proc = subprocess.Popen(
        _launcher(format_args(spec)),
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = proc.communicate(timeout=_TIMEOUT_S)
    assert proc.returncode == 0, err
    seen = json.loads(out)
    # `exec` conserva el PID: las señales de cancelación llegan al programa real (Req 14.6)
    assert seen["pid"] == proc.pid
    assert seen["RLIMIT_CPU"] == [30, 30]
    assert seen["RLIMIT_FSIZE"] == [5 * _MB, 5 * _MB]
    assert seen["RLIMIT_NOFILE"] == [64, 64]


@_LINUX_ONLY
@pytest.mark.req("13.7")
def test_address_space_limit_is_set_and_enforced(tmp_path: Path) -> None:
    probe = _run(format_args(_spec(sys.executable, "-c", _PROBE, as_mb=4000)), tmp_path)
    assert probe.returncode == 0, probe.stderr
    assert json.loads(probe.stdout)["RLIMIT_AS"] == [4000 * _MB, 4000 * _MB]

    allocate = "bytearray(1_000_000_000)"
    result = _run(format_args(_spec(sys.executable, "-c", allocate, as_mb=500)), tmp_path)
    assert result.returncode == 1
    assert "MemoryError" in result.stderr


@pytest.mark.req("13.7")
def test_inherited_lower_hard_limit_is_kept(tmp_path: Path) -> None:
    # El lanzador externo baja RLIMIT_NOFILE a 50; el interno pide 100 y no puede ampliarlo
    inner = LaunchSpec(
        address_space_mb=4000,
        cpu_seconds=30,
        file_size_mb=5,
        open_files=100,
        argv=(sys.executable, "-c", _PROBE),
    )
    outer = LaunchSpec(
        address_space_mb=4000,
        cpu_seconds=30,
        file_size_mb=5,
        open_files=50,
        argv=tuple(_launcher(format_args(inner))),
    )
    result = _run(format_args(outer), tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["RLIMIT_NOFILE"] == [50, 50]


@pytest.mark.req("13.7")
def test_exceeding_file_size_limit_terminates_with_sigxfsz(tmp_path: Path) -> None:
    head = shutil.which("head")
    if head is None:
        pytest.skip("head no está en el PATH")
    output = tmp_path / "salida.bin"
    with output.open("wb") as stdout:
        result = subprocess.run(
            _launcher(format_args(_spec(head, "-c", "3000000", "/dev/zero", fsize_mb=1))),
            cwd=tmp_path,
            stdout=stdout,
            stderr=subprocess.PIPE,
            timeout=_TIMEOUT_S,
            check=False,
        )
    # Con SIGXFSZ ignorado, head recibiría EFBIG y saldría con código 1
    assert result.returncode == -signal.SIGXFSZ
    assert output.stat().st_size <= 1 * _MB


@_LINUX_ONLY
@pytest.mark.req("13.7")
def test_program_does_not_inherit_ignored_sigpipe_or_sigxfsz(tmp_path: Path) -> None:
    result = _run(format_args(_spec("cat", "/proc/self/status")), tmp_path)
    assert result.returncode == 0, result.stderr
    (line,) = [ln for ln in result.stdout.splitlines() if ln.startswith("SigIgn:")]
    ignored = int(line.split()[1], 16)
    for signum in (signal.SIGPIPE, signal.SIGXFSZ):
        assert not ignored & (1 << (signum - 1)), signal.Signals(signum).name


@pytest.mark.req("13.7")
def test_cpu_limit_terminates_busy_program(tmp_path: Path) -> None:
    result = _run(format_args(_spec(sys.executable, "-c", "while True: pass", cpu_s=1)), tmp_path)
    # Linux envía SIGKILL al llegar al límite duro, igual al blando; macOS envía SIGXCPU
    expected = -signal.SIGKILL if sys.platform.startswith("linux") else -signal.SIGXCPU
    assert result.returncode == expected


@pytest.mark.req("13.7")
def test_program_exit_code_is_returned(tmp_path: Path) -> None:
    result = _run(format_args(_spec(sys.executable, "-c", "raise SystemExit(7)")), tmp_path)
    assert result.returncode == 7


@pytest.mark.req("13.7")
def test_invalid_args_exit_with_launch_error(tmp_path: Path) -> None:
    result = _run(["--as-mb", "1", "--", "prog"], tmp_path)
    assert result.returncode == EXIT_LAUNCH_ERROR
    assert result.stderr.startswith(f"{MODULE}: argumentos inválidos:")


@pytest.mark.req("13.7")
def test_missing_program_exits_with_not_found(tmp_path: Path) -> None:
    result = _run(format_args(_spec("converter-programa-inexistente")), tmp_path)
    assert result.returncode == EXIT_NOT_FOUND
    assert "converter-programa-inexistente" in result.stderr


@pytest.mark.req("13.7")
def test_non_executable_program_exits_with_cannot_execute(tmp_path: Path) -> None:
    program = tmp_path / "no-ejecutable"
    program.write_text("#!/bin/sh\n")
    program.chmod(0o644)
    result = _run(format_args(_spec(str(program))), tmp_path)
    assert result.returncode == EXIT_CANNOT_EXECUTE
    assert result.stderr.startswith(f"{MODULE}: no se pudo ejecutar")
