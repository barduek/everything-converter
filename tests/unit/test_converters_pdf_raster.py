"""Pruebas de src/converter/converters/pdf_raster.py: el convertidor `pdf.raster` con el worker
real en el Ejecutor aislado, la verificación del Límite_de_Píxeles antes de rasterizar, el PNG o el
ZIP según la cantidad de páginas, el avance por página y la clasificación de los fallos del worker
(Req 8.4, 8.5, 8.6, 8.8, 8.9, 8.10, 8.11, 15.10).

Los PDF se arman con `tests/pdf_samples.py` (páginas en blanco con tamaño, `/CropBox` y `/Rotate`) y
con `tests/pdf_text_support.py` (páginas con texto). La información del origen sale del inspector
`pdf` real. El rasterizado en sí está en `test_workers_pdf_raster.py`.
"""

from __future__ import annotations

import io
import subprocess
import sys
import zipfile
from dataclasses import dataclass, fields
from pathlib import Path

import pytest
from PIL import Image

from converter.config import Config, load_config
from converter.converters.base import (
    ConversionContext,
    ConversionFailure,
    ConversionOutcome,
    ConversionRequest,
    NoticeCollector,
    get_converter,
)
from converter.converters.pdf_raster import (
    CONVERTER_ID,
    OUTPUT_NAME,
    EventCollector,
    OversizedPage,
    PdfRasterConverter,
    entry_base,
    first_page_over_limit,
    worker_failure,
)
from converter.detection.inspectors.base import InspectorInput
from converter.detection.inspectors.pdf import PdfInfo, PdfPage, inspect_pdf
from converter.detection.source_info import NoInfo, SourceInfo
from converter.eventlog import Tool, ToolDetail
from converter.messages_es import FailureReason, render_failure
from converter.models import Category, LossClass, Stage
from converter.registry.model import ConversionPair
from converter.sandbox.runner import CompletedRun, SandboxRunner
from converter.storage.workspace import Workspace, WorkspaceManager
from converter.tasks.progress import ProgressReporter
from converter.workers.pdf_raster import (
    DoneEvent,
    PageEvent,
    PagePoints,
    WorkerOutputError,
    dump_event,
    raster_size,
)
from tests.pdf_samples import PageSpec
from tests.pdf_samples import build_pdf as build_blank_pdf
from tests.pdf_text_support import build_pdf as build_text_pdf

DPI = 150  # Resolución_de_Rasterizado predeterminada
LETTER = (612, 792)


class RecordingProgress(ProgressReporter):
    """Porcentaje_de_Avance que además guarda las fracciones que recibe."""

    def __init__(self) -> None:
        super().__init__()
        self.fractions: list[float] = []

    def report(self, fraction: float) -> None:
        self.fractions.append(fraction)
        super().report(fraction)


@dataclass(frozen=True, slots=True)
class NamedContext(ConversionContext):
    """Contexto con el Nombre_Original, que el convertidor lee si el contexto lo tiene."""

    original_name: str


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return WorkspaceManager(tmp_path).create()


def _config(**env: str) -> Config:
    config = load_config(env)
    assert isinstance(config, Config)
    return config


def _inspect(workspace: Workspace) -> PdfInfo:
    """La información del origen que entrega el inspector `pdf` real."""
    result = inspect_pdf(
        InspectorInput(path=workspace.source_file, real_type="PDF", max_image_pixels=100_000_000)
    )
    assert isinstance(result, PdfInfo), result
    return result


def _context(
    workspace: Workspace,
    data: bytes,
    *,
    config: Config | None = None,
    progress: ProgressReporter | None = None,
    source: SourceInfo | None = None,
    original_name: str | None = None,
) -> ConversionContext:
    workspace.source_file.write_bytes(data)
    pair = ConversionPair(
        source="PDF",
        target="PNG",
        category=Category.DOCUMENT,
        loss_class=LossClass.LIMITED,
        loss_description="Cada página se convierte en una imagen; el texto deja de ser texto.",
        converter_id=CONVERTER_ID,
    )
    ctx = ConversionContext(
        task_id="3b9f0c1e-0000-4000-8000-000000000000",
        pair=pair,
        request=ConversionRequest(target="PNG"),
        source=source if source is not None else _inspect(workspace),
        workspace=workspace,
        config=config if config is not None else _config(),
        runner=SandboxRunner(),
        progress=progress if progress is not None else ProgressReporter(),
        notices=NoticeCollector(),
    )
    if original_name is None:
        return ctx
    values = {field.name: getattr(ctx, field.name) for field in fields(ctx)}
    return NamedContext(**values, original_name=original_name)


def _png(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data), formats=["PNG"])
    image.load()
    return image


def _zip_names(outcome: ConversionOutcome) -> list[str]:
    with zipfile.ZipFile(outcome.result_path) as archive:
        return archive.namelist()


# --- Registro ---


def test_pdf_raster_is_registered_and_has_no_destination_rejections() -> None:
    converter = get_converter(CONVERTER_ID)

    assert isinstance(converter, PdfRasterConverter)
    assert converter.id == "pdf.raster"
    assert converter.preflight(NoInfo(), ConversionRequest(target="PNG"), _config()) is None


def test_importing_the_converter_does_not_load_pdfium() -> None:
    # Solo el worker usa pypdfium2: el proceso principal importa el convertidor para registrarlo
    script = (
        "import sys; import converter.converters.pdf_raster; "
        "print('pypdfium2' in sys.modules, 'PIL' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["False", "False"]


# --- Verificación del Límite_de_Píxeles ---


@pytest.mark.req("8.11")
def test_first_page_over_limit_is_none_when_every_page_fits() -> None:
    pages = [PdfPage(*LETTER), PdfPage(595, 842)]

    assert first_page_over_limit(pages, 72, 595 * 842) is None  # A4 es la mayor
    assert first_page_over_limit([], 72, 1) is None


@pytest.mark.req("8.11")
def test_first_page_over_limit_is_the_first_in_page_order_and_not_the_largest() -> None:
    pages = [PdfPage(100, 100), PdfPage(300, 300), PdfPage(100, 100), PdfPage(900, 900)]

    found = first_page_over_limit(pages, 72, 50_000)

    assert found == OversizedPage(page=2, width=300, height=300)


@pytest.mark.req("8.11")
def test_first_page_over_limit_compares_the_product_and_fails_only_above_the_limit() -> None:
    pages = [PdfPage(*LETTER)]

    assert first_page_over_limit(pages, 72, 612 * 792) is None  # igual al límite: pasa
    assert first_page_over_limit(pages, 72, 612 * 792 - 1) == OversizedPage(1, 612, 792)


@pytest.mark.req("8.11")
def test_first_page_over_limit_uses_the_rounded_up_raster_size_of_the_unrounded_points() -> None:
    # 100,2 pt a 72 ppp mide 101 píxeles (no 100): con 101 × 101 píxeles el límite de 10 200 falla
    page = PdfPage(100.2, 100.2)

    assert raster_size(page, 72) == (101, 101)
    assert first_page_over_limit([page], 72, 10_201) is None
    assert first_page_over_limit([page], 72, 10_200) == OversizedPage(1, 101, 101)


@pytest.mark.req("8.11")
def test_first_page_over_limit_uses_the_resolution() -> None:
    pages = [PdfPage(*LETTER)]

    assert first_page_over_limit(pages, 72, 1_000_000) is None
    assert first_page_over_limit(pages, 150, 1_000_000) is not None


# --- Nombre de las entradas del ZIP ---


@pytest.mark.req("8.8", "15.12")
@pytest.mark.parametrize(
    ("original_name", "expected"),
    [
        ("informe.pdf", "informe"),
        ("informe.final.pdf", "informe.final"),
        ("Vacaciones 2024.PDF", "Vacaciones 2024"),
        ("a:b*c.pdf", "a_b_c"),  # Caracteres_No_Permitidos
        (".pdf", "resultado"),  # sin Nombre_Base
        ("sin extensión", "sin extensión"),
        ("ñ" * 150 + ".pdf", "ñ" * 100),  # 200 bytes en UTF-8, por caracteres completos
    ],
)
def test_entry_base_is_the_normalized_base_name_of_the_original_name(
    workspace: Workspace, original_name: str, expected: str
) -> None:
    ctx = _context(workspace, build_blank_pdf([PageSpec()]), original_name=original_name)

    assert entry_base(ctx) == expected


@pytest.mark.req("8.8")
def test_entry_base_is_resultado_while_the_context_has_no_original_name(
    workspace: Workspace,
) -> None:
    assert entry_base(_context(workspace, build_blank_pdf([PageSpec()]))) == "resultado"


# --- Conversión con el worker real ---


@pytest.mark.asyncio
@pytest.mark.req("8.9", "8.4")
async def test_run_with_one_page_delivers_the_png_and_no_zip(workspace: Workspace) -> None:
    ctx = _context(workspace, build_text_pdf([["Una página"]]))

    outcome = await PdfRasterConverter().run(ctx)

    assert outcome.result_kind == "single"
    assert outcome.detected_language is None
    assert outcome.result_path == workspace.work_dir / OUTPUT_NAME
    assert not zipfile.is_zipfile(outcome.result_path)
    image = _png(outcome.result_path.read_bytes())
    assert image.size == raster_size(PagePoints(400, 400), DPI)
    assert ctx.notices.notices == ()


@pytest.mark.asyncio
@pytest.mark.req("8.5", "8.6", "8.8", "15.10")
async def test_run_with_several_pages_delivers_a_zip_of_pngs_named_by_page_number(
    workspace: Workspace,
) -> None:
    pages = [[f"Página {n}"] for n in range(1, 11)]
    ctx = _context(workspace, build_text_pdf(pages), original_name="Mi informe.pdf")

    outcome = await PdfRasterConverter().run(ctx)

    assert outcome.result_kind == "zip"
    assert outcome.result_path == workspace.work_dir / OUTPUT_NAME
    assert _zip_names(outcome) == [f"Mi informe_{n:02d}.png" for n in range(1, 11)]
    with zipfile.ZipFile(outcome.result_path) as archive:
        assert all(entry.compress_type == zipfile.ZIP_DEFLATED for entry in archive.infolist())
        assert all(entry.date_time == (1980, 1, 1, 0, 0, 0) for entry in archive.infolist())
        assert not any(entry.is_dir() for entry in archive.infolist())
        assert all(_png(archive.read(entry)).format == "PNG" for entry in archive.infolist())


@pytest.mark.asyncio
@pytest.mark.req("8.8")
async def test_run_names_the_entries_resultado_when_the_context_has_no_original_name(
    workspace: Workspace,
) -> None:
    ctx = _context(workspace, build_text_pdf([["Uno"], ["Dos"]]))

    outcome = await PdfRasterConverter().run(ctx)

    assert _zip_names(outcome) == ["resultado_1.png", "resultado_2.png"]


@pytest.mark.asyncio
@pytest.mark.req("8.6")
async def test_run_gives_one_image_per_page_including_pages_without_text(
    workspace: Workspace,
) -> None:
    ctx = _context(workspace, build_text_pdf([[], ["Texto"], [], ["Más texto"]]))

    outcome = await PdfRasterConverter().run(ctx)

    assert len(_zip_names(outcome)) == 4


@pytest.mark.asyncio
@pytest.mark.req("8.4")
async def test_run_rasterizes_each_page_at_the_configured_resolution(
    tmp_path: Path,
) -> None:
    specs = [PageSpec(width=612, height=792), PageSpec(width=200, height=300, rotate=90)]
    displayed = [(612, 792), (300, 200)]  # la segunda se muestra apaisada
    data = build_blank_pdf(specs)
    for dpi in (72, 300):
        root = tmp_path / str(dpi)
        root.mkdir()
        ctx = _context(
            WorkspaceManager(root).create(), data, config=_config(CONVERTER_RASTER_DPI=str(dpi))
        )

        outcome = await PdfRasterConverter().run(ctx)

        with zipfile.ZipFile(outcome.result_path) as archive:
            sizes = [_png(archive.read(name)).size for name in archive.namelist()]
        # Ancho y alto de la página en pulgadas por la resolución, con tolerancia de ±1 píxel
        assert len(sizes) == len(displayed)
        for (width, height), (width_pt, height_pt) in zip(sizes, displayed, strict=True):
            assert abs(width - width_pt / 72 * dpi) <= 1
            assert abs(height - height_pt / 72 * dpi) <= 1


@pytest.mark.asyncio
@pytest.mark.req("8.4", "8.11")
async def test_rasterized_size_is_what_the_verification_computed(workspace: Workspace) -> None:
    # La verificación usa los puntos sin redondear del inspector; el PNG mide lo que da raster_size
    ctx = _context(workspace, build_blank_pdf([PageSpec(width=595.276, height=841.89)]))
    assert isinstance(ctx.source, PdfInfo)

    outcome = await PdfRasterConverter().run(ctx)

    assert _png(outcome.result_path.read_bytes()).size == raster_size(ctx.source.pages[0], DPI)


@pytest.mark.asyncio
@pytest.mark.req("8.4")
async def test_run_reports_progress_page_by_page(workspace: Workspace) -> None:
    progress = RecordingProgress()
    ctx = _context(workspace, build_text_pdf([["a"], [], ["b"], ["c"]]), progress=progress)

    await PdfRasterConverter().run(ctx)

    assert progress.fractions == [0.25, 0.5, 0.75, 1.0]
    assert progress.percent == 100


@pytest.mark.asyncio
@pytest.mark.req("8.10")
async def test_run_gives_the_same_images_for_a_pdf_with_restricted_permissions(
    tmp_path: Path,
) -> None:
    pages = [["Contenido", "reservado"], ["Página dos"]]
    plain_ws = WorkspaceManager(tmp_path).create()
    restricted_ws = WorkspaceManager(tmp_path).create()

    plain = await PdfRasterConverter().run(_context(plain_ws, build_text_pdf(pages)))
    restricted = await PdfRasterConverter().run(
        _context(restricted_ws, build_text_pdf(pages, owner_password=b"propietario"))
    )

    assert restricted.result_kind == plain.result_kind == "zip"
    with (
        zipfile.ZipFile(plain.result_path) as left,
        zipfile.ZipFile(restricted.result_path) as right,
    ):
        assert left.namelist() == right.namelist()
        for name in left.namelist():
            assert _png(left.read(name)).tobytes() == _png(right.read(name)).tobytes()


# --- Límite_de_Píxeles en la conversión ---


@pytest.mark.asyncio
@pytest.mark.req("8.11")
async def test_run_fails_with_the_first_page_over_the_limit_without_rasterizing_any(
    workspace: Workspace,
) -> None:
    # Las páginas 1 y 3 caben; la 2 y la 4 no (la 4 es la mayor): se informa la 2
    specs = [PageSpec(100, 100), PageSpec(400, 300), PageSpec(100, 100), PageSpec(900, 900)]
    progress = RecordingProgress()
    ctx = _context(
        workspace,
        build_blank_pdf(specs),
        config=_config(CONVERTER_MAX_IMAGE_PIXELS="100000", CONVERTER_RASTER_DPI="72"),
        progress=progress,
    )

    with pytest.raises(ConversionFailure) as error:
        await PdfRasterConverter().run(ctx)

    failure = error.value.failure
    assert failure.reason is FailureReason.PAGE_PIXEL_LIMIT
    assert failure.stage is Stage.CONVERSION
    assert dict(failure.params) == {
        "pagina": 2,
        "ppp": 72,
        "ancho": 400,
        "alto": 300,
        "limite_pixeles": 100_000,
    }
    message = render_failure(failure)
    assert "página 2" in message.message
    assert "72 ppp" in message.message
    assert "400 × 300" in message.message
    assert "100000" in message.message
    # Sin rasterizar ninguna página ni dejar Archivo_Resultado
    assert list(workspace.work_dir.iterdir()) == []
    assert progress.fractions == []
    assert ctx.notices.notices == ()


@pytest.mark.asyncio
@pytest.mark.req("8.11")
async def test_run_accepts_a_page_whose_pixels_equal_the_limit_and_rejects_one_pixel_more(
    tmp_path: Path,
) -> None:
    data = build_blank_pdf([PageSpec(width=200, height=100)])
    at_limit = WorkspaceManager(tmp_path).create()
    above_limit = WorkspaceManager(tmp_path).create()

    outcome = await PdfRasterConverter().run(
        _context(
            at_limit,
            data,
            config=_config(CONVERTER_MAX_IMAGE_PIXELS="20000", CONVERTER_RASTER_DPI="72"),
        )
    )
    assert _png(outcome.result_path.read_bytes()).size == (200, 100)

    with pytest.raises(ConversionFailure) as error:
        await PdfRasterConverter().run(
            _context(
                above_limit,
                data,
                config=_config(CONVERTER_MAX_IMAGE_PIXELS="19999", CONVERTER_RASTER_DPI="72"),
            )
        )
    assert error.value.failure.reason is FailureReason.PAGE_PIXEL_LIMIT
    assert error.value.failure.params["pagina"] == 1


@pytest.mark.asyncio
@pytest.mark.req("8.11")
async def test_the_limit_applies_to_the_displayed_size_with_rotation_and_cropbox(
    workspace: Workspace,
) -> None:
    # MediaBox de 612 × 792 pero CropBox de 30 × 20: la página se muestra de 30 × 20 puntos
    data = build_blank_pdf([PageSpec(612, 792, crop_box=(0, 0, 30, 20))])
    ctx = _context(
        workspace,
        data,
        config=_config(CONVERTER_MAX_IMAGE_PIXELS="600", CONVERTER_RASTER_DPI="72"),
    )

    outcome = await PdfRasterConverter().run(ctx)

    assert _png(outcome.result_path.read_bytes()).size == (30, 20)


# --- Fallos del worker ---


@pytest.mark.asyncio
@pytest.mark.req("16.2")
async def test_run_fails_with_damaged_file_for_a_pdf_pdfium_cannot_read(
    workspace: Workspace,
) -> None:
    # El inspector rechazaría este archivo; con una información válida la conversión llega al worker
    info = PdfInfo(pages=(PdfPage(*LETTER),), permissions_restricted=False)
    ctx = _context(workspace, b"%PDF-1.4\nesto no es un PDF", source=info)

    with pytest.raises(ConversionFailure) as error:
        await PdfRasterConverter().run(ctx)

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
    data = build_text_pdf([["Secreto"]], owner_password=b"propietario", user_password=b"clave")
    info = PdfInfo(pages=(PdfPage(400, 400),), permissions_restricted=True)
    ctx = _context(workspace, data, source=info)

    with pytest.raises(ConversionFailure) as error:
        await PdfRasterConverter().run(ctx)

    assert error.value.failure.reason is FailureReason.INTERNAL_ERROR
    detail = error.value.detail
    assert isinstance(detail, ToolDetail)
    assert detail.pattern is not None
    assert detail.pattern.id == "pdf_password_error"


@pytest.mark.asyncio
@pytest.mark.req("8.6", "8.11")
@pytest.mark.parametrize("declared_pages", [1, 3])
async def test_run_fails_with_internal_error_if_the_worker_saw_other_pages_than_the_inspector(
    workspace: Workspace, declared_pages: int
) -> None:
    # La verificación del Límite_de_Píxeles no vale si no es del mismo documento que se rasteriza
    info = PdfInfo(pages=(PdfPage(*LETTER),) * declared_pages, permissions_restricted=False)
    ctx = _context(workspace, build_text_pdf([["Uno"], ["Dos"]]), source=info)

    with pytest.raises(ConversionFailure) as error:
        await PdfRasterConverter().run(ctx)

    assert error.value.failure.reason is FailureReason.INTERNAL_ERROR


@pytest.mark.asyncio
async def test_run_needs_the_information_of_a_pdf(workspace: Workspace) -> None:
    ctx = _context(workspace, build_text_pdf([["Uno"]]), source=NoInfo())

    with pytest.raises(TypeError, match="pdf.raster"):
        await PdfRasterConverter().run(ctx)


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
        (
            _completed("RuntimeError: el tamaño rasterizado no coincide con raster_size"),
            "error_interno",
        ),
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


def test_collector_reports_each_page_and_the_total_at_the_end() -> None:
    collector, progress = _collector()

    _feed(collector, PageEvent(1, 3), PageEvent(2, 3), PageEvent(3, 3), DoneEvent(3))

    assert collector.result() == 3
    assert progress.fractions == [1 / 3, 2 / 3, 1.0]


@pytest.mark.parametrize(
    "lines",
    [
        [],  # sin evento final
        [dump_event(PageEvent(1, 2))],  # termina antes del evento final
        ["no es JSON"],
        [dump_event(PageEvent(2, 2))],  # empieza en la página 2
        [dump_event(PageEvent(1, 3)), dump_event(PageEvent(1, 3))],  # repetida
        [dump_event(PageEvent(1, 3)), dump_event(PageEvent(3, 3))],  # salta la 2
        [dump_event(PageEvent(1, 3)), dump_event(PageEvent(2, 4))],  # cambia el total
        [dump_event(PageEvent(1, 1)), dump_event(DoneEvent(2))],  # cuenta mal las páginas
        [dump_event(PageEvent(1, 2)), dump_event(DoneEvent(1))],  # faltan páginas
        [dump_event(DoneEvent(0)), dump_event(DoneEvent(0))],  # dos eventos finales
        [dump_event(DoneEvent(0)), dump_event(PageEvent(1, 1))],  # página tras el final
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
    _feed(collector, PageEvent(1, 1), DoneEvent(1))

    assert progress.fractions == []
    with pytest.raises(WorkerOutputError, match="JSON"):
        collector.result()
