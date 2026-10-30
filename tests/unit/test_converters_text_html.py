"""Pruebas de src/converter/converters/text_docs/to_html.py: el convertidor `text.html` con el
worker real en el Ejecutor aislado, para TXT y MD en UTF-8 y Windows-1252, y la clasificación de
los fallos del worker (Req 2.12, 6.2, 6.3, 6.14, 16.9, 16.10).

El contenido de los documentos (escapes, estructura, argumentos) está en
`test_workers_text_html.py`.
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
from converter.converters.text_docs.to_html import (
    CONVERTER_ID,
    OUTPUT_NAME,
    TextHtmlConverter,
    worker_failure,
)
from converter.detection.source_info import NoInfo, TextInfo
from converter.detection.text import TextKind
from converter.eventlog import ToolDetail
from converter.messages_es import FailureReason
from converter.models import Category, LossClass, Stage
from converter.registry.model import ConversionPair
from converter.sandbox.runner import CompletedRun, SandboxRunner
from converter.storage.workspace import Workspace, WorkspaceManager
from converter.tasks.progress import ProgressReporter
from converter.workers.text_html import render_document

UTF8 = TextInfo(encoding=TextKind.UTF8, had_bom=False)
UTF8_WITH_BOM = TextInfo(encoding=TextKind.UTF8, had_bom=True)
WINDOWS_1252 = TextInfo(encoding=TextKind.WINDOWS_1252, had_bom=False)

BOM = b"\xef\xbb\xbf"


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return WorkspaceManager(tmp_path).create()


def _config() -> Config:
    config = load_config({})
    assert isinstance(config, Config)
    return config


def _context(
    workspace: Workspace,
    data: bytes,
    *,
    source_format: str = "TXT",
    info: TextInfo | NoInfo = UTF8,
    progress: ProgressReporter | None = None,
) -> ConversionContext:
    workspace.source_file.write_bytes(data)
    pair = ConversionPair(
        source=source_format,
        target="HTML",
        category=Category.DOCUMENT,
        loss_class=LossClass.LOSSLESS,
        loss_description="",
        converter_id=CONVERTER_ID,
    )
    return ConversionContext(
        task_id="3b9f0c1e-0000-4000-8000-000000000000",
        pair=pair,
        request=ConversionRequest(target="HTML"),
        source=info,
        workspace=workspace,
        config=_config(),
        runner=SandboxRunner(),
        progress=progress if progress is not None else ProgressReporter(),
        notices=NoticeCollector(),
    )


# --- Registro ---


def test_text_html_is_registered_and_has_no_destination_rejections() -> None:
    converter = get_converter(CONVERTER_ID)

    assert isinstance(converter, TextHtmlConverter)
    assert converter.id == "text.html"
    assert converter.preflight(UTF8, ConversionRequest(target="HTML"), _config()) is None


def test_importing_the_converter_does_not_load_the_markdown_and_html_libraries() -> None:
    # Solo el worker carga markdown-it-py y BeautifulSoup: el proceso principal importa el
    # convertidor para registrarlo
    script = (
        "import sys; import converter.converters.text_docs.to_html; "
        "print('markdown_it' in sys.modules, 'bs4' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["False", "False"]


# --- Conversión con el worker real ---


@pytest.mark.asyncio
@pytest.mark.req("6.2")
async def test_run_converts_txt_to_an_html_document_with_the_text_escaped(
    workspace: Workspace,
) -> None:
    text = 'Línea 1 & <b>negrita</b>\n\n"Línea" 3\n'
    ctx = _context(workspace, text.encode("utf-8"))

    outcome = await TextHtmlConverter().run(ctx)

    assert outcome.result_kind == "single"
    assert outcome.detected_language is None
    assert outcome.result_path == workspace.work_dir / OUTPUT_NAME
    result = outcome.result_path.read_bytes()
    assert result == render_document("TXT", text).encode("utf-8")
    html = result.decode("utf-8")
    assert '<html lang="es">' in html
    assert '<meta charset="utf-8">' in html
    assert "<pre>\nLínea 1 &amp; &lt;b&gt;negrita&lt;/b&gt;\n\n&quot;Línea&quot; 3\n</pre>" in html
    assert ctx.notices.notices == ()


@pytest.mark.asyncio
@pytest.mark.req("6.3")
async def test_run_converts_md_to_the_commonmark_html(workspace: Workspace) -> None:
    text = (
        "# Título\n\nUn *párrafo* con `código` y [enlace](https://example.com).\n\n- uno\n- dos\n"
    )
    ctx = _context(workspace, text.encode("utf-8"), source_format="MD")

    outcome = await TextHtmlConverter().run(ctx)

    html = outcome.result_path.read_text(encoding="utf-8")
    assert html == render_document("MD", text)
    assert "<h1>Título</h1>" in html
    assert "<em>párrafo</em>" in html
    assert "<code>código</code>" in html
    assert "<ul>\n<li>uno</li>\n<li>dos</li>\n</ul>" in html
    assert ctx.notices.notices == ()


@pytest.mark.asyncio
@pytest.mark.req("6.14", "2.12")
@pytest.mark.parametrize("source_format", ["TXT", "MD"])
async def test_run_writes_utf8_when_the_source_is_windows_1252(
    workspace: Workspace, source_format: str
) -> None:
    text = "Camión ñandú ¿qué? ¡Sí! €5\n"
    ctx = _context(workspace, text.encode("cp1252"), source_format=source_format, info=WINDOWS_1252)

    outcome = await TextHtmlConverter().run(ctx)

    result = outcome.result_path.read_bytes()
    assert result == render_document(source_format, text).encode("utf-8")
    assert not result.startswith(BOM)


@pytest.mark.asyncio
@pytest.mark.req("6.14", "2.12")
@pytest.mark.parametrize("source_format", ["TXT", "MD"])
async def test_run_drops_the_utf8_bom_of_the_source(
    workspace: Workspace, source_format: str
) -> None:
    text = "ñandú\n"
    ctx = _context(
        workspace, BOM + text.encode("utf-8"), source_format=source_format, info=UTF8_WITH_BOM
    )

    outcome = await TextHtmlConverter().run(ctx)

    result = outcome.result_path.read_bytes()
    assert result == render_document(source_format, text).encode("utf-8")
    assert BOM not in result


@pytest.mark.asyncio
@pytest.mark.req("2.12")
async def test_run_ignores_the_charset_the_document_declares(workspace: Workspace) -> None:
    # Un MD con HTML embebido que declara otra codificación sigue siendo el mismo texto
    text = '<meta charset="iso-8859-5">\n\nñandú\n'
    ctx = _context(workspace, text.encode("utf-8"), source_format="MD")

    outcome = await TextHtmlConverter().run(ctx)

    html = outcome.result_path.read_text(encoding="utf-8")
    assert html == render_document("MD", text)
    assert "ñandú" in html


@pytest.mark.asyncio
async def test_run_reports_full_progress_when_it_finishes(workspace: Workspace) -> None:
    progress = ProgressReporter()
    ctx = _context(workspace, b"hola", progress=progress)

    await TextHtmlConverter().run(ctx)

    assert progress.percent == 100


@pytest.mark.asyncio
@pytest.mark.req("6.2")
async def test_run_converts_an_empty_text(workspace: Workspace) -> None:
    ctx = _context(workspace, b"")

    outcome = await TextHtmlConverter().run(ctx)

    assert outcome.result_path.read_text(encoding="utf-8") == render_document("TXT", "")


# --- Entradas que no corresponden al convertidor ---


@pytest.mark.asyncio
async def test_run_needs_the_information_of_a_text_source(workspace: Workspace) -> None:
    ctx = _context(workspace, b"hola", info=NoInfo())

    with pytest.raises(TypeError, match="text.html"):
        await TextHtmlConverter().run(ctx)


@pytest.mark.asyncio
async def test_run_only_converts_txt_and_md(workspace: Workspace) -> None:
    ctx = _context(workspace, b"<p>hola</p>", source_format="HTML")

    with pytest.raises(ValueError, match="HTML"):
        await TextHtmlConverter().run(ctx)


# --- Fallos del worker ---


@pytest.mark.asyncio
@pytest.mark.req("16.9")
async def test_run_fails_with_internal_error_when_the_text_does_not_match_its_encoding(
    workspace: Workspace,
) -> None:
    # El Detector_de_Tipo clasificó el origen como UTF-8, pero sus bytes no lo son: el worker
    # falla en la decodificación y la Tarea no recibe un Archivo_Resultado
    ctx = _context(workspace, b"\xff\xfe\xfa", info=UTF8)

    with pytest.raises(ConversionFailure) as error:
        await TextHtmlConverter().run(ctx)

    failure = error.value.failure
    assert failure.reason is FailureReason.INTERNAL_ERROR
    assert failure.stage is Stage.CONVERSION
    detail = error.value.detail
    assert isinstance(detail, ToolDetail)
    assert detail.exit_code == 1
    assert not (workspace.work_dir / OUTPUT_NAME).exists()


@pytest.mark.asyncio
@pytest.mark.req("16.9")
async def test_run_fails_with_internal_error_when_the_source_file_is_missing(
    workspace: Workspace,
) -> None:
    ctx = _context(workspace, b"hola")
    workspace.source_file.unlink()

    with pytest.raises(ConversionFailure) as error:
        await TextHtmlConverter().run(ctx)

    assert error.value.failure.reason is FailureReason.INTERNAL_ERROR


def _completed(*stderr: str, returncode: int = 1) -> CompletedRun:
    return CompletedRun(returncode=returncode, stderr_lines=stderr)


@pytest.mark.req("16.9", "16.10")
@pytest.mark.parametrize(
    ("completed", "reason"),
    [
        (_completed("OSError: [Errno 28] No space left on device"), "sin_espacio_proceso"),
        (_completed("OSError: [Errno 28] ENOSPC"), "sin_espacio_proceso"),
        (_completed("MemoryError"), "error_interno"),
        (_completed("OSError: [Errno 27] File too large"), "error_interno"),
        (_completed("algo que nadie reconoce"), "error_interno"),
        (_completed(returncode=-9), "error_interno"),  # SIGKILL por RLIMIT_AS
        (_completed(returncode=-11), "error_interno"),  # SIGSEGV
        (_completed(returncode=127), "error_interno"),  # el lanzador no encontró el programa
        (_completed(returncode=0), "error_interno"),  # terminó bien sin escribir el resultado
    ],
)
def test_worker_failure_classifies_the_stderr_of_the_worker(
    completed: CompletedRun, reason: str
) -> None:
    failure = worker_failure(completed)

    assert failure.failure.reason is FailureReason(reason)
    assert failure.failure.stage is Stage.CONVERSION
    assert isinstance(failure.detail, ToolDetail)
    assert failure.detail.exit_code == (completed.returncode if completed.returncode >= 0 else None)
