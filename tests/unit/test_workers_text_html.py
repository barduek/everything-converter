"""Pruebas de src/converter/workers/text_html.py: el documento HTML de TXT y de MD, el escape de
los caracteres reservados, la decodificación UTF-8 y Windows-1252, los argumentos y el worker como
proceso (Req 2.12, 6.2, 6.3, 6.14).

El recorrido completo a través del Ejecutor aislado y del convertidor `text.html` está en
`test_converters_text_html.py`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from converter.converters.text_docs.dom import markdown_to_html, parse_html, visible_text
from converter.detection.text import TextKind
from converter.workers.text_html import (
    EXIT_USAGE,
    MODULE,
    WorkerArgs,
    convert_file,
    format_args,
    html_document,
    md_to_html,
    parse_args,
    render_document,
    txt_to_html,
)

BOM = b"\xef\xbb\xbf"


def _body(document: str) -> str:
    """Contenido textual de `<body>` tal como lo escribe el worker."""
    head, separator, rest = document.partition("<body>\n")
    assert separator and head
    body, separator, tail = rest.partition("</body>\n")
    assert separator and tail == "</html>\n"
    return body


# --- Estructura del documento ---


@pytest.mark.req("6.2", "6.3")
@pytest.mark.parametrize("source_format", ["TXT", "MD"])
def test_the_document_is_html5_in_spanish_declaring_utf8(source_format: str) -> None:
    document = render_document(source_format, "texto")

    assert document.startswith(
        '<!DOCTYPE html>\n<html lang="es">\n<head>\n<meta charset="utf-8">\n'
    )
    assert document.endswith("</body>\n</html>\n")
    doc = parse_html(document)
    assert doc.html is not None
    assert doc.html.get("lang") == "es"
    meta = doc.find("meta")
    assert meta is not None
    assert meta.get("charset") == "utf-8"
    assert doc.title is not None
    assert doc.title.string == "Documento"


@pytest.mark.req("6.2", "6.3")
def test_html_document_ends_a_body_without_a_final_line_break_on_its_own_line() -> None:
    assert _body(html_document("<p>a</p>")) == "<p>a</p>\n"
    assert _body(html_document("<p>a</p>\n")) == "<p>a</p>\n"
    assert _body(html_document("")) == ""


def test_render_document_rejects_other_formats() -> None:
    with pytest.raises(ValueError, match="HTML"):
        render_document("HTML", "<p>x</p>")


# --- TXT a HTML ---


@pytest.mark.req("6.2")
def test_txt_goes_into_a_single_pre_element() -> None:
    document = txt_to_html("uno\ndos\n")

    assert _body(document) == "<pre>\nuno\ndos\n</pre>\n"
    pre = parse_html(document).find_all("pre")
    assert len(pre) == 1


@pytest.mark.req("6.2")
def test_txt_reserved_characters_are_written_as_character_references() -> None:
    text = "a < b > c & d \"e\" 'f' <script>alert(1)</script> &amp; &lt;"

    body = _body(txt_to_html(text))

    assert body == (
        "<pre>\na &lt; b &gt; c &amp; d &quot;e&quot; &#x27;f&#x27; "
        "&lt;script&gt;alert(1)&lt;/script&gt; &amp;amp; &amp;lt;</pre>\n"
    )
    for reserved in "<>":
        assert reserved not in body.removeprefix("<pre>").removesuffix("</pre>\n")


@pytest.mark.req("6.2")
def test_txt_text_is_what_the_browser_reads_back() -> None:
    text = 'a < b & c "d"\n\ttabulado\n  sangría  final  \n\nlínea tras un blanco\n'

    doc = parse_html(txt_to_html(text))

    pre = doc.find("pre")
    assert pre is not None
    assert pre.get_text() == text
    assert visible_text(doc) == text
    # Sin scripts ni etiquetas que el texto haya podido abrir
    assert doc.find("script") is None


@pytest.mark.req("6.2")
@pytest.mark.parametrize("text", ["\nuna línea tras un blanco", "\n\n\nvarios blancos", "\n"])
def test_txt_that_starts_with_blank_lines_keeps_them(text: str) -> None:
    # El analizador de HTML descarta un salto de línea justo tras <pre>: el worker agrega uno
    doc = parse_html(txt_to_html(text))

    pre = doc.find("pre")
    assert pre is not None
    assert pre.get_text() == text


@pytest.mark.req("6.2")
def test_txt_keeps_the_word_sequence_and_each_line_in_its_own_line() -> None:
    text = "Primera línea con palabras\nsegunda & tercera <b>\n\ncuarta"

    visible = visible_text(parse_html(txt_to_html(text)))

    assert visible.split("\n") == text.split("\n")
    assert visible.split() == text.split()


@pytest.mark.req("6.2")
def test_empty_txt_gives_an_empty_pre() -> None:
    document = txt_to_html("")

    assert _body(document) == "<pre>\n</pre>\n"
    assert visible_text(parse_html(document)) == ""


@pytest.mark.req("6.2")
def test_txt_line_breaks_are_normalized_by_html_parsing_not_by_the_worker() -> None:
    # CRLF y CR se conservan en el archivo; el analizador los lee como un salto de línea
    document = txt_to_html("uno\r\ndos\rtres")

    assert "uno\r\ndos\rtres" in document
    assert visible_text(parse_html(document)) == "uno\ndos\ntres"


# --- MD a HTML ---


@pytest.mark.req("6.3")
def test_md_body_is_the_commonmark_output() -> None:
    text = (
        "# Título\n\nUn *párrafo* con **fuerza** y [un enlace](https://example.com).\n\n"
        "* uno\n* dos\n\n1. primero\n2. segundo\n\n```\ncódigo & <etiquetas>\n```\n"
    )

    document = md_to_html(text)

    assert _body(document) == markdown_to_html(text)
    assert _body(document) == (
        "<h1>Título</h1>\n"
        '<p>Un <em>párrafo</em> con <strong>fuerza</strong> y <a href="https://example.com">'
        "un enlace</a>.</p>\n"
        "<ul>\n<li>uno</li>\n<li>dos</li>\n</ul>\n"
        "<ol>\n<li>primero</li>\n<li>segundo</li>\n</ol>\n"
        "<pre><code>código &amp; &lt;etiquetas&gt;\n</code></pre>\n"
    )


@pytest.mark.req("6.3")
def test_md_embedded_html_is_kept_as_commonmark_defines() -> None:
    text = 'Antes\n\n<div class="nota">\n*sin énfasis*\n</div>\n\nDespués <b>negrita</b>\n'

    body = _body(md_to_html(text))

    assert body == markdown_to_html(text)
    assert '<div class="nota">' in body
    assert "<b>negrita</b>" in body


@pytest.mark.req("6.3")
def test_md_html_with_scripts_is_not_sanitized() -> None:
    # Los Scripts se omiten cuando el destino es PDF, TXT o MD; en HTML se conservan (design.md)
    text = "<script>var x = 1;</script>\n\nTexto\n"

    assert "<script>var x = 1;</script>" in _body(md_to_html(text))


@pytest.mark.req("6.3")
def test_empty_md_gives_an_empty_body() -> None:
    assert _body(md_to_html("")) == ""


@pytest.mark.req("6.3")
def test_md_ignores_the_charset_declared_by_its_embedded_html() -> None:
    # El Texto_Decodificado ya es texto: el documento sigue declarando UTF-8 (Req 2.12)
    text = '<meta charset="iso-8859-5">\n\nñandú ¿qué?\n'

    document = md_to_html(text)

    assert document.startswith(
        '<!DOCTYPE html>\n<html lang="es">\n<head>\n<meta charset="utf-8">\n'
    )
    assert "ñandú ¿qué?" in document


# --- Argumentos ---


@pytest.mark.parametrize(
    "args",
    [
        WorkerArgs(
            source_format="TXT",
            encoding=TextKind.UTF8,
            source=Path("/ws/source/original"),
            destination=Path("/ws/work/output.html"),
        ),
        WorkerArgs(
            source_format="MD",
            encoding=TextKind.WINDOWS_1252,
            source=Path("-raro"),
            destination=Path("--otro"),
        ),
    ],
)
def test_format_args_round_trips(args: WorkerArgs) -> None:
    formatted = format_args(args)

    # Todo va después de "--": una ruta que empieza con "-" no es una opción
    assert formatted[0] == "--"
    assert parse_args(formatted) == args


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["TXT", "utf8", "origen"],
        ["TXT", "utf8", "a", "b", "c"],
        ["HTML", "utf8", "a", "b"],
        ["txt", "utf8", "a", "b"],
        ["TXT", "latin1", "a", "b"],
        ["TXT", "invalid", "a", "b"],
        ["--otra", "TXT", "utf8", "a", "b"],
    ],
)
def test_parse_args_rejects_invalid_arguments(args: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        parse_args(args)
    assert error.value.code == EXIT_USAGE


def test_worker_args_reject_unsupported_values() -> None:
    with pytest.raises(ValueError, match="formato"):
        WorkerArgs(
            source_format="HTML",
            encoding=TextKind.UTF8,
            source=Path("a"),
            destination=Path("b"),
        )
    with pytest.raises(ValueError, match="codificación"):
        WorkerArgs(
            source_format="TXT",
            encoding=TextKind.INVALID,
            source=Path("a"),
            destination=Path("b"),
        )


# --- Conversión de archivos ---


def _convert(tmp_path: Path, data: bytes, source_format: str, encoding: TextKind) -> bytes:
    source = tmp_path / "origen"
    destination = tmp_path / "resultado.html"
    source.write_bytes(data)
    convert_file(
        WorkerArgs(
            source_format=source_format,
            encoding=encoding,
            source=source,
            destination=destination,
        )
    )
    return destination.read_bytes()


@pytest.mark.req("6.14", "2.12")
@pytest.mark.parametrize("source_format", ["TXT", "MD"])
def test_a_windows_1252_source_is_written_as_utf8(tmp_path: Path, source_format: str) -> None:
    text = "Camión ñandú ¿qué? ¡Sí! €5 «comillas» “inglesas”"

    result = _convert(tmp_path, text.encode("cp1252"), source_format, TextKind.WINDOWS_1252)

    decoded = result.decode("utf-8")  # UTF-8 válido
    assert text in decoded
    assert not result.startswith(BOM)
    assert b"\xf1" not in result  # ni un solo byte de Windows-1252 sin traducir


@pytest.mark.req("6.14", "2.12")
def test_windows_1252_bytes_without_a_character_become_the_unicode_character_of_equal_value(
    tmp_path: Path,
) -> None:
    result = _convert(tmp_path, b"a\x81b\x8d\x8f\x90\x9dc", "TXT", TextKind.WINDOWS_1252)

    assert "a\u0081b\u008d\u008f\u0090\u009dc" in result.decode("utf-8")


@pytest.mark.req("6.14", "2.12")
def test_a_windows_1252_source_that_starts_with_utf8_bom_bytes_keeps_them_as_text(
    tmp_path: Path,
) -> None:
    result = _convert(tmp_path, BOM + b"hola", "TXT", TextKind.WINDOWS_1252)

    assert "ï»¿hola" in result.decode("utf-8")


@pytest.mark.req("6.14", "2.12")
@pytest.mark.parametrize("source_format", ["TXT", "MD"])
def test_the_initial_bom_of_a_utf8_source_is_not_part_of_the_text(
    tmp_path: Path, source_format: str
) -> None:
    result = _convert(tmp_path, BOM + "ñandú".encode(), source_format, TextKind.UTF8)

    assert not result.startswith(BOM)
    assert "\ufeff" not in result.decode("utf-8")
    assert "ñandú" in result.decode("utf-8")


@pytest.mark.req("6.14")
def test_the_result_is_utf8_without_bom_and_keeps_every_unicode_character(
    tmp_path: Path,
) -> None:
    text = "日本語 😀 ñ \u2060 \u00a0 ﷽"

    result = _convert(tmp_path, text.encode("utf-8"), "TXT", TextKind.UTF8)

    assert not result.startswith(BOM)
    assert text in result.decode("utf-8")


@pytest.mark.req("6.2")
def test_a_utf8_source_declared_as_windows_1252_is_not_reinterpreted_by_its_own_content(
    tmp_path: Path,
) -> None:
    # La codificación que cuenta es la que clasificó el Detector_de_Tipo, no la que diga el texto
    data = '<meta charset="utf-8">\nñ'.encode("cp1252")

    result = _convert(tmp_path, data, "TXT", TextKind.WINDOWS_1252)

    assert "&lt;meta charset=&quot;utf-8&quot;&gt;\nñ" in result.decode("utf-8")


def test_invalid_utf8_declared_as_utf8_raises_instead_of_replacing_characters(
    tmp_path: Path,
) -> None:
    with pytest.raises(UnicodeDecodeError):
        _convert(tmp_path, b"\xff\xfe", "TXT", TextKind.UTF8)


# --- El worker como proceso ---


def _run_worker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-m", MODULE, *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.req("6.2", "6.3", "6.14")
@pytest.mark.parametrize("source_format", ["TXT", "MD"])
def test_the_worker_process_writes_the_document(tmp_path: Path, source_format: str) -> None:
    source = tmp_path / "origen"
    destination = tmp_path / "resultado.html"
    text = "Título & más\n\nñandú\n"
    source.write_bytes(text.encode("utf-8"))

    completed = _run_worker("--", source_format, "utf8", str(source), str(destination))

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert destination.read_bytes() == render_document(source_format, text).encode("utf-8")


def test_the_worker_exits_with_the_traceback_on_stderr_when_the_source_is_missing(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "resultado.html"

    completed = _run_worker("--", "TXT", "utf8", str(tmp_path / "no-existe"), str(destination))

    assert completed.returncode == 1
    assert "FileNotFoundError" in completed.stderr
    assert not destination.exists()


def test_the_worker_rejects_invalid_arguments_with_exit_code_2() -> None:
    completed = _run_worker("solo-uno")

    assert completed.returncode == EXIT_USAGE
    assert completed.stdout == ""


def test_importing_the_worker_does_not_load_the_markdown_and_html_libraries() -> None:
    # Solo el worker, y solo con un MD, carga markdown-it-py y BeautifulSoup
    script = (
        "import sys; import converter.workers.text_html as w; w.txt_to_html('x'); "
        "print('markdown_it' in sys.modules, 'bs4' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["False", "False"]
