"""Núcleo puro del planificador del Gestor_de_Tareas (Req 14.4, 14.10, 19.8).

`SchedulerCore` guarda una única cola FIFO con los Identificadores_de_Tarea en_cola, en el orden
en que se registró su ingreso a en_cola, y el conjunto de Identificadores de las Tareas
en_proceso. No tiene E/S, reloj ni asyncio y no conoce los Estados_de_Tarea: la capa asíncrona
del Gestor_de_Tareas aplica las transiciones con la máquina de estados y llama a estos métodos.

- `enqueue` registra el ingreso de una Tarea a en_cola detrás de las que ya esperan. Hay una sola
  cola y un solo Límite_de_Concurrencia para todas las Tareas, sin importar el
  Navegador_del_Usuario desde el que se crearon (Req 19.8).
- `remove` saca una Tarea de la cola (cancelación en_cola) o del conjunto en_proceso (Tarea que
  se completó, falló, se canceló o superó su Tiempo_Máximo_de_Procesamiento). Es idempotente:
  los caminos de limpieza pueden llamarlo más de una vez para la misma Tarea sin que eso
  interrumpa el despacho de las demás (Req 16.11).
- `dispatch` saca de la cola las Tareas que ingresaron primero mientras haya menos Tareas
  en_proceso que el Límite_de_Concurrencia, y devuelve las que deben pasar a en_proceso, en
  orden de ingreso (Req 14.4, 14.10). No se llama solo: la capa asíncrona lo llama después de
  cada cambio que agrega o libera trabajo.

Invariantes que mantienen los tres métodos: cada Identificador está como mucho una vez entre la
cola y el conjunto en_proceso, el conjunto en_proceso nunca supera el Límite_de_Concurrencia y,
después de `dispatch`, la cola está vacía o el conjunto en_proceso está lleno.

Ningún método espera ni bloquea, y el único bucle asyncio del proceso los ejecuta de a uno, por
lo que no hace falta un lock.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class SchedulerCore:
    """Cola FIFO global y conjunto en_proceso acotado por el Límite_de_Concurrencia."""

    limit: int  # Límite_de_Concurrencia (CONVERTER_CONCURRENCY, de 1 a 64)
    # Ambos empiezan vacíos: el Gestor_de_Tareas no recupera Tareas al iniciar (Req 14.13)
    queue: deque[str] = field(default_factory=deque, init=False)  # orden de ingreso a en_cola
    running: set[str] = field(default_factory=set, init=False)  # Tareas en_proceso

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError(f"limit debe ser 1 o mayor: {self.limit}")

    def enqueue(self, task_id: str) -> None:
        """Registra el ingreso de la Tarea a en_cola al final de la cola.

        Una Tarea que ya está en la cola o en_proceso indica un error del llamador: se rechaza
        con `ValueError` sin cambiar nada, porque repetirla la despacharía dos veces. El mensaje
        no incluye el Identificador_de_Tarea para que no quede en registros.
        """
        if task_id in self.running or task_id in self.queue:
            raise ValueError("la Tarea ya está en la cola o en_proceso")
        self.queue.append(task_id)

    def remove(self, task_id: str) -> None:
        """Saca la Tarea de la cola o del conjunto en_proceso; si no está, no hace nada.

        Las demás Tareas en_cola conservan su orden. Liberar un lugar no despacha: el llamador
        debe llamar a `dispatch` después.
        """
        if task_id in self.running:
            self.running.remove(task_id)
        elif task_id in self.queue:
            self.queue.remove(task_id)

    def dispatch(self) -> list[str]:
        """Pasa a en_proceso las Tareas en_cola más antiguas mientras haya lugar.

        Devuelve sus Identificadores en orden de ingreso a en_cola, para que el llamador les
        aplique `Started`. Si no hay lugar o la cola está vacía, devuelve una lista vacía.
        """
        started: list[str] = []
        while self.queue and len(self.running) < self.limit:
            task_id = self.queue.popleft()
            self.running.add(task_id)
            started.append(task_id)
        return started
