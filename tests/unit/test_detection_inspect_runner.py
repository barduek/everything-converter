"""Pruebas de src/converter/detection/inspect_runner.py: la función de inspección que ejecuta los
inspectores en subprocesos (Req 2.11, 16.2, 16.8, 16.9, 19.10).

Las pruebas de subproceso ejecutan el worker real `converter.workers.inspect` a través del
Ejecutor aislado, con el inspector `fake` de `tests/inspector_fakes.py` registrado en ambos
procesos. Las del semáforo y de las salidas inválidas usan un ejecutor de prueba que entrega una
salida fija, sin subprocesos, para controlar cuándo termina cada inspección.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from converter.detection.detector import Inspect, InspectionRequest
from converter.detection.inspect_runner import (
    INSPECT_TIMEOUT_SECONDS,
    MAX_CONCURRENT_INSPECTIONS,
    MAX_OUTPUT_CHARS,
    InspectRunner,
)
from converter.detection.inspectors import register_inspector
from converter.detection.source_info import (
    SourceInfo,
    SourceRejection,
    dump_inspector_output,
)
from converter.eventlog import ErrorDetail, ToolDetail
from converter.messages_es import FailureReason
from converter.sandbox.limits import Limits, SandboxTool, tool_limits
from converter.sandbox.runner import (
    KILL_GRACE_SECONDS,
    CompletedRun,
    SandboxRunner,
    minimal_env,
    python_module_command,
)
from converter.storage.workspace import Workspace, WorkspaceManager
from converter.workers import inspect as worker
from tests import inspector_fakes as fakes
from tests.inspector_fakes import FAKE, FakeInfo

MAX_PIXELS = 1234
_TEST_TIMEOUT_S = 60  # tope de cada inspección en estas pruebas

LineCallback = Callable[[str], None]


@pytest.fixture(autouse=True)
def registered(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes.isolate_registries(monkeypatch)
    fakes.register_fakes()


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    storage = tmp_path / "storage"
    storage.mkdir()
    return WorkspaceManager(storage).create()


@pytest.fixture
def fake_python(tmp_path: Path) -> str:
    return str(fakes.worker_wrapper(tmp_path))


def _request(workspace: Workspace, command: str, inspector: str = FAKE) -> InspectionRequest:
    workspace.source_file.write_text(command, encoding="ascii")
    return InspectionRequest(inspector=inspector, real_type="PNG", workspace=workspace)


def _info(workspace: Workspace, command: str, values: tuple[int, ...] = ()) -> FakeInfo:
    return FakeInfo(
        command=command,
        path=str(workspace.source_file),
        real_type="PNG",
        max_image_pixels=MAX_PIXELS,
        values=values,
    )


async def _inspect(
    inspect: InspectRunner, request: InspectionRequest
) -> SourceInfo | SourceRejection:
    async with asyncio.timeout(_TEST_TIMEOUT_S):
        return await inspect(request)


def _pattern_id(detail: ToolDetail) -> str | None:
    return None if detail.pattern is None else detail.pattern.id


def _internal(result: object, exception: str) -> ErrorDetail:
    """Comprueba que `result` es `error_interno` con el ErrorDetail de `exception`."""
    assert isinstance(result, SourceRejection)
    assert result.reason is FailureReason.INTERNAL_ERROR
    assert isinstance(result.detail, ErrorDetail)
    assert result.detail.exception.rpartition(".")[2] == exception
    return result.detail


class ScriptedRunner(SandboxRunner):
    """Ejecutor sin subprocesos: registra cada llamada y entrega una salida fija.

    Si `gate` está, cada ejecución espera a que se fije antes de entregar la salida, y el
    ejecutor cuenta las que corren a la vez.
    """

    def __init__(
        self,
        stdout: Sequence[str] = (),
        *,
        returncode: int = 0,
        stderr: Sequence[str] = (),
        gate: asyncio.Event | None = None,
    ) -> None:
        super().__init__()
        self.stdout = tuple(stdout)
        self.returncode = returncode
        self.stderr = tuple(stderr)
        self.gate = gate
        self.calls: list[tuple[list[str], Path, Limits, dict[str, str]]] = []
        self.running = 0
        self.peak = 0

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        limits: Limits,
        env: Mapping[str, str],
        on_stdout_line: LineCallback | None = None,
        on_stderr_line: LineCallback | None = None,
    ) -> CompletedRun:
        self.calls.append((list(argv), cwd, limits, dict(env)))
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            if self.gate is not None:
                await self.gate.wait()
        finally:
            self.running -= 1
        assert on_stdout_line is not None
        for line in self.stdout:
            on_stdout_line(line)
        return CompletedRun(returncode=self.returncode, stderr_lines=self.stderr)


def test_defaults_follow_the_design() -> None:
    assert INSPECT_TIMEOUT_SECONDS == 8
    assert MAX_CONCURRENT_INSPECTIONS == 4
    # Es la función de inspección que recibe el Detector_de_Tipo (mypy comprueba el tipo)
    inspect: Inspect = InspectRunner(SandboxRunner(), max_image_pixels=MAX_PIXELS, cpus=1)
    assert callable(inspect)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_image_pixels": 0},
        {"max_image_pixels": 10, "timeout_seconds": 0},
        {"max_image_pixels": 10, "timeout_seconds": float("nan")},
        {"max_image_pixels": 10, "max_concurrent": 0},
    ],
)
def test_invalid_settings_raise(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        InspectRunner(SandboxRunner(), cpus=1, **kwargs)  # type: ignore[arg-type]


# --- Invocación del worker ---


@pytest.mark.asyncio
@pytest.mark.req("2.11")
async def test_runs_the_worker_in_the_sandbox_with_python_limits(workspace: Workspace) -> None:
    info = _info(workspace, fakes.INFO)
    runner = ScriptedRunner([dump_inspector_output(info)])
    inspect = InspectRunner(runner, max_image_pixels=MAX_PIXELS, cpus=2, python="/py")

    assert await _inspect(inspect, _request(workspace, fakes.INFO)) == info

    ((argv, cwd, limits, env),) = runner.calls
    source = str(workspace.source_file)
    assert argv == python_module_command(
        worker.MODULE,
        *("--real-type", "PNG", "--max-image-pixels", str(MAX_PIXELS), "--", FAKE, source),
        python="/py",
    )
    assert cwd == workspace.tmp_dir
    assert env == minimal_env(workspace.tmp_dir)
    assert limits == tool_limits(SandboxTool.PYTHON, timeout_seconds=8, cpus=2)


@pytest.mark.asyncio
async def test_the_inspector_declares_the_sandbox_limits(workspace: Workspace) -> None:
    register_inspector("probe_media", info_type=FakeInfo, sandbox_tool=SandboxTool.FFMPEG)(
        fakes.fake_inspector
    )
    runner = ScriptedRunner([dump_inspector_output(_info(workspace, fakes.INFO))])
    inspect = InspectRunner(runner, max_image_pixels=MAX_PIXELS, cpus=1)
    await _inspect(inspect, _request(workspace, fakes.INFO, inspector="probe_media"))
    ((_, _, limits, _),) = runner.calls
    assert limits == tool_limits(SandboxTool.FFMPEG, timeout_seconds=8, cpus=1)


# --- Errores sin subproceso o con una salida inválida ---


@pytest.mark.asyncio
@pytest.mark.req("16.9")
async def test_unknown_inspector_is_an_internal_error_without_launching(
    workspace: Workspace,
) -> None:
    runner = ScriptedRunner()
    inspect = InspectRunner(runner, max_image_pixels=MAX_PIXELS, cpus=1)
    result = await _inspect(inspect, _request(workspace, fakes.INFO, inspector="no_existe"))
    _internal(result, "UnknownInspectorError")
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.req("16.9")
@pytest.mark.parametrize(
    "stdout",
    [
        pytest.param([], id="sin salida"),
        pytest.param(["no es JSON"], id="texto"),
        pytest.param(['{"info": {"type": "fake", "data": {}}}'], id="faltan campos"),
        pytest.param(['{"rejection": {"reason": "tipo_sin_destinos", "params": {}}}'], id="motivo"),
        pytest.param(['{"info": {"type": "none", "data": {}}}'], id="otro tipo"),
        pytest.param(['{"rejection": {"reason": "archivo_danado", "params": {}}}'] * 2, id="dos"),
    ],
)
async def test_invalid_output_is_an_internal_error(workspace: Workspace, stdout: list[str]) -> None:
    inspect = InspectRunner(ScriptedRunner(stdout), max_image_pixels=MAX_PIXELS, cpus=1)
    result = await _inspect(inspect, _request(workspace, fakes.INFO))
    _internal(result, "InspectorOutputError")


@pytest.mark.asyncio
@pytest.mark.req("16.9")
async def test_output_over_the_cap_is_an_internal_error(workspace: Workspace) -> None:
    runner = ScriptedRunner(["{", "x" * MAX_OUTPUT_CHARS])
    inspect = InspectRunner(runner, max_image_pixels=MAX_PIXELS, cpus=1)
    _internal(await _inspect(inspect, _request(workspace, fakes.INFO)), "InspectorOutputError")


@pytest.mark.asyncio
@pytest.mark.req("16.2", "16.9")
@pytest.mark.parametrize(
    ("stderr", "returncode", "reason", "pattern"),
    [
        pytest.param(
            ["OSError: cannot identify image file"],
            1,
            FailureReason.DAMAGED,
            "unidentified_image",
            id="dañado",
        ),
        # El Límite_de_Píxeles necesita las Dimensiones_Mostradas, que solo da el inspector
        pytest.param(
            ["PIL.Image.DecompressionBombError: Image size"],
            1,
            FailureReason.INTERNAL_ERROR,
            "decompression_bomb",
            id="sin parámetros",
        ),
        # La falta de espacio no es un Motivo_de_Fallo de la etapa detección
        pytest.param(
            ["OSError: [Errno 28] No space left on device"],
            1,
            FailureReason.INTERNAL_ERROR,
            "no_space",
            id="sin espacio",
        ),
        pytest.param([], -9, FailureReason.INTERNAL_ERROR, None, id="señal"),
        pytest.param(["Traceback"], 1, FailureReason.INTERNAL_ERROR, None, id="no clasificado"),
    ],
)
async def test_failed_worker_is_classified_from_its_sanitized_output(
    workspace: Workspace,
    stderr: list[str],
    returncode: int,
    reason: FailureReason,
    pattern: str | None,
) -> None:
    runner = ScriptedRunner(returncode=returncode, stderr=stderr)
    inspect = InspectRunner(runner, max_image_pixels=MAX_PIXELS, cpus=1)
    result = await _inspect(inspect, _request(workspace, fakes.INFO))
    assert result == SourceRejection(reason)
    assert isinstance(result, SourceRejection) and isinstance(result.detail, ToolDetail)
    assert _pattern_id(result.detail) == pattern
    assert (result.detail.exit_code, result.detail.signal) == (
        (None, 9) if returncode < 0 else (returncode, None)
    )


@pytest.mark.asyncio
@pytest.mark.req("16.9")
async def test_failed_worker_without_a_declared_tool_is_an_internal_error(
    workspace: Workspace,
) -> None:
    register_inspector("no_tool", info_type=FakeInfo)(fakes.fake_inspector)
    runner = ScriptedRunner(returncode=1, stderr=["OSError: cannot identify image file"])
    inspect = InspectRunner(runner, max_image_pixels=MAX_PIXELS, cpus=1)
    result = await _inspect(inspect, _request(workspace, fakes.INFO, inspector="no_tool"))
    _internal(result, "InspectorProcessError")


@pytest.mark.asyncio
@pytest.mark.req("16.9")
async def test_worker_that_cannot_start_is_an_internal_error(workspace: Workspace) -> None:
    workspace.tmp_dir.rmdir()  # directorio de trabajo del subproceso
    inspect = InspectRunner(SandboxRunner(), max_image_pixels=MAX_PIXELS, cpus=1)
    result = await _inspect(inspect, _request(workspace, fakes.INFO))
    assert _internal(result, "FileNotFoundError").errno == "ENOENT"


# --- Semáforo ---


@pytest.mark.asyncio
@pytest.mark.req("19.10")
async def test_at_most_four_inspections_run_at_once(workspace: Workspace) -> None:
    gate = asyncio.Event()
    info = _info(workspace, fakes.INFO)
    runner = ScriptedRunner([dump_inspector_output(info)], gate=gate)
    inspect = InspectRunner(runner, max_image_pixels=MAX_PIXELS, cpus=1)
    request = _request(workspace, fakes.INFO)

    tasks = [asyncio.create_task(inspect(request)) for _ in range(MAX_CONCURRENT_INSPECTIONS + 3)]
    for _ in range(50):  # el bucle no tiene otro trabajo: las que pueden empezar ya empezaron
        await asyncio.sleep(0)
    assert (len(runner.calls), runner.running) == (4, 4)

    gate.set()
    async with asyncio.timeout(_TEST_TIMEOUT_S):
        results = await asyncio.gather(*tasks)
    assert results == [info] * 7
    assert (len(runner.calls), runner.peak) == (7, 4)


@pytest.mark.asyncio
@pytest.mark.req("19.10")
async def test_a_cancelled_inspection_releases_its_place(workspace: Workspace) -> None:
    gate = asyncio.Event()
    info = _info(workspace, fakes.INFO)
    runner = ScriptedRunner([dump_inspector_output(info)], gate=gate)
    inspect = InspectRunner(runner, max_image_pixels=MAX_PIXELS, cpus=1, max_concurrent=1)
    request = _request(workspace, fakes.INFO)

    first = asyncio.create_task(inspect(request))
    second = asyncio.create_task(inspect(request))
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(runner.calls) == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()
    assert await asyncio.wait_for(second, _TEST_TIMEOUT_S) == info
    assert runner.peak == 1


# --- Subproceso real con el worker ---


@pytest.mark.asyncio
@pytest.mark.req("2.11")
@pytest.mark.parametrize(
    "command",
    [
        pytest.param(fakes.INFO, id="información"),
        # Lo que el inspector imprime en stdout no se mezcla con el resultado
        pytest.param(fakes.NOISY, id="ruido en stdout"),
        # Más largo que una línea del ejecutor: el worker lo parte y aquí se vuelve a unir
        pytest.param(fakes.BIG, id="resultado largo"),
    ],
)
async def test_worker_round_trip_returns_the_inspector_info(
    workspace: Workspace, fake_python: str, command: str
) -> None:
    inspect = InspectRunner(SandboxRunner(), max_image_pixels=MAX_PIXELS, python=fake_python)
    values = fakes.big_values() if command == fakes.BIG else ()
    result = await _inspect(inspect, _request(workspace, command))
    assert result == _info(workspace, command, values)


@pytest.mark.asyncio
@pytest.mark.req("16.2")
async def test_worker_round_trip_returns_the_inspector_rejection(
    workspace: Workspace, fake_python: str
) -> None:
    inspect = InspectRunner(SandboxRunner(), max_image_pixels=MAX_PIXELS, python=fake_python)
    result = await _inspect(inspect, _request(workspace, fakes.DAMAGED))
    assert result == SourceRejection(FailureReason.DAMAGED)
    assert isinstance(result, SourceRejection) and result.detail is None


@pytest.mark.asyncio
@pytest.mark.req("16.9")
async def test_info_of_another_type_is_an_internal_error(
    workspace: Workspace, fake_python: str
) -> None:
    inspect = InspectRunner(SandboxRunner(), max_image_pixels=MAX_PIXELS, python=fake_python)
    result = await _inspect(inspect, _request(workspace, fakes.WRONG_TYPE))
    _internal(result, "InspectorOutputError")


@pytest.mark.asyncio
@pytest.mark.req("16.2", "16.8", "16.9")
@pytest.mark.parametrize(
    ("command", "reason", "pattern"),
    [
        pytest.param(fakes.RAISE_DAMAGED, FailureReason.DAMAGED, "unidentified_image", id="dañado"),
        pytest.param(fakes.RAISE, FailureReason.INTERNAL_ERROR, None, id="interno"),
    ],
)
async def test_inspector_exception_is_classified_without_copying_its_output(
    workspace: Workspace,
    fake_python: str,
    command: str,
    reason: FailureReason,
    pattern: str | None,
) -> None:
    inspect = InspectRunner(SandboxRunner(), max_image_pixels=MAX_PIXELS, python=fake_python)
    result = await _inspect(inspect, _request(workspace, command))
    assert result == SourceRejection(reason)
    assert isinstance(result, SourceRejection) and isinstance(result.detail, ToolDetail)
    assert result.detail.exit_code == 1
    assert _pattern_id(result.detail) == pattern
    # El traceback cita la ruta del archivo; el detalle no copia ninguna línea
    assert result.detail.unrecognized_lines > 0
    assert str(workspace.path) not in json.dumps(result.detail.as_json())


@pytest.mark.asyncio
@pytest.mark.req("2.11", "16.9")
async def test_inspection_timeout_stops_the_worker_with_an_internal_error(
    workspace: Workspace, fake_python: str
) -> None:
    loop = asyncio.get_running_loop()
    timeout = 1.0
    inspect = InspectRunner(
        SandboxRunner(), max_image_pixels=MAX_PIXELS, timeout_seconds=timeout, python=fake_python
    )
    started = loop.time()
    result = await _inspect(inspect, _request(workspace, fakes.HANG))
    elapsed = loop.time() - started
    _internal(result, "TimeoutError")
    # El worker responde a SIGTERM: no hace falta esperar el SIGKILL
    assert timeout <= elapsed < timeout + KILL_GRACE_SECONDS
