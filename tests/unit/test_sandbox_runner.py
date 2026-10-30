"""Pruebas de src/converter/sandbox/runner.py: lanzamiento a través del lanzador, grupo de
procesos propio, entorno mínimo, lectura por líneas, cancelación y timeout (Req 13.3, 13.6, 13.7,
14.6, 14.7).

Los programas de prueba son intérpretes de Python que imprimen lo que observan, o `env` cuando
hace falta ver el entorno exactamente como lo recibe una herramienta.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from converter.eventlog import Tool, sanitize_tool_output
from converter.sandbox.launch import EXIT_NOT_FOUND, MODULE, LaunchSpec, parse_args
from converter.sandbox.limits import Limits
from converter.sandbox.runner import (
    KILL_GRACE_SECONDS,
    MAX_LINE_BYTES,
    NEUTRAL_PROXY,
    STDERR_TAIL_CHARS,
    STDERR_TAIL_LINES,
    CompletedRun,
    LineCallback,
    SandboxRunner,
    launcher_command,
    minimal_env,
    python_module_command,
)

_MB = 1_000_000
_LIMITS = Limits(address_space_mb=4000, cpu_seconds=60, file_size_mb=50, open_files=64)
_TEST_TIMEOUT_S = 60  # tope de cada espera de un subproceso en estas pruebas

# Imprime en JSON el PID, la sesión, el grupo, el directorio de trabajo y los límites
_PROBE = """\
import json, os, resource
names = ("RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NOFILE")
print(json.dumps({
    "pid": os.getpid(),
    "sid": os.getsid(0),
    "pgid": os.getpgrp(),
    "cwd": os.getcwd(),
    **{name: resource.getrlimit(getattr(resource, name)) for name in names},
}))
"""

# Ignora SIGTERM y lanza un nieto que también lo ignora; ambos esperan 120 s
_IGNORES_SIGTERM = """\
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
grandchild = subprocess.Popen([sys.executable, "-c", (
    "import signal, time\\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
    "print('nieto listo', flush=True)\\n"
    "time.sleep(120)\\n"
)])
print(f"pids {os.getpid()} {grandchild.pid}", flush=True)
time.sleep(120)
"""

# Lanza un nieto que hereda stdout y termina sin esperarlo
_LEAVES_GRANDCHILD = """\
import os, subprocess, sys
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print(f"pids {os.getpid()} {grandchild.pid}", flush=True)
"""

_SLEEPS = """\
import os, time
print(f"pids {os.getpid()}", flush=True)
time.sleep(120)
"""


@dataclass(frozen=True)
class _Workspace:
    cwd: Path  # directorio de trabajo del subproceso
    tmp: Path  # HOME y TMPDIR


@pytest.fixture
def ws(tmp_path: Path) -> _Workspace:
    work, tmp = tmp_path / "work", tmp_path / "tmp"
    work.mkdir()
    tmp.mkdir()
    return _Workspace(cwd=work, tmp=tmp)


def _is_zombie(pid: int) -> bool:
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            return True
        return stat.rpartition(")")[2].split()[0] == "Z"
    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return not out or out.startswith("Z")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # macOS puede responder EPERM ante un zombi
    return not _is_zombie(pid)


async def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    # Un nieto huérfano queda a cargo de init, que lo recoge poco después de morir
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while _alive(pid):
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


@pytest.fixture
def stray_pids() -> Iterator[list[int]]:
    # Si una prueba falla, no deja procesos de 120 s vivos
    pids: list[int] = []
    yield pids
    for pid in pids:
        if _alive(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)


def _pids(lines: list[str]) -> list[int]:
    for line in lines:
        if line.startswith("pids "):
            return [int(pid) for pid in line.split()[1:]]
    return []


async def _run(
    runner: SandboxRunner,
    argv: list[str],
    ws: _Workspace,
    *,
    on_stdout_line: LineCallback | None = None,
    on_stderr_line: LineCallback | None = None,
) -> CompletedRun:
    async with asyncio.timeout(_TEST_TIMEOUT_S):
        return await runner.run(
            argv,
            cwd=ws.cwd,
            limits=_LIMITS,
            env=minimal_env(ws.tmp),
            on_stdout_line=on_stdout_line,
            on_stderr_line=on_stderr_line,
        )


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


# --- Comando y entorno ---


@pytest.mark.req("13.7")
def test_launcher_command_runs_argv_through_isolated_launcher() -> None:
    command = launcher_command(["ffmpeg", "-i", "entrada"], _LIMITS)
    assert command[:4] == [sys.executable, "-I", "-m", MODULE]
    assert parse_args(command[4:]) == LaunchSpec(
        address_space_mb=4000,
        cpu_seconds=60,
        file_size_mb=50,
        open_files=64,
        argv=("ffmpeg", "-i", "entrada"),
    )
    assert python_module_command("converter.workers.inspect", "media") == [
        sys.executable,
        "-I",
        "-m",
        "converter.workers.inspect",
        "media",
    ]


@pytest.mark.req("13.3", "13.6", "13.7")
def test_minimal_env_has_only_the_documented_variables(tmp_path: Path) -> None:
    path = os.pathsep.join(["/usr/bin", "", "relativo", "./bin", "/bin"])
    assert minimal_env(tmp_path, path=path) == {
        "PATH": os.pathsep.join(["/usr/bin", "/bin"]),  # sin entradas relativas
        "LANG": "C.UTF-8",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "http_proxy": "http://127.0.0.1:9",
        "https_proxy": "http://127.0.0.1:9",
        "no_proxy": "",
    }
    fonts = tmp_path / "default-only.conf"
    assert minimal_env(tmp_path, fontconfig_file=fonts, path=path)["FONTCONFIG_FILE"] == str(fonts)


@pytest.mark.req("13.7")
def test_minimal_env_takes_path_from_the_converter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", os.pathsep.join(["/opt/converter/venv/bin", ".", "/usr/bin"]))
    assert minimal_env(tmp_path)["PATH"] == os.pathsep.join(["/opt/converter/venv/bin", "/usr/bin"])


# --- Ejecución ---


@pytest.mark.asyncio
@pytest.mark.req("13.7", "14.6")
async def test_child_runs_with_limits_in_its_own_session(ws: _Workspace) -> None:
    lines: list[str] = []
    result = await _run(SandboxRunner(), _python(_PROBE), ws, on_stdout_line=lines.append)
    assert result == CompletedRun(returncode=0, stderr_lines=())
    (line,) = lines
    seen = json.loads(line)
    # Líder de su sesión y de su grupo: las señales al grupo alcanzan a sus hijos (Req 14.6)
    assert seen["sid"] == seen["pid"]
    assert seen["pgid"] == seen["pid"]
    assert Path(seen["cwd"]).resolve() == ws.cwd.resolve()
    assert seen["RLIMIT_CPU"] == [60, 60]
    assert seen["RLIMIT_FSIZE"] == [50 * _MB, 50 * _MB]
    assert seen["RLIMIT_NOFILE"] == [64, 64]


@pytest.mark.asyncio
@pytest.mark.req("13.3", "13.6", "13.7")
async def test_child_receives_only_the_minimal_environment(
    ws: _Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nada del entorno del Conversor llega al subproceso
    monkeypatch.setenv("CONVERTER_SECRETO_DE_PRUEBA", "no-debe-llegar")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    env = minimal_env(ws.tmp, fontconfig_file=ws.tmp / "fonts.conf")
    lines: list[str] = []
    async with asyncio.timeout(_TEST_TIMEOUT_S):
        result = await SandboxRunner().run(
            ["env"], cwd=ws.cwd, limits=_LIMITS, env=env, on_stdout_line=lines.append
        )
    assert result.returncode == 0, result.stderr_lines
    seen = dict(line.split("=", 1) for line in lines)
    if sys.platform == "darwin":
        seen.pop("__CF_USER_TEXT_ENCODING", None)  # lo agrega macOS a cada proceso
    assert seen == env


@pytest.mark.asyncio
@pytest.mark.req("13.3", "13.6", "13.7")
async def test_proxy_variables_are_always_neutralized(ws: _Workspace) -> None:
    env = {
        "PATH": minimal_env(ws.tmp)["PATH"],
        "http_proxy": "http://proxy.example:3128",
        "no_proxy": "*",
    }
    lines: list[str] = []
    async with asyncio.timeout(_TEST_TIMEOUT_S):
        await SandboxRunner().run(
            ["env"], cwd=ws.cwd, limits=_LIMITS, env=env, on_stdout_line=lines.append
        )
    seen = dict(line.split("=", 1) for line in lines)
    assert seen["http_proxy"] == NEUTRAL_PROXY
    assert seen["https_proxy"] == NEUTRAL_PROXY
    assert seen["no_proxy"] == ""


@pytest.mark.asyncio
@pytest.mark.req("13.7")
async def test_working_directory_cannot_replace_the_launcher(ws: _Workspace) -> None:
    # Un paquete `converter` dentro del directorio de trabajo no se importa gracias a `-I`
    fake = ws.cwd / "converter" / "sandbox"
    fake.mkdir(parents=True)
    (ws.cwd / "converter" / "__init__.py").write_text("")
    (fake / "__init__.py").write_text("")
    (fake / "launch.py").write_text("print('lanzador falso'); raise SystemExit(99)\n")
    lines: list[str] = []
    result = await _run(SandboxRunner(), _python("print('ok')"), ws, on_stdout_line=lines.append)
    assert (result.returncode, lines) == (0, ["ok"])


@pytest.mark.asyncio
@pytest.mark.req("13.7")
async def test_output_is_delivered_line_by_line(ws: _Workspace) -> None:
    code = f"""\
import sys
out, err = sys.stdout.buffer, sys.stderr.buffer
out.write(b"uno\\ndos\\r\\n\\n"); out.flush()
err.write(b"e1\\ne2\\n"); err.flush()
out.write(b"x" * {MAX_LINE_BYTES + 10} + b"\\n\\xff ok\\ntres")
sys.exit(3)
"""
    stdout: list[str] = []
    stderr: list[str] = []
    result = await _run(
        SandboxRunner(),
        _python(code),
        ws,
        on_stdout_line=stdout.append,
        on_stderr_line=stderr.append,
    )
    assert result.returncode == 3
    # Sin saltos ni "\r" final, la línea larga cortada, bytes inválidos reemplazados y la última
    # línea entregada aunque no termine en un salto
    assert stdout == ["uno", "dos", "", "x" * MAX_LINE_BYTES, "\ufffd ok", "tres"]
    assert stderr == ["e1", "e2"]
    assert result.stderr_lines == ("e1", "e2")


@pytest.mark.asyncio
@pytest.mark.req("13.7")
async def test_result_keeps_a_bounded_stderr_tail(ws: _Workspace) -> None:
    runner = SandboxRunner()
    many = "import sys\nfor i in range(500): print(f'línea {i}', file=sys.stderr)\n"
    seen: list[str] = []
    result = await _run(runner, _python(many), ws, on_stderr_line=seen.append)
    assert len(seen) == 500  # el callback recibe todas
    assert result.stderr_lines == tuple(f"línea {i}" for i in range(500 - STDERR_TAIL_LINES, 500))

    width = 60_000
    wide = f"import sys\nfor i in range(10): print(str(i) * {width}, file=sys.stderr)\n"
    result = await _run(runner, _python(wide), ws)
    kept = STDERR_TAIL_CHARS // width
    assert result.stderr_lines == tuple(str(i) * width for i in range(10 - kept, 10))


@pytest.mark.asyncio
@pytest.mark.req("13.7")
@pytest.mark.parametrize(
    ("code", "returncode"),
    [
        pytest.param("raise SystemExit(7)", 7, id="código"),
        pytest.param("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", -9, id="señal"),
    ],
)
async def test_program_exit_status_is_returned(ws: _Workspace, code: str, returncode: int) -> None:
    result = await _run(SandboxRunner(), _python(code), ws)
    assert result.returncode == returncode


@pytest.mark.asyncio
@pytest.mark.req("13.7")
async def test_launcher_errors_can_be_sanitized(ws: _Workspace) -> None:
    result = await _run(SandboxRunner(), ["converter-programa-inexistente"], ws)
    assert result.returncode == EXIT_NOT_FOUND
    detail = sanitize_tool_output(Tool.FFMPEG, result.stderr_lines, returncode=result.returncode)
    assert detail.pattern is not None
    assert detail.pattern.id == "launch_not_found"


@pytest.mark.asyncio
@pytest.mark.req("13.7")
async def test_invalid_arguments_raise_before_launching(ws: _Workspace) -> None:
    runner = SandboxRunner()
    with pytest.raises(ValueError):
        await runner.run([], cwd=ws.cwd, limits=_LIMITS, env=minimal_env(ws.tmp))
    with pytest.raises(FileNotFoundError):
        await runner.run(
            _python("pass"), cwd=ws.cwd / "no-existe", limits=_LIMITS, env=minimal_env(ws.tmp)
        )


# --- Cancelación, timeout y procesos que quedan ---


@pytest.mark.asyncio
@pytest.mark.req("14.6", "14.7")
async def test_timeout_kills_child_and_grandchild_that_ignore_sigterm(
    ws: _Workspace, stray_pids: list[int]
) -> None:
    loop = asyncio.get_running_loop()
    lines: list[str] = []
    ready = asyncio.Event()

    def on_stdout(line: str) -> None:
        lines.append(line)
        if "nieto listo" in lines and _pids(lines):
            ready.set()

    deadlines: list[asyncio.Timeout] = []

    async def with_deadline() -> CompletedRun:
        # Como el Gestor_de_Tareas: el Tiempo_Máximo_de_Procesamiento es un asyncio.timeout
        async with asyncio.timeout(None) as deadline:
            deadlines.append(deadline)
            return await SandboxRunner().run(
                _python(_IGNORES_SIGTERM),
                cwd=ws.cwd,
                limits=_LIMITS,
                env=minimal_env(ws.tmp),
                on_stdout_line=on_stdout,
            )

    task = asyncio.create_task(with_deadline())
    await asyncio.wait_for(ready.wait(), _TEST_TIMEOUT_S)
    stray_pids.extend(_pids(lines))
    started = loop.time()
    deadlines[0].reschedule(started)  # el plazo vence ahora
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task, _TEST_TIMEOUT_S)
    elapsed = loop.time() - started

    # SIGTERM no los detiene: el SIGKILL al grupo sale después de la espera de 2 s
    assert KILL_GRACE_SECONDS - 0.1 <= elapsed < KILL_GRACE_SECONDS + 3
    child, grandchild = _pids(lines)
    assert await _wait_dead(child)
    assert await _wait_dead(grandchild)


@pytest.mark.asyncio
@pytest.mark.req("14.6")
async def test_cancel_stops_a_child_that_honors_sigterm_without_waiting(
    ws: _Workspace, stray_pids: list[int]
) -> None:
    loop = asyncio.get_running_loop()
    lines: list[str] = []
    ready = asyncio.Event()

    def on_stdout(line: str) -> None:
        lines.append(line)
        ready.set()

    task = asyncio.create_task(
        SandboxRunner().run(
            _python(_SLEEPS),
            cwd=ws.cwd,
            limits=_LIMITS,
            env=minimal_env(ws.tmp),
            on_stdout_line=on_stdout,
        )
    )
    await asyncio.wait_for(ready.wait(), _TEST_TIMEOUT_S)
    stray_pids.extend(_pids(lines))
    started = loop.time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert loop.time() - started < KILL_GRACE_SECONDS - 0.5
    (child,) = _pids(lines)
    assert await _wait_dead(child)


@pytest.mark.asyncio
@pytest.mark.req("14.6")
async def test_processes_left_in_the_group_are_killed_when_the_program_exits(
    ws: _Workspace, stray_pids: list[int]
) -> None:
    # El nieto mantiene abierto stdout durante 120 s; `run` no lo espera
    lines: list[str] = []
    result = await _run(
        SandboxRunner(), _python(_LEAVES_GRANDCHILD), ws, on_stdout_line=lines.append
    )
    stray_pids.extend(_pids(lines))
    assert result.returncode == 0
    _, grandchild = _pids(lines)
    assert await _wait_dead(grandchild)


@pytest.mark.asyncio
@pytest.mark.req("14.6")
async def test_callback_error_stops_the_child_and_propagates(
    ws: _Workspace, stray_pids: list[int]
) -> None:
    seen: list[str] = []

    def on_stdout(line: str) -> None:
        seen.append(line)
        stray_pids.extend(_pids([line]))
        raise RuntimeError("fallo del callback")

    with pytest.raises(RuntimeError, match="fallo del callback"):
        await _run(SandboxRunner(), _python(_SLEEPS), ws, on_stdout_line=on_stdout)
    assert len(seen) == 1  # después del error no se entregan más líneas
    (child,) = _pids(seen)
    assert await _wait_dead(child)
