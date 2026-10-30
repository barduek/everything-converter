"""Pruebas de src/converter/tasks/view.py: la vista pública de una Tarea y su JSON (Req 2.6, 3.1,
3.2, 3.5, 3.6, 16.4, 16.5, 19.6).

Las Tareas se arman a mano en cada estado, porque la vista solo lee la Tarea; las transiciones
las prueba el Gestor_de_Tareas.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest

from converter.detection.source_info import DetectionResult, NoInfo
from converter.messages_es import FailureReason, NoticeCode, render_failure, render_notice
from converter.models import Category, Failure, LossClass, Notice, Stage, TaskState
from converter.registry.model import ConversionPair
from converter.storage.workspace import Workspace
from converter.tasks.model import ResultInfo, Task
from converter.tasks.progress import ProgressReporter
from converter.tasks.view import (
    DetectionStatus,
    DownloadView,
    FailureView,
    NoticeView,
    PairView,
    TargetView,
    TaskView,
)

TASK_ID = "6f1c2c9e-8a0b-4d7e-9a35-2f0d4b8c1e77"
WORKSPACE = Workspace(Path("/var/lib/converter/storage/ws-" + "3b" * 16))

JSON_KEYS = [
    "id",
    "original_name",
    "state",
    "state_label",
    "detection",
    "real_type",
    "notices",
    "targets",
    "pair",
    "language",
    "detected_language",
    "progress",
    "failure",
    "download",
    "version",
]

JPG_NOTE = "Compresión con pérdida; la transparencia se reemplaza por fondo blanco."
WEBP_NOTE = "Modo sin pérdida de WEBP. Las imágenes de 16 bits por canal se reducen a 8 bits."
PDF_NOTE = "Una página con la imagen."


def _pair(target: str, loss_class: LossClass, note: str, source: str = "PNG") -> ConversionPair:
    return ConversionPair(
        source=source,
        target=target,
        category=Category.IMAGE,
        loss_class=loss_class,
        loss_description=note,
        converter_id="image.raster",
    )


PNG_TARGETS = (
    _pair("JPG", LossClass.LOSSY, JPG_NOTE),
    _pair("WEBP", LossClass.LOSSLESS, WEBP_NOTE),
    _pair("PDF", LossClass.LOSSLESS, PDF_NOTE),
)


def _task(**fields: object) -> Task:
    return Task(
        id=TASK_ID,
        workspace=WORKSPACE,
        original_name="Vacaciones 2024.PNG",
        size_bytes=1234,
        created_at=10.0,
        **fields,  # type: ignore[arg-type]
    )


def _detected(task: Task, real_type: str = "PNG") -> Task:
    task.real_type = real_type
    task.detection = DetectionResult(real_type, NoInfo())
    task.detection_done.set()
    return task


@pytest.mark.req("3.1", "3.2", "3.5")
def test_a_received_task_after_detection_has_the_shape_of_the_design() -> None:
    task = _detected(_task(version=3))

    body = TaskView.from_task(task, PNG_TARGETS).to_json()

    assert list(body) == JSON_KEYS
    assert body == {
        "id": TASK_ID,
        "original_name": "Vacaciones 2024.PNG",
        "state": "recibida",
        "state_label": "Recibida",
        "detection": "completa",
        "real_type": "PNG",
        "notices": [],
        "targets": [
            {
                "format": "JPG",
                "loss_class": "Con_Pérdida",
                "loss_description": JPG_NOTE,
                "requires_confirmation": True,
                "is_transcription": False,
            },
            {
                "format": "WEBP",
                "loss_class": "Sin_Pérdida",
                "loss_description": WEBP_NOTE,
                "requires_confirmation": False,
                "is_transcription": False,
            },
            {
                "format": "PDF",
                "loss_class": "Sin_Pérdida",
                "loss_description": PDF_NOTE,
                "requires_confirmation": False,
                "is_transcription": False,
            },
        ],
        "pair": None,
        "language": None,
        "detected_language": None,
        "progress": None,
        "failure": None,
        "download": None,
        "version": 3,
    }
    json.dumps(body, allow_nan=False)  # serializable tal cual


@pytest.mark.req("3.2")
def test_a_transcription_target_is_marked_and_requires_confirmation() -> None:
    note = "Precisión variable según la calidad del audio."
    pair = ConversionPair("MP3", "TXT", Category.AUDIO, LossClass.APPROXIMATE, note, "t.whisper")
    task = _detected(_task(), "MP3")

    (target,) = TaskView.from_task(task, (pair,)).targets

    assert target == TargetView("TXT", LossClass.APPROXIMATE, note, True, True)


def test_while_detecting_there_is_no_real_type_nor_targets() -> None:
    view = TaskView.from_task(_task(), ())

    assert view.detection is DetectionStatus.RUNNING
    assert view.real_type is None
    assert view.targets == ()
    assert view.state is TaskState.RECEIVED


@pytest.mark.req("2.6")
def test_detection_notices_are_shown_in_spanish() -> None:
    notice = Notice(
        NoticeCode.EXTENSION_MISMATCH,
        MappingProxyType({"extension": ".txt", "tipo_real": "PNG"}),
    )
    task = _detected(_task(notices=[notice]))

    view = TaskView.from_task(task, PNG_TARGETS)

    assert view.notices == (NoticeView("extension_no_coincide", render_notice(notice)),)
    assert ".txt" in view.notices[0].message and "PNG" in view.notices[0].message


@pytest.mark.req("3.6", "16.1", "16.4")
def test_a_failed_task_shows_stage_reason_message_and_action() -> None:
    failure = Failure(
        FailureReason.NO_TARGETS,
        Stage.DETECTION,
        {"tipo_real": "SRT", "formatos_origen": ["PNG", "TXT"]},
    )
    task = _task(state=TaskState.FAILED, real_type="SRT", failure=failure)
    task.detection_done.set()

    view = TaskView.from_task(task, ())

    message = render_failure(failure)
    assert view.failure == FailureView(
        Stage.DETECTION, "tipo_sin_destinos", message.message, message.action
    )
    assert view.to_json()["failure"] == {
        "stage": "detección",
        "reason": "tipo_sin_destinos",
        "message": message.message,
        "action": message.action,
    }
    assert view.real_type == "SRT"
    assert view.targets == ()
    assert view.state_label == "Fallida"


def test_the_progress_is_shown_only_while_processing() -> None:
    progress = ProgressReporter()
    progress.report(0.427)
    processing = _detected(_task(state=TaskState.PROCESSING, progress=progress))
    queued = _detected(_task(state=TaskState.QUEUED, progress=progress))

    assert TaskView.from_task(processing, ()).progress == 42
    assert TaskView.from_task(queued, ()).progress is None


@pytest.mark.req("16.5")
def test_a_completed_task_offers_the_download_without_storage_paths() -> None:
    result = ResultInfo(
        path=WORKSPACE.result_file,
        download_name="Vacaciones 2024.jpg",
        media_type="image/jpeg",
        size_bytes=99,
        completed_at=20.0,
    )
    pair = PNG_TARGETS[0]
    task = _detected(
        _task(state=TaskState.COMPLETED, pair=pair, result=result, language=None, version=7)
    )

    view = TaskView.from_task(task, PNG_TARGETS)

    assert view.pair == PairView("PNG", "JPG")
    assert view.download == DownloadView(
        "Vacaciones 2024.jpg", f"/api/tasks/{TASK_ID}/result", expired=False
    )
    text = json.dumps(view.to_json(), ensure_ascii=False)
    assert str(WORKSPACE.path) not in text
    assert WORKSPACE.name not in text


def test_the_download_is_not_offered_before_completion() -> None:
    result = ResultInfo(WORKSPACE.result_file, "a.jpg", "image/jpeg", 1, 1.0)
    task = _detected(_task(state=TaskState.PROCESSING, result=result))

    assert TaskView.from_task(task, ()).download is None


@pytest.mark.req("19.6")
def test_targets_of_another_type_are_rejected() -> None:
    task = _detected(_task(), "TXT")
    with pytest.raises(ValueError, match="Tipo_Real"):
        TaskView.from_task(task, PNG_TARGETS)


@pytest.mark.req("19.6")
def test_a_task_without_detection_offers_no_targets() -> None:
    with pytest.raises(ValueError, match="detección"):
        TaskView.from_task(_task(), PNG_TARGETS)
