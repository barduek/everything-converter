"""Porcentaje_de_Avance de una Tarea en_proceso (Req 14.5, 14.12).

`ProgressReporter` recibe fracciones crudas del avance y publica el Porcentaje_de_Avance como
`floor(100 × máximo)`, donde el máximo es la mayor fracción vista:

- Cada fracción se acota a [0, 1] antes de usarla: un valor negativo cuenta como 0 y uno mayor
  que 1 (incluido el infinito) cuenta como 1. Las fuentes de progreso son estimaciones (por
  ejemplo, `out_time_us` de FFmpeg dividido por una duración que puede quedar corta) y pueden
  salirse del rango.
- Una fracción NaN se ignora y deja el Porcentaje_de_Avance sin cambios.
- Como solo cuenta el máximo, una fracción menor que una anterior no hace retroceder el
  Porcentaje_de_Avance, que nunca disminuye entre publicaciones consecutivas (Req 14.12) y
  siempre es un entero entre 0 y 100.
- Empieza en 0 %: el Gestor_de_Tareas crea el reporter al pasar la Tarea a en_proceso, así la
  primera actualización está disponible de inmediato (Req 14.5).

Una Tarea de varios pasos asigna a cada paso una franja del avance total con `band(start, end)`.
Por ejemplo, MP4 a TXT usa `band(0.0, 0.1)` para la extracción del audio y `band(0.1, 1.0)` para
la transcripción. Cada `ProgressBand` recibe fracciones de su propio paso, acotadas e ignoradas
igual que en `ProgressReporter`, y las traslada a su franja, que también admite subfranjas. El
final de una franja se alcanza exacto, de modo que la franja que termina en 1 llega a 100 %.

Las fuentes de progreso aceptan cualquier `ProgressSink`: el reporter completo o una franja.

Los reporters no tienen lock: el único bucle asyncio del proceso es el que procesa la salida de
las herramientas y llama a `report`, de a una llamada por vez.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol


class ProgressSink(Protocol):
    """Destino de fracciones de avance: `ProgressReporter` o una de sus franjas."""

    def report(self, fraction: float) -> None: ...


def _clamp(fraction: float) -> float | None:
    """Acota la fracción a [0, 1]; devuelve None si es NaN."""
    if math.isnan(fraction):
        return None
    return min(max(fraction, 0.0), 1.0)


def _check_bounds(start: float, end: float) -> None:
    # Las comparaciones con NaN son falsas, así que un límite NaN también se rechaza
    if not 0.0 <= start <= end <= 1.0:
        raise ValueError(f"la franja debe cumplir 0 <= start <= end <= 1: [{start}, {end}]")


class ProgressReporter:
    """Porcentaje_de_Avance acotado a [0, 100] que no retrocede."""

    __slots__ = ("_maximum",)

    def __init__(self) -> None:
        self._maximum = 0.0  # mayor fracción vista, ya acotada a [0, 1]

    def report(self, fraction: float) -> None:
        """Registra una fracción cruda del avance total (acotada a [0, 1]; NaN se ignora)."""
        value = _clamp(fraction)
        if value is not None and value > self._maximum:
            self._maximum = value

    @property
    def percent(self) -> int:
        """Porcentaje_de_Avance: `floor(100 × máximo visto)`, entre 0 y 100."""
        return math.floor(100 * self._maximum)

    def band(self, start: float, end: float) -> ProgressBand:
        """Franja [start, end] del avance total para un paso de la Tarea.

        `start` y `end` son fracciones del avance total con `0 <= start <= end <= 1`; otros
        valores, incluido NaN, lanzan `ValueError`.
        """
        return ProgressBand(self, start, end)


@dataclass(frozen=True, slots=True)
class ProgressBand:
    """Paso de una Tarea que ocupa la franja [start, end] del avance total."""

    reporter: ProgressReporter
    start: float  # fracción del avance total al empezar el paso
    end: float  # fracción del avance total al terminar el paso

    def __post_init__(self) -> None:
        _check_bounds(self.start, self.end)

    def report(self, fraction: float) -> None:
        """Registra una fracción cruda del avance de este paso (acotada a [0, 1]; NaN se ignora)."""
        value = _clamp(fraction)
        if value is not None:
            self.reporter.report(self._to_total(value))

    def band(self, start: float, end: float) -> ProgressBand:
        """Subfranja [start, end] de este paso, con fracciones del paso y no del total."""
        _check_bounds(start, end)
        return ProgressBand(self.reporter, self._to_total(start), self._to_total(end))

    def _to_total(self, value: float) -> float:
        """Traslada una fracción del paso, ya acotada a [0, 1], a la franja del avance total.

        `start + ancho × valor` no decrece con el valor y vale `start` exacto en 0, pero en 1
        puede quedar un ulp por debajo o por encima de `end`. Por eso 1 da `end` exacto y el
        resto se acota a `end`.
        """
        if value >= 1.0:
            return self.end
        return min(self.end, self.start + (self.end - self.start) * value)
