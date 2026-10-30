"""Pruebas de src/converter/tasks/manager.py: creación, detección, vista, confirmación, rechazo de
la confirmación y cancelación de las Tareas (Req 1.1, 1.2, 1.7, 2.6, 2.11, 3.1–3.7, 11.9, 11.10,
13.8, 14.1, 14.6, 14.11, 14.13, 16.1, 16.7, 16.9, 16.12, 17.4, 19.2, 19.5, 19.6, 19.10, 19.11).

El Detector_de_Tipo es el real, sobre un registro con los Formatos de `catalog/formats.toml` y
pares de prueba propios (el `catalog/pairs.toml` del repositorio puede no tener pares). La
función de inspección, que en el Conversor ejecuta un subproceso, es un doble que devuelve,
lanza o no termina según la prueba, y el convertidor de los pares es un doble con `preflight`
configurable cuyo `run` no termina: una Tarea confirmada pasa a en_proceso si el planificador
tiene lugar y queda allí hasta que se cancela. La ejecución y la entrega se prueban en
`test_task_execution.py`. El barrendero y el Log_de_Eventos son los reales, sobre un directorio
temporal y un búfer en memoria.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from converter.config import Config, load_config
from converter.converters.base import ConversionContext, ConversionOutcome, ConversionRequest
from converter.detection.detector import InspectionRequest, TypeDetector, extension_notice
from converter.detection.source_info import NoInfo, SourceInfo, SourceRejection
from converter.eventlog import EventLog, pair_label
from converter.languages import Language, LanguageCatalog
from converter.messages_es import (
    CancelReason,
    FailureReason,
    RejectionCode,
    render_error,
    render_failure,
    render_notice,
)
from converter.models import DestinationRejection, Failure, LossClass, Stage, TaskState
from converter.registry.loader import load_catalog
from converter.registry.registry import ConversionRegistry
from converter.sandbox.runner import SandboxRunner
from converter.storage.sweeper import CleanupKind, Sweeper
from converter.storage.workspace import WorkspaceManager
from converter.tasks.manager import (
    DETECTION_TIMEOUT_SECONDS,
    ConfirmationRequired,
    StateForbids,
    TargetRejected,
    TaskManager,
    TaskUnavailable,
)
from converter.tasks.model import CompletedUpload, ConfirmRequest, Task
from converter.tasks.view import DetectionStatus, NoticeView, PairView, TargetView, TaskView
from tests import format_samples as samples

CONVERTER = "test.copy"
CLOCK_START = 1_000.0

# Pares de prueba en este orden de registro: los de PNG no van seguidos ni en orden alfabético
PAIRS: tuple[tuple[str, str, LossClass, str], ...] = (
    ("PNG", "WEBP", LossClass.LOSSLESS, "Modo sin pérdida de WEBP."),
    ("TXT", "HTML", LossClass.LOSSLESS, "Texto escapado dentro de un documento HTML."),
    ("PNG", "JPG", LossClass.LOSSY, "Compresión con pérdida; la transparencia se reemplaza."),
    ("MP3", "WAV", LossClass.LOSSLESS, "No recupera calidad."),
    ("PNG", "PDF", LossClass.LOSSLESS, "Una página con la imagen."),
    ("MP3", "TXT", LossClass.APPROXIMATE, "Precisión variable según la calidad del audio."),
)

TEXT = "El año pasó rápido.\n".encode()
SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHola\n"
BINARY = b"\x00\x01\x02 sin firma"

# Forma del Identificador_de_Tarea que exige la API
TASK_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def _config() -> Config:
    config = load_config({})
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
    for source, target, loss_class, note in PAIRS:
        registry.register_pair(
            {
                "source": source,
                "target": target,
                "loss_class": loss_class.value,
                "loss_description": note,
                "converter": CONVERTER,
            }
        )
    return registry


class Inspector:
    """Función de inspección de prueba.

    Devuelve `result` (o lo lanza si es una excepción). Con `hang`, no termina hasta que la
    cancelan y entonces tarda `stop_delay` segundos en detenerse, como el ejecutor que espera al
    subproceso. Con `gate`, cada inspección espera a que haya `gate_count` en curso a la vez.
    """

    def __init__(self) -> None:
        self.result: object = NoInfo()
        self.hang = False
        self.stop_delay = 0.0
        self.stopped = 0
        self.requests: list[InspectionRequest] = []
        self.gate: asyncio.Event | None = None
        self.gate_count = 0

    async def __call__(self, request: InspectionRequest) -> SourceInfo | SourceRejection:
        self.requests.append(request)
        if self.gate is not None:
            if len(self.requests) >= self.gate_count:
                self.gate.set()
            await self.gate.wait()
        if self.hang:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(self.stop_delay)
                self.stopped += 1
                raise
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result  # type: ignore[return-value]  # las pruebas de errores devuelven otros


class FakeClock:
    """Reloj monotónico que solo avanza cuando la prueba lo pide."""

    def __init__(self) -> None:
        self.now = CLOCK_START

    def __call__(self) -> float:
        return self.now


class FakeConverter:
    """Convertidor de los pares de prueba.

    `preflight` registra cada llamada y devuelve `rejection`, o lanza `error` si está. `run`
    cuenta las ejecuciones y espera hasta que la cancelan: las Tareas iniciadas quedan
    en_proceso.
    """

    id = CONVERTER

    def __init__(self) -> None:
        self.rejection: object = None
        self.error: BaseException | None = None
        self.calls: list[tuple[SourceInfo, ConversionRequest, Config]] = []
        self.runs: list[str] = []  # Identificadores de las Tareas ejecutadas, en orden

    def preflight(
        self, source: SourceInfo, request: ConversionRequest, config: Config
    ) -> DestinationRejection | None:
        self.calls.append((source, request, config))
        if self.error is not None:
            raise self.error
        return self.rejection  # type: ignore[return-value]  # las pruebas de errores devuelven otros

    async def run(self, ctx: ConversionContext) -> ConversionOutcome:
        self.runs.append(ctx.task_id)
        await asyncio.Event().wait()  # hasta que cancelan la Tarea o cierran el Gestor
        raise AssertionError("una espera sin fin terminó")


# Idiomas_Soportados de prueba; en el MVP languages.es.json no existe
LANGUAGES = LanguageCatalog([Language("es", "español"), Language("en", "inglés")])


@dataclass
class Env:
    registry: ConversionRegistry
    storage: WorkspaceManager
    sweeper: Sweeper
    log: io.StringIO
    inspector: Inspector
    clock: FakeClock
    config: Config = field(default_factory=_config)
    converter: FakeConverter = field(default_factory=FakeConverter)
    managers: list[TaskManager] = field(default_factory=list)

    def new_manager(self, **options: Any) -> TaskManager:
        detector = TypeDetector(self.registry, self.config, self.inspector)
        options.setdefault("converters", {CONVERTER: self.converter})
        options.setdefault("languages", LANGUAGES)
        manager = TaskManager(
            config=self.config,
            registry=self.registry,
            detector=detector,
            runner=SandboxRunner(),
            sweeper=self.sweeper,
            event_log=EventLog(self.log),
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


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    root = tmp_path / "storage"
    root.mkdir()
    storage = WorkspaceManager(root)
    log = io.StringIO()
    clock = FakeClock()
    environment = Env(
        registry=_build_registry(tmp_path),
        storage=storage,
        sweeper=Sweeper(storage, EventLog(io.StringIO()), clock=clock),
        log=log,
        inspector=Inspector(),
        clock=clock,
    )
    environment.new_manager()
    yield environment
    for manager in environment.managers:
        await manager.aclose()


async def _receive(
    manager: TaskManager, storage: WorkspaceManager, content: bytes, name: str
) -> Task:
    workspace = storage.create()
    workspace.source_file.write_bytes(content)
    return await manager.create(CompletedUpload(workspace, name, len(content)))


async def receive(env: Env, content: bytes, name: str) -> Task:
    return await _receive(env.manager, env.storage, content, name)


async def detected(task: Task, seconds: float = 5.0) -> None:
    async with asyncio.timeout(seconds):
        await task.detection_done.wait()


def _uuids(*values: uuid.UUID) -> Callable[[], uuid.UUID]:
    remaining: Iterator[uuid.UUID] = iter(values)
    return lambda: next(remaining)


def _v4(number: int) -> uuid.UUID:
    return uuid.UUID(int=number, version=4)


# --- Inicio y creación ---


@pytest.mark.req("14.13")
@pytest.mark.asyncio
async def test_the_manager_starts_without_tasks(env: Env) -> None:
    task = await receive(env, TEXT, "notas.txt")
    restarted = env.new_manager()  # otro inicio, con el mismo Almacenamiento_Temporal

    assert len(env.manager) == 1
    assert len(restarted) == 0
    with pytest.raises(TaskUnavailable):
        restarted.view(task.id)


@pytest.mark.req("1.1", "1.2", "1.7")
@pytest.mark.asyncio
async def test_create_registers_a_received_task_with_its_workspace_and_name(env: Env) -> None:
    name = "../fotos/Vacaciones: 2024 ñ.PNG"
    workspace = env.storage.create()
    workspace.source_file.write_bytes(samples.PNG)

    task = await env.manager.create(CompletedUpload(workspace, name, len(samples.PNG)))
    before = env.manager.view(task.id)
    await detected(task)

    assert task.state is TaskState.RECEIVED
    assert task.workspace == workspace
    assert task.original_name == name
    assert task.size_bytes == len(samples.PNG)
    assert task.created_at == CLOCK_START
    assert before.state is TaskState.RECEIVED
    assert before.detection is DetectionStatus.RUNNING
    assert before.original_name == name
    assert env.manager.view(task.id).original_name == name
    # El Archivo_de_Origen sigue en el Espacio_de_Trabajo de la Tarea, sin cambios
    assert workspace.source_file.read_bytes() == samples.PNG
    assert env.inspector.requests == [
        InspectionRequest(inspector="image", real_type="PNG", workspace=workspace)
    ]


@pytest.mark.req("19.2")
@pytest.mark.asyncio
async def test_identifiers_are_canonical_uuid4_and_distinct(env: Env) -> None:
    tasks = [await receive(env, TEXT, f"nota{n}.txt") for n in range(20)]

    ids = [task.id for task in tasks]
    assert len(set(ids)) == len(ids)
    for task_id in ids:
        assert TASK_ID.fullmatch(task_id)
        assert uuid.UUID(task_id).version == 4
        assert str(uuid.UUID(task_id)) == task_id


@pytest.mark.req("19.2")
@pytest.mark.asyncio
async def test_an_issued_identifier_is_never_repeated(env: Env) -> None:
    first, second, third = _v4(1), _v4(2), _v4(3)
    manager = env.new_manager(new_uuid=_uuids(first, first, second, first, second, third))

    tasks = [await _receive(manager, env.storage, TEXT, "a.txt") for _ in range(3)]

    assert [task.id for task in tasks] == [str(first), str(second), str(third)]


@pytest.mark.req("19.2")
@pytest.mark.asyncio
async def test_a_generator_that_only_repeats_cannot_create_tasks(env: Env) -> None:
    manager = env.new_manager(new_uuid=lambda: _v4(7))
    await _receive(manager, env.storage, TEXT, "a.txt")

    with pytest.raises(RuntimeError):
        await _receive(manager, env.storage, TEXT, "b.txt")
    assert len(manager) == 1


# --- Detección sin rechazo y Formatos_de_Destino ---


@pytest.mark.req("3.1", "3.2", "3.5")
@pytest.mark.parametrize(
    ("content", "name", "real_type"),
    [(samples.PNG, "foto.png", "PNG"), (samples.mp3(), "voz.mp3", "MP3"), (TEXT, "a.txt", "TXT")],
    ids=["PNG", "MP3", "TXT"],
)
@pytest.mark.asyncio
async def test_targets_follow_the_registry_order_with_their_loss_class(
    env: Env, content: bytes, name: str, real_type: str
) -> None:
    task = await receive(env, content, name)
    await detected(task, seconds=1.0)  # Req 3.1: los destinos se pueden mostrar enseguida

    view = env.manager.view(task.id)

    expected = [
        TargetView(target, loss_class, note, loss_class is not LossClass.LOSSLESS, False)
        if loss_class is not LossClass.APPROXIMATE
        else TargetView(target, loss_class, note, True, True)
        for source, target, loss_class, note in PAIRS
        if source == real_type
    ]
    assert list(view.targets) == expected
    assert [t.format for t in view.targets] == [
        pair.target for pair in env.registry.targets_for(real_type)
    ]
    assert view.detection is DetectionStatus.DONE
    assert view.real_type == real_type
    assert view.state is TaskState.RECEIVED
    assert view.failure is None
    assert view.notices == ()
    assert view.version == 1


@pytest.mark.req("2.6")
@pytest.mark.asyncio
async def test_a_foreign_extension_is_reported_before_choosing_the_target(env: Env) -> None:
    task = await receive(env, samples.PNG, "foto.TXT")
    await detected(task)

    view = env.manager.view(task.id)

    (png,) = (fmt for fmt in env.registry.formats() if fmt.name == "PNG")
    notice = extension_notice(png, ".TXT")
    assert notice is not None
    assert view.notices == (NoticeView("extension_no_coincide", render_notice(notice)),)
    assert view.real_type == "PNG"
    assert [t.format for t in view.targets] == ["WEBP", "JPG", "PDF"]


# --- Rechazos_de_Origen y errores internos ---


def _source_formats(env: Env) -> list[str]:
    return list(env.registry.source_formats())


@pytest.mark.req("3.6", "16.1", "16.7")
@pytest.mark.asyncio
async def test_a_type_without_targets_fails_with_the_list_of_source_formats(env: Env) -> None:
    task = await receive(env, SRT, "subtitulos.srt")
    await detected(task)

    view = env.manager.view(task.id)

    failure = Failure(
        FailureReason.NO_TARGETS,
        Stage.DETECTION,
        {"tipo_real": "SRT", "formatos_origen": _source_formats(env)},
    )
    assert task.state is TaskState.FAILED
    assert task.failure == failure
    assert view.real_type == "SRT"
    assert view.targets == ()
    assert view.detection is DetectionStatus.DONE
    assert view.failure is not None
    assert view.failure.message == render_failure(failure).message
    assert "SRT" in view.failure.message and "PNG, TXT, MP3" in view.failure.message
    (event,) = env.events()
    assert event["event"] == "task_failed"
    assert event["task_id"] == task.id
    assert event["real_type"] == "SRT"
    assert event["pair"] is None
    assert event["stage"] == "detección"
    assert event["reason"] == "tipo_sin_destinos"


@pytest.mark.req("16.1", "16.7")
@pytest.mark.parametrize(
    ("content", "name", "inspected", "reason", "real_type"),
    [
        (BINARY, "datos.bin", None, FailureReason.UNRECOGNIZED_FORMAT, None),
        (
            samples.PNG,
            "anim.png",
            SourceRejection(FailureReason.ANIMATED_IMAGE),
            FailureReason.ANIMATED_IMAGE,
            "PNG",
        ),
        (
            samples.PNG,
            "roto.png",
            SourceRejection(FailureReason.DAMAGED),
            FailureReason.DAMAGED,
            "PNG",
        ),
    ],
    ids=["formato_no_reconocido", "imagen_animada", "archivo_danado"],
)
@pytest.mark.asyncio
async def test_a_source_rejection_fails_the_task_with_its_reason(
    env: Env,
    content: bytes,
    name: str,
    inspected: SourceRejection | None,
    reason: FailureReason,
    real_type: str | None,
) -> None:
    if inspected is not None:
        env.inspector.result = inspected
    task = await receive(env, content, name)
    await detected(task)

    view = env.manager.view(task.id)

    assert task.state is TaskState.FAILED
    assert task.failure is not None and task.failure.reason is reason
    assert task.real_type == real_type
    assert task.detection is None
    assert view.failure is not None
    assert (view.failure.stage, view.failure.reason) == (Stage.DETECTION, reason.value)
    assert view.failure.action == render_failure(task.failure).action
    assert view.targets == ()
    (event,) = env.events()
    assert (event["task_id"], event["reason"], event["real_type"]) == (
        task.id,
        reason.value,
        real_type,
    )
    # Req 17.4: el Espacio_de_Trabajo de la Tarea fallida queda encolado para borrarse
    (job,) = env.sweeper.pending
    assert (job.workspace, job.kind, job.task_id) == (task.workspace, CleanupKind.REMOVE, task.id)


@pytest.mark.req("16.9", "16.7")
@pytest.mark.parametrize(
    ("raised", "exception"),
    [
        (RuntimeError("detalle con «contenido» del archivo"), "RuntimeError"),
        (object(), "TypeError"),  # la función de inspección devuelve otro tipo
    ],
    ids=["excepcion", "otro_tipo"],
)
@pytest.mark.asyncio
async def test_an_error_of_the_detector_is_an_internal_error(
    env: Env, raised: object, exception: str
) -> None:
    env.inspector.result = raised
    task = await receive(env, samples.PNG, "foto.png")
    await detected(task)

    assert task.state is TaskState.FAILED
    assert task.failure == Failure(FailureReason.INTERNAL_ERROR, Stage.DETECTION, {})
    view = env.manager.view(task.id)
    assert view.failure is not None
    assert view.failure.action == "Vuelva a subir el archivo e intente la conversión de nuevo."
    (event,) = env.events()
    assert event["reason"] == "error_interno"
    assert event["detail"]["exception"] == exception
    assert "contenido" not in env.log.getvalue()  # el mensaje de la excepción no se registra


@pytest.mark.req("16.9")
@pytest.mark.asyncio
async def test_a_registry_that_cannot_detect_is_an_internal_error(env: Env, tmp_path: Path) -> None:
    # Sin pares, el detector lanza DetectionSetupError: no depende del archivo
    empty_dir = tmp_path / "vacío"
    empty_dir.mkdir()
    empty = empty_dir / "pairs.toml"
    empty.write_text("", encoding="utf-8")
    registry = load_catalog(
        [CONVERTER],
        event_log=EventLog(io.StringIO()),
        pairs_path=empty,
        extra_pairs_dir=empty_dir / "pairs.d",
    )
    manager = TaskManager(
        config=_config(),
        registry=registry,
        detector=TypeDetector(registry, _config(), env.inspector),
        runner=SandboxRunner(),
        sweeper=env.sweeper,
        event_log=EventLog(env.log),
    )
    env.managers.append(manager)

    task = await _receive(manager, env.storage, TEXT, "a.txt")
    await detected(task)

    assert task.state is TaskState.FAILED
    assert task.failure is not None and task.failure.reason is FailureReason.INTERNAL_ERROR
    (event,) = env.events()
    assert event["detail"]["exception"] == "converter.detection.detector.DetectionSetupError"


# --- Plazo de la detección ---


@pytest.mark.req("2.11")
@pytest.mark.asyncio
async def test_a_detection_that_does_not_finish_fails_at_the_deadline(env: Env) -> None:
    assert DETECTION_TIMEOUT_SECONDS <= 10.0
    env.inspector.hang = True
    env.inspector.stop_delay = 1.0  # el detector tarda en detenerse, como el ejecutor
    manager = env.new_manager(detection_timeout=0.2)

    start = time.monotonic()
    task = await _receive(manager, env.storage, samples.PNG, "foto.png")
    await detected(task)
    elapsed = time.monotonic() - start

    # El fallo se registra al vencer el plazo, sin esperar a que el detector se detenga
    assert 0.19 <= elapsed < 0.2 + env.inspector.stop_delay
    assert task.state is TaskState.FAILED
    assert task.failure == Failure(FailureReason.INTERNAL_ERROR, Stage.DETECTION, {})
    assert manager.view(task.id).detection is DetectionStatus.DONE
    (event,) = env.events()
    assert event["detail"]["exception"] == "TimeoutError"
    await manager.aclose()
    assert env.inspector.stopped == 1  # el trabajo de la Tarea esperó a que el detector termine


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf")])
@pytest.mark.asyncio
async def test_the_detection_timeout_must_be_positive(env: Env, timeout: float) -> None:
    with pytest.raises(ValueError):
        env.new_manager(detection_timeout=timeout)


@pytest.mark.req("19.10")
@pytest.mark.asyncio
async def test_detections_run_at_the_same_time(env: Env) -> None:
    # Cada inspección espera a que estén las cinco en curso: si se esperaran entre sí, no
    # terminarían. Más detecciones que el Límite_de_Concurrencia predeterminado (2)
    env.inspector.gate = asyncio.Event()
    env.inspector.gate_count = 5

    tasks = [await receive(env, samples.PNG, f"foto{n}.png") for n in range(5)]
    for task in tasks:
        await detected(task)

    assert all(task.state is TaskState.RECEIVED for task in tasks)
    assert all(env.manager.view(task.id).real_type == "PNG" for task in tasks)


# --- Vista ---


@pytest.mark.req("19.6")
@pytest.mark.asyncio
async def test_a_view_has_only_the_data_of_the_requested_task(env: Env) -> None:
    mine = await receive(env, samples.PNG, "mía.png")
    other = await receive(env, SRT, "ajena-secreta.srt")
    await detected(mine)
    await detected(other)

    view = env.manager.view(mine.id)

    text = json.dumps(view.to_json(), ensure_ascii=False)
    assert view.id == mine.id
    assert other.id not in text
    assert "ajena-secreta" not in text
    assert "SRT" not in text
    assert str(mine.workspace.path) not in text  # tampoco rutas del almacenamiento (Req 16.5)


@pytest.mark.req("19.5", "19.6")
@pytest.mark.parametrize("task_id", ["", "no-es-un-id", str(uuid.uuid4()), "6F1C2C9E"])
@pytest.mark.asyncio
async def test_an_unknown_identifier_gives_the_same_error(env: Env, task_id: str) -> None:
    await receive(env, TEXT, "a.txt")

    with pytest.raises(TaskUnavailable) as raised:
        env.manager.view(task_id)

    assert str(raised.value) == "la Tarea no existe o ya no está disponible"


@pytest.mark.asyncio
async def test_closing_before_the_detection_starts_still_ends_it(env: Env) -> None:
    task = await receive(env, samples.PNG, "foto.png")  # el trabajo todavía no empezó

    await env.manager.aclose()

    # Nadie que espere el resultado de la detección queda esperando
    assert task.detection_done.is_set()
    assert task.detection is None


# --- Confirmación, rechazo de la confirmación y cancelación ---

PNG_TARGETS = ["WEBP", "JPG", "PDF"]  # destinos de PNG en orden de registro


async def ready(env: Env, content: bytes = samples.PNG, name: str = "foto.png") -> Task:
    """Tarea recibida con la detección terminada."""
    task = await receive(env, content, name)
    await detected(task)
    assert task.state is TaskState.RECEIVED
    return task


async def occupy_slots(env: Env) -> list[Task]:
    """Ocupa cada lugar del Límite_de_Concurrencia con una Tarea en_proceso que no termina."""
    running = []
    for _ in range(env.config.concurrency):
        task = await ready(env)
        await env.manager.confirm(task.id, ConfirmRequest("PDF"))
        assert task.state is TaskState.PROCESSING
        running.append(task)
    return running


async def until(condition: Callable[[], object], seconds: float = 5.0) -> None:
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.005)


def gate_detection(env: Env) -> asyncio.Event:
    """Detiene la próxima detección en el inspector hasta que la prueba active el evento."""
    gate = asyncio.Event()
    env.inspector.gate = gate
    env.inspector.gate_count = 1_000  # nunca se alcanza: solo la prueba abre el paso
    return gate


async def during_detection(
    env: Env, gate: asyncio.Event, *requests: Coroutine[Any, Any, TaskView]
) -> list[TaskView | BaseException]:
    """Lanza `requests` en ese orden mientras el inspector está detenido, deja que todas
    empiecen, abre el paso y devuelve sus resultados o excepciones, en el mismo orden."""
    await until(lambda: env.inspector.requests)
    pending = [asyncio.create_task(request) for request in requests]
    await asyncio.sleep(0.01)
    gate.set()
    return await asyncio.gather(*pending, return_exceptions=True)


def assert_unchanged(env: Env, task: Task, version: int) -> None:
    """La Tarea sigue en recibida con su Archivo_de_Origen, sin par ni Motivo_de_Fallo."""
    assert task.state is TaskState.RECEIVED
    assert task.version == version
    assert (task.pair, task.language, task.failure, task.result) == (None, None, None, None)
    assert task.workspace.source_file.is_file()
    assert env.sweeper.pending == ()
    assert env.events() == []


@pytest.mark.req("14.1", "3.3")
@pytest.mark.asyncio
async def test_a_lossless_target_queues_the_task_with_its_pair(env: Env) -> None:
    await occupy_slots(env)  # sin lugar en el planificador, la Tarea queda en_cola
    task = await ready(env)
    version = task.version

    view = await env.manager.confirm(task.id, ConfirmRequest("WEBP"))

    (pair,) = (p for p in env.registry.targets_for("PNG") if p.target == "WEBP")
    assert task.state is TaskState.QUEUED
    assert task.pair == pair
    assert task.language is None
    assert task.queued_seq == env.config.concurrency + 1
    assert view.state is TaskState.QUEUED
    assert view.pair == PairView("PNG", "WEBP")
    assert view.version == version + 1
    assert task.detection is not None
    assert env.converter.calls[-1] == (task.detection.info, ConversionRequest("WEBP"), env.config)


@pytest.mark.req("3.3", "14.1")
@pytest.mark.asyncio
async def test_a_lossy_target_needs_the_loss_to_be_accepted(env: Env) -> None:
    task = await ready(env)
    version = task.version

    with pytest.raises(ConfirmationRequired) as raised:
        await env.manager.confirm(task.id, ConfirmRequest("JPG"))

    assert raised.value.code == "confirmacion_requerida"
    assert_unchanged(env, task, version)
    assert env.converter.calls == []

    view = await env.manager.confirm(task.id, ConfirmRequest("JPG", acknowledge_loss=True))

    assert view.state is TaskState.PROCESSING  # el planificador tenía lugar
    assert view.pair == PairView("PNG", "JPG")


@pytest.mark.req("3.4", "16.12")
@pytest.mark.parametrize("target", ["MP3", "PNG", "NO-EXISTE", "jpg", ""])
@pytest.mark.asyncio
async def test_a_target_without_a_pair_is_rejected_with_the_offered_targets(
    env: Env, target: str
) -> None:
    task = await ready(env)
    version = task.version

    with pytest.raises(TargetRejected) as raised:
        await env.manager.confirm(task.id, ConfirmRequest(target, acknowledge_loss=True))

    rejection = raised.value.rejection
    assert rejection.code == "destino_no_disponible"
    assert dict(rejection.params) == {
        "destino": target,
        "tipo_real": "PNG",
        "destinos": PNG_TARGETS,
    }
    message = render_error(raised.value.code, raised.value.params)
    assert message.action.endswith("WEBP, JPG, PDF.")
    assert_unchanged(env, task, version)
    assert env.converter.calls == []


@pytest.mark.req("16.12")
@pytest.mark.asyncio
async def test_a_preflight_rejection_keeps_the_task_received(env: Env) -> None:
    rejection = DestinationRejection(
        RejectionCode.DIMENSIONS_EXCEEDED,
        {"ancho": 20_000, "alto": 10, "destino": "WEBP", "maximo": 16_383},
    )
    env.converter.rejection = rejection
    task = await ready(env)
    version = task.version

    with pytest.raises(TargetRejected) as raised:
        await env.manager.confirm(task.id, ConfirmRequest("WEBP"))

    assert raised.value.rejection is rejection
    assert raised.value.code == "dimensiones_excedidas"
    assert_unchanged(env, task, version)


@pytest.mark.req("16.9", "16.7")
@pytest.mark.parametrize(
    "problem",
    [
        RuntimeError("detalle con «contenido» del archivo"),
        "no es un rechazo",
        DestinationRejection("tarea_no_disponible", {}),  # no es un Rechazo_de_Destino
        DestinationRejection(RejectionCode.DIMENSIONS_EXCEEDED, {"ancho": 1}),  # faltan datos
    ],
    ids=["excepcion", "otro_tipo", "otro_codigo", "parametros_invalidos"],
)
@pytest.mark.asyncio
async def test_a_preflight_error_fails_the_task_with_an_internal_error(
    env: Env, problem: object
) -> None:
    if isinstance(problem, BaseException):
        env.converter.error = problem
    else:
        env.converter.rejection = problem
    task = await ready(env)

    view = await env.manager.confirm(task.id, ConfirmRequest("WEBP"))

    assert view.state is TaskState.FAILED
    assert task.failure == Failure(FailureReason.INTERNAL_ERROR, Stage.CONVERSION, {})
    assert view.failure is not None and view.failure.reason == "error_interno"
    (event,) = env.events()
    assert (event["reason"], event["stage"]) == ("error_interno", "conversión")
    assert event["pair"] == pair_label("PNG", "WEBP")
    assert "contenido" not in env.log.getvalue()
    (job,) = env.sweeper.pending
    assert (job.workspace, job.kind) == (task.workspace, CleanupKind.REMOVE)


@pytest.mark.req("11.9", "11.10", "14.1")
@pytest.mark.parametrize(
    ("requested", "expected"), [(None, "es"), ("auto", "auto"), ("en", "en"), ("es", "es")]
)
@pytest.mark.asyncio
async def test_a_transcription_pair_gets_its_language(
    env: Env, requested: str | None, expected: str
) -> None:
    task = await ready(env, samples.mp3(), "voz.mp3")

    view = await env.manager.confirm(
        task.id, ConfirmRequest("TXT", language=requested, acknowledge_loss=True)
    )

    assert view.state is TaskState.PROCESSING
    assert (task.language, view.language) == (expected, expected)
    assert env.converter.calls[-1][1] == ConversionRequest("TXT", expected)


@pytest.mark.req("11.9", "13.8", "16.12")
@pytest.mark.parametrize("requested", ["pt", "ES", ""])
@pytest.mark.asyncio
async def test_an_unsupported_language_is_rejected_with_the_supported_ones(
    env: Env, requested: str
) -> None:
    task = await ready(env, samples.mp3(), "voz.mp3")
    version = task.version

    with pytest.raises(TargetRejected) as raised:
        await env.manager.confirm(
            task.id, ConfirmRequest("TXT", language=requested, acknowledge_loss=True)
        )

    assert raised.value.code == "idioma_no_soportado"
    assert dict(raised.value.params) == {"valor": requested, "idiomas": ["español", "inglés"]}
    message = render_error(raised.value.code, raised.value.params)
    assert message.message.endswith("Idiomas disponibles: español, inglés, o detección automática.")
    assert task.state is TaskState.RECEIVED
    assert (task.version, task.pair, task.language) == (version, None, None)
    assert env.converter.calls == []  # Req 13.8: no se llega al convertidor


@pytest.mark.req("14.1")
@pytest.mark.asyncio
async def test_the_language_is_ignored_outside_a_transcription_pair(env: Env) -> None:
    task = await ready(env, samples.mp3(), "voz.mp3")

    await env.manager.confirm(task.id, ConfirmRequest("WAV", language="pt"))

    assert task.state is TaskState.PROCESSING
    assert task.language is None
    assert env.converter.calls[-1][1] == ConversionRequest("WAV", None)


@pytest.mark.req("3.3", "3.4", "11.9", "16.12")
@pytest.mark.asyncio
async def test_the_validations_follow_the_design_order(env: Env) -> None:
    env.converter.rejection = DestinationRejection(
        RejectionCode.TRANSCRIPTION_TOO_LONG, {"duracion": 3_700_000, "maximo_min": 60}
    )
    task = await ready(env, samples.mp3(), "voz.mp3")
    manager = env.manager

    # El destino antes que la aceptación de la pérdida y el idioma
    with pytest.raises(TargetRejected) as raised:
        await manager.confirm(task.id, ConfirmRequest("PDF", language="pt"))
    assert raised.value.code == "destino_no_disponible"
    # La aceptación de la pérdida antes que el idioma
    with pytest.raises(ConfirmationRequired):
        await manager.confirm(task.id, ConfirmRequest("TXT", language="pt"))
    # El idioma antes que el preflight
    with pytest.raises(TargetRejected) as raised:
        await manager.confirm(task.id, ConfirmRequest("TXT", "pt", acknowledge_loss=True))
    assert raised.value.code == "idioma_no_soportado"
    assert env.converter.calls == []
    # Y por último el preflight
    with pytest.raises(TargetRejected) as raised:
        await manager.confirm(task.id, ConfirmRequest("TXT", acknowledge_loss=True))
    assert raised.value.code == "transcripcion_excede_duracion"
    assert task.state is TaskState.RECEIVED


@pytest.mark.req("14.11")
@pytest.mark.asyncio
async def test_confirming_outside_received_reports_the_current_state(env: Env) -> None:
    running, *_ = await occupy_slots(env)
    queued = await ready(env)
    await env.manager.confirm(queued.id, ConfirmRequest("WEBP"))
    cancelled = await ready(env)
    await env.manager.cancel(cancelled.id)
    failed = await receive(env, SRT, "subtitulos.srt")  # Rechazo_de_Origen: tipo_sin_destinos
    await detected(failed)

    for task, state in [
        (running, TaskState.PROCESSING),
        (queued, TaskState.QUEUED),
        (cancelled, TaskState.CANCELLED),
        (failed, TaskState.FAILED),
    ]:
        version = task.version
        with pytest.raises(StateForbids) as raised:
            await env.manager.confirm(task.id, ConfirmRequest("PDF"))

        assert raised.value.state is state
        assert raised.value.code == "estado_no_permite"
        assert dict(raised.value.params) == {"estado": state.value}
        assert (task.state, task.version) == (state, version)
    assert queued.pair is not None and queued.pair.target == "WEBP"


@pytest.mark.req("2.11", "14.1")
@pytest.mark.asyncio
async def test_a_confirmation_during_the_detection_waits_for_its_result(env: Env) -> None:
    gate = gate_detection(env)
    task = await receive(env, samples.PNG, "foto.png")

    (view,) = await during_detection(
        env, gate, env.manager.confirm(task.id, ConfirmRequest("WEBP"))
    )

    assert isinstance(view, TaskView)
    assert view.state is TaskState.PROCESSING
    assert view.detection is DetectionStatus.DONE


@pytest.mark.req("14.11", "16.1")
@pytest.mark.asyncio
async def test_a_confirmation_waiting_for_a_rejected_source_reports_the_failure(env: Env) -> None:
    gate = gate_detection(env)
    env.inspector.result = SourceRejection(FailureReason.ANIMATED_IMAGE)
    task = await receive(env, samples.PNG, "anim.png")

    (refused,) = await during_detection(
        env, gate, env.manager.confirm(task.id, ConfirmRequest("WEBP"))
    )

    assert isinstance(refused, StateForbids) and refused.state is TaskState.FAILED
    assert task.failure is not None and task.failure.reason is FailureReason.ANIMATED_IMAGE
    assert task.pair is None


@pytest.mark.req("3.7")
@pytest.mark.asyncio
async def test_declining_keeps_the_task_received_with_the_same_targets(env: Env) -> None:
    task = await ready(env)
    before = env.manager.view(task.id)
    with pytest.raises(ConfirmationRequired):
        await env.manager.confirm(task.id, ConfirmRequest("JPG"))

    view = await env.manager.decline(task.id, "JPG")

    assert view == before
    assert [target.format for target in view.targets] == PNG_TARGETS
    assert_unchanged(env, task, before.version)
    # El usuario puede elegir otro destino
    view = await env.manager.confirm(task.id, ConfirmRequest("WEBP"))
    assert view.state is TaskState.PROCESSING


@pytest.mark.req("3.7", "14.11")
@pytest.mark.asyncio
async def test_declining_outside_received_reports_the_current_state(env: Env) -> None:
    task = await ready(env)
    await env.manager.confirm(task.id, ConfirmRequest("JPG", acknowledge_loss=True))

    with pytest.raises(StateForbids) as raised:
        await env.manager.decline(task.id, "JPG")

    assert raised.value.state is TaskState.PROCESSING


@pytest.mark.req("14.6", "17.4")
@pytest.mark.asyncio
async def test_cancelling_a_received_task(env: Env) -> None:
    task = await ready(env)
    version = task.version

    view = await env.manager.cancel(task.id)

    assert view.state is TaskState.CANCELLED
    assert view.version == version + 1
    assert task.cancel_reason is CancelReason.BY_USER
    assert task.failure is None
    (job,) = env.sweeper.pending
    assert (job.workspace, job.kind, job.task_id) == (task.workspace, CleanupKind.REMOVE, task.id)


@pytest.mark.req("14.6")
@pytest.mark.asyncio
async def test_cancelling_during_the_detection_stops_it(env: Env) -> None:
    env.inspector.hang = True
    env.inspector.stop_delay = 0.1  # el detector tarda en detenerse, como el ejecutor
    task = await receive(env, samples.PNG, "foto.png")
    await until(lambda: env.inspector.requests)

    start = time.monotonic()
    view = await env.manager.cancel(task.id)

    assert time.monotonic() - start < 10.0
    assert env.inspector.stopped == 1  # el subproceso terminó antes de pasar a cancelada
    assert view.state is TaskState.CANCELLED
    assert view.detection is DetectionStatus.DONE
    assert task.detection is None  # la detección no registró nada
    assert env.events() == []


@pytest.mark.req("14.6", "19.8")
@pytest.mark.asyncio
async def test_cancelling_a_queued_task_takes_it_out_of_the_queue(env: Env) -> None:
    await occupy_slots(env)
    tasks = [await ready(env) for _ in range(3)]
    for task in tasks:
        await env.manager.confirm(task.id, ConfirmRequest("PDF"))
    first, second, third = tasks
    queue = env.manager._scheduler.queue  # cola única del planificador, en orden de ingreso
    base = env.config.concurrency
    assert [task.queued_seq for task in tasks] == [base + 1, base + 2, base + 3]
    assert list(queue) == [first.id, second.id, third.id]

    view = await env.manager.cancel(second.id)

    assert view.state is TaskState.CANCELLED
    assert list(queue) == [first.id, third.id]
    assert (first.state, third.state) == (TaskState.QUEUED, TaskState.QUEUED)


@pytest.mark.req("14.11")
@pytest.mark.asyncio
async def test_cancelling_a_terminal_task_reports_its_state(env: Env) -> None:
    cancelled = await ready(env)
    await env.manager.cancel(cancelled.id)
    failed = await receive(env, SRT, "subtitulos.srt")
    await detected(failed)

    for task, state in [(cancelled, TaskState.CANCELLED), (failed, TaskState.FAILED)]:
        version = task.version
        with pytest.raises(StateForbids) as raised:
            await env.manager.cancel(task.id)

        assert raised.value.state is state
        assert (task.state, task.version) == (state, version)
    assert cancelled.cancel_reason is CancelReason.BY_USER
    assert failed.cancel_reason is None


@pytest.mark.req("19.5")
@pytest.mark.asyncio
async def test_requests_on_an_unknown_task_give_the_same_error(env: Env) -> None:
    unknown = str(uuid.uuid4())

    with pytest.raises(TaskUnavailable):
        await env.manager.confirm(unknown, ConfirmRequest("WEBP"))
    with pytest.raises(TaskUnavailable):
        await env.manager.decline(unknown, "WEBP")
    with pytest.raises(TaskUnavailable):
        await env.manager.cancel(unknown)


@pytest.mark.req("19.11")
@pytest.mark.asyncio
async def test_simultaneous_confirmations_accept_only_one(env: Env) -> None:
    gate = gate_detection(env)
    task = await receive(env, samples.PNG, "foto.png")

    first, *others = await during_detection(
        env,
        gate,
        *(
            env.manager.confirm(task.id, ConfirmRequest(target, acknowledge_loss=True))
            for target in ("JPG", "WEBP", "PDF")
        ),
    )

    # La primera en llegar se acepta; las demás se evalúan sobre el estado que dejó
    assert isinstance(first, TaskView) and first.pair == PairView("PNG", "JPG")
    assert all(isinstance(o, StateForbids) and o.state is TaskState.PROCESSING for o in others)
    assert task.pair is not None and task.pair.target == "JPG"


@pytest.mark.req("19.11", "14.6")
@pytest.mark.parametrize("cancel_first", [False, True], ids=["confirma_y_cancela", "cancela"])
@pytest.mark.asyncio
async def test_a_confirmation_and_a_cancellation_are_served_one_at_a_time(
    env: Env, cancel_first: bool
) -> None:
    gate = gate_detection(env)
    task = await receive(env, samples.PNG, "foto.png")
    confirm = env.manager.confirm(task.id, ConfirmRequest("WEBP"))
    cancel = env.manager.cancel(task.id)

    results = await during_detection(
        env, gate, *((cancel, confirm) if cancel_first else (confirm, cancel))
    )

    assert task.state is TaskState.CANCELLED
    if cancel_first:
        cancelled, refused = results
        assert isinstance(cancelled, TaskView) and cancelled.state is TaskState.CANCELLED
        assert isinstance(refused, StateForbids) and refused.state is TaskState.CANCELLED
        assert task.pair is None
    else:
        started, cancelled = results
        assert isinstance(started, TaskView) and started.state is TaskState.PROCESSING
        assert isinstance(cancelled, TaskView) and cancelled.state is TaskState.CANCELLED
        assert task.pair is not None and task.pair.target == "WEBP"
