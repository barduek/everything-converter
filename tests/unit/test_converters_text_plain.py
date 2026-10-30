"""Pruebas de src/converter/converters/text_docs/to_txt.py: el convertidor `text.plain` con el
worker real en el Ejecutor aislado, el aviso de Recursos_Externos omitidos, la codificación del
resultado y la clasificación de los fallos del worker (Req 2.12, 6.4, 6.6, 6.7, 6.13, 6.14).

La conversión del texto en sí (listas, saneamiento, eventos, argumentos) está en
`test_workers_text_plain.py`.
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
from converter.converters.text_docs.to_txt import (
    CONVERTER_ID,
    OUTPUT_NAME,
    PlainTextConverter,
    read_done_event,
    worker_failure,
)
from converter.detection.source_info import NoInfo, SourceInfo, TextInfo
from converter.detection.text import BOM_UTF8, TextKind
from converter.eventlog import Tool, ToolDetail
from converter.messages_es import FailureReason, NoticeCode, render_notice
from converter.models import Category, LossClass, Stage
from converter.registry.model import ConversionPair
from converter.sandbox.runner import CompletedRun, SandboxRunner
from converter.storage.workspace import Workspace, WorkspaceManager
from converter.tasks.progress import ProgressReporter
from converter.workers.text_plain import DoneEvent, WorkerOutputError, dump_event


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return WorkspaceManager(tmp_path).create()


def _config() -> Config:
    config = load_config({})
    assert isinstance(config, Config)
    return config


def _context(
    workspace: Workspace,
    data: bytes | None,
    source_format: str,
    *,
    target: str = "TXT",
    encoding: TextKind = TextKind.UTF8,
    source: SourceInfo | None = None,
    progress: ProgressReporter | None = None,
) -> ConversionContext:
    if data is not None:  # None deja el Archivo_de_Origen sin crear
        workspace.source_file.write_bytes(data)
    pair = ConversionPair(
        source=source_format,
        target=target,
        category=Category.DOCUMENT,
        loss_class=LossClass.LOSSY,
        loss_description="Se elimina el marcado.",
        converter_id=CONVERTER_ID,
    )
    return ConversionContext(
        task_id="3b9f0c1e-0000-4000-8000-000000000000",
        pair=pair,
        request=ConversionRequest(target=target),
        source=source if source is not None else TextInfo(encoding=encoding, had_bom=False),
        workspace=workspace,
        config=_config(),
        runner=SandboxRunner(),
        progress=progress if progress is not None else ProgressReporter(),
        notices=NoticeCollector(),
    )


# --- Registro ---


def test_text_plain_is_registered_and_has_no_destination_rejections() -> None:
    converter = get_converter(CONVERTER_ID)

    assert isinstance(converter, PlainTextConverter)
    assert converter.id == "text.plain"
    assert converter.preflight(NoInfo(), ConversionRequest(target="TXT"), _config()) is None


def test_importing_the_converter_does_not_load_the_html_libraries() -> None:
    # Solo el worker usa BeautifulSoup y markdown-it-py: el proceso principal importa el
    # convertidor para registrarlo
    script = (
        "import sys; import converter.converters.text_docs.to_txt; "
        "print('bs4' in sys.modules, 'markdown_it' in sys.modules, 'html5lib' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["False", "False", "False"]


# --- Conversión con el worker real ---


@pytest.mark.asyncio
@pytest.mark.req("6.4")
async def test_run_converts_md_to_the_visible_text_with_list_markers(
    workspace: Workspace,
) -> None:
    md = "# Título\n\nUn *párrafo*.\n\n- uno\n- dos\n\n3. tres\n4. cuatro\n"
    ctx = _context(workspace, md.encode(), "MD")

    outcome = await PlainTextConverter().run(ctx)

    assert outcome.result_kind == "single"
    assert outcome.detected_language is None
    assert outcome.result_path == workspace.work_dir / OUTPUT_NAME
    expected = "Título\nUn párrafo.\n- uno\n- dos\n3. tres\n4. cuatro"
    assert outcome.result_path.read_bytes() == expected.encode()
    assert ctx.notices.notices == ()


@pytest.mark.asyncio
@pytest.mark.req("6.4")
async def test_run_converts_html_to_the_visible_text_with_one_line_per_block(
    workspace: Workspace,
) -> None:
    html = "<h1>Título</h1><p>uno<br>dos</p><ol><li>a</li><li>b<ul><li>c</li></ul></li></ol>"
    ctx = _context(workspace, html.encode(), "HTML")

    outcome = await PlainTextConverter().run(ctx)

    assert outcome.result_path.read_text(encoding="utf-8") == (
        "Título\nuno\ndos\n1. a\n2. b\n   - c"
    )


@pytest.mark.asyncio
@pytest.mark.req("6.4", "6.14")
async def test_run_writes_utf8_without_bom(workspace: Workspace) -> None:
    source = BOM_UTF8 + "<p>Canción</p>".encode()
    ctx = _context(workspace, source, "HTML", source=TextInfo(TextKind.UTF8, had_bom=True))

    outcome = await PlainTextConverter().run(ctx)

    assert outcome.result_path.read_bytes() == "Canción".encode()


@pytest.mark.asyncio
@pytest.mark.req("6.14", "2.12")
@pytest.mark.parametrize("source_format", ["MD", "HTML"])
async def test_run_writes_a_windows_1252_source_as_utf8(
    workspace: Workspace, source_format: str
) -> None:
    text = "¿Cómo está, señor Núñez? “Muy bien” – 5 €"
    ctx = _context(workspace, text.encode("cp1252"), source_format, encoding=TextKind.WINDOWS_1252)

    outcome = await PlainTextConverter().run(ctx)

    assert outcome.result_path.read_bytes() == text.encode("utf-8")


@pytest.mark.asyncio
@pytest.mark.req("2.12")
async def test_run_ignores_the_charset_the_html_declares(workspace: Workspace) -> None:
    html = '<head><meta charset="iso-8859-5"></head><body>ñandú ¿qué?</body>'
    ctx = _context(workspace, html.encode(), "HTML")

    outcome = await PlainTextConverter().run(ctx)

    assert outcome.result_path.read_text(encoding="utf-8") == "ñandú ¿qué?"


@pytest.mark.asyncio
async def test_run_reports_full_progress(workspace: Workspace) -> None:
    progress = ProgressReporter()
    ctx = _context(workspace, b"<p>x</p>", "HTML", progress=progress)

    await PlainTextConverter().run(ctx)

    assert progress.percent == 100


# --- Scripts y Recursos_Externos ---


@pytest.mark.asyncio
@pytest.mark.req("6.7", "6.13")
@pytest.mark.parametrize(
    ("source_format", "document", "expected"),
    [
        (
            "HTML",
            '<p>visible</p><script>CANARIO()</script><p onclick="CANARIO()">más</p>'
            '<a href="javascript:CANARIO()">enlace</a>',
            "visible\nmás\nenlace",
        ),
        (
            "MD",
            "visible\n\n<script>CANARIO()</script>\n\n[enlace](javascript:CANARIO())\n\n"
            '<b onclick="CANARIO()">más</b>',
            "visible\nenlace\nmás",
        ),
    ],
)
async def test_run_leaves_the_code_of_scripts_out_of_the_result(
    workspace: Workspace, source_format: str, document: str, expected: str
) -> None:
    ctx = _context(workspace, document.encode(), source_format)

    outcome = await PlainTextConverter().run(ctx)

    assert outcome.result_path.read_text(encoding="utf-8") == expected
    assert ctx.notices.notices == ()


@pytest.mark.asyncio
@pytest.mark.req("6.6")
async def test_run_adds_a_notice_with_the_number_of_external_resources_of_an_html(
    workspace: Workspace,
) -> None:
    html = (
        '<p>texto</p><img src="http://example.com/a.png"><img src="relativa.png">'
        '<link rel="stylesheet" href="https://example.com/s.css">'
        '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw="><a href="http://example.com/">ir</a>'
    )
    ctx = _context(workspace, html.encode(), "HTML")

    outcome = await PlainTextConverter().run(ctx)

    assert outcome.result_path.read_text(encoding="utf-8") == "texto\nir"
    (notice,) = ctx.notices.notices
    assert notice.code is NoticeCode.EXTERNAL_RESOURCES_SKIPPED
    assert notice.params == {"cantidad": 3}
    assert render_notice(notice).endswith("Recursos omitidos: 3.")


@pytest.mark.asyncio
@pytest.mark.req("6.6")
async def test_run_adds_no_notice_for_an_html_without_external_resources(
    workspace: Workspace,
) -> None:
    html = '<p>texto</p><img src="data:image/gif;base64,R0lGODlhAQABAAAAACw="><a href="#a">ir</a>'
    ctx = _context(workspace, html.encode(), "HTML")

    await PlainTextConverter().run(ctx)

    assert ctx.notices.notices == ()


@pytest.mark.asyncio
@pytest.mark.req("6.6")
async def test_run_adds_no_notice_for_an_md_even_if_it_references_external_resources(
    workspace: Workspace,
) -> None:
    # El criterio 6.6 pide el aviso para un MD solo cuando el destino es PDF
    ctx = _context(workspace, b"texto\n\n![logo](http://example.com/logo.png)\n", "MD")

    outcome = await PlainTextConverter().run(ctx)

    assert outcome.result_path.read_text(encoding="utf-8") == "texto"
    assert ctx.notices.notices == ()


# --- Uso indebido ---


@pytest.mark.asyncio
async def test_run_needs_the_information_of_a_text_format(workspace: Workspace) -> None:
    ctx = _context(workspace, b"<p>x</p>", "HTML", source=NoInfo())

    with pytest.raises(TypeError, match="texto"):
        await PlainTextConverter().run(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize(("source_format", "target"), [("TXT", "TXT"), ("MD", "HTML")])
async def test_run_rejects_pairs_other_than_md_or_html_to_txt(
    workspace: Workspace, source_format: str, target: str
) -> None:
    ctx = _context(workspace, b"x", source_format, target=target)

    with pytest.raises(ValueError, match="text.plain"):
        await PlainTextConverter().run(ctx)


# --- Fallos del worker ---


@pytest.mark.asyncio
@pytest.mark.req("16.9")
async def test_run_fails_with_internal_error_when_the_worker_cannot_read_the_source(
    workspace: Workspace,
) -> None:
    ctx = _context(workspace, None, "HTML")  # sin Archivo_de_Origen

    with pytest.raises(ConversionFailure) as error:
        await PlainTextConverter().run(ctx)

    assert error.value.failure.reason is FailureReason.INTERNAL_ERROR
    assert error.value.failure.stage is Stage.CONVERSION
    detail = error.value.detail
    assert isinstance(detail, ToolDetail)
    assert detail.exit_code == 1
    assert ctx.notices.notices == ()


def _completed(*stderr: str, returncode: int = 1) -> CompletedRun:
    return CompletedRun(returncode=returncode, stderr_lines=stderr)


@pytest.mark.req("16.9", "16.10")
@pytest.mark.parametrize(
    ("completed", "reason"),
    [
        (_completed("OSError: [Errno 28] No space left on device"), "sin_espacio_proceso"),
        (_completed("MemoryError"), "error_interno"),
        (_completed("FileNotFoundError: [Errno 2] No such file or directory"), "error_interno"),
        (_completed("algo que nadie reconoce"), "error_interno"),
        (_completed("PIL.UnidentifiedImageError: cannot identify image file"), "error_interno"),
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
    assert failure.detail.tool is Tool.PILLOW
    assert failure.detail.exit_code == (completed.returncode if completed.returncode > 0 else None)


# --- Protocolo del worker ---


def test_read_done_event_accepts_exactly_one_valid_line() -> None:
    assert read_done_event([dump_event(DoneEvent(4))]) == DoneEvent(4)


@pytest.mark.parametrize(
    "lines",
    [
        [],  # sin evento final
        ["no es JSON"],
        [dump_event(DoneEvent(0)), dump_event(DoneEvent(0))],  # dos eventos finales
        ["salida ajena", dump_event(DoneEvent(0))],  # algo más en la salida estándar
    ],
)
def test_read_done_event_rejects_output_that_breaks_the_protocol(lines: list[str]) -> None:
    with pytest.raises(WorkerOutputError):
        read_done_event(lines)
