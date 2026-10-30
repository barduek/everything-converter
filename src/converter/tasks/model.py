"""Tarea, resultado y recepción completada del Gestor_de_Tareas (Req 1.1, 1.2, 1.7, 19.2).

- `CompletedUpload` es lo que la API entrega al Gestor_de_Tareas cuando termina de recibir un
  archivo: el Espacio_de_Trabajo en que se recibió, con el Archivo_de_Origen en
  `source/original`, el Nombre_Original sin modificar y el tamaño en bytes (Req 1.1, 1.7).
- `ConfirmRequest` es la confirmación del Formato_de_Destino: el destino, el
  Idioma_de_Transcripción y la aceptación de la Descripción_de_Pérdida (Req 3.3, 14.1).
- `Task` es una Tarea. Asocia un Identificador_de_Tarea con un único Espacio_de_Trabajo, y por
  lo tanto con exactamente un Archivo_de_Origen (Req 1.2), su Nombre_Original, su Tipo_Real, el
  Par_de_Conversión elegido, el Idioma_de_Transcripción, su Estado_de_Tarea y, si falla, su
  Motivo_de_Fallo.
- `ResultInfo` describe el Archivo_Resultado entregado en `result/output`.

`Task` es el único modelo mutable. Solo lo modifica el Gestor_de_Tareas, que cambia `state`
únicamente con `tasks.state_machine.transition` (Req 14.3) y aumenta `version` en cada cambio
visible. Las solicitudes que modifican una Tarea (confirmar, rechazar la confirmación,
cancelar) toman su `lock`. El registro del resultado de la detección no lo toma: es una sola
asignación sin esperas en el único bucle asyncio, y una confirmación que tiene el lock espera
`detection_done`, así que tomarlo la bloquearía.

Los Datos_de_Tarea salen del proceso solo a través de `tasks.view.TaskView` (Req 19.6). Por eso
el `repr` de una Tarea omite el Nombre_Original, que el Log_de_Eventos no registra.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from converter.models import Failure, Notice, TaskState

if TYPE_CHECKING:
    from converter.detection.source_info import DetectionResult
    from converter.messages_es import CancelReason
    from converter.registry.model import ConversionPair
    from converter.storage.workspace import Workspace
    from converter.tasks.progress import ProgressReporter

MAX_ORIGINAL_NAME_CHARS = 255  # Req 1.10: un nombre más largo no crea una Tarea


@dataclass(frozen=True, slots=True)
class CompletedUpload:
    """Recepción terminada de un archivo: lo necesario para crear su Tarea (Req 1.1).

    La API valida la solicitud antes (Req 1.3, 1.4, 1.10). Estas comprobaciones son una
    salvaguarda: lanzan `TypeError` o `ValueError` si el nombre no es un texto o supera los 255
    caracteres, o si el tamaño no es un entero de al menos 1 byte.
    """

    workspace: Workspace  # Espacio_de_Trabajo de la recepción; el origen está en source/original
    original_name: str = field(repr=False)  # Nombre_Original sin modificar (Req 1.7)
    size_bytes: int  # tamaño del Archivo_de_Origen, de 1 byte al Límite_de_Tamaño_Global

    def __post_init__(self) -> None:
        if not isinstance(self.original_name, str):
            raise TypeError("el Nombre_Original debe ser un texto")
        if len(self.original_name) > MAX_ORIGINAL_NAME_CHARS:
            raise ValueError(f"el Nombre_Original supera los {MAX_ORIGINAL_NAME_CHARS} caracteres")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise TypeError("el tamaño del Archivo_de_Origen debe ser un entero")
        if self.size_bytes < 1:
            raise ValueError("el Archivo_de_Origen tiene al menos 1 byte")


@dataclass(frozen=True, slots=True)
class ConfirmRequest:
    """Confirmación del Formato_de_Destino que la API entrega al Gestor_de_Tareas (Req 3.3,
    11.3, 14.1).

    Los valores son los que envió el usuario, sin validar contra el registro: el
    Gestor_de_Tareas decide si forman un par, si falta aceptar la pérdida o si el idioma es un
    Idioma_Soportado. Estas comprobaciones de tipo son una salvaguarda de la validación de la
    API: lanzan `TypeError` si `target` no es un texto, `language` no es un texto ni None o
    `acknowledge_loss` no es un booleano.
    """

    target: str  # nombre del Formato_de_Destino pedido, p. ej. "JPG"
    # Idioma_de_Transcripción pedido: código, "auto" o None (español, Req 11.10). Solo se usa
    # en un Par_de_Transcripción
    language: str | None = None
    acknowledge_loss: bool = False  # el usuario aceptó la Descripción_de_Pérdida (Req 3.3)

    def __post_init__(self) -> None:
        if not isinstance(self.target, str):
            raise TypeError("el Formato_de_Destino pedido debe ser un texto")
        if self.language is not None and not isinstance(self.language, str):
            raise TypeError("el Idioma_de_Transcripción pedido debe ser un texto o None")
        if not isinstance(self.acknowledge_loss, bool):
            raise TypeError("acknowledge_loss debe ser un booleano")


@dataclass(frozen=True, slots=True)
class ResultInfo:
    """Archivo_Resultado entregado de una Tarea completada."""

    path: Path  # result/output del Espacio_de_Trabajo; nunca sale del proceso
    download_name: str  # nombre de descarga (Req 15.2, 15.10)
    media_type: str  # Content-Type del Formato_de_Destino, o de ZIP
    size_bytes: int
    completed_at: float  # reloj monotónico del Gestor_de_Tareas al pasar a completada
    expired: bool = False  # venció el Período_de_Retención (Req 15.8)


@dataclass(eq=False, slots=True, kw_only=True)
class Task:
    """Tarea. Dos Tareas son iguales solo si son el mismo objeto."""

    id: str  # Identificador_de_Tarea: UUID v4 en forma canónica (Req 19.2)
    workspace: Workspace  # Espacio_de_Trabajo de la recepción (Req 1.1, 1.2)
    original_name: str = field(repr=False)  # Nombre_Original sin modificar (Req 1.7)
    size_bytes: int
    created_at: float  # reloj monotónico del Gestor_de_Tareas al crear la Tarea
    state: TaskState = TaskState.RECEIVED
    version: int = 0  # aumenta en cada cambio visible
    # Tipo_Real, si el Detector_de_Tipo lo determinó, también en un Rechazo_de_Origen (Req 16.7)
    real_type: str | None = None
    detection: DetectionResult | None = None  # detección terminada sin Rechazo_de_Origen
    # Se activa cuando la detección termina, con o sin rechazo, o se interrumpe
    detection_done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    pair: ConversionPair | None = None  # Par_de_Conversión confirmado
    language: str | None = None  # código ISO o "auto"; solo en un Par_de_Transcripción
    detected_language: str | None = None  # Idioma_Detectado con Detección_Automática_de_Idioma
    queued_seq: int | None = None  # orden de ingreso a en_cola
    started_at: float | None = None  # reloj monotónico al pasar a en_proceso
    progress: ProgressReporter | None = None  # Porcentaje_de_Avance mientras está en_proceso
    notices: list[Notice] = field(default_factory=list)  # avisos al usuario, en orden
    failure: Failure | None = None  # Motivo_de_Fallo, solo en fallida (Req 16.1)
    cancel_reason: CancelReason | None = None  # motivo de cancelación, solo en cancelada
    result: ResultInfo | None = None  # Archivo_Resultado, solo en completada
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
