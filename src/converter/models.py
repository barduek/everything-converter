"""Modelos de dominio compartidos: enumeraciones, fallos, rechazos y avisos.

Los nombres de los miembros están en inglés. Sus valores usan el término en español del
glosario, que es lo que viaja en la API.

- `Category` y `LossClass` son la Categoría y la Clasificación_de_Pérdida de cada
  Par_de_Conversión (Req 4.1, 4.2).
- `TaskState` es el Estado_de_Tarea y `TERMINAL_STATES` reúne los estados desde los que no hay
  transición (Req 14.3).
- `Failure` es el Motivo_de_Fallo de una Tarea fallida, con su Etapa_de_Procesamiento
  (Req 16.1). `DestinationRejection` es un Rechazo_de_Destino: la Tarea sigue en recibida y no
  se registra un Motivo_de_Fallo (Req 16.12). `Notice` es un aviso al usuario.

El mensaje en español, la acción sugerida y la Etapa_de_Procesamiento de cada código se obtienen
de `messages_es.py`; estos modelos guardan solo el código y los parámetros del caso.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Solo para anotaciones: messages_es.py importa `Stage` de este módulo, así que importarlo
    # aquí al ejecutar formaría un ciclo
    from converter.messages_es import FailureReason


class Category(StrEnum):
    """Categoría de un Formato."""

    IMAGE = "imagen"
    DOCUMENT = "documento"
    AUDIO = "audio"
    VIDEO = "video"


class LossClass(StrEnum):
    """Clasificación_de_Pérdida de un Par_de_Conversión (Req 4.2)."""

    LOSSLESS = "Sin_Pérdida"
    LOSSY = "Con_Pérdida"
    LIMITED = "Limitada"
    APPROXIMATE = "Aproximada"  # Par_de_Transcripción


class TaskState(StrEnum):
    """Estado_de_Tarea. Las transiciones admitidas están en el Gestor_de_Tareas (Req 14.3)."""

    RECEIVED = "recibida"
    QUEUED = "en_cola"
    PROCESSING = "en_proceso"
    COMPLETED = "completada"
    FAILED = "fallida"
    CANCELLED = "cancelada"


# Ninguna transición de Req 14.3 sale de estos estados
TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}
)


class Stage(StrEnum):
    """Etapa_de_Procesamiento en la que ocurre un error o un fallo."""

    RECEPTION = "recepción"
    DETECTION = "detección"
    CONVERSION = "conversión"
    TRANSCRIPTION = "transcripción"
    DELIVERY = "entrega"


@dataclass(frozen=True, slots=True)
class Failure:
    """Motivo_de_Fallo de una Tarea que pasa a fallida (Req 16.1)."""

    reason: FailureReason  # p. ej. FailureReason.DAMAGED
    stage: Stage
    params: Mapping[str, str | int | list[str]]  # valores del caso: límite, Códec, páginas


@dataclass(frozen=True, slots=True)
class DestinationRejection:
    """Rechazo_de_Destino: la Tarea sigue en recibida, sin Motivo_de_Fallo (Req 16.12)."""

    code: str  # p. ej. "dimensiones_excedidas"
    params: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Notice:
    """Aviso al usuario sobre la detección o el resultado de la conversión."""

    code: str  # p. ej. "bits_reducidos", "fuentes_reemplazadas", "dimensiones_rellenadas"
    params: Mapping[str, object]
