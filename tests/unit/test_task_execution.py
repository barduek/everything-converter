"""Pruebas de la ejecución, la entrega y la retención del Gestor_de_Tareas
(src/converter/tasks/manager.py; Req 14.2, 14.4–14.8, 14.10, 15.2, 15.8–15.11, 16.3, 16.6,
16.7, 16.9–16.11, 17.2–17.4).

El Detector_de_Tipo es un doble que devuelve el Tipo_Real que fija la prueba, y el convertidor de
los pares es un doble cuyo `run` sigue el guion de cada prueba. El ejecutor aislado, el
barrendero y el Log_de_Eventos son los reales, sobre un directorio temporal y un búfer en
memoria. El reloj del Gestor_de_Tareas es simulado: la retención avanza solo cuando la prueba lo
pide. El Tiempo_Máximo_de_Procesamiento se mide con el reloj del bucle, así que las pruebas del
timeout inyectan un plazo corto.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import errno
import io
import json
import os
import shutil
import signal
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pytest
import pytest_asyncio

from converter.config import Config, load_config
from converter.converters.base import (
    ConversionContext,
    ConversionFailure,
    ConversionOutcome,
    ConversionRequest,
)
from converter.detection.source_info import DetectionResult, NoInfo, SourceInfo
from converter.eventlog import EventLog, Tool, sanitize_tool_output
from converter.messages_es import CancelReason, FailureReason, NoticeCode
from converter.models import (
    TERMINAL_STATES,
    Category,
    DestinationRejection,
    Failure,
    LossClass,
    Stage,
    TaskState,
)
from converter.registry.loader import load_catalog
from converter.registry.model import ConversionPair
from converter.registry.registry import ConversionRegistry
from converter.sandbox.limits import Limits
from converter.sandbox.runner import SandboxRunner, minimal_env
from converter.storage.sweeper import CleanupKind, Sweeper
from converter.storage.workspace import Workspace, WorkspaceManager
from converter.tasks.manager import (
    RECORD_RETENTION_SECONDS,
    DownloadUnavailable,
    ResultExpired,
    TaskManager,
    TaskUnavailable,
    processing_timeout_seconds,
)
from converter.tasks.model import CompletedUpload, ConfirmRequest, Task
from converter.tasks.view import DownloadView

CONVERTER = "test.script"
CLOCK_START = 1_000.0
SOURCE = b"contenido del origen"
RESULT = b"contenido del resultado"
PLENTY_OF_SPACE = 10**12  # bytes libres

PAIRS: tuple[tuple[str, str, LossClass], ...] = (
    ("PNG", "JPG", LossClass.LOSSY),
    ("PDF", "PNG", LossClass.LIMITED),
    ("MP3", "WAV", LossClass.LOSSLESS),
)

_LIMITS = Limits(address_space_mb=4000, cpu_seconds=60, file_size_mb=50, open_files=64)
_KILL_GRACE_S = 0.2  # espera entre SIGTERM y SIGKILL en estas pruebas; el ejecutor usa 2 s

# Ignora SIGTERM, imprime su PID y espera 120 s
_IGNORES_SIGTERM = (
    "import os, signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print(os.getpid(), flush=True)\n"
    "time.sleep(120)\n"
)


def _config(**values: str) -> Config:
    config = load_config(values)
    assert isinstance(config, Config)
    return config


def _build_registry(directory: Path) -> ConversionRegistry:
    empty = directory / "pairs.toml"
    empty.write_text("", encoding="utf-8")
    registry = load_catalog(
        [CONVERTER],
        event_log=EventLog(io.StringIO()),
        pairs_path=empty,
        extra_pairs_dir=directory / "pairs.d",
    )
    for source, target, loss_class in PAIRS:
        registry.register_pair(
            {
                "source": source,
                "target": target,
                "loss_class": loss_class.value,
                "loss_description": "" if loss_class is LossClass.LOSSLESS else "Pierde datos.",
                "converter": CONVERTER,
            }
        )
    return registry


class FakeClock:
    """Reloj monotónico que solo avanza cuando la prueba lo pide."""

    def __init__(self) -> None:
        self.now = CLOCK_START

    def __call__(self) -> float:
        return self.now


class FixedDetector:
    """Detector_de_Tipo que devuelve `real_type` sin inspector; con `gate`, espera a que se abra."""

    def __init__(self) -> None:
        self.real_type = "PNG"
        self.gate: asyncio.Event | None = None

    async def detect(self, workspace: Workspace, original_name: str) -> DetectionResult:
        if self.gate is not None:
            await self.gate.wait()
        return DetectionResult(real_type=self.real_type, info=NoInfo())


type Script = Callable[[ConversionContext], Awaitable[object]]


def produce(
    content: bytes = RESULT,
    kind: Literal["single", "zip"] = "single",
    *,
    language: str | None = None,
) -> Script:
    """Guion que escribe `content` en `work/output` y lo devuelve como resultado."""

    async def script(ctx: ConversionContext) -> ConversionOutcome:
        path = ctx.workspace.work_dir / "output"
        path.write_bytes(content)
        return ConversionOutcome(result_path=path, result_kind=kind, detected_language=language)

    return script


def fail_with(error: BaseException) -> Script:
    async def script(ctx: ConversionContext) -> ConversionOutcome:
        raise error

    return script


class Held:
    """Guion que espera hasta que la prueba libera la Tarea y después produce el resultado."""

    def __init__(self) -> None:
        self.gates: dict[str, asyncio.Event] = {}
        self.started: list[str] = []

    async def __call__(self, ctx: ConversionContext) -> object:
        self.started.append(ctx.task_id)
        await self.gates.setdefault(ctx.task_id, asyncio.Event()).wait()
        return await produce()(ctx)

    def release(self, task: Task) -> None:
        self.gates.setdefault(task.id, asyncio.Event()).set()


class ScriptedConverter:
    """Convertidor de los pares de prueba: `run` ejecuta `script` y guarda cada contexto."""

    id = CONVERTER

    def __init__(self) -> None:
        self.script: Script = produce()
        self.contexts: list[ConversionContext] = []

    def preflight(
        self, source: SourceInfo, request: ConversionRequest, config: Config
    ) -> DestinationRejection | None:
        return None

    async def run(self, ctx: ConversionContext) -> ConversionOutcome:
        self.contexts.append(ctx)
        return await self.script(ctx)  # type: ignore[return-value]  # algunos guiones no cumplen


@dataclass
class Env:
    registry: ConversionRegistry
    storage: WorkspaceManager
    sweeper: Sweeper
    log: io.StringIO
    clock: FakeClock
    detector: FixedDetector = field(default_factory=FixedDetector)
    converter: ScriptedConverter = field(default_factory=ScriptedConverter)
    config: Config = field(default_factory=_config)
    managers: list[TaskManager] = field(default_factory=list)

    def new_manager(self, **options: Any) -> TaskManager:
        options.setdefault("config", self.config)
        options.setdefault("runner", SandboxRunner(kill_grace_seconds=_KILL_GRACE_S))
        # Espacio libre de sobra, salvo en las pruebas de la falta de espacio
        options.setdefault("free_bytes", lambda path: PLENTY_OF_SPACE)
        manager = TaskManager(
            registry=self.registry,
            detector=self.detector,
            sweeper=self.sweeper,
            event_log=EventLog(self.log),
            converters={CONVERTER: self.converter},
            clock=self.clock,
            **options,
        )
        self.managers.append(manager)
        return manager

    @property
    def manager(self) -> TaskManager:
        return self.managers[0]

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log.getvalue().splitlines()]

    def pending(self) -> list[tuple[Workspace, CleanupKind]]:
        return [(job.workspace, job.kind) for job in self.sweeper.pending]


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    root = tmp_path / "storage"
    root.mkdir()
    storage = WorkspaceManager(root)
    clock = FakeClock()
    environment = Env(
        registry=_build_registry(tmp_path),
        storage=storage,
        sweeper=Sweeper(storage, EventLog(io.StringIO()), clock=clock),
        log=io.StringIO(),
        clock=clock,
    )
    environment.new_manager()
    yield environment
    for manager in environment.managers:
        await manager.aclose()


@pytest.fixture
def stray_pids() -> Iterator[list[int]]:
    # Si una prueba falla, no deja procesos de 120 s vivos
    pids: list[int] = []
    yield pids
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


async def until(condition: Callable[[], object], seconds: float = 10.0) -> None:
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.005)


async def receive(env: Env, name: str = "foto.png", manager: TaskManager | None = None) -> Task:
    manager = manager if manager is not None else env.manager
    workspace = env.storage.create()
    workspace.source_file.write_bytes(SOURCE)
    return await manager.create(CompletedUpload(workspace, name, len(SOURCE)))


async def start(
    env: Env, name: str = "foto.png", target: str = "JPG", manager: TaskManager | None = None
) -> Task:
    """Tarea recibida, detectada y confirmada: en_proceso si hay lugar, si no en_cola."""
    manager = manager if manager is not None else env.manager
    task = await receive(env, name, manager)
    await manager.confirm(task.id, ConfirmRequest(target, acknowledge_loss=True))
    assert task.state in (TaskState.QUEUED, TaskState.PROCESSING)
    return task


async def finished(task: Task, seconds: float = 10.0) -> None:
    await until(lambda: task.state in TERMINAL_STATES, seconds)


def state_of(task: Task) -> TaskState:
    """Estado actual; evita que el verificador de tipos lo fije por una aserción anterior."""
    return task.state


def hold(env: Env) -> Held:
    held = Held()
    env.converter.script = held
    return held


def run_ignoring_sigterm(pids: list[int]) -> Script:
    """Guion que lanza, con el ejecutor aislado, un subproceso que ignora SIGTERM."""

    async def script(ctx: ConversionContext) -> ConversionOutcome:
        await ctx.runner.run(
            [sys.executable, "-c", _IGNORES_SIGTERM],
            cwd=ctx.workspace.tmp_dir,
            limits=_LIMITS,
            env=minimal_env(ctx.workspace.tmp_dir),
            on_stdout_line=lambda line: pids.append(int(line)),
        )
        raise AssertionError("el subproceso terminó solo")

    return script


def absorbing_cancellation() -> Script:
    """Guion que absorbe la cancelación y devuelve un resultado como si hubiera terminado."""

    async def script(ctx: ConversionContext) -> object:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(120)
        return await produce()(ctx)

    return script


def as_script(make_outcome: Callable[[ConversionContext], object]) -> Script:
    """Guion que devuelve lo que arma `make_outcome`."""

    async def script(ctx: ConversionContext) -> object:
        return make_outcome(ctx)

    return script


def reaped(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


# --- Planificación y progreso ---


@pytest.mark.req("14.2", "14.5", "14.10")
@pytest.mark.asyncio
async def test_a_confirmed_task_starts_at_once_with_progress_at_zero(env: Env) -> None:
    held = hold(env)
    task = await start(env)

    view = env.manager.view(task.id)

    # La confirmación ya despachó la Tarea: en_proceso con 0 % desde el cambio
    assert view.state is TaskState.PROCESSING
    assert view.progress == 0
    assert task.started_at == CLOCK_START
    await until(lambda: held.started)
    (ctx,) = env.converter.contexts
    assert task.detection is not None
    assert (ctx.task_id, ctx.workspace, ctx.source) == (
        task.id,
        task.workspace,
        task.detection.info,
    )
    assert ctx.request == ConversionRequest("JPG")
    assert ctx.config is env.config
    ctx.progress.report(0.426)
    assert env.manager.view(task.id).progress == 42

    held.release(task)
    await finished(task)
    assert env.manager.view(task.id).progress is None  # solo mientras está en_proceso


@pytest.mark.req("14.4", "14.10", "19.8")
@pytest.mark.asyncio
async def test_the_queue_is_global_fifo_with_a_single_limit(env: Env) -> None:
    assert env.config.concurrency == 2
    held = hold(env)
    tasks = [await start(env, f"foto{n}.png") for n in range(4)]

    assert [task.state for task in tasks] == [TaskState.PROCESSING] * 2 + [TaskState.QUEUED] * 2
    await until(lambda: len(held.started) == 2)

    held.release(tasks[1])
    await finished(tasks[1])

    # El lugar liberado es para la Tarea que ingresó primero a en_cola
    assert [task.state for task in tasks[2:]] == [TaskState.PROCESSING, TaskState.QUEUED]
    await until(lambda: len(held.started) == 3)
    assert held.started == [task.id for task in tasks[:3]]


# --- Entrega ---


@pytest.mark.req("15.2", "15.9", "16.6", "17.2")
@pytest.mark.asyncio
async def test_the_result_is_delivered_with_its_download_name(env: Env) -> None:
    async def script(ctx: ConversionContext) -> object:
        ctx.notices.add(NoticeCode.BITS_REDUCED)
        return await produce(language="en")(ctx)

    env.converter.script = script
    task = await start(env, "Vacaciones 2024.PNG")
    await finished(task)

    info = env.manager.result(task.id)
    view = env.manager.view(task.id)

    assert task.state is TaskState.COMPLETED
    assert info.path == task.workspace.result_file
    assert info.path.read_bytes() == RESULT
    assert (info.download_name, info.media_type) == ("Vacaciones 2024.jpg", "image/jpeg")
    assert (info.size_bytes, info.completed_at, info.expired) == (len(RESULT), CLOCK_START, False)
    assert view.download == DownloadView(
        "Vacaciones 2024.jpg", f"/api/tasks/{task.id}/result", False
    )
    assert [notice.code for notice in view.notices] == ["bits_reducidos"]
    assert view.detected_language == "en"
    assert not (task.workspace.work_dir / "output").exists()
    assert env.events() == []
    # Req 17.2: la poda deja solo result/
    assert env.pending() == [(task.workspace, CleanupKind.PRUNE)]
    await env.sweeper.run_due()
    assert [entry.name for entry in task.workspace.path.iterdir()] == ["result"]
    # Req 15.9: cada descarga entrega el mismo contenido
    assert env.manager.result(task.id).path.read_bytes() == RESULT


@pytest.mark.req("15.10")
@pytest.mark.asyncio
async def test_a_zip_result_is_named_with_the_zip_extension(env: Env) -> None:
    env.detector.real_type = "PDF"
    env.converter.script = produce(b"PK\x05\x06" + bytes(18), kind="zip")
    task = await start(env, "informe.pdf", target="PNG")
    await finished(task)

    info = env.manager.result(task.id)

    assert (info.download_name, info.media_type) == ("informe.zip", "application/zip")


@pytest.mark.req("16.6", "16.9", "15.11")
@pytest.mark.asyncio
async def test_a_failed_delivery_offers_nothing_to_download(env: Env) -> None:
    async def script(ctx: ConversionContext) -> object:
        outcome = await produce()(ctx)
        shutil.rmtree(ctx.workspace.result_dir)  # mover el resultado a result/ va a fallar
        return outcome

    env.converter.script = script
    task = await start(env)
    await finished(task)

    assert task.state is TaskState.FAILED
    assert task.failure == Failure(FailureReason.INTERNAL_ERROR, Stage.DELIVERY, {})
    assert task.result is None
    assert env.manager.view(task.id).download is None
    with pytest.raises(DownloadUnavailable) as raised:
        env.manager.result(task.id)
    assert raised.value.state is TaskState.FAILED
    (event,) = env.events()
    assert (event["stage"], event["reason"]) == ("entrega", "error_interno")
    assert env.pending() == [(task.workspace, CleanupKind.REMOVE)]


# --- Fallos ---


@pytest.mark.req("16.3", "16.7", "17.4")
@pytest.mark.asyncio
async def test_a_conversion_failure_fails_the_task_with_its_reason(env: Env) -> None:
    detail = sanitize_tool_output(Tool.PILLOW, ["OSError: image file is truncated"], returncode=1)
    env.converter.script = fail_with(ConversionFailure(FailureReason.DAMAGED, detail=detail))
    task = await start(env)
    await finished(task)

    view = env.manager.view(task.id)

    assert task.state is TaskState.FAILED
    assert task.failure == Failure(FailureReason.DAMAGED, Stage.CONVERSION, {})
    assert view.failure is not None
    assert (view.failure.stage, view.failure.reason) == (Stage.CONVERSION, "archivo_danado")
    assert view.download is None
    (event,) = env.events()
    assert event["event"] == "task_failed"
    assert (event["task_id"], event["real_type"], event["pair"]) == (task.id, "PNG", "PNG→JPG")
    assert (event["stage"], event["reason"]) == ("conversión", "archivo_danado")
    assert event["detail"] == detail.as_json()
    # Req 17.4: el Espacio_de_Trabajo se borra completo, también lo que quedó en work/
    assert env.pending() == [(task.workspace, CleanupKind.REMOVE)]
    await env.sweeper.run_due()
    assert not task.workspace.path.exists()


def _missing(ctx: ConversionContext) -> object:
    return ConversionOutcome(ctx.workspace.work_dir / "output", "single")


def _directory(ctx: ConversionContext) -> object:
    (ctx.workspace.work_dir / "output").mkdir()
    return _missing(ctx)


def _outside_work(ctx: ConversionContext) -> object:
    path = ctx.workspace.tmp_dir / "output"
    path.write_bytes(RESULT)
    return ConversionOutcome(path, "single")


def _symlink(ctx: ConversionContext) -> object:
    target = ctx.workspace.tmp_dir / "output"
    target.write_bytes(RESULT)
    (ctx.workspace.work_dir / "output").symlink_to(target)
    return _missing(ctx)


def _relative(ctx: ConversionContext) -> object:
    (ctx.workspace.work_dir / "output").write_bytes(RESULT)
    return ConversionOutcome(Path("output"), "single")


def _other_type(ctx: ConversionContext) -> object:
    return str(ctx.workspace.work_dir / "output")


@pytest.mark.req("16.3", "16.9")
@pytest.mark.parametrize(
    ("make_outcome", "exception"),
    [
        (_missing, "converter.tasks.manager.MissingResultError"),
        (_directory, "converter.tasks.manager.MissingResultError"),
        (_outside_work, "converter.tasks.manager.MissingResultError"),
        (_symlink, "converter.tasks.manager.MissingResultError"),
        (_relative, "converter.tasks.manager.MissingResultError"),
        (_other_type, "TypeError"),
    ],
    ids=["ausente", "directorio", "fuera_de_work", "enlace", "ruta_relativa", "otro_tipo"],
)
@pytest.mark.asyncio
async def test_a_missing_result_fails_the_task(
    env: Env, make_outcome: Callable[[ConversionContext], object], exception: str
) -> None:
    env.converter.script = as_script(make_outcome)
    task = await start(env)
    await finished(task)

    assert task.state is TaskState.FAILED
    assert task.failure == Failure(FailureReason.INTERNAL_ERROR, Stage.CONVERSION, {})
    assert task.result is None
    (event,) = env.events()
    assert event["detail"]["exception"] == exception


@pytest.mark.req("16.9", "16.11", "16.8")
@pytest.mark.asyncio
async def test_an_unexpected_exception_only_affects_its_task(env: Env) -> None:
    runs = 0

    async def script(ctx: ConversionContext) -> object:
        nonlocal runs
        runs += 1
        if runs == 1:
            raise RuntimeError("«contenido» secreto del archivo")
        return await produce()(ctx)

    env.converter.script = script
    tasks = [await start(env, "a.png"), await start(env, "b.png")]
    for task in tasks:
        await finished(task)

    # Falla la Tarea que se ejecutó primero
    failing, working = sorted(tasks, key=lambda t: t.id != env.converter.contexts[0].task_id)
    assert failing.state is TaskState.FAILED
    assert failing.failure == Failure(FailureReason.INTERNAL_ERROR, Stage.CONVERSION, {})
    view = env.manager.view(failing.id)
    assert view.failure is not None
    assert view.failure.action == "Vuelva a subir el archivo e intente la conversión de nuevo."
    assert working.state is TaskState.COMPLETED
    (event,) = env.events()
    assert (event["task_id"], event["detail"]["exception"]) == (failing.id, "RuntimeError")
    assert "secreto" not in env.log.getvalue()  # Req 16.8: el mensaje no se registra


@pytest.mark.req("16.10")
@pytest.mark.parametrize(
    ("make_script", "free", "reason"),
    [
        (
            lambda: fail_with(OSError(errno.ENOSPC, "No space left on device")),
            10**12,
            FailureReason.NO_SPACE,
        ),
        (
            lambda: fail_with(ConversionFailure(FailureReason.INTERNAL_ERROR)),
            0,
            FailureReason.NO_SPACE,
        ),
        (lambda: as_script(_missing), 0, FailureReason.NO_SPACE),
        (
            lambda: fail_with(ConversionFailure(FailureReason.INTERNAL_ERROR)),
            10**12,
            FailureReason.INTERNAL_ERROR,
        ),
        # Un motivo propio del convertidor no se reemplaza
        (
            lambda: fail_with(ConversionFailure(FailureReason.DAMAGED)),
            0,
            FailureReason.DAMAGED,
        ),
    ],
    ids=["enospc", "error_sin_espacio_libre", "resultado_ausente", "con_espacio", "motivo_propio"],
)
@pytest.mark.asyncio
async def test_a_lack_of_space_fails_with_sin_espacio_proceso(
    env: Env, make_script: Callable[[], Script], free: int, reason: FailureReason
) -> None:
    measured: list[Path] = []

    def free_bytes(path: Path) -> int:
        measured.append(path)
        return free

    manager = env.new_manager(free_bytes=free_bytes)
    env.converter.script = make_script()
    task = await start(env, manager=manager)
    await finished(task)

    assert task.state is TaskState.FAILED
    assert task.failure == Failure(reason, Stage.CONVERSION, {})
    assert all(path == task.workspace.path for path in measured)
    (event,) = env.events()
    assert event["reason"] == reason.value


# --- Tiempo_Máximo_de_Procesamiento ---


@pytest.mark.req("14.7")
def test_the_processing_timeout_follows_the_category_or_the_transcription() -> None:
    config = _config(
        CONVERTER_TIMEOUT_IMAGE_MINUTES="1",
        CONVERTER_TIMEOUT_DOCUMENT_MINUTES="3",
        CONVERTER_TIMEOUT_AUDIO_MINUTES="4",
        CONVERTER_TIMEOUT_VIDEO_MINUTES="5",
        CONVERTER_TIMEOUT_TRANSCRIPTION_MINUTES="6",
    )

    def pair(source: str, target: str, category: Category, loss: LossClass) -> ConversionPair:
        return ConversionPair(source, target, category, loss, "", CONVERTER)

    cases = [
        (pair("PNG", "JPG", Category.IMAGE, LossClass.LOSSY), 60),
        (pair("PDF", "PNG", Category.DOCUMENT, LossClass.LIMITED), 180),
        (pair("MP3", "WAV", Category.AUDIO, LossClass.LOSSLESS), 240),
        (pair("MP4", "WEBM", Category.VIDEO, LossClass.LOSSY), 300),
        (pair("MP4", "MP3", Category.VIDEO, LossClass.LOSSY), 300),
        (pair("MP3", "TXT", Category.AUDIO, LossClass.APPROXIMATE), 360),
        (pair("MP4", "SRT", Category.VIDEO, LossClass.APPROXIMATE), 360),
    ]
    assert [processing_timeout_seconds(p, config) for p, _ in cases] == [s for _, s in cases]
    defaults = _config()
    assert processing_timeout_seconds(cases[0][0], defaults) == 2 * 60
    assert processing_timeout_seconds(cases[5][0], defaults) == 120 * 60


@pytest.mark.req("14.7")
@pytest.mark.asyncio
async def test_the_processing_timeout_stops_the_subprocess_and_fails_the_task(
    env: Env, stray_pids: list[int]
) -> None:
    timeout = 0.3
    manager = env.new_manager(processing_timeout=lambda pair, config: timeout)
    env.converter.script = run_ignoring_sigterm(stray_pids)

    begin = time.monotonic()
    task = await start(env, manager=manager)
    await finished(task)
    elapsed = time.monotonic() - begin

    # SIGTERM no lo detiene: el SIGKILL al grupo sale después de la espera (con margen para la
    # resolución de los temporizadores del bucle)
    assert timeout + _KILL_GRACE_S - 0.05 <= elapsed < timeout + 10.0
    assert task.state is TaskState.FAILED
    assert task.failure == Failure(FailureReason.TIMED_OUT, Stage.CONVERSION, {})
    (pid,) = stray_pids
    assert reaped(pid)
    (event,) = env.events()
    assert (event["reason"], event["detail"]["exception"]) == ("tiempo_excedido", "TimeoutError")
    assert env.pending() == [(task.workspace, CleanupKind.REMOVE)]


@pytest.mark.req("14.7")
@pytest.mark.asyncio
async def test_a_converter_that_ignores_the_timeout_still_fails(env: Env) -> None:
    manager = env.new_manager(processing_timeout=lambda pair, config: 0.05)
    env.converter.script = absorbing_cancellation()

    task = await start(env, manager=manager)
    await finished(task)

    assert task.state is TaskState.FAILED
    assert task.failure is not None and task.failure.reason is FailureReason.TIMED_OUT
    assert task.result is None


# --- Cancelación en_proceso y cierre ---


@pytest.mark.req("14.6", "16.11", "17.4")
@pytest.mark.asyncio
async def test_cancelling_a_running_task_stops_its_subprocess(
    env: Env, stray_pids: list[int]
) -> None:
    manager = env.new_manager(config=dataclasses.replace(env.config, concurrency=1))
    env.converter.script = run_ignoring_sigterm(stray_pids)
    running = await start(env, "a.png", manager=manager)
    queued = await start(env, "b.png", manager=manager)
    await until(lambda: stray_pids)
    assert queued.state is TaskState.QUEUED

    begin = time.monotonic()
    view = await manager.cancel(running.id)

    assert time.monotonic() - begin < 10.0
    assert view.state is TaskState.CANCELLED
    assert running.cancel_reason is CancelReason.BY_USER
    assert reaped(stray_pids[0])
    assert state_of(queued) is TaskState.PROCESSING  # el lugar liberado se usa enseguida
    assert env.events() == []
    assert (running.workspace, CleanupKind.REMOVE) in env.pending()


@pytest.mark.req("14.6")
@pytest.mark.asyncio
async def test_a_converter_that_ignores_the_cancellation_still_ends_cancelled(env: Env) -> None:
    env.converter.script = absorbing_cancellation()
    task = await start(env)
    await until(lambda: env.converter.contexts)

    view = await env.manager.cancel(task.id)

    assert view.state is TaskState.CANCELLED
    assert task.result is None
    assert env.events() == []


@pytest.mark.asyncio
async def test_closing_stops_the_running_tasks_and_starts_no_more(env: Env) -> None:
    held = hold(env)
    manager = env.new_manager(config=dataclasses.replace(env.config, concurrency=1))
    running = await start(env, "a.png", manager=manager)
    queued = await start(env, "b.png", manager=manager)
    await until(lambda: held.started)

    await manager.aclose()

    assert (running.state, queued.state) == (TaskState.PROCESSING, TaskState.QUEUED)
    assert held.started == [running.id]


# --- Retención ---


@pytest.mark.req("14.8", "17.4")
@pytest.mark.asyncio
async def test_a_received_task_is_cancelled_when_the_retention_period_ends(env: Env) -> None:
    hold(env)
    received = await receive(env, "sin-destino.png")
    await until(received.detection_done.is_set)
    running = await start(env)
    retention = env.config.retention_seconds

    env.clock.now = CLOCK_START + retention - 0.001
    await env.manager.run_retention_due()
    assert received.state is TaskState.RECEIVED
    assert env.pending() == []

    env.clock.now = CLOCK_START + retention
    await env.manager.run_retention_due()

    assert state_of(received) is TaskState.CANCELLED
    assert received.cancel_reason is CancelReason.RETENTION_EXPIRED
    assert env.pending() == [(received.workspace, CleanupKind.REMOVE)]
    assert running.state is TaskState.PROCESSING  # la retención no toca las Tareas en_proceso
    assert env.events() == []


@pytest.mark.req("14.8", "19.11")
@pytest.mark.asyncio
async def test_a_confirmation_in_progress_wins_over_the_retention(env: Env) -> None:
    hold(env)
    env.detector.gate = asyncio.Event()
    task = await receive(env)
    confirm = asyncio.create_task(
        env.manager.confirm(task.id, ConfirmRequest("JPG", acknowledge_loss=True))
    )
    await until(task.lock.locked)  # la confirmación espera la detección con el lock tomado
    env.clock.now = CLOCK_START + env.config.retention_seconds

    expire = asyncio.create_task(env.manager.run_retention_due())
    await asyncio.sleep(0.01)
    assert task.state is TaskState.RECEIVED  # la retención espera el lock
    env.detector.gate.set()
    view = await confirm
    await expire

    assert view.state is TaskState.PROCESSING
    assert state_of(task) is TaskState.PROCESSING
    assert task.cancel_reason is None


@pytest.mark.req("15.8", "15.9", "17.3")
@pytest.mark.asyncio
async def test_an_expired_result_is_rejected_and_its_workspace_removed(env: Env) -> None:
    task = await start(env)
    await finished(task)
    await env.sweeper.run_due()  # la poda de la entrega
    retention = env.config.retention_seconds

    env.clock.now = CLOCK_START + retention - 0.001
    await env.manager.run_retention_due()
    assert env.manager.result(task.id).path.read_bytes() == RESULT
    view = env.manager.view(task.id)
    assert view.download is not None and not view.download.expired
    assert env.pending() == []

    # La vista no espera a la próxima pasada para informar el vencimiento
    env.clock.now = CLOCK_START + retention
    view = env.manager.view(task.id)

    assert view.download is not None and view.download.expired
    with pytest.raises(ResultExpired) as raised:
        env.manager.result(task.id)
    assert raised.value.code == "resultado_expirado"
    assert env.pending() == [(task.workspace, CleanupKind.REMOVE)]
    await env.manager.run_retention_due()
    assert env.manager.view(task.id).version == view.version  # se marca una sola vez
    await env.sweeper.run_due()
    assert not task.workspace.path.exists()


@pytest.mark.req("15.8", "19.5")
@pytest.mark.asyncio
async def test_a_finished_task_record_is_kept_for_24_hours(env: Env) -> None:
    completed = await start(env, "a.png")
    await finished(completed)
    env.converter.script = fail_with(ConversionFailure(FailureReason.DAMAGED))
    failed = await start(env, "b.png")
    await finished(failed)
    retention = env.config.retention_seconds

    env.clock.now = CLOCK_START + retention  # el resultado expira y su espacio se borra
    await env.manager.run_retention_due()
    env.clock.now = CLOCK_START + RECORD_RETENTION_SECONDS - 0.001
    await env.manager.run_retention_due()
    assert env.manager.view(failed.id).state is TaskState.FAILED

    env.clock.now = CLOCK_START + RECORD_RETENTION_SECONDS
    await env.manager.run_retention_due()

    with pytest.raises(TaskUnavailable):
        env.manager.view(failed.id)
    view = env.manager.view(completed.id)  # todavía informa que el resultado expiró
    assert view.download is not None and view.download.expired

    env.clock.now = CLOCK_START + retention + RECORD_RETENTION_SECONDS
    await env.manager.run_retention_due()

    with pytest.raises(TaskUnavailable):
        env.manager.view(completed.id)
    assert len(env.manager) == 0
