"""Máquina de estados de la Tarea (Req 14.3, 14.9).

`transition(state, event)` es una función pura, sin E/S ni reloj, y es la única forma en que el
Gestor_de_Tareas cambia el Estado_de_Tarea. Devuelve el nuevo estado si el par (estado, evento)
está en la tabla del diseño, o `InvalidTransition` con el estado actual sin cambios en
cualquier otro caso:

| Estado | Evento | Nuevo estado |
|---|---|---|
| recibida | `ConfirmAccepted` | en_cola |
| recibida | `SourceRejected`, `InternalError` | fallida |
| recibida | `CancelRequested`, `RetentionExpired` | cancelada |
| en_cola | `Started` | en_proceso |
| en_cola | `CancelRequested` | cancelada |
| en_proceso | `Completed` | completada |
| en_proceso | `Failed`, `TimedOut`, `InternalError` | fallida |
| en_proceso | `CancelRequested` | cancelada |

- Los cambios posibles son exactamente las ocho transiciones de Req 14.3.
- Ningún par de la tabla sale de un Estado_Terminal, así que un evento sobre una Tarea completada,
  fallida o cancelada siempre devuelve `InvalidTransition` y el estado no cambia (Req 14.9). Esto
  cubre los eventos que llegan tarde, como la finalización de un subproceso que se canceló.
- Una solicitud de cancelar o confirmar que no se admite en el estado actual también devuelve
  `InvalidTransition`, cuyo `state` es el Estado_de_Tarea actual que se informa al usuario
  (Req 14.11).
- Un Rechazo_de_Destino no es un evento: la Tarea sigue en recibida (Req 16.12).

Los eventos que llevan a fallida (`SourceRejected`, `InternalError`, `Failed` y `TimedOut`)
traen el Motivo_de_Fallo, con su Etapa_de_Procesamiento, que el Gestor_de_Tareas registra en la
Tarea al aplicar la transición. Así toda Tarea fallida tiene un Motivo_de_Fallo (Req 16.1). La
máquina de estados no mira ese dato: solo decide el nuevo estado según la clase del evento.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, final

from converter.models import Failure, TaskState


@final
@dataclass(frozen=True, slots=True)
class ConfirmAccepted:
    """Confirmación del Formato_de_Destino aceptada, con el par y el idioma ya asociados."""


@final
@dataclass(frozen=True, slots=True)
class SourceRejected:
    """Rechazo_de_Origen durante la detección (Req 16.1)."""

    failure: Failure  # Motivo_de_Fallo del rechazo, con la etapa "detección"


@final
@dataclass(frozen=True, slots=True)
class InternalError:
    """Error no clasificado durante la detección o la ejecución (Req 16.9)."""

    failure: Failure  # "error_interno" con la etapa en curso


@final
@dataclass(frozen=True, slots=True)
class CancelRequested:
    """Cancelación solicitada por el usuario (Req 14.6)."""


@final
@dataclass(frozen=True, slots=True)
class RetentionExpired:
    """Venció el Período_de_Retención sin que se eligiera un Formato_de_Destino (Req 14.8)."""


@final
@dataclass(frozen=True, slots=True)
class Started:
    """El planificador asignó un lugar a la Tarea en_cola (Req 14.10)."""


@final
@dataclass(frozen=True, slots=True)
class Completed:
    """El Archivo_Resultado quedó entregado en el Espacio_de_Trabajo."""


@final
@dataclass(frozen=True, slots=True)
class Failed:
    """La conversión falló por un motivo clasificado, incluida la falta de espacio (Req 16.10)."""

    failure: Failure


@final
@dataclass(frozen=True, slots=True)
class TimedOut:
    """Venció el Tiempo_Máximo_de_Procesamiento de la Tarea (Req 14.7)."""

    failure: Failure  # "tiempo_excedido" con la etapa en curso


type TaskEvent = (
    ConfirmAccepted
    | SourceRejected
    | InternalError
    | CancelRequested
    | RetentionExpired
    | Started
    | Completed
    | Failed
    | TimedOut
)


@final
@dataclass(frozen=True, slots=True)
class InvalidTransition:
    """El evento no se admite en el estado actual; la Tarea conserva su Estado_de_Tarea."""

    state: TaskState  # Estado_de_Tarea actual, sin cambios (Req 14.11)
    event: TaskEvent


# Tabla del diseño. Las clases de evento son finales, así que basta comparar la clase exacta
_TRANSITIONS: Final[Mapping[tuple[TaskState, type[TaskEvent]], TaskState]] = MappingProxyType(
    {
        (TaskState.RECEIVED, ConfirmAccepted): TaskState.QUEUED,
        (TaskState.RECEIVED, SourceRejected): TaskState.FAILED,
        (TaskState.RECEIVED, InternalError): TaskState.FAILED,
        (TaskState.RECEIVED, CancelRequested): TaskState.CANCELLED,
        (TaskState.RECEIVED, RetentionExpired): TaskState.CANCELLED,
        (TaskState.QUEUED, Started): TaskState.PROCESSING,
        (TaskState.QUEUED, CancelRequested): TaskState.CANCELLED,
        (TaskState.PROCESSING, Completed): TaskState.COMPLETED,
        (TaskState.PROCESSING, Failed): TaskState.FAILED,
        (TaskState.PROCESSING, TimedOut): TaskState.FAILED,
        (TaskState.PROCESSING, InternalError): TaskState.FAILED,
        (TaskState.PROCESSING, CancelRequested): TaskState.CANCELLED,
    }
)


def transition(state: TaskState, event: TaskEvent) -> TaskState | InvalidTransition:
    """Nuevo Estado_de_Tarea tras aplicar el evento, o `InvalidTransition` si no se admite."""
    new_state = _TRANSITIONS.get((state, type(event)))
    if new_state is None:
        return InvalidTransition(state=state, event=event)
    return new_state
