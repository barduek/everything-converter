"""Límites de recursos de los subprocesos y sus valores predeterminados por herramienta.

Cubre Req 13.7 y 14.7. `Limits` describe los cuatro límites que el lanzador
(`converter.sandbox.launch`) fija con `setrlimit` antes de ejecutar el programa: espacio de
direcciones (`RLIMIT_AS`), tiempo de CPU (`RLIMIT_CPU`), tamaño de archivo (`RLIMIT_FSIZE`) y
archivos abiertos (`RLIMIT_NOFILE`). Los tamaños se expresan en MB de 1 000 000 bytes, como las
Variables_de_Configuración.

`tool_limits` da los límites predeterminados de cada herramienta, que se ajustan con las pruebas
de integración:

- Espacio de direcciones: Python con Pillow o pypdfium2, 2 GB; WeasyPrint, 3 GB; LibreOffice,
  4 GB; FFmpeg (y ffprobe), 3 GB; whisper-cli, 3 GB.
- Tiempo de CPU: es un respaldo del Tiempo_Máximo_de_Procesamiento, que aplica el
  Gestor_de_Tareas. Un proceso no puede consumir más tiempo de CPU que el que suman todas las
  CPU efectivas durante el timeout, así que el límite (ese producto más un margen de 10 s) solo
  actúa sobre un proceso que sigue vivo después del timeout, por ejemplo uno que escapó de su
  grupo de procesos.
- Tamaño de archivo: 20 000 MB, un respaldo contra una salida que crece sin fin. Ningún resultado
  ni Archivo_Intermedio legítimo se acerca a ese tamaño con los límites de entrada vigentes.
- Archivos abiertos: 256.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
from types import MappingProxyType

from converter.sandbox.cpus import effective_cpus
from converter.sandbox.launch import DEFAULT_OPEN_FILES

DEFAULT_FILE_SIZE_MB = 20_000
CPU_MARGIN_SECONDS = 10


@dataclass(frozen=True, slots=True, kw_only=True)
class Limits:
    """Límites de un subproceso; cada valor es un entero mayor o igual a 1."""

    address_space_mb: int  # RLIMIT_AS
    cpu_seconds: int  # RLIMIT_CPU (respaldo; el timeout real es del Gestor_de_Tareas)
    file_size_mb: int  # RLIMIT_FSIZE
    open_files: int = DEFAULT_OPEN_FILES  # RLIMIT_NOFILE

    def __post_init__(self) -> None:
        # El lanzador rechaza los valores menores que 1; se detectan antes de lanzar
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field.name} debe ser un entero positivo: {value!r}")


class SandboxTool(StrEnum):
    """Programa que corre en el subproceso; elige sus límites predeterminados."""

    PYTHON = "python"  # workers e inspectores en Python con Pillow o pypdfium2
    WEASYPRINT = "weasyprint"  # worker en Python que usa WeasyPrint
    LIBREOFFICE = "libreoffice"
    FFMPEG = "ffmpeg"  # también ffprobe
    WHISPER_CLI = "whisper-cli"


ADDRESS_SPACE_MB: Mapping[SandboxTool, int] = MappingProxyType(
    {
        SandboxTool.PYTHON: 2_000,
        SandboxTool.WEASYPRINT: 3_000,
        SandboxTool.LIBREOFFICE: 4_000,
        SandboxTool.FFMPEG: 3_000,
        SandboxTool.WHISPER_CLI: 3_000,
    }
)


def tool_limits(tool: SandboxTool, *, timeout_seconds: int, cpus: int | None = None) -> Limits:
    """Límites predeterminados de `tool` para un paso que se corta a los `timeout_seconds`.

    `cpus` son las CPU efectivas; si se omite, se usa `effective_cpus()`. El tiempo de CPU es
    `timeout_seconds * cpus + CPU_MARGIN_SECONDS` (ver el docstring del módulo).
    """
    if timeout_seconds < 1:
        raise ValueError(f"timeout_seconds debe ser un entero positivo: {timeout_seconds!r}")
    if cpus is None:
        cpus = effective_cpus()
    elif cpus < 1:
        raise ValueError(f"cpus debe ser un entero positivo: {cpus!r}")
    return Limits(
        address_space_mb=ADDRESS_SPACE_MB[SandboxTool(tool)],
        cpu_seconds=timeout_seconds * cpus + CPU_MARGIN_SECONDS,
        file_size_mb=DEFAULT_FILE_SIZE_MB,
    )
