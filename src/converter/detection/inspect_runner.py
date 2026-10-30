"""Función de inspección del Detector_de_Tipo: inspectores en subproceso (Req 2.11, 16.2, 16.9,
19.10).

`InspectRunner` es la función de inspección (`detector.Inspect`) que recibe `TypeDetector` en el
Conversor. Para cada `InspectionRequest`:

1. Busca el inspector en el registro de `detection/inspectors/`. Si no está registrado (el
   catálogo nombra un inspector que no existe), el resultado es `error_interno` y no se lanza
   ningún subproceso.
2. Espera un lugar en su semáforo: corren como máximo `MAX_CONCURRENT_INSPECTIONS` (4)
   inspecciones a la vez. El semáforo es propio de la detección y está separado del
   Límite_de_Concurrencia del planificador, así que la detección no espera a que terminen las
   Tareas en_proceso (Req 19.10).
3. Ejecuta `python -I -m converter.workers.inspect` con `SandboxRunner`: los límites
   predeterminados de la `sandbox_tool` del inspector, el entorno mínimo con `HOME` y `TMPDIR` en
   `tmp/` del Espacio_de_Trabajo, y `tmp/` como directorio de trabajo. El worker recibe el
   Tipo_Real, el Límite_de_Píxeles y la ruta absoluta del Archivo_de_Origen.
4. Corta el subproceso a los `INSPECT_TIMEOUT_SECONDS` (8) segundos, dentro del plazo de 10
   segundos de Req 2.11: el ejecutor envía SIGTERM al grupo de procesos, espera hasta 2 segundos
   y envía SIGKILL. El plazo cuenta desde que el subproceso obtiene su lugar en el semáforo; el
   Gestor_de_Tareas acota además la detección completa a 10 segundos.
5. Concatena las líneas de stdout del worker (ver `workers/inspect.py`), las interpreta con
   `load_inspector_output` y comprueba que la información sea del `info_type` del inspector.

Resultado:

- La información o el rechazo que escribió el inspector. El detector agrega el Tipo_Real al
  rechazo.
- Si el subproceso termina con un código distinto de 0 o por una señal y el inspector declara
  su `tool`, la salida de error saneada se clasifica con `classify_failure`. Si el motivo es un
  rechazo de inspector que no necesita parámetros, ese es el rechazo; por ejemplo,
  `archivo_danado` cuando Pillow informa "cannot identify image file" (Req 16.2). Si no, o si el
  inspector no declara `tool`, es `error_interno`.
- `error_interno` (Req 16.9) si el inspector no está registrado, si el subproceso no puede
  iniciarse, si vence el timeout, o si la salida no es válida (`InspectorOutputError`), supera
  `MAX_OUTPUT_CHARS` caracteres o es de otro tipo de `SourceInfo`. Un timeout de la detección no
  es `tiempo_excedido`: ese motivo es del Tiempo_Máximo_de_Procesamiento de las Tareas
  en_proceso y no ocurre en la etapa detección.

Cada rechazo que arma este módulo lleva en `SourceRejection.detail` el detalle técnico saneado
para el evento `task_failed` (Req 16.7, 16.8): el `ToolDetail` de `sanitize_tool_output`, que no
copia ninguna línea, o el `ErrorDetail` de `describe_exception`, sin el mensaje de la excepción.
Ni la salida del worker ni la ruta del archivo llegan al Log_de_Eventos ni al usuario.

La cancelación de quien espera (por ejemplo, al cancelar la Tarea) se propaga como
`CancelledError` después de que el ejecutor detiene el subproceso. Las demás excepciones, que
indican un error del Conversor y no del archivo, también se propagan: el Gestor_de_Tareas las
convierte en `error_interno`.
"""

from __future__ import annotations

import asyncio
import math
import sys
from typing import TYPE_CHECKING

from converter.converters.errors import classify_failure
from converter.detection.inspectors import (
    InspectorSpec,
    UnknownInspectorError,
    get_inspector,
)
from converter.detection.source_info import (
    INSPECTION_REJECTIONS,
    InspectorOutputError,
    SourceInfo,
    SourceRejection,
    load_inspector_output,
)
from converter.eventlog import ErrorDetail, ToolDetail, describe_exception, sanitize_tool_output
from converter.messages_es import FailureReason
from converter.sandbox.cpus import effective_cpus
from converter.sandbox.limits import tool_limits
from converter.sandbox.runner import (
    CompletedRun,
    SandboxRunner,
    minimal_env,
    python_module_command,
)
from converter.workers import inspect as worker

if TYPE_CHECKING:
    from converter.detection.detector import InspectionRequest

INSPECT_TIMEOUT_SECONDS = 8.0  # dentro de los 10 segundos de Req 2.11
MAX_CONCURRENT_INSPECTIONS = 4  # separado del Límite_de_Concurrencia (Req 19.10)
# Tope de la salida de un inspector: con holgura para un PDF de cientos de miles de páginas
MAX_OUTPUT_CHARS = 16 * 1024 * 1024


class InspectorProcessError(RuntimeError):
    """El subproceso de un inspector que no declara `tool` terminó con error."""


def worker_command(
    spec: InspectorSpec,
    request: InspectionRequest,
    *,
    max_image_pixels: int,
    python: str = sys.executable,
) -> list[str]:
    """`python -I -m converter.workers.inspect …` para inspeccionar el origen de `request`."""
    args = worker.WorkerArgs(
        name=spec.name,
        path=request.workspace.source_file.absolute(),
        real_type=request.real_type,
        max_image_pixels=max_image_pixels,
    )
    return python_module_command(worker.MODULE, *worker.format_args(args), python=python)


class _OutputBuffer:
    """Junta las líneas de stdout del worker, hasta `MAX_OUTPUT_CHARS` caracteres."""

    __slots__ = ("_chars", "_parts")

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._chars = 0

    def append(self, line: str) -> None:
        self._chars += len(line)
        if self._chars > MAX_OUTPUT_CHARS:
            # Desde el callback: el ejecutor detiene el subproceso y propaga el error
            raise InspectorOutputError(f"la salida supera los {MAX_OUTPUT_CHARS} caracteres")
        self._parts.append(line)

    def text(self) -> str:
        return "".join(self._parts)


def _rejection(reason: FailureReason, detail: ToolDetail | ErrorDetail) -> SourceRejection:
    """Rechazo con `reason` si es de un inspector y no pide parámetros; si no, `error_interno`."""
    if reason in INSPECTION_REJECTIONS:
        try:
            return SourceRejection(reason, detail=detail)
        except ValueError:
            pass  # el mensaje pide parámetros que la salida saneada no trae
    return SourceRejection(FailureReason.INTERNAL_ERROR, detail=detail)


def _internal_error(exc: BaseException) -> SourceRejection:
    return SourceRejection(FailureReason.INTERNAL_ERROR, detail=describe_exception(exc))


def _failed_run(spec: InspectorSpec, completed: CompletedRun) -> SourceRejection:
    """Rechazo de un subproceso que terminó con un código distinto de 0 o por una señal."""
    if spec.tool is None:
        error = InspectorProcessError(f"el inspector {spec.name} terminó con error")
        return _internal_error(error)
    detail = sanitize_tool_output(
        spec.tool, completed.stderr_lines, returncode=completed.returncode
    )
    return _rejection(classify_failure(detail=detail), detail)


def _parse(spec: InspectorSpec, text: str) -> SourceInfo | SourceRejection:
    """Resultado del worker; lanza `InspectorOutputError` si no es válido para `spec`."""
    result = load_inspector_output(text)
    if isinstance(result, SourceInfo) and not isinstance(result, spec.info_type):
        raise InspectorOutputError(f"info.type: el inspector {spec.name} no produce ese tipo")
    return result


class InspectRunner:
    """Función de inspección que ejecuta los inspectores en subprocesos aislados."""

    __slots__ = ("_cpus", "_max_image_pixels", "_python", "_runner", "_semaphore", "_timeout")

    def __init__(
        self,
        runner: SandboxRunner,
        *,
        max_image_pixels: int,
        timeout_seconds: float = INSPECT_TIMEOUT_SECONDS,
        max_concurrent: int = MAX_CONCURRENT_INSPECTIONS,
        cpus: int | None = None,
        python: str = sys.executable,
    ) -> None:
        """`max_image_pixels` es el Límite_de_Píxeles de la configuración.

        `cpus` son las CPU efectivas para el límite de tiempo de CPU; si se omite, se usa
        `effective_cpus()`. `python` es el intérprete que ejecuta el worker. Lanza `ValueError`
        si un valor no es positivo.
        """
        if isinstance(max_image_pixels, bool) or max_image_pixels < 1:
            raise ValueError(f"max_image_pixels debe ser un entero positivo: {max_image_pixels!r}")
        if not (math.isfinite(timeout_seconds) and timeout_seconds > 0):
            raise ValueError(f"timeout_seconds debe ser positivo: {timeout_seconds!r}")
        if isinstance(max_concurrent, bool) or max_concurrent < 1:
            raise ValueError(f"max_concurrent debe ser un entero positivo: {max_concurrent!r}")
        self._runner = runner
        self._max_image_pixels = max_image_pixels
        self._timeout = float(timeout_seconds)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._cpus = effective_cpus() if cpus is None else cpus
        self._python = python

    async def __call__(self, request: InspectionRequest) -> SourceInfo | SourceRejection:
        """Ejecuta el inspector de `request` y devuelve su información o un rechazo."""
        try:
            spec = get_inspector(request.inspector)
        except UnknownInspectorError as exc:
            return _internal_error(exc)
        argv = worker_command(
            spec, request, max_image_pixels=self._max_image_pixels, python=self._python
        )
        limits = tool_limits(
            spec.sandbox_tool, timeout_seconds=math.ceil(self._timeout), cpus=self._cpus
        )
        tmp_dir = request.workspace.tmp_dir
        output = _OutputBuffer()
        async with self._semaphore:
            try:
                async with asyncio.timeout(self._timeout):
                    completed = await self._runner.run(
                        argv,
                        cwd=tmp_dir,
                        limits=limits,
                        env=minimal_env(tmp_dir),
                        on_stdout_line=output.append,
                    )
            except TimeoutError as exc:
                return _internal_error(exc)
            except (OSError, InspectorOutputError) as exc:
                # El lanzador no pudo iniciarse, o la salida superó MAX_OUTPUT_CHARS
                return _internal_error(exc)
        if completed.returncode != 0:
            return _failed_run(spec, completed)
        try:
            return _parse(spec, output.text())
        except InspectorOutputError as exc:
            return _internal_error(exc)
