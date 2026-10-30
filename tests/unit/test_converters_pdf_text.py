"""Pruebas de src/converter/converters/pdf_text.py: el convertidor `pdf.text` con el worker real
en el Ejecutor aislado, el aviso de Páginas_sin_Texto, el fallo de un PDF sin texto, el avance por
página y la clasificación de los fallos del worker (Req 8.1, 8.2, 8.3, 8.10).

Los PDF se arman a mano con `tests/pdf_text_support.py`. La extracción en sí (guiones, limpieza,
eventos) está en `test_workers_pdf_text.py`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from converter.config import Config, load_config
from converter.converters.base import (
    ConversionContext,
    ConversionFailure,
    ConversionRequest,
    NoticeCollector,
    get_converter,
)
from converter.converters.pdf_text import (
    CONVERTER_ID,
    OUTPUT_NAME,
    EventCollector,
    PdfTextConverter,
    worker_failure,
)
from converter.detection.source_info import NoInfo
from converter.eventlog import Tool, ToolDetail
from converter.messages_es import FailureReason, NoticeCode, render_failure, render_notice
from converter.models import Category, LossClass, Stage
from converter.registry.model import ConversionPair
from converter.sandbox.runner import CompletedRun, SandboxRunner
from converter.storage.workspace import Workspace, WorkspaceManager
from converter.tasks.progress import ProgressReporter
from converter.workers.pdf_text import (
    DoneEvent,
    PageEvent,
    WorkerOutputError,
    dump_event,
)
from tests.pdf_text_support import build_pdf


class RecordingProgress(ProgressReporter):
    """Porcentaje_de_Avance que además guarda las fracciones que recibe."""

    def __init__(self) -> None:
        super().__init__()
        self.fractions: list[float] = []

    def report(self, fraction: float) -> None:
        self.fractions.append(fraction)
        super().report(fraction)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return WorkspaceManager(tmp_path).create()


def _config() -> Config:
    config = load_config({})
    assert isinstance(config, Config)
    return config


def _context(
    workspace: Workspace, data: bytes, progress: ProgressReporter | None = None
) -> ConversionContext:
    workspace.source_file.write_bytes(data)
    pair = ConversionPair(
        source="PDF",
        target="TXT",
        category=Category.DOCUMENT,
        loss_class=LossClass.LIMITED,
        loss_description="Solo extrae la capa de texto; se pierde la maquetación.",
        converter_id=CONVERTER_ID,
    )
    return ConversionContext(
        task_id="3b9f0c1e-0000-4000-8000-000000000000",
        pair=pair,
        request=ConversionRequest(target="TXT"),
        source=NoInfo(),
        workspace=workspace,
        config=_config(),
        runner=SandboxRunner(),
        progress=progress if progress is not None else ProgressReporter(),
        notices=NoticeCollector(),
    )


# --- Registro ---


def test_pdf_text_is_registered_and_has_no_destination_rejections() -> None:
    converter = get_converter(CONVERTER_ID)

    assert isinstance(converter, PdfTextConverter)
    assert converter.id == "pdf.text"
    assert converter.preflight(NoInfo(), ConversionRequest(target="TXT"), _config()) is None


def test_importing_the_converter_does_not_load_pdfium() -> None:
    # Solo el worker usa pypdfium2: el proceso principal importa el convertidor para registrarlo
    script = (
        "import sys; import converter.converters.pdf_text; "
        "print('pypdfium2' in sys.modules, 'PIL' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["False", "False"]


# --- Conversión con el worker real ---


@pytest.mark.asyncio
@pytest.mark.req("8.1")
async def test_run_writes_the_text_of_every_page_in_order(workspace: Workspace) -> None:
    pdf = build_pdf([["Uno", "dos"], ["Tres"], ["Cuatro-", "cinco"]])
    ctx = _context(workspace, pdf)

    outcome = await PdfTextConverter().run(ctx)

    assert outcome.result_kind == "single"
    assert outcome.detected_language is None
    assert outcome.result_path == workspace.work_dir / OUTPUT_NAME
    assert outcome.result_path.read_bytes() == b"Uno\ndos\n\nTres\n\nCuatro-\ncinco"
    assert ctx.notices.notices == ()


@pytest.mark.asyncio
@pytest.mark.req("8.1")
async def test_run_reports_progress_page_by_page(workspace: Workspace) -> None:
    progress = RecordingProgress()
    ctx = _context(workspace, build_pdf([["a"], [], ["b"], ["c"]]), progress)

    await PdfTextConverter().run(ctx)

    assert progress.fractions == [0.25, 0.5, 0.75, 1.0]
    assert progress.percent == 100


@pytest.mark.asyncio
@pytest.mark.req("8.3")
async def test_run_completes_with_a_notice_listing_the_pages_without_text(
    workspace: Workspace,
) -> None:
    ctx = _context(workspace, build_pdf([[], ["Hola"], ["  "], ["\t"], ["Chau"], []]))

    outcome = await PdfTextConverter().run(ctx)

    assert outcome.result_path.read_text(encoding="utf-8") == "Hola\n\nChau"
    (notice,) = ctx.notices.notices
    assert notice.code is NoticeCode.PAGES_WITHOUT_TEXT
    assert notice.params == {"paginas": [1, 3, 4, 6]}
    assert render_notice(notice).endswith("Páginas sin texto: 1, 3, 4, 6.")


@pytest.mark.asyncio
@pytest.mark.req("8.3")
async def test_run_without_blank_pages_adds_no_notice(workspace: Workspace) -> None:
    ctx = _context(workspace, build_pdf([["Uno"], ["Dos"]]))

    await PdfTextConverter().run(ctx)

    assert ctx.notices.notices == ()


@pytest.mark.asyncio
@pytest.mark.req("8.2")
@pytest.mark.parametrize("pages", [[[]], [[], ["   "], []]])
async def test_run_fails_with_pdf_without_text_when_every_page_is_blank(
    workspace: Workspace, pages: list[list[str]]
) -> None:
    ctx = _context(workspace, build_pdf(pages))

    with pytest.raises(ConversionFailure) as error:
        await PdfTextConverter().run(ctx)

    failure = error.value.failure
    assert failure.reason is FailureReason.PDF_WITHOUT_TEXT
    assert failure.stage is Stage.CONVERSION
    message = render_failure(failure)
    assert "escaneado" in message.message
    assert "OCR" in message.message
    assert ctx.notices.notices == ()


@pytest.mark.asyncio
@pytest.mark.req("8.10")
async def test_run_gives_the_same_text_for_a_pdf_with_restricted_permissions(
    workspace: Workspace, tmp_path: Path
) -> None:
    pages = [["Contenido", "reservado"], ["Página dos"]]
    plain = await PdfTextConverter().run(_context(workspace, build_pdf(pages)))
    restricted_workspace = WorkspaceManager(tmp_path).create()
    restricted = await PdfTextConverter().run(
        _context(restricted_workspace, build_pdf(pages, owner_password=b"propietario"))
    )

    assert restricted.result_path.read_bytes() == plain.result_path.read_bytes()
    assert plain.result_path.read_text(encoding="utf-8") == "Contenido\nreservado\n\nPágina dos"


# --- Fallos del worker ---


@pytest.mark.asyncio
@pytest.mark.req("16.2")
async def test_run_fails_with_damaged_file_for_a_pdf_pdfium_cannot_read(
    workspace: Workspace,
) -> None:
    ctx = _context(workspace, b"%PDF-1.4\nesto no es un PDF")

    with pytest.raises(ConversionFailure) as error:
        await PdfTextConverter().run(ctx)

    assert error.value.failure.reason is FailureReason.DAMAGED
    assert error.value.failure.stage is Stage.CONVERSION
    detail = error.value.detail
    assert isinstance(detail, ToolDetail)
    assert detail.tool is Tool.PYPDFIUM2
    assert detail.pattern is not None
    assert detail.pattern.id == "pdf_format_error"
    assert detail.exit_code == 1


@pytest.mark.asyncio
@pytest.mark.req("16.9")
async def test_run_fails_with_internal_error_for_a_protected_pdf(workspace: Workspace) -> None:
    # El Detector_de_Tipo rechaza los PDF_Protegidos al recibirlos (Req 8.7): en la conversión
    # ese motivo ya no corresponde a su etapa
    pdf = build_pdf([["Secreto"]], owner_password=b"propietario", user_password=b"clave")
    ctx = _context(workspace, pdf)

    with pytest.raises(ConversionFailure) as error:
        await PdfTextConverter().run(ctx)

    assert error.value.failure.reason is FailureReason.INTERNAL_ERROR
    detail = error.value.detail
    assert isinstance(detail, ToolDetail)
    assert detail.pattern is not None
    assert detail.pattern.id == "pdf_password_error"


_PDFIUM_ERROR = "pypdfium2._helpers.misc.PdfiumError: "


def _completed(*stderr: str, returncode: int = 1) -> CompletedRun:
    return CompletedRun(returncode=returncode, stderr_lines=stderr)


@pytest.mark.req("16.2", "16.9", "16.10")
@pytest.mark.parametrize(
    ("completed", "reason"),
    [
        (_completed(_PDFIUM_ERROR + "Failed to load page."), "archivo_danado"),
        (
            _completed(_PDFIUM_ERROR + "Failed to load document (PDFium: Data format error)."),
            "archivo_danado",
        ),
        (_completed("OSError: [Errno 28] No space left on device"), "sin_espacio_proceso"),
        (_completed("MemoryError"), "error_interno"),
        (_completed("algo que nadie reconoce"), "error_interno"),
        (_completed(returncode=-11), "error_interno"),  # SIGSEGV
        (_completed(returncode=127), "error_interno"),  # el lanzador no encontró el programa
    ],
)
def test_worker_failure_classifies_the_stderr_of_the_worker(
    completed: CompletedRun, reason: str
) -> None:
    failure = worker_failure(completed)

    assert failure.failure.reason is FailureReason(reason)
    assert failure.failure.stage is Stage.CONVERSION
    assert isinstance(failure.detail, ToolDetail)
    assert failure.detail.exit_code == (completed.returncode if completed.returncode > 0 else None)


# --- Protocolo del worker ---


def _collector() -> tuple[EventCollector, RecordingProgress]:
    progress = RecordingProgress()
    return EventCollector(progress), progress


def _feed(collector: EventCollector, *events: PageEvent | DoneEvent) -> None:
    for event in events:
        collector.feed(dump_event(event))


def test_collector_summarizes_the_pages_without_text_and_reports_each_page() -> None:
    collector, progress = _collector()

    _feed(
        collector,
        PageEvent(1, 3, False),
        PageEvent(2, 3, True),
        PageEvent(3, 3, False),
        DoneEvent(3, 1),
    )

    extraction = collector.result()
    assert extraction.pages == 3
    assert extraction.pages_without_text == (1, 3)
    assert extraction.pages_with_text == 1
    assert progress.fractions == [1 / 3, 2 / 3, 1.0]


def test_collector_accepts_a_document_without_pages() -> None:
    collector, progress = _collector()

    _feed(collector, DoneEvent(0, 0))

    extraction = collector.result()
    assert extraction.pages == 0
    assert extraction.pages_without_text == ()
    assert extraction.pages_with_text == 0
    assert progress.fractions == []


@pytest.mark.parametrize(
    "lines",
    [
        [],  # sin evento final
        [dump_event(PageEvent(1, 2, True))],  # termina antes del evento final
        ["no es JSON"],
        [dump_event(PageEvent(2, 2, True))],  # empieza en la página 2
        [dump_event(PageEvent(1, 3, True)), dump_event(PageEvent(1, 3, True))],  # repetida
        [dump_event(PageEvent(1, 3, True)), dump_event(PageEvent(3, 3, True))],  # salta la 2
        [dump_event(PageEvent(1, 3, True)), dump_event(PageEvent(2, 4, True))],  # cambia el total
        [dump_event(PageEvent(1, 1, True)), dump_event(DoneEvent(2, 2))],  # cuenta mal las páginas
        [dump_event(PageEvent(1, 1, True)), dump_event(DoneEvent(1, 0))],  # cuenta mal el texto
        [dump_event(PageEvent(1, 2, True)), dump_event(DoneEvent(1, 1))],  # faltan páginas
        [dump_event(DoneEvent(0, 0)), dump_event(DoneEvent(0, 0))],  # dos eventos finales
        [dump_event(DoneEvent(0, 0)), dump_event(PageEvent(1, 1, True))],  # página tras el final
    ],
)
def test_collector_rejects_output_that_breaks_the_protocol(lines: list[str]) -> None:
    collector, _ = _collector()

    for line in lines:
        collector.feed(line)  # nunca lanza: el error queda para `result`

    with pytest.raises(WorkerOutputError):
        collector.result()


def test_collector_ignores_lines_after_the_first_error() -> None:
    collector, progress = _collector()

    collector.feed("basura")
    _feed(collector, PageEvent(1, 1, True), DoneEvent(1, 1))

    assert progress.fractions == []
    with pytest.raises(WorkerOutputError, match="JSON"):
        collector.result()
