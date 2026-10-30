"""Vista pública de una Tarea (Req 2.6, 3.1, 3.2, 3.5, 3.6, 16.4, 16.5, 19.6).

`TaskView` es la única forma en que los Datos_de_Tarea salen del proceso. `TaskView.from_task`
la arma con una sola Tarea, la que pide la solicitud, y con los Pares_de_Conversión que el
Registro_de_Conversiones tiene para su Tipo_Real; no recibe ni consulta otras Tareas, así una
respuesta nunca lleva datos de otra Tarea (Req 19.6). `to_json` da el cuerpo JSON de la API.

Campos, en el orden del JSON:

| Campo | Contenido |
|---|---|
| `id`, `original_name` | Identificador_de_Tarea y Nombre_Original sin modificar |
| `state`, `state_label` | Estado_de_Tarea y su nombre en español |
| `detection` | `"en_curso"` mientras el Detector_de_Tipo trabaja, `"completa"` después |
| `real_type` | Tipo_Real, también si hubo un Rechazo_de_Origen que lo determinó (Req 2.7) |
| `notices` | avisos con `code` y `message` en español, p. ej. la extensión distinta (Req 2.6) |
| `targets` | Formatos_de_Destino en orden de registro (Req 3.1, 3.2, 3.5) |
| `pair` | par confirmado: `source` y `target` |
| `language`, `detected_language` | Idioma_de_Transcripción e Idioma_Detectado |
| `progress` | Porcentaje_de_Avance de una Tarea en_proceso, o `null` |
| `failure` | `stage`, `reason`, `message` y `action` de una Tarea fallida (Req 16.4) |
| `download` | `name`, `url` y `expired` de una Tarea completada |
| `version` | contador de cambios visibles |

Cada Formato_de_Destino lleva `format` y los datos de su Par_de_Conversión: `loss_class`,
`loss_description`, `requires_confirmation` e `is_transcription`. Solo aparecen cuando la
detección terminó sin Rechazo_de_Origen; con un rechazo, la lista está vacía y `failure` lo
explica (Req 3.6). Los mensajes se arman con `messages_es`, que no admite detalle técnico, y la
vista no incluye rutas del Almacenamiento_Temporal (Req 16.5).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from converter.messages_es import render_failure, render_notice, state_label
from converter.models import LossClass, Stage, TaskState

if TYPE_CHECKING:
    from converter.eventlog import JsonValue
    from converter.registry.model import ConversionPair
    from converter.tasks.model import Task

RESULT_URL = "/api/tasks/{id}/result"  # descarga del Archivo_Resultado


class DetectionStatus(StrEnum):
    """Estado de la detección del Tipo_Real."""

    RUNNING = "en_curso"
    DONE = "completa"


@dataclass(frozen=True, slots=True)
class TargetView:
    """Formato_de_Destino ofrecido, con los datos de su Par_de_Conversión (Req 3.2)."""

    format: str
    loss_class: LossClass
    loss_description: str  # Descripción_de_Pérdida completa (Req 3.3)
    requires_confirmation: bool  # la Clasificación_de_Pérdida no es Sin_Pérdida
    is_transcription: bool  # Par_de_Transcripción: se elige el Idioma_de_Transcripción

    @classmethod
    def from_pair(cls, pair: ConversionPair) -> TargetView:
        return cls(
            format=pair.target,
            loss_class=pair.loss_class,
            loss_description=pair.loss_description,
            requires_confirmation=pair.requires_confirmation,
            is_transcription=pair.is_transcription,
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "format": self.format,
            "loss_class": self.loss_class.value,
            "loss_description": self.loss_description,
            "requires_confirmation": self.requires_confirmation,
            "is_transcription": self.is_transcription,
        }


@dataclass(frozen=True, slots=True)
class NoticeView:
    """Aviso al usuario en español."""

    code: str
    message: str

    def to_json(self) -> dict[str, JsonValue]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class FailureView:
    """Mensaje de una Tarea fallida: etapa, motivo y acción sugerida (Req 16.4)."""

    stage: Stage
    reason: str  # código del Motivo_de_Fallo, p. ej. "archivo_danado"
    message: str
    action: str

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "stage": self.stage.value,
            "reason": self.reason,
            "message": self.message,
            "action": self.action,
        }


@dataclass(frozen=True, slots=True)
class PairView:
    """Par_de_Conversión confirmado."""

    source: str
    target: str

    def to_json(self) -> dict[str, JsonValue]:
        return {"source": self.source, "target": self.target}


@dataclass(frozen=True, slots=True)
class DownloadView:
    """Oferta de descarga del Archivo_Resultado de una Tarea completada."""

    name: str  # nombre con que se descarga (Req 15.1)
    url: str
    expired: bool  # venció el Período_de_Retención (Req 15.8)

    def to_json(self) -> dict[str, JsonValue]:
        return {"name": self.name, "url": self.url, "expired": self.expired}


@dataclass(frozen=True, slots=True)
class TaskView:
    """Datos_de_Tarea de una sola Tarea, listos para la API."""

    id: str
    original_name: str
    state: TaskState
    state_label: str
    detection: DetectionStatus
    real_type: str | None
    notices: tuple[NoticeView, ...]
    targets: tuple[TargetView, ...]
    pair: PairView | None
    language: str | None
    detected_language: str | None
    progress: int | None
    failure: FailureView | None
    download: DownloadView | None
    version: int

    @classmethod
    def from_task(cls, task: Task, targets: Sequence[ConversionPair]) -> TaskView:
        """Vista de `task` con `targets`, los pares del registro para su Tipo_Real.

        `targets` va en orden de registro y solo puede tener pares cuando la detección terminó
        sin Rechazo_de_Origen; todos deben tener el Tipo_Real de la Tarea como
        Formato_de_Origen. Si no, lanza `ValueError`: la vista no mezcla datos que no son de
        esta Tarea.
        """
        if targets and task.detection is None:
            raise ValueError("una Tarea sin detección terminada no ofrece Formatos_de_Destino")
        if task.detection is not None and any(
            pair.source != task.detection.real_type for pair in targets
        ):
            raise ValueError("los Formatos_de_Destino deben ser del Tipo_Real de la Tarea")
        done = task.detection_done.is_set()
        return cls(
            id=task.id,
            original_name=task.original_name,
            state=task.state,
            state_label=state_label(task.state),
            detection=DetectionStatus.DONE if done else DetectionStatus.RUNNING,
            real_type=task.real_type,
            notices=tuple(
                NoticeView(code=str(notice.code), message=render_notice(notice))
                for notice in task.notices
            ),
            targets=tuple(TargetView.from_pair(pair) for pair in targets),
            pair=None if task.pair is None else PairView(task.pair.source, task.pair.target),
            language=task.language,
            detected_language=task.detected_language,
            progress=(
                task.progress.percent
                if task.state is TaskState.PROCESSING and task.progress is not None
                else None
            ),
            failure=_failure_view(task),
            download=_download_view(task),
            version=task.version,
        )

    def to_json(self) -> dict[str, JsonValue]:
        """Cuerpo JSON de la vista, con las claves en el orden de la tabla del módulo."""
        return {
            "id": self.id,
            "original_name": self.original_name,
            "state": self.state.value,
            "state_label": self.state_label,
            "detection": self.detection.value,
            "real_type": self.real_type,
            "notices": [notice.to_json() for notice in self.notices],
            "targets": [target.to_json() for target in self.targets],
            "pair": None if self.pair is None else self.pair.to_json(),
            "language": self.language,
            "detected_language": self.detected_language,
            "progress": self.progress,
            "failure": None if self.failure is None else self.failure.to_json(),
            "download": None if self.download is None else self.download.to_json(),
            "version": self.version,
        }


def _failure_view(task: Task) -> FailureView | None:
    if task.state is not TaskState.FAILED or task.failure is None:
        return None
    message = render_failure(task.failure)  # valida la etapa y los parámetros del motivo
    return FailureView(
        stage=Stage(task.failure.stage),
        reason=message.code,
        message=message.message,
        action=message.action,
    )


def _download_view(task: Task) -> DownloadView | None:
    if task.state is not TaskState.COMPLETED or task.result is None:
        return None
    return DownloadView(
        name=task.result.download_name,
        url=RESULT_URL.format(id=task.id),
        expired=task.result.expired,
    )
