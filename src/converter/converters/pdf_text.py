"""Convertidor PDF a TXT, `pdf.text` (Req 8.1, 8.2, 8.3, 8.10).

Lanza `python -m converter.workers.pdf_text` (ver `workers/pdf_text.py`) a través del Ejecutor
aislado y escribe el texto en `work/output.txt`. El worker extrae el texto de cada página con
PDFium, en orden de Número_de_Página, y deja `\\n\\n` entre el texto de dos páginas. Este módulo
decide qué hacer con lo que informa:

- **Progreso.** Cada línea `page` del worker mueve el Porcentaje_de_Avance a la fracción de
  páginas procesadas (página N de T → N / T).
- **Todas las páginas sin texto** (Req 8.2): la Tarea falla con `pdf_sin_texto` y no hay
  Archivo_Resultado. Incluye un PDF sin páginas. El mensaje dice que el PDF parece escaneado y
  que el reconocimiento de texto (OCR) está fuera del alcance.
- **Algunas páginas sin texto** (Req 8.3): la Tarea se completa con el texto de las demás y el
  aviso `paginas_sin_texto` lista los Números_de_Página de las Páginas_sin_Texto.
- **Permisos restringidos** (Req 8.10): PDFium abre sin contraseña un PDF_con_Permisos_Restringidos
  e ignora sus restricciones, así que el resultado es el mismo que el del PDF sin cifrar. Este
  módulo no hace nada especial.
- **Fallos del worker.** Con un código de salida distinto de 0, la salida de error se sanea con
  `sanitize_tool_output` y se clasifica con `classify_failure`: un PDF cuyo contenido PDFium no
  puede leer da `archivo_danado`, y la falta de espacio, `sin_espacio_proceso`. Un PDF_Protegido
  no llega aquí (el Detector_de_Tipo lo rechaza al recibirlo, Req 8.7); si aun así llegara, o si el
  motivo no es de la etapa de conversión, es `error_interno`. También lo es una salida del worker
  que no sigue su protocolo. El detalle técnico saneado va en `ConversionFailure.detail`.

El convertidor no usa la información que obtuvo el inspector del PDF (`ctx.source`), así que no
depende de él. No importa pypdfium2: la biblioteca solo la carga el worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

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
from converter.messages_es import FailureReason, NoticeCode
from converter.models import DestinationRejection
from converter.sandbox.limits import SandboxTool, tool_limits
from converter.sandbox.runner import CompletedRun, minimal_env, python_module_command
from converter.workers import pdf_text as worker
from converter.workers.pdf_text import DoneEvent, PageEvent, WorkerOutputError

if TYPE_CHECKING:
    from converter.config import Config
    from converter.tasks.progress import ProgressSink

CONVERTER_ID = "pdf.text"
OUTPUT_NAME = "output.txt"  # en work/

# Motivos que el worker puede dar en la etapa de conversión; PDF_Protegido es de la detección
_WORKER_REASONS = frozenset({FailureReason.DAMAGED, FailureReason.NO_SPACE})


@dataclass(frozen=True, slots=True)
class Extraction:
    """Lo que informó el worker sobre el documento."""

    pages: int  # total de páginas
    pages_without_text: tuple[int, ...]  # Números_de_Página de las Páginas_sin_Texto, crecientes

    @property
    def pages_with_text(self) -> int:
        return self.pages - len(self.pages_without_text)


class EventCollector:
    """Interpreta la salida estándar del worker, línea por línea, y reporta el avance.

    `feed` es el callback de líneas del Ejecutor aislado y nunca lanza excepciones: un error del
    protocolo queda guardado y `result` lo lanza, así la salida de error del worker, que
    clasifica el fallo, sigue disponible.
    """

    __slots__ = ("_blank", "_done", "_error", "_pages", "_progress", "_total")

    def __init__(self, progress: ProgressSink) -> None:
        self._progress = progress
        self._pages = 0  # páginas informadas
        self._total: int | None = None  # total que declara el worker
        self._blank: list[int] = []
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
        if not event.has_text:
            self._blank.append(event.page)
        self._progress.report(event.page / event.total)

    def _finish(self, event: DoneEvent) -> None:
        if self._done is not None:
            raise WorkerOutputError("hay dos eventos finales")
        if event.pages != self._pages or event.pages_with_text != self._pages - len(self._blank):
            raise WorkerOutputError("el evento final no coincide con las páginas informadas")
        if self._total not in (None, event.pages):
            raise WorkerOutputError("el evento final no coincide con el total de páginas")
        self._done = event

    def result(self) -> Extraction:
        """Resumen del documento. Lanza `WorkerOutputError` si la salida no siguió el protocolo
        o terminó sin el evento final."""
        if self._error is not None:
            raise self._error
        if self._done is None:
            raise WorkerOutputError("falta el evento final")
        return Extraction(pages=self._done.pages, pages_without_text=tuple(self._blank))


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
class PdfTextConverter:
    """PDF a TXT: texto de la Capa_de_Texto de todas las páginas, en UTF-8."""

    def preflight(
        self, source: SourceInfo, request: ConversionRequest, config: Config
    ) -> DestinationRejection | None:
        """PDF a TXT no tiene Rechazos_de_Destino propios (Req 16.12)."""
        return None

    async def run(self, ctx: ConversionContext) -> ConversionOutcome:
        """Extrae el texto del PDF en un subproceso y lo deja en `work/output.txt`.

        Lanza `ConversionFailure` con `pdf_sin_texto` si ninguna página tiene texto, y con el
        motivo que corresponda si el worker falla.
        """
        workspace = ctx.workspace
        output = (workspace.work_dir / OUTPUT_NAME).absolute()
        args = worker.WorkerArgs(source=workspace.source_file.absolute(), output=output)
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
            extraction = collector.result()
        except WorkerOutputError as exc:
            raise ConversionFailure(
                FailureReason.INTERNAL_ERROR, detail=describe_exception(exc)
            ) from exc

        if extraction.pages_with_text == 0:
            raise ConversionFailure(FailureReason.PDF_WITHOUT_TEXT)
        if extraction.pages_without_text:
            ctx.notices.add(
                NoticeCode.PAGES_WITHOUT_TEXT, {"paginas": list(extraction.pages_without_text)}
            )
        return ConversionOutcome(result_path=output, result_kind="single")
