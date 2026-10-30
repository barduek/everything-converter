"""Pruebas de src/converter/workers/text_pdf.py: documentos que se entregan a WeasyPrint, hojas de
estilo, U+2060, saneamiento, caracteres no representables, fetcher que solo responde `data:`,
eventos y argumentos (Req 6.1, 6.6, 6.7, 6.8, 6.11, 6.12, 6.13, 13.7).

Casi todo se prueba sin WeasyPrint: el worker prepara el HTML y la hoja de estilo antes de
importarlo, y la comprobación de caracteres no representables ocurre en ese punto, con fuentes
creadas para la prueba (`tests/fontbuild.py`). Las pruebas que necesitan WeasyPrint (Pango) se
omiten si no está instalado, como en el equipo de desarrollo; corren dentro de la
Imagen_de_Contenedor. El PDF mismo se prueba con el convertidor, en `test_converters_text_pdf.py`.
"""

from __future__ import annotations

import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from converter.converters.text_docs.dom import WORD_JOINER, parse_html
from converter.converters.text_docs.layout import (
    DEFAULT_FONT_DIR,
    LAYOUT_FONT_FAMILY,
    LAYOUT_FONT_FILE,
    MAX_UNREPRESENTABLE,
    TXT_PAGE,
)
from converter.detection.text import TextKind
from converter.workers.text_pdf import (
    CHECKED_FORMATS,
    EXIT_USAGE,
    MODULE,
    SOURCE_FORMATS,
    DoneEvent,
    UnrepresentableEvent,
    UnrepresentableTextError,
    UrlRefusedError,
    WorkerArgs,
    WorkerOutputError,
    add_word_joiners,
    convert_file,
    dump_event,
    format_args,
    load_event,
    make_url_fetcher,
    markup_document,
    markup_stylesheet,
    parse_args,
    parse_data_url,
    prepare_document,
    txt_document,
    txt_stylesheet,
)
from tests.fontbuild import build_font

# Caracteres de las fuentes de prueba: ASCII imprimible y las letras y signos del español
_SPANISH = "áéíóúüñÁÉÍÓÚÜÑ¿¡"
_COVERED = "".join(chr(code) for code in range(0x20, 0x7F)) + _SPANISH


@pytest.fixture
def font_dir(tmp_path: Path) -> Path:
    """Fuente_Predeterminada de prueba: avances de DejaVu Sans Mono (80 columnas por línea)."""
    directory = tmp_path / "fonts"
    build_font(directory / LAYOUT_FONT_FILE, _COVERED, family=LAYOUT_FONT_FAMILY)
    build_font(directory / "DejaVuSans.ttf", _COVERED, family="DejaVu Sans")
    return directory


def _rules(css: str) -> dict[str, dict[str, str]]:
    """Declaraciones de cada regla de una hoja sin anidamiento, por selector."""
    rules: dict[str, dict[str, str]] = {}
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        declarations: dict[str, str] = {}
        for part in body.split(";"):
            if part.strip():
                name, _, value = part.partition(":")
                declarations[name.strip()] = value.strip()
        rules[" ".join(selector.split())] = declarations
    return rules


def _pages(document_html: str) -> list[str]:
    """Texto de cada `<pre>` de un documento de TXT a PDF, como lo lee un analizador de HTML."""
    soup = BeautifulSoup(document_html, "html5lib")
    return [pre.get_text() for pre in soup.find_all("pre")]


# --- Hojas de estilo ---


@pytest.mark.req("6.12")
def test_markup_stylesheet_is_a4_with_dejavu_and_the_design_properties() -> None:
    rules = _rules(markup_stylesheet())

    assert rules["@page"]["size"] == "A4"
    assert rules["@page"]["margin"] == "2cm"
    html = rules["html"]
    assert html["font-family"].startswith('"DejaVu Sans",')
    assert html["hyphens"] == "manual"
    assert html["font-variant-ligatures"] == "none"
    assert html["text-align"] == "left"
    assert rules["pre"]["white-space"] == "pre-wrap"
    assert rules["pre, code, kbd, samp, tt"]["font-family"].startswith('"DejaVu Sans Mono",')


@pytest.mark.req("6.1")
def test_txt_stylesheet_uses_the_layout_page_and_white_space_pre() -> None:
    rules = _rules(txt_stylesheet())

    assert rules["@page"] == {"size": "210mm 297mm", "margin": "20mm"}
    pre = rules["pre"]
    assert pre["white-space"] == "pre"
    assert pre["font-family"] == '"DejaVu Sans Mono", monospace'
    assert (pre["font-size"], pre["line-height"]) == ("10pt", "12pt")
    assert pre["margin"] == "0"
    assert rules["pre + pre"] == {"break-before": "page"}
    assert rules["html"]["font-variant-ligatures"] == "none"
    assert (TXT_PAGE.font_size_pt, TXT_PAGE.line_height_pt) == (10, 12)


# --- TXT: maquetación y caracteres no representables ---


@pytest.mark.req("6.1")
def test_txt_pages_follow_the_layout_of_80_columns_and_60_lines(font_dir: Path) -> None:
    text = "x" * 200 + "\n" + "y\n" * 124

    pages = _pages(txt_document(text, font_dir).html)

    # 3 líneas de la primera línea del origen y 124 de "y": 127 líneas
    assert [len(page.split("\n")) for page in pages] == [60, 60, 7]
    lines = [line for page in pages for line in page.split("\n")]
    assert lines[:3] == ["x" * 80, "x" * 80, "x" * 40]
    assert lines[3:6] == ["y", "y", "y"]


@pytest.mark.req("6.1")
def test_txt_keeps_every_character_and_expands_tabs(font_dir: Path) -> None:
    text = "uno dos\n\tcon tabulación, ñandú ¿qué?\n\n  fin"

    (page,) = _pages(txt_document(text, font_dir).html)

    assert page == "uno dos\n        con tabulación, ñandú ¿qué?\n\n  fin"


@pytest.mark.req("6.1")
def test_txt_breaks_lines_only_where_the_layout_does(font_dir: Path) -> None:
    text = "palabra " * 30  # 240 caracteres

    (page,) = _pages(txt_document(text, font_dir).html)

    lines = page.split("\n")
    assert "".join(lines) == text
    assert [len(line) for line in lines] == [80, 80, 80]
    assert all(line.endswith(" ") for line in lines)  # el corte cae junto a un espacio


@pytest.mark.req("6.1")
def test_txt_text_is_escaped_in_the_pages(font_dir: Path) -> None:
    document = txt_document('<b>&"x"</b>', font_dir)

    assert "&lt;b&gt;&amp;&quot;x&quot;&lt;/b&gt;" in document.html
    assert _pages(document.html) == ['<b>&"x"</b>']


@pytest.mark.req("6.1")
def test_txt_pages_keep_leading_blank_lines_and_an_empty_text_has_one_page(
    font_dir: Path,
) -> None:
    assert _pages(txt_document("\n\nuno", font_dir).html) == ["\n\nuno"]
    assert _pages(txt_document("", font_dir).html) == [""]


@pytest.mark.req("6.1", "6.12")
def test_txt_document_has_the_txt_stylesheet_and_no_external_resources(font_dir: Path) -> None:
    document = txt_document("hola", font_dir)

    assert document.stylesheet == txt_stylesheet(TXT_PAGE)
    assert document.external_resources == 0
    assert '<meta charset="utf-8">' in document.html


@pytest.mark.req("6.11")
def test_txt_with_unrepresentable_characters_stops_before_the_layout(font_dir: Path) -> None:
    with pytest.raises(UnrepresentableTextError) as error:
        txt_document("año 日本, 日 b 🙂 本", font_dir)

    assert error.value.characters == ("日", "本", "🙂")


@pytest.mark.req("6.11")
def test_at_most_10_unrepresentable_characters_are_reported(font_dir: Path) -> None:
    chars = [chr(0x4E00 + n) for n in range(15)]

    with pytest.raises(UnrepresentableTextError) as error:
        txt_document(" ".join(chars + chars), font_dir)

    assert MAX_UNREPRESENTABLE == 10
    assert error.value.characters == tuple(chars[:10])


@pytest.mark.req("6.11")
def test_spaces_tabs_and_line_breaks_are_not_unrepresentable(font_dir: Path) -> None:
    assert _pages(txt_document(" \t\r\n \r\n", font_dir).html)


@pytest.mark.req("6.11")
def test_txt_needs_the_default_fonts(tmp_path: Path) -> None:
    # Una instalación incompleta no se confunde con texto no representable
    with pytest.raises(FileNotFoundError):
        txt_document("hola", tmp_path / "sin-fuentes")


# --- MD y HTML: DOM saneado ---


@pytest.mark.req("6.7", "6.13")
@pytest.mark.parametrize("source_format", ["HTML", "MD"])
def test_scripts_are_removed_from_the_document(source_format: str, font_dir: Path) -> None:
    if source_format == "HTML":
        source = (
            '<p>visible</p><script>CANARIO()</script><p onclick="CANARIO()">más</p>'
            '<a href="javascript:CANARIO()">enlace</a>'
        )
    else:
        source = (
            "visible\n\n<script>CANARIO()</script>\n\n[enlace](javascript:CANARIO())\n\n"
            '<b onclick="CANARIO()">más</b>'
        )

    document = markup_document(source, source_format, font_dir)

    assert "CANARIO" not in document.html
    assert "<script" not in document.html
    assert "visible" in document.html
    assert "enlace" in document.html


@pytest.mark.req("6.6")
@pytest.mark.parametrize("source_format", ["HTML", "MD"])
def test_external_resources_are_counted_and_removed(source_format: str, font_dir: Path) -> None:
    if source_format == "HTML":
        source = (
            '<p>texto</p><img src="http://example.com/a.png" alt="ALT"><img src="relativa.png">'
            '<link rel="stylesheet" href="https://example.com/s.css">'
            '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw="><a href="http://e.com/">ir</a>'
        )
        expected = 3
    else:
        source = (
            "texto\n\n![ALT](http://example.com/a.png) ![x](relativa.png)\n\n[ir](http://e.com/)"
        )
        expected = 2

    document = markup_document(source, source_format, font_dir)

    assert document.external_resources == expected
    assert "example.com/a.png" not in document.html
    assert "relativa.png" not in document.html
    assert "ALT" not in document.html
    if source_format == "HTML":
        assert "data:image/gif" in document.html  # los data: se conservan
    assert "http://e.com/" in document.html  # los enlaces de navegación también


@pytest.mark.req("6.12")
def test_the_document_sheets_stay_in_the_html_and_the_base_sheet_is_separate(
    font_dir: Path,
) -> None:
    source = '<style>p { color: red }</style><p style="margin: 0">hola</p>'

    document = markup_document(source, "HTML", font_dir)

    # Las hojas del documento son de autor; la base va aparte, como hoja de usuario, así que
    # WeasyPrint le da menor prioridad (agente de usuario < usuario < autor)
    assert "<style>p { color: red }</style>" in document.html
    assert 'style="margin: 0"' in document.html
    assert document.stylesheet == markup_stylesheet()
    assert "color: red" not in document.stylesheet


@pytest.mark.req("2.12")
def test_the_charset_the_html_declares_does_not_change_the_text(font_dir: Path) -> None:
    source = '<head><meta charset="iso-8859-5"></head><body>ñandú ¿qué?</body>'

    html = markup_document(source, "HTML", font_dir).html

    assert "ñandú ¿qué?" in html.replace(WORD_JOINER, "")
    assert "iso-8859-5" not in html  # el documento se serializa como UTF-8


@pytest.mark.req("6.11")
def test_md_with_unrepresentable_visible_text_is_rejected(font_dir: Path) -> None:
    md = "# Título 日本\n\nUn [enlace 🙂](http://e.com/) y `código 本`.\n\n    日 sangrado\n"

    with pytest.raises(UnrepresentableTextError) as error:
        markup_document(md, "MD", font_dir)

    assert error.value.characters == ("日", "本", "🙂")


@pytest.mark.req("6.11")
def test_md_checks_the_visible_text_and_not_what_the_pdf_never_shows(font_dir: Path) -> None:
    md = (
        "Texto\n\n<!-- 日 -->\n\n<script>日()</script>\n\n![日](http://e.com/日.png)\n\n"
        "[ir](http://e.com/本)\n"
    )

    document = markup_document(md, "MD", font_dir)

    assert document.external_resources == 1


@pytest.mark.req("6.11")
def test_html_is_not_checked_for_unrepresentable_characters(tmp_path: Path) -> None:
    # Ni siquiera lee el directorio de fuentes
    document = markup_document("<p>日本 🙂</p>", "HTML", tmp_path / "sin-fuentes")

    assert "日本 🙂" in document.html


@pytest.mark.req("6.11")
def test_only_txt_and_md_are_checked() -> None:
    assert CHECKED_FORMATS == ("TXT", "MD")
    assert SOURCE_FORMATS == ("TXT", "MD", "HTML")


def test_markup_document_rejects_other_formats(font_dir: Path) -> None:
    with pytest.raises(ValueError, match="TXT"):
        markup_document("x", "TXT", font_dir)
    with pytest.raises(ValueError, match="DOCX"):
        prepare_document("x", "DOCX", font_dir)


@pytest.mark.req("6.12")
def test_prepare_document_dispatches_by_source_format(font_dir: Path) -> None:
    assert prepare_document("a", "TXT", font_dir).stylesheet == txt_stylesheet()
    assert prepare_document("a", "MD", font_dir).stylesheet == markup_stylesheet()
    assert prepare_document("a", "HTML", font_dir).stylesheet == markup_stylesheet()


# --- U+2060 ---


def _joined(html: str) -> str:
    doc = parse_html(html)
    add_word_joiners(doc)
    body = doc.find("body")
    assert body is not None
    return body.decode_contents()


@pytest.mark.req("6.12")
def test_word_joiners_follow_punctuation_inside_words() -> None:
    wj = WORD_JOINER

    assert _joined("<p>foo-bar a/b x.y: ok</p>") == f"<p>foo-{wj}bar a/{wj}b x.{wj}y: ok</p>"


@pytest.mark.req("6.12")
def test_word_joiners_cross_inline_elements() -> None:
    wj = WORD_JOINER

    assert _joined("<p>foo-<b>bar</b></p>") == f"<p>foo-{wj}<b>bar</b></p>"
    assert _joined("<p><b>foo-</b>bar</p>") == f"<p><b>foo-{wj}</b>bar</p>"
    assert _joined("<p><i>a-</i><em><u>b</u></em></p>") == f"<p><i>a-{wj}</i><em><u>b</u></em></p>"


@pytest.mark.req("6.12")
@pytest.mark.parametrize(
    "html",
    [
        "<p>foo-</p><p>bar</p>",  # párrafos distintos
        "<p>foo-<br>bar</p>",  # salto forzado
        "<div>foo-<div>bar</div></div>",
        "<ul><li>a-</li><li>b</li></ul>",
        "<table><tr><td>a-</td><td>b</td></tr></table>",
        '<p>foo-<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">bar</p>',
        "<p>foo- <b>bar</b></p>",  # separado por un espacio
    ],
)
def test_word_joiners_do_not_cross_line_boundaries(html: str) -> None:
    assert WORD_JOINER not in _joined(html)


@pytest.mark.req("6.12")
def test_word_joiners_leave_styles_scripts_comments_and_head_alone() -> None:
    html = (
        "<head><title>a-b</title></head><body><p>x</p><style>a{color:red;margin:0}</style>"
        "<!-- c-d --><template>e-f</template></body>"
    )

    assert _joined(html) == (
        "<p>x</p><style>a{color:red;margin:0}</style><!-- c-d --><template>e-f</template>"
    )


@pytest.mark.req("6.12")
def test_word_joiners_apply_inside_pre_and_count_changed_texts() -> None:
    doc = parse_html("<pre>a-b</pre><p>sin signos</p><p>c/d</p>")

    assert add_word_joiners(doc) == 2
    assert add_word_joiners(doc) == 0  # idempotente


@pytest.mark.req("6.12")
def test_word_joiners_in_a_document_without_body_do_nothing() -> None:
    doc = parse_html("<frameset><frame></frameset>")

    assert add_word_joiners(doc) == 0


@pytest.mark.req("6.12")
def test_markup_document_has_word_joiners_and_removing_them_gives_the_visible_text(
    font_dir: Path,
) -> None:
    md = "Un texto-largo con *énfasis-fuerte* y a/b.\n"

    document = markup_document(md, "MD", font_dir)

    assert f"texto-{WORD_JOINER}largo" in document.html
    assert f"énfasis-{WORD_JOINER}fuerte" in document.html
    assert f"a/{WORD_JOINER}b" in document.html
    assert WORD_JOINER not in document.html.replace(f"-{WORD_JOINER}", "-").replace(
        f"/{WORD_JOINER}", "/"
    )


# --- Sin red: solo URL data: ---


@pytest.mark.req("6.6", "13.7")
def test_data_urls_are_decoded() -> None:
    plain = parse_data_url("data:text/css;charset=UTF-8,a%7Bcolor%3Ared%7D")
    assert (plain.media_type, plain.charset, plain.body) == ("text/css", "utf-8", b"a{color:red}")
    assert plain.content_type == "text/css; charset=utf-8"

    base64_url = parse_data_url("data:image/png;base64,iVBORw0KGgo=")
    assert (base64_url.media_type, base64_url.charset) == ("image/png", None)
    assert base64_url.body == b"\x89PNG\r\n\x1a\n"
    assert base64_url.content_type == "image/png"


@pytest.mark.req("6.6", "13.7")
def test_data_urls_follow_the_lenient_rules_of_browsers() -> None:
    assert parse_data_url("DATA:text/plain,hola").body == b"hola"  # esquema sin mayúsculas
    assert parse_data_url("data:,sin tipo").media_type == "text/plain"
    assert parse_data_url("data:;base64,aG9s\n YQ").body == b"hola"  # espacios y relleno faltante
    assert parse_data_url("data:text/plain,a%2Cb#fragmento").body == b"a,b"
    assert parse_data_url("data:no es un tipo,x").media_type == "text/plain"
    assert parse_data_url("data:text/plain;charset=raro valor,x").charset is None
    assert parse_data_url("data:text/plain,%C3%B1").body == "ñ".encode()


@pytest.mark.req("6.6", "13.7")
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "FILE:///etc/passwd",
        "http://127.0.0.1:1/x",
        "https://example.com/a.png",
        "ftp://example.com/a",
        "//example.com/a.png",
        "/etc/passwd",
        "relativa.png",
        "about:blank",
        "javascript:alert(1)",
        " data:text/plain,x",  # nada delante del esquema
        "\x00data:text/plain,x",
        "x-data:text/plain,x",
        "",
        "data",
    ],
)
def test_every_scheme_but_data_is_refused(url: str) -> None:
    with pytest.raises(UrlRefusedError, match="data:"):
        parse_data_url(url)


@pytest.mark.req("6.6", "13.7")
@pytest.mark.parametrize("url", ["data:text/plain", "data:image/png;base64,@@@@", "data:;base64,a"])
def test_invalid_data_urls_are_refused(url: str) -> None:
    with pytest.raises(UrlRefusedError):
        parse_data_url(url)


@pytest.fixture(scope="module")
def weasyprint_urls() -> None:
    """Omite la prueba si WeasyPrint no se puede importar (por ejemplo, sin Pango)."""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", "import weasyprint"],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("WeasyPrint no está instalado; se prueba en la Imagen_de_Contenedor")


@pytest.mark.req("6.6", "13.7")
def test_the_weasyprint_fetcher_serves_data_urls_and_refuses_file_urls(
    weasyprint_urls: None, tmp_path: Path
) -> None:
    from weasyprint.urls import URLFetchingError, fetch  # type: ignore[import-untyped]

    secret = tmp_path / "secreto.txt"
    secret.write_text("CANARIO", encoding="utf-8")
    fetcher = make_url_fetcher()

    with fetch(fetcher, "data:text/css;charset=utf-8,a%7Bcolor%3Ared%7D") as response:
        assert response.content_type == "text/css"
        assert response.charset == "utf-8"
        assert response.read() == b"a{color:red}"
    for url in (secret.as_uri(), "http://127.0.0.1:9/x", "https://example.com/a.png"):
        with pytest.raises(URLFetchingError), fetch(fetcher, url):
            pytest.fail(f"el fetcher respondió {url}")


# --- Eventos ---


@pytest.mark.parametrize(
    "event",
    [
        DoneEvent(0),
        DoneEvent(7),
        UnrepresentableEvent(("日",)),
        UnrepresentableEvent(("日", "🙂", "\u0001", "é")),
        UnrepresentableEvent(tuple(chr(0x4E00 + n) for n in range(10))),
    ],
)
def test_events_survive_dump_and_load(event: DoneEvent | UnrepresentableEvent) -> None:
    line = dump_event(event)

    assert line.isascii()
    assert "\n" not in line
    assert load_event(line) == event


def test_the_done_event_format() -> None:
    assert dump_event(DoneEvent(3)) == '{"event":"done","external_resources":3}'
    assert dump_event(UnrepresentableEvent(("日",))) == (
        '{"event":"unrepresentable","characters":["\\u65e5"]}'
    )


@pytest.mark.parametrize(
    "line",
    [
        "",
        "no es JSON",
        "[]",
        '{"event":"otro"}',
        '{"event":"done"}',
        '{"event":"done","external_resources":-1}',
        '{"event":"done","external_resources":true}',
        '{"event":"done","external_resources":"2"}',
        '{"event":"done","external_resources":1,"extra":1}',
        '{"event":"unrepresentable","characters":[]}',
        '{"event":"unrepresentable","characters":"日"}',
        '{"event":"unrepresentable","characters":["日","日"]}',  # repetidos
        '{"event":"unrepresentable","characters":["日日"]}',  # más de un carácter
        '{"event":"unrepresentable","characters":[" "]}',  # un espacio no se informa
        '{"event":"unrepresentable","characters":["\\n"]}',
        '{"event":"unrepresentable","characters":[1]}',
        '{"event":"unrepresentable","characters":'
        + str(list("abcdefghijk")).replace("'", '"')
        + "}",
    ],
)
def test_load_event_rejects_invalid_lines(line: str) -> None:
    with pytest.raises(WorkerOutputError):
        load_event(line)


def test_events_validate_their_data() -> None:
    with pytest.raises(ValueError, match="Recursos_Externos"):
        DoneEvent(-1)
    with pytest.raises(ValueError):
        UnrepresentableEvent(())
    with pytest.raises(ValueError):
        UnrepresentableEvent(("a", "a"))


# --- Argumentos ---


def test_parse_args_round_trips_format_args(tmp_path: Path) -> None:
    for source_format in SOURCE_FORMATS:
        for encoding in (TextKind.UTF8, TextKind.WINDOWS_1252):
            args = WorkerArgs(
                source_format=source_format,
                encoding=encoding,
                source=tmp_path / "origen",
                destination=tmp_path / "resultado.pdf",
                font_dir=tmp_path / "fuentes",
            )
            assert parse_args(format_args(args)) == args


def test_font_dir_defaults_to_the_image_path() -> None:
    args = parse_args(["--", "TXT", "utf8", "origen", "resultado.pdf"])

    assert args.font_dir == DEFAULT_FONT_DIR == Path("/opt/converter/fonts/default")
    assert inspect.signature(WorkerArgs).parameters["font_dir"].default == DEFAULT_FONT_DIR


def test_paths_that_start_with_a_dash_are_not_options() -> None:
    args = WorkerArgs(
        source_format="MD",
        encoding=TextKind.UTF8,
        source=Path("-origen"),
        destination=Path("-resultado.pdf"),
        font_dir=Path("-fuentes"),
    )

    assert parse_args(format_args(args)) == args


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["TXT"],
        ["--", "DOCX", "utf8", "a", "b"],
        ["--", "TXT", "latin1", "a", "b"],
        ["--", "TXT", "invalid", "a", "b"],
        ["--", "TXT", "utf8", "a"],
        ["--", "TXT", "utf8", "a", "b", "c"],
    ],
)
def test_parse_args_rejects_invalid_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        parse_args(argv)

    assert error.value.code == EXIT_USAGE


def test_worker_args_validate_their_values(tmp_path: Path) -> None:
    common = {"source": tmp_path / "a", "destination": tmp_path / "b"}
    with pytest.raises(ValueError, match="origen"):
        WorkerArgs(source_format="DOCX", encoding=TextKind.UTF8, **common)
    with pytest.raises(ValueError, match="codificación"):
        WorkerArgs(source_format="TXT", encoding=TextKind.INVALID, **common)


# --- convert_file y el worker como proceso ---


def _write_source(tmp_path: Path, data: bytes) -> tuple[Path, Path]:
    source = tmp_path / "origen"
    source.write_bytes(data)
    return source, tmp_path / "resultado.pdf"


@pytest.mark.req("6.11")
@pytest.mark.parametrize("source_format", ["TXT", "MD"])
def test_convert_file_reports_unrepresentable_characters_without_writing_a_pdf(
    tmp_path: Path, font_dir: Path, source_format: str
) -> None:
    source, destination = _write_source(tmp_path, "Año 日本 🙂".encode())
    args = WorkerArgs(
        source_format=source_format,
        encoding=TextKind.UTF8,
        source=source,
        destination=destination,
        font_dir=font_dir,
    )

    assert convert_file(args) == UnrepresentableEvent(("日", "本", "🙂"))
    assert not destination.exists()


@pytest.mark.req("6.11")
def test_convert_file_decodes_windows_1252_before_checking_the_characters(
    tmp_path: Path, font_dir: Path
) -> None:
    source, destination = _write_source(
        tmp_path, b"cuesta 5 \x80 \xbf"
    )  # euro: no está en la fuente
    args = WorkerArgs(
        source_format="TXT",
        encoding=TextKind.WINDOWS_1252,
        source=source,
        destination=destination,
        font_dir=font_dir,
    )

    assert convert_file(args) == UnrepresentableEvent(("€",))


def test_convert_file_raises_when_the_bytes_are_not_of_the_encoding(
    tmp_path: Path, font_dir: Path
) -> None:
    source, destination = _write_source(tmp_path, b"\xff\xfe")
    args = WorkerArgs(
        source_format="TXT",
        encoding=TextKind.UTF8,
        source=source,
        destination=destination,
        font_dir=font_dir,
    )

    with pytest.raises(UnicodeDecodeError):
        convert_file(args)


def _run_worker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-m", MODULE, *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.req("6.11")
@pytest.mark.parametrize("source_format", ["TXT", "MD"])
def test_the_worker_process_reports_unrepresentable_characters_on_stdout(
    tmp_path: Path, font_dir: Path, source_format: str
) -> None:
    source, destination = _write_source(tmp_path, "日本 y 🙂".encode())

    completed = _run_worker(
        f"--font-dir={font_dir}", "--", source_format, "utf8", str(source), str(destination)
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == dump_event(UnrepresentableEvent(("日", "本", "🙂"))) + "\n"
    assert not destination.exists()


def test_the_worker_exits_with_the_traceback_on_stderr_without_fonts(
    tmp_path: Path,
) -> None:
    source, destination = _write_source(tmp_path, b"hola")

    completed = _run_worker(
        f"--font-dir={tmp_path / 'sin-fuentes'}", "--", "TXT", "utf8", str(source), str(destination)
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "FileNotFoundError" in completed.stderr
    assert not destination.exists()


def test_the_worker_exits_with_the_traceback_on_stderr_when_the_source_is_missing(
    tmp_path: Path, font_dir: Path
) -> None:
    completed = _run_worker(
        f"--font-dir={font_dir}",
        "--",
        "HTML",
        "utf8",
        str(tmp_path / "no-existe"),
        str(tmp_path / "resultado.pdf"),
    )

    assert completed.returncode == 1
    assert "FileNotFoundError" in completed.stderr


def test_the_worker_rejects_invalid_arguments_with_exit_code_2() -> None:
    completed = _run_worker("solo-uno")

    assert completed.returncode == EXIT_USAGE
    assert completed.stdout == ""


def test_what_libraries_print_to_stdout_does_not_mix_with_the_event(tmp_path: Path) -> None:
    # El descriptor 1 apunta a stderr mientras se convierte; el evento sale por el original
    script = (
        "import sys, os; import converter.workers.text_pdf as w\n"
        "def fake(args):\n"
        "    print('ruido de una biblioteca')\n"
        "    os.write(1, b'ruido de una biblioteca nativa\\n')\n"
        "    return w.DoneEvent(2)\n"
        "w.convert_file = fake\n"
        "sys.exit(w.main(['--', 'HTML', 'utf8', 'a', 'b']))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == '{"event":"done","external_resources":2}\n'
    assert "ruido de una biblioteca" in completed.stderr
    assert "ruido de una biblioteca nativa" in completed.stderr


def test_importing_the_worker_does_not_load_the_html_libraries_or_weasyprint() -> None:
    # Solo el proceso del worker carga estas bibliotecas, y solo al convertir
    script = (
        "import sys; import converter.workers.text_pdf as w; w.parse_data_url('data:,x'); "
        "w.markup_stylesheet(); print(*(name in sys.modules for name in "
        "('bs4', 'markdown_it', 'html5lib', 'weasyprint')))"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["False", "False", "False", "False"]
