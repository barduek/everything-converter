"""Pruebas de src/converter/converters/image_pdf.py: convertidor `image.pdf` (Req 5.4, 5.10, 4.11,
16.2, 16.9, 16.10).

`preflight` se prueba con el `ImageInfo` del inspector `image`. `run` lanza el worker real a través
del Ejecutor aislado sobre un Espacio_de_Trabajo temporal; los fallos que ese worker no produce
con archivos reales (falta de espacio, salida sin resultado, señal) se prueban con un ejecutor que
devuelve el resultado que fija la prueba. La conversión de píxeles y el formato del PDF se
prueban en `test_workers_image_pdf.py`.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from pathlib import Path

# pypdfium2 no publica anotaciones de tipos; sus módulos quedan como Any
import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pytest
from PIL import Image

from converter.config import Config, load_config
from converter.converters import load_converters
from converter.converters.base import (
    ConversionContext,
    ConversionFailure,
    ConversionRequest,
    NoticeCollector,
    get_converter,
)
from converter.converters.image_pdf import ImagePdfConverter
from converter.converters.images import RESULT_NAME
from converter.detection.inspectors.image import ImageInfo
from converter.detection.source_info import TextInfo
from converter.detection.text import TextKind
from converter.eventlog import Tool, ToolDetail
from converter.messages_es import FailureReason, NoticeCode
from converter.messages_es import render_failure as render_failure_message
from converter.models import Category, LossClass, Stage
from converter.registry.model import ConversionPair
from converter.sandbox.limits import Limits
from converter.sandbox.runner import CompletedRun, LineCallback, SandboxRunner
from converter.storage.workspace import WorkspaceManager
from converter.tasks.progress import ProgressReporter
from converter.workers.image_pdf import MAX_JPEG_DIMENSION
from tests.image_samples import BLOCK, GRID_COLUMNS, GRID_ROWS, grid_file, png_bytes

FORMATS = ("PNG", "JPG", "WEBP")
POINTS_PER_PIXEL = 0.75  # 96 ppp


def _config(**values: str) -> Config:
    config = load_config(values)
    assert isinstance(config, Config)
    return config


def _info(
    width: int = 48, height: int = 32, *, tag: int = 1, bits: int = 8, mode: str = "RGB"
) -> ImageInfo:
    return ImageInfo(
        width=width,
        height=height,
        mode=mode,
        bits_per_channel=bits,
        has_alpha=mode == "RGBA",
        frames=1,
        exif_orientation=tag,
    )


def _pair(source: str) -> ConversionPair:
    return ConversionPair(
        source=source,
        target="PDF",
        category=Category.IMAGE,
        loss_class=LossClass.LOSSY,
        loss_description="Prueba.",
        converter_id="image.pdf",
    )


def _page_size(path: Path) -> tuple[float, float]:
    """Ancho y alto en puntos de la única página del PDF `path`."""
    document = pdfium.PdfDocument(str(path))
    try:
        assert len(document) == 1
        width, height = document[0].get_size()
        return width, height
    finally:
        document.close()


# --- Registro ---


@pytest.mark.req("4.3")
def test_the_converter_is_registered_as_image_pdf() -> None:
    converter = get_converter("image.pdf")

    assert isinstance(converter, ImagePdfConverter)
    assert converter.id == "image.pdf"
    assert load_converters()["image.pdf"] is converter


# --- preflight ---


@pytest.mark.req("5.5")
@pytest.mark.parametrize(
    ("width", "height"), [(48, 32), (16384, 20000), (65535, 65535), (100_000, 1000)]
)
def test_preflight_rejects_nothing_whatever_the_dimensions(width: int, height: int) -> None:
    # Req 5.5 fija máximos solo para WEBP y JPG
    assert (
        ImagePdfConverter().preflight(_info(width, height), ConversionRequest("PDF"), _config())
        is None
    )


def test_preflight_fails_for_information_that_is_not_from_an_image() -> None:
    text = TextInfo(encoding=TextKind.UTF8, had_bom=False)

    with pytest.raises(TypeError, match="image.raster"):
        ImagePdfConverter().preflight(text, ConversionRequest("PDF"), _config())


# --- run ---


class ScriptedRunner(SandboxRunner):
    """Ejecutor que no lanza nada: devuelve el resultado fijado y guarda lo que recibió."""

    def __init__(self, completed: CompletedRun) -> None:
        super().__init__()
        self.completed = completed
        self.calls: list[tuple[Sequence[str], Path, Limits, Mapping[str, str]]] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        limits: Limits,
        env: Mapping[str, str],
        on_stdout_line: LineCallback | None = None,
        on_stderr_line: LineCallback | None = None,
    ) -> CompletedRun:
        self.calls.append((argv, cwd, limits, env))
        return self.completed


def _context(
    tmp_path: Path,
    source: str,
    data: bytes,
    info: ImageInfo,
    *,
    runner: SandboxRunner | None = None,
    config: Config | None = None,
) -> ConversionContext:
    workspace = WorkspaceManager(tmp_path).create()
    workspace.source_file.write_bytes(data)
    return ConversionContext(
        task_id="3b9f0c1e-0000-4000-8000-000000000000",
        pair=_pair(source),
        request=ConversionRequest(target="PDF"),
        source=info,
        workspace=workspace,
        config=config or _config(),
        runner=runner or SandboxRunner(),
        progress=ProgressReporter(),
        notices=NoticeCollector(),
    )


def _grid_context(
    tmp_path: Path,
    source: str,
    tag: int | None = None,
    *,
    runner: SandboxRunner | None = None,
    config: Config | None = None,
) -> ConversionContext:
    info = _info(GRID_COLUMNS * BLOCK, GRID_ROWS * BLOCK, tag=tag or 1)
    return _context(tmp_path, source, grid_file(source, tag), info, runner=runner, config=config)


def _png_16_bit() -> bytes:
    sample = [0x1234, 0x5678, 0x9ABC, 0xFFFF]
    return png_bytes(16, 16, [sample * 16] * 16, bitdepth=16, alpha=True)


@pytest.mark.req("5.4")
@pytest.mark.asyncio
@pytest.mark.parametrize("source", FORMATS)
async def test_run_converts_an_8_bit_source_to_a_one_page_pdf_without_notices(
    tmp_path: Path, source: str
) -> None:
    ctx = _grid_context(tmp_path, source)

    outcome = await ImagePdfConverter().run(ctx)

    assert outcome.result_kind == "single"
    assert outcome.detected_language is None
    assert outcome.result_path == (ctx.workspace.work_dir / RESULT_NAME).absolute()
    assert outcome.result_path.read_bytes().startswith(b"%PDF-")
    assert ctx.notices.notices == ()
    assert ctx.progress.percent == 100
    assert _page_size(outcome.result_path) == pytest.approx(
        (GRID_COLUMNS * BLOCK * POINTS_PER_PIXEL, GRID_ROWS * BLOCK * POINTS_PER_PIXEL)
    )


@pytest.mark.req("5.3", "5.4")
@pytest.mark.asyncio
async def test_run_adds_the_bits_reduced_notice_for_a_16_bit_png(tmp_path: Path) -> None:
    ctx = _context(tmp_path, "PNG", _png_16_bit(), _info(16, 16, bits=16, mode="RGBA"))

    outcome = await ImagePdfConverter().run(ctx)

    assert [notice.code for notice in ctx.notices.notices] == [NoticeCode.BITS_REDUCED]
    assert _page_size(outcome.result_path) == pytest.approx((12.0, 12.0))


@pytest.mark.req("5.10")
@pytest.mark.asyncio
@pytest.mark.parametrize("source", FORMATS)
async def test_run_applies_the_orientation_of_the_source(tmp_path: Path, source: str) -> None:
    ctx = _grid_context(tmp_path, source, 6)

    outcome = await ImagePdfConverter().run(ctx)

    # Almacenada de 48 × 32 y con orientación 6, se muestra de 32 × 48
    assert _page_size(outcome.result_path) == pytest.approx(
        (GRID_ROWS * BLOCK * POINTS_PER_PIXEL, GRID_COLUMNS * BLOCK * POINTS_PER_PIXEL)
    )


@pytest.mark.req("5.9")
@pytest.mark.asyncio
async def test_run_reports_an_internal_error_when_the_worker_hits_the_pixel_limit(
    tmp_path: Path,
) -> None:
    # 48 × 32 = 1536 píxeles, más del doble del límite: Pillow se niega a decodificarla
    ctx = _grid_context(tmp_path, "PNG", config=_config(CONVERTER_MAX_IMAGE_PIXELS="100"))

    with pytest.raises(ConversionFailure) as failure:
        await ImagePdfConverter().run(ctx)

    # `excede_limite_pixeles` es un motivo de la detección: durante la conversión, un error interno
    assert failure.value.failure.reason is FailureReason.INTERNAL_ERROR
    assert failure.value.failure.stage is Stage.CONVERSION


@pytest.mark.asyncio
async def test_run_reports_an_internal_error_for_a_side_over_the_jpeg_limit(
    tmp_path: Path,
) -> None:
    # Límite conocido (docstring del módulo): el Conversor lo informaría como archivo dañado si
    # Pillow lo detectara al guardar. La imagen es válida, así que debe ser un error interno
    data = io.BytesIO()
    Image.new("L", (MAX_JPEG_DIMENSION + 1, 1)).save(data, "PNG")
    ctx = _context(tmp_path, "PNG", data.getvalue(), _info(MAX_JPEG_DIMENSION + 1, 1, mode="L"))

    with pytest.raises(ConversionFailure) as failure:
        await ImagePdfConverter().run(ctx)

    assert failure.value.failure.reason is FailureReason.INTERNAL_ERROR
    assert failure.value.failure.stage is Stage.CONVERSION
    assert ctx.notices.notices == ()
    assert ctx.progress.percent == 0


@pytest.mark.req("16.2")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [grid_file("PNG")[: len(grid_file("PNG")) // 2], b"no es una imagen" * 8],
    ids=["PNG truncado", "no es una imagen"],
)
async def test_run_fails_with_a_damaged_file_when_pillow_cannot_decode_the_source(
    tmp_path: Path, data: bytes
) -> None:
    ctx = _context(tmp_path, "PNG", data, _info())

    with pytest.raises(ConversionFailure) as failure:
        await ImagePdfConverter().run(ctx)

    assert failure.value.failure.reason is FailureReason.DAMAGED
    assert failure.value.failure.stage is Stage.CONVERSION
    assert isinstance(failure.value.detail, ToolDetail)
    assert failure.value.detail.tool is Tool.PILLOW
    assert failure.value.detail.exit_code == 1
    assert ctx.notices.notices == ()
    assert ctx.progress.percent == 0
    text = render_failure_message(failure.value.failure).text
    assert text.startswith("Etapa: conversión.")
    assert "Archivo dañado o ilegible" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("completed", "reason"),
    [
        pytest.param(
            CompletedRun(1, ("OSError: [Errno 28] No space left on device",)),
            FailureReason.NO_SPACE,
            id="sin espacio",
            marks=pytest.mark.req("16.10"),
        ),
        pytest.param(
            CompletedRun(-9, ()),
            FailureReason.INTERNAL_ERROR,
            id="señal",
            marks=pytest.mark.req("16.9"),
        ),
        pytest.param(
            CompletedRun(1, ("ValueError: algo inesperado",)),
            FailureReason.INTERNAL_ERROR,
            id="excepción no clasificada",
            marks=pytest.mark.req("16.9"),
        ),
        pytest.param(
            CompletedRun(0, ()),
            FailureReason.INTERNAL_ERROR,
            id="sin resultado",
            marks=pytest.mark.req("16.9"),
        ),
    ],
)
async def test_run_classifies_the_failures_of_the_worker(
    tmp_path: Path, completed: CompletedRun, reason: FailureReason
) -> None:
    runner = ScriptedRunner(completed)
    ctx = _grid_context(tmp_path, "PNG", runner=runner)

    with pytest.raises(ConversionFailure) as failure:
        await ImagePdfConverter().run(ctx)

    assert failure.value.failure.reason is reason
    assert failure.value.failure.stage is Stage.CONVERSION
    assert isinstance(failure.value.detail, ToolDetail)
    assert ctx.notices.notices == ()


@pytest.mark.req("13.7", "14.7")
@pytest.mark.asyncio
async def test_run_launches_the_pdf_worker_with_the_limits_and_environment_of_python(
    tmp_path: Path,
) -> None:
    runner = ScriptedRunner(CompletedRun(1, ()))
    ctx = _grid_context(tmp_path, "WEBP", runner=runner)

    with pytest.raises(ConversionFailure):
        await ImagePdfConverter().run(ctx)

    ((argv, cwd, limits, env),) = runner.calls
    assert list(argv[1:4]) == ["-I", "-m", "converter.workers.image_pdf"]
    # Formato de origen, origen y resultado; el destino siempre es PDF y no se pasa
    assert argv[-3] == "WEBP"
    assert argv[-2] == str(ctx.workspace.source_file.absolute())
    assert argv[-1] == str((ctx.workspace.work_dir / RESULT_NAME).absolute())
    assert "100000000" in argv  # Límite_de_Píxeles de la configuración
    assert cwd == ctx.workspace.tmp_dir
    assert limits.address_space_mb == 2000
    assert env["HOME"] == env["TMPDIR"] == str(ctx.workspace.tmp_dir.absolute())
