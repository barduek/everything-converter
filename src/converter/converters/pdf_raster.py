"""Convertidor PDF a PNG, `pdf.raster` (Req 8.4, 8.5, 8.6, 8.8, 8.9, 8.10, 8.11, 15.10).

Rasteriza cada página del PDF a la Resolución_de_Rasterizado (`ctx.config.raster_dpi`) con el
subproceso `python -m converter.workers.pdf_raster` (ver `workers/pdf_raster.py`), a través del
Ejecutor aislado. Este módulo decide lo que no necesita PDFium:

- **Límite_de_Píxeles** (Req 8.11). Antes de lanzar el worker se recorren todas las páginas en
  orden de Número_de_Página con el tamaño mostrado que obtuvo el inspector `pdf`, sin redondear, y
  `raster_size`, la misma función que usa el worker para dibujar. La primera cuyo ancho × alto en
  píxeles supera `ctx.config.max_image_pixels` hace fallar la Tarea con `pagina_excede_pixeles`,
  con su Número_de_Página y su tamaño, sin rasterizar ninguna página ni dejar Archivo_Resultado.
  No es un Rechazo_de_Destino: la Tarea ya está en_proceso (D18).
- **Resultado** (Req 8.5, 8.9). Con una página, el Archivo_Resultado es su PNG (`result_kind`
  "single"); con varias, un ZIP de PNG sin carpetas (`result_kind` "zip", que el
  Gestor_de_Tareas nombra con ".zip", Req 15.10). Las entradas del ZIP se llaman
  `page_entry_name(Nombre_Base, N, T)` (Req 8.8). Una imagen por página, incluidas las
  Páginas_sin_Texto, y la imagen N es la página N (Req 8.4, 8.6).
- **Progreso.** Cada línea `page` del worker mueve el Porcentaje_de_Avance a la fracción de
  páginas procesadas (página N de T → N / T).
- **Permisos restringidos** (Req 8.10): PDFium los ignora, así que este módulo no hace nada
  especial.
- **Fallos del worker.** Con un código de salida distinto de 0, la salida de error se sanea con
  `sanitize_tool_output` y se clasifica con `classify_failure`: un PDF cuyo contenido PDFium no
  puede leer da `archivo_danado`, y la falta de espacio, `sin_espacio_proceso`. Un PDF_Protegido
  no llega aquí (el Detector_de_Tipo lo rechaza al recibirlo, Req 8.7); si aun así llegara, o si el
  motivo no es de la etapa de conversión, es `error_interno`. También lo es una salida del worker
  que no sigue su protocolo o que no rasterizó las páginas del inspector. El detalle técnico
  saneado va en `ConversionFailure.detail`.

Del `SourceInfo` del inspector `pdf` el módulo solo usa el tamaño mostrado de cada página, por eso
`PdfSource` lo declara como protocolo y no importa el inspector. No importa pypdfium2: la
biblioteca solo la carga el worker.

**Pendiente: Nombre_Original en el contexto.** Las entradas del ZIP se forman con el Nombre_Base
normalizado del Nombre_Original (Req 8.8), pero `ConversionContext` no lo trae. El módulo lo lee
del atributo `original_name` del contexto si existe (`entry_base`); si no, usa el Nombre_Base
vacío, es decir "resultado". Agregar ese campo a `ConversionContext` y pasarlo desde
`TaskManager._convert` completa el Req 8.8 sin cambiar este módulo.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from converter.converters.base import (
    ConversionContext,
    ConversionFailure,
    ConversionOutcome,
    ConversionRequest,
    register_converter,
)
from converter.converters.errors import classify_failure
from converter.detection.source_info import SourceInfo
from converter.eventlog import Tool, describe_exception, sanitize_tool_output
from converter.messages_es import FailureReason
from converter.models import DestinationRejection
from converter.naming import normalize_base, split_name
from converter.sandbox.limits import SandboxTool, tool_limits
from converter.sandbox.runner import CompletedRun, minimal_env, python_module_command
from converter.workers import pdf_raster as worker
from converter.workers.pdf_raster import DoneEvent, PageEvent, PageSize, WorkerOutputError

if TYPE_CHECKING:
    from converter.config import Config
    from converter.tasks.progress import ProgressSink

CONVERTER_ID = "pdf.raster"
OUTPUT_NAME = "output"  # en work/: un PNG o un ZIP, según la cantidad de páginas

# Motivos que el worker puede dar en la etapa de conversión; PDF_Protegido es de la detección
_WORKER_REASONS = frozenset({FailureReason.DAMAGED, FailureReason.NO_SPACE})


@runtime_checkable
class PdfSource(Protocol):
    """Lo que el convertidor usa del `PdfInfo` del inspector `pdf` (design.md, "Información del
    origen")."""

    @property
    def pages(self) -> Sequence[PageSize]:
        """Tamaño mostrado de cada página, sin redondear, en orden de Número_de_Página."""
        ...


def _pdf_source(source: SourceInfo) -> PdfSource:
    """`source` como información de un PDF. Lanza `TypeError` si es de otro Formato."""
    if not isinstance(source, PdfSource):
        raise TypeError(f"pdf.raster necesita la información de un PDF: {type(source)}")
    return source


@dataclass(frozen=True, slots=True)
class OversizedPage:
    """Una página que rasterizada supera el Límite_de_Píxeles."""

    page: int  # Número_de_Página, desde 1
    width: int  # ancho rasterizado en píxeles
    height: int  # alto rasterizado en píxeles


def first_page_over_limit(
    pages: Iterable[PageSize], dpi: int, max_pixels: int
) -> OversizedPage | None:
    """La primera página, en orden de Número_de_Página, cuyo ancho × alto en píxeles a `dpi`
    supera `max_pixels`, o None si ninguna lo supera (Req 8.11).

    Recorre las páginas en orden y se detiene en la primera; el tamaño sale de
    `raster_size`, la función con la que el worker rasteriza.
    """
    for number, page in enumerate(pages, start=1):
        width, height = worker.raster_size(page, dpi)
        if width * height > max_pixels:
            return OversizedPage(page=number, width=width, height=height)
    return None


def entry_base(ctx: ConversionContext) -> str:
    """Nombre_Base normalizado de las entradas del ZIP (Req 8.8, 15.12).

    Sale del Nombre_Original del contexto, si lo tiene (ver el docstring del módulo); en otro
    caso es "resultado", el Nombre_Base de un Nombre_Original sin base.
    """
    name = getattr(ctx, "original_name", None)
    base = split_name(name)[0] if isinstance(name, str) else ""
    return normalize_base(base)


class EventCollector:
    """Interpreta la salida estándar del worker, línea por línea, y reporta el avance.

    `feed` es el callback de líneas del Ejecutor aislado y nunca lanza excepciones: un error del
    protocolo queda guardado y `result` lo lanza, así la salida de error del worker, que
    clasifica el fallo, sigue disponible.
    """

    __slots__ = ("_done", "_error", "_pages", "_progress", "_total")

    def __init__(self, progress: ProgressSink) -> None:
        self._progress = progress
        self._pages = 0  # páginas informadas
        self._total: int | None = None  # total que declara el worker
        self._done: DoneEvent | None = None
        self._error: WorkerOutputError | None = None

    def feed(self, line: str) -> None:
        """Procesa una línea de la salida del worker; el primer error invalida el resto."""
        if self._error is not None:
            return
        try:
            event = worker.load_event(line)
            if isinstance(event, PageEvent):
                self._page(event)
            else:
                self._finish(event)
        except WorkerOutputError as exc:
            self._error = exc

    def _page(self, event: PageEvent) -> None:
        if self._done is not None:
            raise WorkerOutputError("hay una página después del evento final")
        if event.page != self._pages + 1:
            raise WorkerOutputError("las páginas no llegan en orden")
        if self._total not in (None, event.total):
            raise WorkerOutputError("el total de páginas cambió")
        self._total = event.total
        self._pages = event.page
        self._progress.report(event.page / event.total)

    def _finish(self, event: DoneEvent) -> None:
        if self._done is not None:
            raise WorkerOutputError("hay dos eventos finales")
        if event.pages != self._pages:
            raise WorkerOutputError("el evento final no coincide con las páginas informadas")
        if self._total not in (None, event.pages):
            raise WorkerOutputError("el evento final no coincide con el total de páginas")
        self._done = event

    def result(self) -> int:
        """Cantidad de páginas que rasterizó el worker. Lanza `WorkerOutputError` si la salida no
        siguió el protocolo o terminó sin el evento final."""
        if self._error is not None:
            raise self._error
        if self._done is None:
            raise WorkerOutputError("falta el evento final")
        return self._done.pages


def worker_failure(completed: CompletedRun) -> ConversionFailure:
    """Fallo de un worker que terminó con un código distinto de 0 o por una señal."""
    detail = sanitize_tool_output(
        Tool.PYPDFIUM2, completed.stderr_lines, returncode=completed.returncode
    )
    reason = classify_failure(detail=detail)
    if reason not in _WORKER_REASONS:
        reason = FailureReason.INTERNAL_ERROR
    return ConversionFailure(reason, detail=detail)


@register_converter(CONVERTER_ID)
class PdfRasterConverter:
    """PDF a PNG: una imagen por página; un PNG, o un ZIP de PNG si hay varias páginas."""

    def preflight(
        self, source: SourceInfo, request: ConversionRequest, config: Config
    ) -> DestinationRejection | None:
        """PDF a PNG no tiene Rechazos_de_Destino propios (Req 16.12): una página demasiado grande
        falla durante el procesamiento (Req 8.11)."""
        return None

    async def run(self, ctx: ConversionContext) -> ConversionOutcome:
        """Rasteriza el PDF en un subproceso y deja el PNG o el ZIP en `work/output`.

        Lanza `ConversionFailure` con `pagina_excede_pixeles` si alguna página supera el
        Límite_de_Píxeles (antes de rasterizar ninguna), y con el motivo que corresponda si el
        worker falla. Lanza `TypeError` si `ctx.source` no es la información de un PDF.
        """
        pdf = _pdf_source(ctx.source)
        dpi = ctx.config.raster_dpi
        limit = ctx.config.max_image_pixels
        oversized = first_page_over_limit(pdf.pages, dpi, limit)
        if oversized is not None:
            raise ConversionFailure(
                FailureReason.PAGE_PIXEL_LIMIT,
                params={
                    "pagina": oversized.page,
                    "ppp": dpi,
                    "ancho": oversized.width,
                    "alto": oversized.height,
                    "limite_pixeles": limit,
                },
            )

        workspace = ctx.workspace
        output = (workspace.work_dir / OUTPUT_NAME).absolute()
        args = worker.WorkerArgs(
            source=workspace.source_file.absolute(),
            output=output,
            entry_base=entry_base(ctx),
            dpi=dpi,
        )
        argv = python_module_command(worker.MODULE, *worker.format_args(args))
        limits = tool_limits(
            SandboxTool.PYTHON, timeout_seconds=ctx.config.timeout_document_seconds
        )
        collector = EventCollector(ctx.progress)

        completed = await ctx.runner.run(
            argv,
            cwd=workspace.tmp_dir,
            limits=limits,
            env=minimal_env(workspace.tmp_dir),
            on_stdout_line=collector.feed,
        )
        if completed.returncode != 0:
            raise worker_failure(completed)
        try:
            pages = collector.result()
        except WorkerOutputError as exc:
            raise ConversionFailure(
                FailureReason.INTERNAL_ERROR, detail=describe_exception(exc)
            ) from exc
        if pages != len(pdf.pages):
            # El inspector y el worker no vieron el mismo documento: la verificación del
            # Límite_de_Píxeles no corresponde a lo que se rasterizó
            raise ConversionFailure(FailureReason.INTERNAL_ERROR)
        return ConversionOutcome(result_path=output, result_kind="single" if pages == 1 else "zip")
