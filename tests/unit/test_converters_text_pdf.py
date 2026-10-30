"""Pruebas de src/converter/converters/text_docs/to_pdf.py: el convertidor `text.pdf` con el worker
real en el Ejecutor aislado, los caracteres no representables, el aviso de Recursos_Externos
omitidos, el entorno y los límites del subproceso y la clasificación de los fallos del worker
(Req 6.1, 6.6, 6.7, 6.8, 6.11, 6.12, 6.13, 13.7, 16.9, 16.10).

El contenido de los documentos (maquetación, U+2060, saneamiento, hojas de estilo, fetcher,
eventos y argumentos) está en `test_workers_text_pdf.py`.

Sin WeasyPrint (Pango) ni DejaVu, como en el equipo de desarrollo, el PDF no se puede escribir:
las pruebas de caracteres no representables y de fallos corren igual, porque el worker termina
antes de importar WeasyPrint, y las que escriben un PDF real se omiten y corren dentro de la
Imagen_de_Contenedor. Las pruebas del aviso y de los fallos del protocolo usan un ejecutor que
entrega la salida del worker fijada de antemano (`ScriptedRunner`), como otras pruebas de
convertidores.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pypdfium2  # type: ignore[import-untyped]
import pytest

from converter.config import Config, load_config
from converter.converters.base import (
    ConversionContext,
    ConversionFailure,
    ConversionRequest,
    NoticeCollector,
    get_converter,
)
from converter.converters.text_docs.layout import (
    DEFAULT_FONT_DIR,
    FONTCONFIG_FILE,
    LAYOUT_FONT_FAMILY,
    LAYOUT_FONT_FILE,
)
from converter.converters.text_docs.to_pdf import (
    CONVERTER_ID,
    OUTPUT_NAME,
    TextPdfConverter,
    read_final_event,
    worker_failure,
)
from converter.detection.source_info import NoInfo, SourceInfo, TextInfo
from converter.detection.text import TextKind
from converter.eventlog import ErrorDetail, Tool, ToolDetail
from converter.messages_es import FailureReason, NoticeCode, render_failure, render_notice
from converter.models import Category, LossClass, Stage
from converter.registry.model import ConversionPair
from converter.sandbox.limits import Limits
from converter.sandbox.runner import CompletedRun, LineCallback, SandboxRunner
from converter.storage.workspace import Workspace, WorkspaceManager
from converter.tasks.progress import ProgressReporter
from converter.workers import text_pdf as worker
from converter.workers.pdf_text import clean_page_text
from converter.workers.text_pdf import (
    DoneEvent,
    UnrepresentableEvent,
    WorkerOutputError,
    dump_event,
)
from tests.fontbuild import build_font
from tests.pdf_text_support import build_pdf

UTF8 = TextInfo(encoding=TextKind.UTF8, had_bom=False)
WINDOWS_1252 = TextInfo(encoding=TextKind.WINDOWS_1252, had_bom=False)

_COVERED = "".join(chr(code) for code in range(0x20, 0x7F)) + "áéíóúüñÁÉÍÓÚÜÑ¿¡"
_BULLETS = frozenset("•◦▪")  # marcadores de lista que dibuja WeasyPrint


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return WorkspaceManager(tmp_path).create()


@pytest.fixture
def font_dir(tmp_path: Path) -> Path:
    """Fuente_Predeterminada de prueba: avances de DejaVu Sans Mono (80 columnas por línea)."""
    directory = tmp_path / "fonts"
    build_font(directory / LAYOUT_FONT_FILE, _COVERED, family=LAYOUT_FONT_FAMILY)
    build_font(directory / "DejaVuSans.ttf", _COVERED, family="DejaVu Sans")
    return directory


def _config() -> Config:
    config = load_config({})
    assert isinstance(config, Config)
    return config


def _context(
    workspace: Workspace,
    data: bytes | None,
    source_format: str,
    *,
    target: str = "PDF",
    info: SourceInfo = UTF8,
    runner: SandboxRunner | None = None,
    progress: ProgressReporter | None = None,
) -> ConversionContext:
    if data is not None:  # None deja el Archivo_de_Origen sin crear
        workspace.source_file.write_bytes(data)
    pair = ConversionPair(
        source=source_format,
        target=target,
        category=Category.DOCUMENT,
        loss_class=LossClass.LOSSY,
        loss_description="Se pierde el formato.",
        converter_id=CONVERTER_ID,
    )
    return ConversionContext(
        task_id="3b9f0c1e-0000-4000-8000-000000000000",
        pair=pair,
        request=ConversionRequest(target=target),
        source=info,
        workspace=workspace,
        config=_config(),
        runner=runner if runner is not None else SandboxRunner(),
        progress=progress if progress is not None else ProgressReporter(),
        notices=NoticeCollector(),
    )


class ScriptedRunner(SandboxRunner):
    """Ejecutor que no lanza nada: entrega la salida fijada y guarda lo que recibió.

    Con `create_output`, escribe el archivo de resultado que el worker recibe como último
    argumento, como haría el worker real al terminar.
    """

    def __init__(
        self,
        stdout: Sequence[str] = (),
        *,
        returncode: int = 0,
        stderr: Sequence[str] = (),
        create_output: bool = True,
    ) -> None:
        super().__init__()
        self.stdout = list(stdout)
        self.completed = CompletedRun(returncode, tuple(stderr))
        self.create_output = create_output
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
        if self.create_output:
            Path(argv[-1]).write_bytes(b"%PDF-1.7\n")
        for line in self.stdout:
            if on_stdout_line is not None:
                on_stdout_line(line)
        return self.completed


class RecordingRunner(SandboxRunner):
    """Ejecutor real que además guarda lo que recibió."""

    def __init__(self) -> None:
        super().__init__()
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
        return await super().run(
            argv,
            cwd=cwd,
            limits=limits,
            env=env,
            on_stdout_line=on_stdout_line,
            on_stderr_line=on_stderr_line,
        )


# --- Registro ---


def test_text_pdf_is_registered_and_has_no_destination_rejections() -> None:
    converter = get_converter(CONVERTER_ID)

    assert isinstance(converter, TextPdfConverter)
    assert converter.id == "text.pdf"
    assert converter.preflight(NoInfo(), ConversionRequest(target="PDF"), _config()) is None


def test_the_converter_defaults_to_the_paths_of_the_container_image() -> None:
    assert Path("/opt/converter/fonts/default") == DEFAULT_FONT_DIR
    assert Path("/opt/converter/fontconfig/default-only.conf") == FONTCONFIG_FILE


def test_importing_the_converter_does_not_load_the_html_libraries_or_weasyprint() -> None:
    # Solo el worker usa BeautifulSoup, markdown-it-py y WeasyPrint: el proceso principal importa
    # el convertidor para registrarlo
    script = (
        "import sys; import converter.converters.text_docs.to_pdf; "
        "print(*(name in sys.modules for name in "
        "('bs4', 'markdown_it', 'html5lib', 'weasyprint')))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["False", "False", "False", "False"]


# --- Caracteres no representables, con el worker real (no llega a WeasyPrint) ---


@pytest.mark.asyncio
@pytest.mark.req("6.11")
@pytest.mark.parametrize("source_format", ["TXT", "MD"])
async def test_run_fails_with_the_unrepresentable_characters_of_a_txt_or_md(
    workspace: Workspace, font_dir: Path, source_format: str
) -> None:
    ctx = _context(workspace, "Año 日本, 日 y 🙂 本".encode(), source_format)

    with pytest.raises(ConversionFailure) as error:
        await TextPdfConverter(font_dir=font_dir).run(ctx)

    failure = error.value.failure
    assert failure.reason is FailureReason.UNREPRESENTABLE_CHARACTERS
    assert failure.stage is Stage.CONVERSION
    assert failure.params == {"caracteres": ["日", "本", "🙂"]}
    message = render_failure(failure)
    assert "«日» (U+65E5)" in message.message
    assert "«🙂» (U+1F642)" in message.message
    assert not (workspace.work_dir / OUTPUT_NAME).exists()  # sin Archivo_Resultado
    assert ctx.notices.notices == ()
    assert ctx.progress.percent == 0


@pytest.mark.asyncio
@pytest.mark.req("6.11")
async def test_run_reports_at_most_10_distinct_unrepresentable_characters(
    workspace: Workspace, font_dir: Path
) -> None:
    chars = [chr(0x4E00 + n) for n in range(15)]
    ctx = _context(workspace, " ".join(chars + chars).encode(), "TXT")

    with pytest.raises(ConversionFailure) as error:
        await TextPdfConverter(font_dir=font_dir).run(ctx)

    assert error.value.failure.params == {"caracteres": chars[:10]}


@pytest.mark.asyncio
@pytest.mark.req("6.11", "6.14")
async def test_run_decodes_a_windows_1252_source_before_checking_it(
    workspace: Workspace, font_dir: Path
) -> None:
    # 0x80 es el euro en Windows-1252; la fuente de prueba no lo cubre
    ctx = _context(workspace, b"cuesta 5 \x80", "TXT", info=WINDOWS_1252)

    with pytest.raises(ConversionFailure) as error:
        await TextPdfConverter(font_dir=font_dir).run(ctx)

    assert error.value.failure.params == {"caracteres": ["€"]}


# --- Entorno y límites del subproceso ---


@pytest.mark.asyncio
@pytest.mark.req("6.11", "13.7")
async def test_run_launches_the_worker_with_the_default_only_fontconfig_and_weasyprint_limits(
    workspace: Workspace, font_dir: Path, tmp_path: Path
) -> None:
    runner = RecordingRunner()
    ctx = _context(workspace, "日".encode(), "TXT", runner=runner)

    with pytest.raises(ConversionFailure):
        await TextPdfConverter(font_dir=font_dir).run(ctx)

    ((argv, cwd, limits, env),) = runner.calls
    assert argv[3:] == [
        worker.MODULE,
        f"--font-dir={font_dir}",
        "--",
        "TXT",
        "utf8",
        str(workspace.source_file.absolute()),
        str((workspace.work_dir / OUTPUT_NAME).absolute()),
    ]
    assert cwd == workspace.tmp_dir
    assert limits.address_space_mb == 3_000  # WeasyPrint, 3 GB
    assert env["FONTCONFIG_FILE"] == str(FONTCONFIG_FILE)
    assert Path(env["FONTCONFIG_FILE"]).name == "default-only.conf"
    assert env["HOME"] == env["TMPDIR"] == str(workspace.tmp_dir.absolute())
    assert env["http_proxy"] == env["https_proxy"] == "http://127.0.0.1:9"


@pytest.mark.asyncio
@pytest.mark.req("6.11")
async def test_the_fontconfig_file_and_the_font_directory_can_be_replaced(
    workspace: Workspace, font_dir: Path, tmp_path: Path
) -> None:
    runner = RecordingRunner()
    conf = tmp_path / "otra.conf"
    ctx = _context(workspace, "日".encode(), "TXT", runner=runner)

    with pytest.raises(ConversionFailure):
        await TextPdfConverter(font_dir=font_dir, fontconfig_file=conf).run(ctx)

    ((argv, _, _, env),) = runner.calls
    assert env["FONTCONFIG_FILE"] == str(conf)
    assert f"--font-dir={font_dir}" in argv


# --- Fallos del worker real ---


@pytest.mark.asyncio
@pytest.mark.req("16.9")
async def test_run_fails_with_internal_error_when_the_worker_cannot_read_the_source(
    workspace: Workspace, font_dir: Path
) -> None:
    ctx = _context(workspace, None, "HTML")  # sin Archivo_de_Origen

    with pytest.raises(ConversionFailure) as error:
        await TextPdfConverter(font_dir=font_dir).run(ctx)

    assert error.value.failure.reason is FailureReason.INTERNAL_ERROR
    assert error.value.failure.stage is Stage.CONVERSION
    detail = error.value.detail
    assert isinstance(detail, ToolDetail)
    assert detail.exit_code == 1
    assert ctx.notices.notices == ()


@pytest.mark.asyncio
@pytest.mark.req("6.11", "16.9")
async def test_an_incomplete_font_installation_is_an_internal_error_and_not_unrepresentable_text(
    workspace: Workspace, tmp_path: Path
) -> None:
    ctx = _context(workspace, b"hola", "TXT")

    with pytest.raises(ConversionFailure) as error:
        await TextPdfConverter(font_dir=tmp_path / "sin-fuentes").run(ctx)

    assert error.value.failure.reason is FailureReason.INTERNAL_ERROR


def _completed(*stderr: str, returncode: int = 1) -> CompletedRun:
    return CompletedRun(returncode=returncode, stderr_lines=stderr)


@pytest.mark.req("16.9", "16.10")
@pytest.mark.parametrize(
    ("completed", "reason"),
    [
        (_completed("OSError: [Errno 28] No space left on device"), "sin_espacio_proceso"),
        (_completed("MemoryError"), "error_interno"),
        (_completed("FileNotFoundError: [Errno 2] No such file or directory"), "error_interno"),
        (_completed("OSError: cannot load library 'libpango-1.0-0'"), "error_interno"),
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


# --- Salida del worker: avisos y fallos del protocolo, con el ejecutor fijado ---


@pytest.mark.asyncio
@pytest.mark.req("6.6")
@pytest.mark.parametrize("source_format", ["HTML", "MD"])
async def test_run_adds_a_notice_with_the_number_of_external_resources(
    workspace: Workspace, source_format: str
) -> None:
    runner = ScriptedRunner([dump_event(DoneEvent(3))])
    ctx = _context(workspace, b"x", source_format, runner=runner)

    outcome = await TextPdfConverter().run(ctx)

    assert outcome.result_kind == "single"
    assert outcome.detected_language is None
    assert outcome.result_path == (workspace.work_dir / OUTPUT_NAME).absolute()
    (notice,) = ctx.notices.notices
    assert notice.code is NoticeCode.EXTERNAL_RESOURCES_SKIPPED
    assert notice.params == {"cantidad": 3}
    assert render_notice(notice).endswith("Recursos omitidos: 3.")
    assert ctx.progress.percent == 100


@pytest.mark.asyncio
@pytest.mark.req("6.6")
@pytest.mark.parametrize("source_format", ["TXT", "MD", "HTML"])
async def test_run_adds_no_notice_without_external_resources(
    workspace: Workspace, source_format: str
) -> None:
    runner = ScriptedRunner([dump_event(DoneEvent(0))])
    ctx = _context(workspace, b"x", source_format, runner=runner)

    await TextPdfConverter().run(ctx)

    assert ctx.notices.notices == ()


@pytest.mark.asyncio
@pytest.mark.req("6.6")
async def test_run_adds_no_notice_for_a_txt(workspace: Workspace) -> None:
    # Un TXT no referencia recursos; si el worker informara alguno, no es un aviso del usuario
    runner = ScriptedRunner([dump_event(DoneEvent(2))])
    ctx = _context(workspace, b"x", "TXT", runner=runner)

    await TextPdfConverter().run(ctx)

    assert ctx.notices.notices == ()


@pytest.mark.asyncio
@pytest.mark.req("6.11")
async def test_run_turns_the_unrepresentable_event_into_the_failure(workspace: Workspace) -> None:
    runner = ScriptedRunner(
        [dump_event(UnrepresentableEvent(("日", "\u0001")))], create_output=False
    )
    ctx = _context(workspace, b"x", "MD", runner=runner)

    with pytest.raises(ConversionFailure) as error:
        await TextPdfConverter().run(ctx)

    assert error.value.failure.reason is FailureReason.UNREPRESENTABLE_CHARACTERS
    assert error.value.failure.params == {"caracteres": ["日", "\u0001"]}
    assert "U+0001" in render_failure(error.value.failure).message
    assert ctx.progress.percent == 0


@pytest.mark.asyncio
@pytest.mark.req("16.9")
@pytest.mark.parametrize(
    "stdout",
    [
        [],  # sin evento final
        ["no es JSON"],
        [dump_event(DoneEvent(0)), dump_event(DoneEvent(0))],  # dos eventos finales
        ["salida ajena", dump_event(DoneEvent(0))],  # algo más en la salida estándar
        ['{"event":"unrepresentable","characters":[]}'],
    ],
)
async def test_run_fails_with_internal_error_when_the_worker_breaks_the_protocol(
    workspace: Workspace, stdout: list[str]
) -> None:
    ctx = _context(workspace, b"x", "HTML", runner=ScriptedRunner(stdout))

    with pytest.raises(ConversionFailure) as error:
        await TextPdfConverter().run(ctx)

    assert error.value.failure.reason is FailureReason.INTERNAL_ERROR
    assert isinstance(error.value.detail, ErrorDetail)
    assert ctx.notices.notices == ()
    assert ctx.progress.percent == 0


@pytest.mark.asyncio
@pytest.mark.req("16.9")
async def test_run_fails_with_internal_error_when_the_pdf_is_missing(
    workspace: Workspace,
) -> None:
    runner = ScriptedRunner([dump_event(DoneEvent(0))], create_output=False)
    ctx = _context(workspace, b"x", "TXT", runner=runner)

    with pytest.raises(ConversionFailure) as error:
        await TextPdfConverter().run(ctx)

    assert error.value.failure.reason is FailureReason.INTERNAL_ERROR
    assert ctx.progress.percent == 0


@pytest.mark.asyncio
@pytest.mark.req("16.9", "16.10")
async def test_run_classifies_a_worker_that_exits_with_an_error(workspace: Workspace) -> None:
    runner = ScriptedRunner(
        returncode=1, stderr=["OSError: [Errno 28] No space left on device"], create_output=False
    )
    ctx = _context(workspace, b"x", "TXT", runner=runner)

    with pytest.raises(ConversionFailure) as error:
        await TextPdfConverter().run(ctx)

    assert error.value.failure.reason is FailureReason.NO_SPACE


def test_read_final_event_accepts_exactly_one_valid_line() -> None:
    assert read_final_event([dump_event(DoneEvent(4))]) == DoneEvent(4)
    assert read_final_event([dump_event(UnrepresentableEvent(("日",)))]) == UnrepresentableEvent(
        ("日",)
    )
    for lines in ([], ["a", "b"]):
        with pytest.raises(WorkerOutputError):
            read_final_event(lines)


# --- Uso indebido ---


@pytest.mark.asyncio
async def test_run_needs_the_information_of_a_text_format(workspace: Workspace) -> None:
    ctx = _context(workspace, b"x", "HTML", info=NoInfo())

    with pytest.raises(TypeError, match="texto"):
        await TextPdfConverter().run(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_format", "target"), [("TXT", "HTML"), ("DOCX", "PDF"), ("PDF", "TXT")]
)
async def test_run_rejects_pairs_other_than_txt_md_or_html_to_pdf(
    workspace: Workspace, source_format: str, target: str
) -> None:
    ctx = _context(workspace, b"x", source_format, target=target)

    with pytest.raises(ValueError, match="text.pdf"):
        await TextPdfConverter().run(ctx)


# --- PDF reales: solo con WeasyPrint (Pango) y DejaVu, dentro de la Imagen_de_Contenedor ---


def _read_pdf(path: Path) -> tuple[list[str], list[tuple[float, float]]]:
    """Texto, sin U+2060 ni U+FEFF, y tamaño en puntos de cada página de un PDF."""
    pdf: Any = pypdfium2.PdfDocument(str(path))
    try:
        texts: list[str] = []
        sizes: list[tuple[float, float]] = []
        for index in range(len(pdf)):
            page = pdf[index]
            textpage = page.get_textpage()
            texts.append(clean_page_text(textpage.get_text_range()))
            textpage.close()
            width, height = page.get_size()
            sizes.append((width, height))
            page.close()
        return texts, sizes
    finally:
        pdf.close()


def test_the_pdf_reader_of_these_tests_reads_pages_text_and_sizes(tmp_path: Path) -> None:
    path = tmp_path / "prueba.pdf"
    path.write_bytes(build_pdf([["uno dos", "tres"], []]))

    texts, sizes = _read_pdf(path)

    assert [text.split() for text in texts] == [["uno", "dos", "tres"], []]
    assert sizes == [(400.0, 400.0)] * 2


@pytest.fixture(scope="module")
def dejavu() -> None:
    """Omite la prueba si la Fuente_Predeterminada y su fontconfig no están instaladas."""
    if not (DEFAULT_FONT_DIR / LAYOUT_FONT_FILE).is_file() or not FONTCONFIG_FILE.is_file():
        pytest.skip("DejaVu no está instalado; se prueba en la Imagen_de_Contenedor")


@pytest.fixture(scope="module")
def rendering(dejavu: None) -> None:
    """Omite la prueba si, además de DejaVu, no está WeasyPrint (con Pango)."""
    probe = subprocess.run(
        [sys.executable, "-I", "-c", "import weasyprint"],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("WeasyPrint no está instalado; se prueba en la Imagen_de_Contenedor")


async def _convert(workspace: Workspace, source: str, source_format: str) -> ConversionContext:
    ctx = _context(workspace, source.encode(), source_format)
    outcome = await get_converter(CONVERTER_ID).run(ctx)
    assert outcome.result_path == (workspace.work_dir / OUTPUT_NAME).absolute()
    assert outcome.result_path.read_bytes().startswith(b"%PDF-")
    return ctx


def _words(texts: Sequence[str]) -> list[str]:
    return [word for word in " ".join(texts).split() if word not in _BULLETS]


@pytest.mark.integration
@pytest.mark.usefixtures("rendering")
@pytest.mark.asyncio
@pytest.mark.req("6.1", "6.8")
async def test_txt_to_pdf_has_one_pdf_page_per_layout_page_and_every_word(
    workspace: Workspace,
) -> None:
    text = "".join(f"línea {n} con palabras de prueba\n" for n in range(125)) + "fin"

    await _convert(workspace, text, "TXT")

    texts, sizes = _read_pdf(workspace.work_dir / OUTPUT_NAME)
    assert len(texts) == 3  # 60 + 60 + 6 líneas
    assert _words(texts) == text.split()
    assert all(abs(width - 595.28) < 1 and abs(height - 841.89) < 1 for width, height in sizes)


@pytest.mark.integration
@pytest.mark.usefixtures("rendering")
@pytest.mark.asyncio
@pytest.mark.req("6.1")
async def test_txt_to_pdf_wraps_long_lines_without_losing_text(workspace: Workspace) -> None:
    text = "palabra " * 100 + "\n\tcon tabulación\n" + "x" * 200

    await _convert(workspace, text, "TXT")

    texts, _ = _read_pdf(workspace.work_dir / OUTPUT_NAME)
    assert "".join("".join(texts).split()) == "".join(text.split())


@pytest.mark.integration
@pytest.mark.usefixtures("rendering")
@pytest.mark.asyncio
@pytest.mark.req("6.12")
async def test_md_to_pdf_keeps_the_words_of_the_visible_text(workspace: Workspace) -> None:
    md = (
        "# Título principal\n\n"
        "Un párrafo con palabras-compuestas, a/b y *énfasis* o **fuerte**.\n\n"
        "- uno\n- dos\n\n[enlace](http://example.com/x) final\n"
    )

    ctx = await _convert(workspace, md, "MD")

    texts, _ = _read_pdf(workspace.work_dir / OUTPUT_NAME)
    expected = "Título principal Un párrafo con palabras-compuestas, a/b y énfasis o fuerte."
    assert _words(texts) == [*expected.split(), "uno", "dos", "enlace", "final"]
    assert ctx.notices.notices == ()  # un enlace de navegación no es un Recurso_Externo


@pytest.mark.integration
@pytest.mark.usefixtures("rendering")
@pytest.mark.asyncio
@pytest.mark.req("6.6", "6.7", "6.13", "13.7")
async def test_html_to_pdf_omits_scripts_and_external_resources_without_fetching_them(
    workspace: Workspace, tmp_path: Path
) -> None:
    secret = tmp_path / "secreto.txt"
    secret.write_text("CANARIO", encoding="utf-8")
    html = (
        "<p>visible</p><script>escribir('CODIGO')</script>"
        '<img src="http://127.0.0.1:9/a.png" alt="ALT"><img src="relativa.png">'
        f'<link rel="attachment" href="{secret.as_uri()}">'
        "<style>@font-face { font-family: X; src: url(" + secret.as_uri() + ") }</style>"
    )

    ctx = await _convert(workspace, html, "HTML")

    pdf = (workspace.work_dir / OUTPUT_NAME).read_bytes()
    texts, _ = _read_pdf(workspace.work_dir / OUTPUT_NAME)
    assert _words(texts) == ["visible"]
    # El archivo local no se adjuntó ni se leyó: el fetcher rechaza `file:`
    assert b"secreto" not in pdf
    assert b"CANARIO" not in pdf
    (notice,) = ctx.notices.notices
    assert notice.params == {"cantidad": 3}  # dos img y el @font-face con url()


@pytest.mark.integration
@pytest.mark.usefixtures("rendering")
@pytest.mark.asyncio
@pytest.mark.req("6.12")
async def test_the_stylesheets_of_an_html_take_priority_over_the_base_sheet(
    workspace: Workspace,
) -> None:
    html = "<style>@page { size: A5 }</style><p>hola</p>"

    await _convert(workspace, html, "HTML")

    _, sizes = _read_pdf(workspace.work_dir / OUTPUT_NAME)
    ((width, height),) = sizes
    assert abs(width - 419.53) < 1 and abs(height - 595.28) < 1  # A5, no A4


@pytest.mark.integration
@pytest.mark.usefixtures("rendering")
@pytest.mark.asyncio
@pytest.mark.req("6.11")
async def test_the_default_font_covers_spanish_text(workspace: Workspace) -> None:
    text = "Año nuevo: ¿cómo estás? ¡Qué bien! «comillas» “inglesas” 5 € — fin…\n"

    await _convert(workspace, text, "TXT")

    texts, _ = _read_pdf(workspace.work_dir / OUTPUT_NAME)
    assert _words(texts) == text.split()


@pytest.mark.integration
@pytest.mark.usefixtures("dejavu")
@pytest.mark.asyncio
@pytest.mark.req("6.11")
async def test_the_default_font_does_not_cover_cjk_or_emoji(workspace: Workspace) -> None:
    ctx = _context(workspace, "Año 日本 🙂".encode(), "TXT")

    with pytest.raises(ConversionFailure) as error:
        await get_converter(CONVERTER_ID).run(ctx)

    assert error.value.failure.params == {"caracteres": ["日", "本", "🙂"]}
