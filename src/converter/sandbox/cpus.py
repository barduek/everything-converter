"""CPU efectivas del Contenedor, calculadas una vez por ciclo de vida (Req 11.8, 13.4).

Las CPU efectivas son la menor de estas dos cantidades:

- La cuota de CPU del cgroup v2, que `/sys/fs/cgroup/cpu.max` expresa como
  "<cuota> <período>" en microsegundos ("max <período>" si no hay cuota). La razón se redondea
  hacia arriba: con `docker run --cpus 1.5` el archivo contiene "150000 100000" y da 2.
- Las CPU en las que el proceso puede ejecutarse según su afinidad (`os.sched_getaffinity`),
  que refleja `--cpuset-cpus`. Fuera de Linux, donde no existe esa función, se usa
  `os.cpu_count()`.

Si `cpu.max` no existe, no se puede leer o no tiene esa forma, se usa solo la afinidad.

El Conversor fija con este valor, para todo el ciclo de vida del Contenedor, la cantidad de hilos
de FFmpeg, OpenH264, libvpx y whisper.cpp: con una cantidad fija de hilos, el resultado es
reproducible en el mismo Equipo_de_Ejecución. Por eso `effective_cpus()` hace el cálculo en la
primera llamada y después devuelve siempre el mismo valor, aunque la cuota o la afinidad
cambien mientras el Conversor está en marcha.
"""

from __future__ import annotations

import functools
import os
import sys
from collections.abc import Callable
from pathlib import Path

CGROUP_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")

_UNLIMITED = "max"


def _is_digits(text: str) -> bool:
    # Solo dígitos ASCII: `int()` aceptaría además signos, "_" y dígitos no ASCII
    return text.isascii() and text.isdigit()


def parse_cpu_max(text: str) -> int | None:
    """CPU que permite el contenido de `cpu.max`, redondeadas hacia arriba.

    Devuelve None si no hay cuota ("max <período>") o si el texto no tiene la forma
    "<cuota> <período>" con dos enteros positivos.
    """
    fields = text.split()
    if len(fields) != 2:
        return None
    quota, period = fields
    if quota == _UNLIMITED or not (_is_digits(quota) and _is_digits(period)):
        return None
    quota_us, period_us = int(quota), int(period)
    if quota_us < 1 or period_us < 1:
        return None
    return -(-quota_us // period_us)  # división entera hacia arriba; es al menos 1


def read_cpu_quota(path: Path = CGROUP_CPU_MAX) -> int | None:
    """CPU de la cuota del cgroup en `path`, o None si no hay cuota o no se puede leer."""
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    return parse_cpu_max(text)


def affinity_cpus() -> int:
    """CPU en las que el proceso actual puede ejecutarse; al menos 1."""
    if sys.platform.startswith("linux"):
        return max(1, len(os.sched_getaffinity(0)))
    return max(1, os.cpu_count() or 1)


def compute_effective_cpus(
    *, cpu_max: Path = CGROUP_CPU_MAX, affinity: Callable[[], int] = affinity_cpus
) -> int:
    """Menor valor entre la cuota de `cpu_max` y `affinity()`; al menos 1.

    Lee los datos en cada llamada. El Conversor usa `effective_cpus()`, que guarda el primer
    resultado.
    """
    available = max(1, affinity())
    quota = read_cpu_quota(cpu_max)
    return available if quota is None else min(available, quota)


@functools.cache
def effective_cpus() -> int:
    """CPU efectivas del Contenedor: se calculan en la primera llamada y no cambian después."""
    # `CGROUP_CPU_MAX` se lee al llamar, no al definir la función
    return compute_effective_cpus(cpu_max=CGROUP_CPU_MAX)
