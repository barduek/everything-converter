"""Barrendero del Gestor_de_Archivos_Temporales (Req 1.3, 1.4, 1.8, 1.10, 17.2–17.4, 17.9, 17.10).

Los borrados del Almacenamiento_Temporal no se hacen en el momento en que se piden: quien los
pide los encola y el barrendero los ejecuta en su próxima pasada. Las pasadas corren cada 5
segundos, así cada borrado ocurre muy por debajo de los 60 segundos que exigen los requisitos.

- `enqueue_remove` elimina el Espacio_de_Trabajo completo: el de una recepción que termina sin
  crear una Tarea, con `task_id=None` (Req 1.3, 1.4, 1.8, 1.10, 17.10), el de una Tarea fallida
  o cancelada (Req 17.4) y el de una Tarea completada al vencer el Período_de_Retención
  (Req 17.3).
- `enqueue_prune` deja solo `result/` en el Espacio_de_Trabajo de una Tarea completada
  (Req 17.2).
- `enqueue_purge_failures` recibe los Espacios_de_Trabajo que la purga del arranque no pudo
  eliminar (Req 17.5): registra ese primer intento fallido y programa los reintentos, sin Tarea.

Reintentos (Req 17.9, D13). Si un borrado falla, el barrendero registra el evento
`cleanup_failed` con el Identificador_de_Tarea (o None), el nombre del Espacio_de_Trabajo, el
número de intento y el motivo saneado, y lo reintenta hasta 3 veces. Cada reintento corre en la
primera pasada desde que pasaron 60 segundos del intento anterior, es decir, entre 60 y 65
segundos después. El nivel es "warning" mientras quedan reintentos y "error" en el cuarto y
último intento; después el borrado se descarta. Repetir un borrado es seguro: `WorkspaceManager`
no hace nada si el Espacio_de_Trabajo ya no existe.

El barrendero solo conoce Espacios_de_Trabajo e Identificadores_de_Tarea. No tiene acceso a las
Tareas y nunca cambia un Estado_de_Tarea (Req 17.9).

Hay como mucho un borrado pendiente por Espacio_de_Trabajo. Si se pide otro para un espacio que
ya tiene uno, la cola no crece: eliminar el espacio completo cubre la poda, y entre dos borrados
del mismo tipo queda el que lleva más intentos. Así los pedidos repetidos no reinician la cuenta
de reintentos ni duplican los eventos del log.

El reloj es monotónico, en segundos, y se inyecta para las pruebas. `run_due` hace una pasada y
`run` la repite cada 5 segundos hasta que se cancela. Los borrados bloquean, así que corren de a
uno en un hilo (`anyio.to_thread`) para no frenar el bucle de eventos. Si se cancela una pasada,
el borrado en curso termina en su hilo y no se reintenta; los demás siguen en la cola, y la
purga del próximo arranque elimina lo que haya quedado.

Los métodos no son seguros entre hilos: se llaman desde el único bucle asyncio del proceso.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from enum import IntEnum
from pathlib import Path
from typing import NoReturn

import anyio
from anyio import to_thread

from converter.eventlog import EventLog
from converter.storage.workspace import RemovalFailure, Workspace, WorkspaceManager

SWEEP_INTERVAL_S = 5.0  # período entre pasadas
RETRY_DELAY_S = 60.0  # D13: tiempo mínimo entre un intento fallido y el siguiente
MAX_RETRIES = 3  # D13: reintentos después del primer intento
MAX_ATTEMPTS = 1 + MAX_RETRIES


class CleanupKind(IntEnum):
    """Tipo de borrado. El orden importa: un tipo mayor cubre a los menores."""

    PRUNE = 1  # Req 17.2: borra todo salvo result/
    REMOVE = 2  # Req 1.3, 1.4, 1.8, 1.10, 17.3, 17.4, 17.10: borra el Espacio_de_Trabajo


@dataclass(frozen=True, slots=True)
class CleanupJob:
    """Borrado pendiente de un Espacio_de_Trabajo."""

    workspace: Workspace
    kind: CleanupKind
    task_id: str | None  # None: recepción que no creó Tarea o purga del arranque
    attempt: int  # número del próximo intento, desde 1
    due: float  # instante del reloj monotónico desde el que se puede ejecutar
    seq: int  # orden de llegada; desempata los borrados que vencen a la vez


class Sweeper:
    """Ejecuta los borrados encolados en pasadas periódicas y reintenta los que fallan."""

    __slots__ = ("_clock", "_interval", "_log", "_manager", "_pending", "_seq")

    def __init__(
        self,
        manager: WorkspaceManager,
        log: EventLog,
        *,
        clock: Callable[[], float] = time.monotonic,
        interval: float = SWEEP_INTERVAL_S,
    ) -> None:
        if not interval > 0:
            raise ValueError(f"interval debe ser mayor que 0: {interval}")
        self._manager = manager
        self._log = log
        self._clock = clock  # segundos de un reloj monotónico
        self._interval = interval
        self._pending: dict[Path, CleanupJob] = {}  # por ruta del Espacio_de_Trabajo
        self._seq = itertools.count()

    @property
    def pending(self) -> tuple[CleanupJob, ...]:
        """Borrados pendientes, en el orden en que se van a ejecutar."""
        return tuple(sorted(self._pending.values(), key=lambda job: (job.due, job.seq)))

    def enqueue_remove(self, ws: Workspace, *, task_id: str | None) -> None:
        """Encola la eliminación del Espacio_de_Trabajo completo para la próxima pasada.

        `task_id` es None si el espacio es de una recepción que terminó sin crear una Tarea.
        """
        self._put(self._new_job(ws, CleanupKind.REMOVE, task_id))

    def enqueue_prune(self, ws: Workspace, *, task_id: str) -> None:
        """Encola la poda del Espacio_de_Trabajo de una Tarea completada: queda solo `result/`."""
        self._put(self._new_job(ws, CleanupKind.PRUNE, task_id))

    def enqueue_purge_failures(self, failures: Iterable[RemovalFailure]) -> None:
        """Registra los fallos de la purga del arranque y programa sus reintentos (Req 17.9).

        Cada fallo cuenta como el primer intento de eliminar ese Espacio_de_Trabajo, sin Tarea.
        """
        for failure in failures:
            job = self._new_job(failure.workspace, CleanupKind.REMOVE, None)
            self._failed(job, failure.error)

    async def run_due(self) -> int:
        """Hace una pasada: ejecuta los borrados vencidos y devuelve cuántos intentó.

        Ejecuta en orden de vencimiento los que vencen hasta el instante en que empieza la
        pasada. Un fallo se registra y, si quedan reintentos, se reprograma.
        """
        now = self._clock()
        attempted = 0
        for job in self.pending:
            if job.due > now:
                break
            key = job.workspace.path
            if self._pending.get(key) is not job:
                continue  # mientras se esperaba un hilo, otro pedido lo reemplazó
            del self._pending[key]
            attempted += 1
            operation = (
                self._manager.remove
                if job.kind is CleanupKind.REMOVE
                else self._manager.prune_for_completed
            )
            try:
                await to_thread.run_sync(operation, job.workspace)
            except Exception as exc:  # cualquier fallo se registra y se reintenta
                self._failed(job, exc)
        return attempted

    async def run(self) -> NoReturn:
        """Hace una pasada cada `interval` segundos hasta que se cancela."""
        while True:
            await self.run_due()
            await anyio.sleep(self._interval)

    def _new_job(self, ws: Workspace, kind: CleanupKind, task_id: str | None) -> CleanupJob:
        return CleanupJob(ws, kind, task_id, attempt=1, due=self._clock(), seq=next(self._seq))

    def _put(self, job: CleanupJob) -> None:
        # Un borrado por espacio: gana el tipo mayor y, en el mismo tipo, el de más intentos
        current = self._pending.get(job.workspace.path)
        if current is None or (job.kind, job.attempt) > (current.kind, current.attempt):
            self._pending[job.workspace.path] = job

    def _failed(self, job: CleanupJob, error: BaseException) -> None:
        will_retry = job.attempt < MAX_ATTEMPTS
        self._log.cleanup_failed(
            task_id=job.task_id,
            workspace=job.workspace.name,
            error=error,
            attempt=job.attempt,
            will_retry=will_retry,
        )
        if will_retry:
            self._put(replace(job, attempt=job.attempt + 1, due=self._clock() + RETRY_DELAY_S))
