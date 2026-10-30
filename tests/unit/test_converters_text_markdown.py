"""Pruebas de src/converter/converters/text_docs/to_md.py: el convertidor `text.markdown` con el
worker real en el Ejecutor aislado, el aviso de Recursos_Externos, la codificación y la
clasificación de los fallos del worker (Req 2.12, 6.5, 6.6, 6.7, 6.13, 6.14).

La conversión de HTML a Markdown en sí (títulos, listas, enlaces, eventos y argumentos) está en
`test_workers_text_markdown.py`.
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
from converter.converters.text_docs.to_md import (
    CONVERTER_ID,
    OUTPUT_NAME,
    EventCollector,
    HtmlToMarkdownConverter,
)
from converter.detection.source_info import NoInfo, SourceInfo, TextInfo
from converter.detection.text import TextKind
from converter.messages_es import FailureReason, NoticeCode, render_notice
from converter.models import Category, LossClass, Stage
from converter.registry.model import ConversionPair
from converter.sandbox.runner import SandboxRunner
from converter.storage.workspace import Workspace, WorkspaceManager
from converter.tasks.progress import ProgressReporter
from converter.workers.text_markdown import DoneEvent, WorkerOutputError, dump_event

UTF8 = TextInfo(encoding=TextKind.UTF8, had_bom=False)
WINDOWS_1252 = TextInfo(encoding=TextKind.WINDOWS_1252, had_bom=False)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return WorkspaceManager(tmp_path).create()


def _config() -> Config:
    config = load_config({})
    assert isinstance(config, Config)
    return config


def _context(workspace: Workspace, data: bytes, source: SourceInfo = UTF8) -> ConversionContext:
    workspace.source_file.write_bytes(data)
    pair = ConversionPair(
        source="HTML",
        target="MD",
        category=Category.DOCUMENT,
        loss_class=LossClass.LIMITED,
        loss_description="Se descartan estilos y elementos sin equivalente en Markdown.",
        converter_id=CONVERTER_ID,
    )
    return ConversionContext(
        task_id="3b9f0c1e-0000-4000-8000-000000000001",
        pair=pair,
        request=ConversionRequest(target="MD"),
        source=source,
        workspace=workspace,
        config=_config(),
        runner=SandboxRunner(),
        progress=ProgressReporter(),
        notices=NoticeCollector(),
    )


# --- Registro ---


def test_text_markdown_is_registered_and_has_no_destination_rejections() -> None:
    converter = get_converter(CONVERTER_ID)

    assert isinstance(converter, HtmlToMarkdownConverter)
    assert converter.id == "text.markdown"
    assert converter.preflight(UTF8, ConversionRequest(target="MD"), _config()) is None


def test_importing_the_converter_does_not_load_markdownify() -> None:
    # Solo el worker usa markdownify: el proceso principal importa el convertidor para registrarlo
    script = (
        "import sys; import converter.converters.text_docs.to_md; "
        "print('markdownify' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["False"]


# --- Conversión con el worker real ---


@pytest.mark.asyncio
@pytest.mark.req("6.5")
async def test_run_writes_the_markdown_of_the_body(workspace: Workspace) -> None:
    html = (
        b"<!DOCTYPE html><html><head><title>T\xc3\xadtulo</title></head><body>"
        b"<h1>Uno</h1><p>Un <strong>p\xc3\xa1rrafo</strong> con un "
        b'<a href="http://e.example/">enlace</a>.</p><ul><li>a</li><li>b</li></ul>'
        b"</body></html>"
    )
    ctx = _context(workspace, html)

    outcome = await HtmlToMarkdownConverter().run(ctx)

    assert outcome.result_kind == "single"
    assert outcome.detected_language is None
    assert outcome.result_path == workspace.work_dir / OUTPUT_NAME
    assert outcome.result_path.read_text(encoding="utf-8") == (
        "# Uno\n\nUn **párrafo** con un [enlace](http://e.example/).\n\n- a\n- b\n"
    )
    assert ctx.notices.notices == ()
    assert ctx.progress.percent == 100


@pytest.mark.asyncio
@pytest.mark.req("6.6")
async def test_run_adds_a_notice_with_the_number_of_external_resources(
    workspace: Workspace,
) -> None:
    html = (
        b'<p>Texto</p><img src="http://a.example/1.png" alt="uno">'
        b'<img src="https://a.example/2.png" alt="dos">'
        b'<link rel="stylesheet" href="http://a.example/estilo.css">'
    )
    ctx = _context(workspace, html)

    outcome = await HtmlToMarkdownConverter().run(ctx)

    assert outcome.result_path.read_text(encoding="utf-8") == "Texto\n"
    (notice,) = ctx.notices.notices
    assert notice.code is NoticeCode.EXTERNAL_RESOURCES_SKIPPED
    assert notice.params == {"cantidad": 3}
    assert render_notice(notice).endswith("Recursos omitidos: 3.")


@pytest.mark.asyncio
@pytest.mark.req("6.6")
async def test_run_without_external_resources_adds_no_notice(workspace: Workspace) -> None:
    ctx = _context(workspace, b'<p><a href="http://e.example/">enlace</a></p>')

    await HtmlToMarkdownConverter().run(ctx)

    assert ctx.notices.notices == ()


@pytest.mark.asyncio
@pytest.mark.req("6.7", "6.13")
async def test_run_leaves_scripts_out_of_the_result(workspace: Workspace) -> None:
    html = (
        b'<p onclick="malo()">uno <a href="javascript:malo()">dos</a></p>'
        b"<script>var malo = function () {};</script><p>tres</p>"
    )
    ctx = _context(workspace, html)

    outcome = await HtmlToMarkdownConverter().run(ctx)

    markdown = outcome.result_path.read_text(encoding="utf-8")
    assert markdown == "uno dos\n\ntres\n"
    assert "malo" not in markdown
    assert ctx.notices.notices == ()  # no hay un aviso de Scripts


@pytest.mark.asyncio
@pytest.mark.req("2.12", "6.14")
async def test_run_writes_utf8_for_a_windows_1252_source(workspace: Workspace) -> None:
    html = "<h1>Año</h1><p>¿Qué tal, señor? ¡Adiós! – «ok»</p>".encode("cp1252")
    ctx = _context(workspace, html, WINDOWS_1252)

    outcome = await HtmlToMarkdownConverter().run(ctx)

    data = outcome.result_path.read_bytes()
    assert data == "# Año\n\n¿Qué tal, señor? ¡Adiós! – «ok»\n".encode()


@pytest.mark.asyncio
@pytest.mark.req("2.12", "6.14")
async def test_run_writes_utf8_without_bom_and_ignores_the_declared_charset(
    workspace: Workspace,
) -> None:
    html = b'\xef\xbb\xbf<meta charset="shift_jis"><p>\xc3\x91and\xc3\xba</p>'
    ctx = _context(workspace, html)

    outcome = await HtmlToMarkdownConverter().run(ctx)

    assert outcome.result_path.read_bytes() == "Ñandú\n".encode()


@pytest.mark.asyncio
async def test_run_needs_the_information_of_a_text(workspace: Workspace) -> None:
    ctx = _context(workspace, b"<p>x</p>", NoInfo())

    with pytest.raises(TypeError, match="texto"):
        await HtmlToMarkdownConverter().run(ctx)


# --- Fallos del worker ---


@pytest.mark.asyncio
@pytest.mark.req("16.9")
async def test_run_fails_with_internal_error_when_the_worker_crashes(
    workspace: Workspace,
) -> None:
    # Un HTML anidado más allá del límite de recursión de Python hace fallar al worker
    ctx = _context(workspace, b"<div>" * 5000 + b"x" + b"</div>" * 5000)

    with pytest.raises(ConversionFailure) as error:
        await HtmlToMarkdownConverter().run(ctx)

    assert error.value.failure.reason is FailureReason.INTERNAL_ERROR
    assert error.value.failure.stage is Stage.CONVERSION
    assert ctx.notices.notices == ()
    assert ctx.progress.percent == 0


# --- Protocolo del worker ---


def _collector(*lines: str) -> EventCollector:
    collector = EventCollector()
    for line in lines:
        collector.feed(line)  # nunca lanza: el error queda para `result`
    return collector


def test_collector_returns_the_final_event() -> None:
    assert _collector(dump_event(DoneEvent(7))).result() == DoneEvent(7)


@pytest.mark.parametrize(
    "lines",
    [
        [],  # sin evento final
        ["no es JSON"],
        ['{"event":"done"}'],
        [dump_event(DoneEvent(0)), dump_event(DoneEvent(0))],  # dos eventos finales
        [dump_event(DoneEvent(1)), "basura"],  # un error posterior invalida la salida
    ],
)
def test_collector_rejects_output_that_breaks_the_protocol(lines: list[str]) -> None:
    with pytest.raises(WorkerOutputError):
        _collector(*lines).result()


def test_collector_ignores_lines_after_the_first_error() -> None:
    collector = _collector("basura", dump_event(DoneEvent(1)))

    with pytest.raises(WorkerOutputError, match="JSON"):
        collector.result()
