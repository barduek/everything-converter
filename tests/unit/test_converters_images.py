"""Pruebas de src/converter/converters/images.py: convertidor `image.raster` (Req 5.3, 5.5, 5.8,
5.10, 16.2, 16.9, 16.10).

`preflight` se prueba con el `ImageInfo` del inspector `image`. `run` lanza el worker real a través
del Ejecutor aislado sobre un Espacio_de_Trabajo temporal; los fallos que ese worker no produce
con archivos reales (falta de espacio, salida sin resultado, señal) se prueban con un ejecutor que
devuelve el resultado que fija la prueba. La conversión de píxeles se prueba en
`test_workers_image.py`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

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
from converter.converters.images import (
    MAX_DIMENSIONS,
    RESULT_NAME,
    RasterImageConverter,
    reduces_bit_depth,
)
from converter.detection.inspectors.image import ImageInfo
from converter.detection.source_info import TextInfo
from converter.detection.text import TextKind
from converter.eventlog import Tool, ToolDetail
from converter.messages_es import FailureReason, NoticeCode, RejectionCode, render_error
from converter.messages_es import render_failure as render_failure_message
from converter.models import Category, DestinationRejection, LossClass, Stage
from converter.registry.model import ConversionPair
from converter.sandbox.limits import Limits
from converter.sandbox.runner import CompletedRun, LineCallback, SandboxRunner
from converter.storage.workspace import WorkspaceManager
from converter.tasks.progress import ProgressReporter
from tests.image_samples import BLOCK, GRID_COLUMNS, GRID_ROWS, grid_file, png_bytes


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


def _pair(source: str, target: str) -> ConversionPair:
    return ConversionPair(
        source=source,
        target=target,
        category=Category.IMAGE,
        loss_class=LossClass.LOSSY,
        loss_description="Prueba.",
        converter_id="image.raster",
    )


# --- Registro ---


@pytest.mark.req("4.3")
def test_the_converter_is_registered_as_image_raster() -> None:
    converter = get_converter("image.raster")

    assert isinstance(converter, RasterImageConverter)
    assert converter.id == "image.raster"
    assert load_converters()["image.raster"] is converter


# --- preflight (Req 5.5) ---


@pytest.mark.req("5.5")
@pytest.mark.parametrize(
    ("target", "width", "height"),
    [
        ("WEBP", 16383, 16383),
        ("WEBP", 16383, 1),
        ("JPG", 65535, 65535),
        ("JPG", 16384, 20000),
        ("PNG", 100_000, 100_000),
    ],
)
def test_preflight_accepts_dimensions_up_to_the_maximum_of_the_target(
    target: str, width: int, height: int
) -> None:
    converter = RasterImageConverter()

    assert converter.preflight(_info(width, height), ConversionRequest(target), _config()) is None


@pytest.mark.req("5.5")
@pytest.mark.parametrize(
    ("target", "width", "height", "maximum"),
    [
        ("WEBP", 16384, 1, 16383),
        ("WEBP", 1, 16384, 16383),
        ("WEBP", 20000, 20000, 16383),
        ("JPG", 65536, 1, 65535),
        ("JPG", 1, 65536, 65535),
    ],
)
def test_preflight_rejects_dimensions_over_the_maximum_of_the_target(
    target: str, width: int, height: int, maximum: int
) -> None:
    rejection = RasterImageConverter().preflight(
        _info(width, height), ConversionRequest(target), _config()
    )

    assert rejection == DestinationRejection(
        RejectionCode.DIMENSIONS_EXCEEDED,
        {"ancho": width, "alto": height, "destino": target, "maximo": maximum},
    )
    assert maximum == MAX_DIMENSIONS[target]


@pytest.mark.req("5.5", "5.10")
@pytest.mark.parametrize(
    ("tag", "shown"),
    [
        *((tag, (3000, 17000)) for tag in (1, 2, 3, 4)),
        *((tag, (17000, 3000)) for tag in (5, 6, 7, 8)),
    ],
)
def test_preflight_reports_the_displayed_dimensions(tag: int, shown: tuple[int, int]) -> None:
    # JPG de 3000 × 17000 almacenados
    rejection = RasterImageConverter().preflight(
        _info(3000, 17000, tag=tag), ConversionRequest("WEBP"), _config()
    )

    assert rejection is not None
    assert (rejection.params["ancho"], rejection.params["alto"]) == shown
    message = render_error(rejection.code, rejection.params)
    assert f"{shown[0]} × {shown[1]} píxeles" in message.message
    assert "16383 × 16383" in message.message
    assert "WEBP" in message.message


@pytest.mark.req("5.5")
def test_preflight_rejects_nothing_for_jpg_at_the_same_dimensions() -> None:
    assert (
        RasterImageConverter().preflight(
            _info(3000, 17000, tag=6), ConversionRequest("JPG"), _config()
        )
        is None
    )


def test_preflight_fails_for_information_that_is_not_from_an_image() -> None:
    text = TextInfo(encoding=TextKind.UTF8, had_bom=False)

    with pytest.raises(TypeError, match="image.raster"):
        RasterImageConverter().preflight(text, ConversionRequest("JPG"), _config())


# --- Aviso de 16 bits (Req 5.3, 5.8) ---


@pytest.mark.req("5.3", "5.8")
@pytest.mark.parametrize(
    ("bits", "target", "expected"),
    [
        (16, "JPG", True),
        (16, "WEBP", True),
        (16, "PNG", False),
        (8, "JPG", False),
        (8, "WEBP", False),
        (1, "JPG", False),
    ],
)
def test_reduces_bit_depth_only_for_16_bit_sources_with_8_bit_targets(
    bits: int, target: str, expected: bool
) -> None:
    assert reduces_bit_depth(_info(bits=bits), target) is expected


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
    target: str,
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
        pair=_pair(source, target),
        request=ConversionRequest(target=target),
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
    target: str,
    tag: int | None = None,
    *,
    runner: SandboxRunner | None = None,
    config: Config | None = None,
) -> ConversionContext:
    info = _info(GRID_COLUMNS * BLOCK, GRID_ROWS * BLOCK, tag=tag or 1)
    return _context(
        tmp_path, source, target, grid_file(source, tag), info, runner=runner, config=config
    )


def _png_16_bit() -> bytes:
    sample = [0x1234, 0x5678, 0x9ABC, 0xFFFF]
    return png_bytes(16, 16, [sample * 16] * 16, bitdepth=16, alpha=True)


@pytest.mark.req("5.3", "5.8")
@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["JPG", "WEBP"])
async def test_run_converts_a_16_bit_png_and_adds_the_bits_reduced_notice(
    tmp_path: Path, target: str
) -> None:
    ctx = _context(tmp_path, "PNG", target, _png_16_bit(), _info(16, 16, bits=16, mode="RGBA"))

    outcome = await RasterImageConverter().run(ctx)

    assert outcome.result_kind == "single"
    assert outcome.detected_language is None
    assert outcome.result_path == (ctx.workspace.work_dir / RESULT_NAME).absolute()
    assert [notice.code for notice in ctx.notices.notices] == [NoticeCode.BITS_REDUCED]
    assert ctx.progress.percent == 100
    with Image.open(outcome.result_path) as result:
        assert result.format == {"JPG": "JPEG", "WEBP": "WEBP"}[target]
        assert result.size == (16, 16)


@pytest.mark.req("5.8")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("PNG", "JPG"),
        ("PNG", "WEBP"),
        ("JPG", "PNG"),
        ("JPG", "WEBP"),
        ("WEBP", "PNG"),
        ("WEBP", "JPG"),
    ],
)
async def test_run_adds_no_notice_for_8_bit_sources(
    tmp_path: Path, source: str, target: str
) -> None:
    ctx = _grid_context(tmp_path, source, target)

    outcome = await RasterImageConverter().run(ctx)

    assert ctx.notices.notices == ()
    assert outcome.result_path.is_file()
    assert outcome.result_path.parent == ctx.workspace.work_dir.absolute()


@pytest.mark.req("5.10")
@pytest.mark.asyncio
async def test_run_applies_the_orientation_of_the_source(tmp_path: Path) -> None:
    ctx = _grid_context(tmp_path, "JPG", "PNG", 6)

    outcome = await RasterImageConverter().run(ctx)

    with Image.open(outcome.result_path) as result:
        assert result.size == (GRID_ROWS * BLOCK, GRID_COLUMNS * BLOCK)  # 32 × 48
        assert 0x0112 not in result.getexif()


@pytest.mark.req("5.9")
@pytest.mark.asyncio
async def test_run_reports_an_internal_error_when_the_worker_hits_the_pixel_limit(
    tmp_path: Path,
) -> None:
    # 48 × 32 = 1536 píxeles, más del doble del límite: Pillow se niega a decodificarla
    ctx = _grid_context(tmp_path, "PNG", "JPG", config=_config(CONVERTER_MAX_IMAGE_PIXELS="100"))

    with pytest.raises(ConversionFailure) as failure:
        await RasterImageConverter().run(ctx)

    # `excede_limite_pixeles` es un motivo de la detección: durante la conversión, un error interno
    assert failure.value.failure.reason is FailureReason.INTERNAL_ERROR
    assert failure.value.failure.stage is Stage.CONVERSION


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
    ctx = _context(tmp_path, "PNG", "JPG", data, _info())

    with pytest.raises(ConversionFailure) as failure:
        await RasterImageConverter().run(ctx)

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
    ctx = _grid_context(tmp_path, "PNG", "JPG", runner=runner)

    with pytest.raises(ConversionFailure) as failure:
        await RasterImageConverter().run(ctx)

    assert failure.value.failure.reason is reason
    assert failure.value.failure.stage is Stage.CONVERSION
    assert isinstance(failure.value.detail, ToolDetail)


@pytest.mark.req("13.7", "14.7")
@pytest.mark.asyncio
async def test_run_launches_the_worker_with_the_limits_and_environment_of_python(
    tmp_path: Path,
) -> None:
    runner = ScriptedRunner(CompletedRun(1, ()))
    ctx = _grid_context(tmp_path, "PNG", "WEBP", runner=runner)

    with pytest.raises(ConversionFailure):
        await RasterImageConverter().run(ctx)

    ((argv, cwd, limits, env),) = runner.calls
    assert list(argv[1:4]) == ["-I", "-m", "converter.workers.image"]
    assert argv[-4:-2] == ["PNG", "WEBP"]
    assert argv[-2] == str(ctx.workspace.source_file.absolute())
    assert argv[-1] == str((ctx.workspace.work_dir / RESULT_NAME).absolute())
    assert "100000000" in argv  # Límite_de_Píxeles de la configuración
    assert cwd == ctx.workspace.tmp_dir
    assert limits.address_space_mb == 2000
    assert env["HOME"] == env["TMPDIR"] == str(ctx.workspace.tmp_dir.absolute())
