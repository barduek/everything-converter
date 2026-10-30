"""Pruebas de src/converter/workers/text_markdown.py: HTML a MD con `markdownify` sobre el DOM
saneado, decodificación, conteo de Recursos_Externos, argumentos y evento de la salida estándar
(Req 2.12, 6.5, 6.6, 6.7, 6.13, 6.14).

Solo se prueba lo que el MVP promete: títulos ATX, viñetas `-`, Scripts y Recursos_Externos fuera
del resultado y UTF-8. La conservación de la Estructura_de_Documento (Req 6.5) y la ida y vuelta
(Req 6.10) quedan para las pruebas de propiedad de las tareas 17.12 y 17.13. El recorrido a través
del Ejecutor aislado y del convertidor `text.markdown` está en `test_converters_text_markdown.py`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from converter.converters.text_docs.dom import parse_html, sanitize_dom
from converter.detection.text import TextKind
from converter.workers.text_markdown import (
    EXIT_USAGE,
    MODULE,
    DoneEvent,
    WorkerArgs,
    WorkerOutputError,
    convert_file,
    dump_event,
    format_args,
    html_to_markdown,
    load_event,
    main,
    parse_args,
)


def _markdown(html: str) -> str:
    doc = parse_html(html)
    sanitize_dom(doc)
    return html_to_markdown(doc)


def _convert(tmp_path: Path, data: bytes, encoding: TextKind = TextKind.UTF8) -> tuple[bytes, int]:
    """Bytes del MD que escribe el worker para `data` y Recursos_Externos que informa."""
    source = tmp_path / "origen.html"
    output = tmp_path / "resultado.md"
    source.write_bytes(data)
    event = convert_file(source, output, encoding)
    return output.read_bytes(), event.external_resources


# --- Formato del Markdown ---


@pytest.mark.req("6.5")
def test_headings_use_atx_style() -> None:
    assert _markdown("<h1>Uno</h1><h2>Dos</h2><h3>Tres</h3><h6>Seis</h6>") == (
        "# Uno\n\n## Dos\n\n### Tres\n\n###### Seis"
    )


@pytest.mark.req("6.5")
def test_unordered_lists_use_a_dash_at_every_level() -> None:
    html = "<ul><li>a<ul><li>b<ul><li>c</li></ul></li></ul></li><li>d</li></ul>"

    assert _markdown(html) == "- a\n  - b\n    - c\n- d"


@pytest.mark.req("6.5")
def test_ordered_lists_are_numbered() -> None:
    assert _markdown("<ol><li>x</li><li>y</li></ol>") == "1. x\n2. y"


@pytest.mark.req("6.5")
def test_paragraphs_emphasis_and_code_blocks() -> None:
    html = (
        "<p>una <em>cursiva</em> y <strong>negrita</strong></p>"
        "<pre><code>x = 1\ny = 2</code></pre><p>fin</p>"
    )

    assert _markdown(html) == ("una *cursiva* y **negrita**\n\n```\nx = 1\ny = 2\n```\n\nfin")


@pytest.mark.req("6.5")
def test_a_link_whose_text_is_its_url_is_still_written_as_a_link() -> None:
    # La forma <destino> solo es un enlace de CommonMark si el destino tiene esquema
    assert _markdown('<a href="www.ejemplo.com">www.ejemplo.com</a>') == (
        "[www.ejemplo.com](www.ejemplo.com)"
    )
    assert _markdown('<a href="http://e.com/a">texto</a>') == "[texto](http://e.com/a)"


@pytest.mark.req("6.5")
def test_only_the_body_is_converted() -> None:
    html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Título</title>"
        "<style>p { color: red }</style></head><body><p>Cuerpo</p></body></html>"
    )

    assert _markdown(html) == "Cuerpo"


@pytest.mark.req("6.5")
def test_elements_that_the_visible_text_hides_are_left_out() -> None:
    html = (
        "<p>antes</p><template><p>plantilla</p></template>"
        "<iframe>marco</iframe><noembed>incrustado</noembed><p>después</p>"
    )

    assert _markdown(html) == "antes\n\ndespués"


@pytest.mark.req("6.5")
@pytest.mark.parametrize("html", ["", "<body></body>", "<p> </p>", "<frameset><frame></frameset>"])
def test_a_document_without_content_gives_an_empty_markdown(html: str) -> None:
    assert _markdown(html) == ""


# --- Scripts ---


@pytest.mark.req("6.7", "6.13")
def test_scripts_and_event_handlers_are_not_in_the_result() -> None:
    html = (
        '<p onclick="malo()">a</p><script>var malo = 1;</script><p>b</p>'
        "<svg><script>malo2()</script></svg>"
    )

    markdown = _markdown(html)

    assert markdown == "a\n\nb"
    assert "malo" not in markdown


@pytest.mark.req("6.5", "6.7", "6.13")
def test_a_link_to_a_script_is_written_as_text_and_keeps_its_spaces() -> None:
    markdown = _markdown('<p>uno<a href=" JavaScript:alert(1)"> dos </a>tres</p>')

    assert markdown.split() == ["uno", "dos", "tres"]
    assert "(" not in markdown
    assert "javascript" not in markdown.lower()


@pytest.mark.req("6.5")
@pytest.mark.parametrize("anchor", ['<a name="ancla">', '<a href="">', "<a>"])
def test_a_link_without_destination_is_text_and_keeps_its_spaces(anchor: str) -> None:
    assert _markdown(f"<p>uno{anchor} dos </a>tres</p>").split() == ["uno", "dos", "tres"]


# --- Recursos_Externos ---


@pytest.mark.req("6.6")
def test_external_resources_are_not_in_the_result(tmp_path: Path) -> None:
    html = (
        b'<p>texto</p><img src="http://x.example/a.png" alt="imagen externa">'
        b'<iframe src="http://x.example/f"></iframe>'
    )

    data, external = _convert(tmp_path, html)

    assert data == b"texto\n"
    assert external == 2


@pytest.mark.req("6.6")
def test_external_resources_are_counted_and_navigation_links_are_not(tmp_path: Path) -> None:
    html = (
        '<link rel="stylesheet" href="estilo.css"><img src="//b.example/1.png">'
        '<p style="background: url(http://d.example/e.png)">x</p>'
        '<video poster="http://v.example/p.png"></video>'
        '<a href="http://n.example/">enlace</a>'
        '<img src="data:image/png;base64,AAAA" alt="en línea">'
    )

    data, external = _convert(tmp_path, html.encode())

    assert external == 4
    markdown = data.decode("utf-8")
    assert "[enlace](http://n.example/)" in markdown
    assert "![en línea](data:image/png;base64,AAAA)" in markdown
    assert "example/1.png" not in markdown
    assert "d.example" not in markdown


@pytest.mark.req("6.6")
def test_a_document_without_external_resources_reports_none(tmp_path: Path) -> None:
    assert _convert(tmp_path, b"<p>hola</p>") == (b"hola\n", 0)


# --- Codificación (UTF-8, sin BOM) ---


@pytest.mark.req("6.14")
def test_the_result_is_utf8_without_bom_even_if_the_source_has_one(tmp_path: Path) -> None:
    data, _ = _convert(tmp_path, b"\xef\xbb\xbf<h1>\xc3\x91and\xc3\xba</h1>")

    assert data == "# Ñandú\n".encode()
    assert not data.startswith(b"\xef\xbb\xbf")


@pytest.mark.req("2.12", "6.14")
def test_a_windows_1252_source_is_written_as_utf8(tmp_path: Path) -> None:
    source = "<p>Año nuevo: ¿qué tal? ¡Adiós! ‘comillas’ €</p>".encode("cp1252")

    data, _ = _convert(tmp_path, source, TextKind.WINDOWS_1252)

    assert data == "Año nuevo: ¿qué tal? ¡Adiós! ‘comillas’ €\n".encode()


@pytest.mark.req("2.12")
def test_the_charset_declared_in_the_document_does_not_change_the_result(
    tmp_path: Path,
) -> None:
    declared = (
        '<meta charset="shift_jis"><meta http-equiv="Content-Type" '
        'content="text/html; charset=iso-8859-5"><p>Ñandú</p>'
    ).encode()

    data, _ = _convert(tmp_path, declared)

    assert data == "Ñandú\n".encode()


@pytest.mark.req("6.14")
def test_a_document_without_content_writes_an_empty_file(tmp_path: Path) -> None:
    assert _convert(tmp_path, b"<html><head><title>x</title></head><body></body></html>") == (
        b"",
        0,
    )


# --- Evento de la salida estándar ---


@pytest.mark.parametrize("count", [0, 1, 250])
def test_the_event_round_trips_as_a_single_ascii_line(count: int) -> None:
    line = dump_event(DoneEvent(count))

    assert "\n" not in line
    assert line.isascii()
    assert load_event(line) == DoneEvent(count)


def test_the_event_has_a_fixed_json_form() -> None:
    assert dump_event(DoneEvent(3)) == '{"event":"done","external_resources":3}'


@pytest.mark.parametrize(
    "line",
    [
        "",
        "no es JSON",
        "[]",
        "3",
        '{"event":"otro","external_resources":0}',
        '{"event":"done"}',
        '{"event":"done","external_resources":0,"extra":1}',
        '{"event":"done","external_resources":-1}',
        '{"event":"done","external_resources":1.0}',
        '{"event":"done","external_resources":true}',
        '{"event":"done","external_resources":"2"}',
    ],
)
def test_load_event_rejects_invalid_lines(line: str) -> None:
    with pytest.raises(WorkerOutputError):
        load_event(line)


def test_a_negative_count_is_not_an_event() -> None:
    with pytest.raises(ValueError, match="negativa"):
        DoneEvent(-1)


# --- Argumentos ---


@pytest.mark.parametrize("encoding", [TextKind.UTF8, TextKind.WINDOWS_1252])
@pytest.mark.parametrize("source", [Path("/ws/source/original"), Path("-guion.html")])
def test_format_args_round_trips_through_parse_args(encoding: TextKind, source: Path) -> None:
    args = WorkerArgs(encoding=encoding, source=source, output=Path("/ws/work/output.md"))

    assert parse_args(format_args(args)) == args


def test_worker_args_reject_an_encoding_without_decoded_text() -> None:
    with pytest.raises(ValueError, match="Texto_Decodificado"):
        WorkerArgs(encoding=TextKind.INVALID, source=Path("a"), output=Path("b"))


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["a.html", "b.md"],  # falta --encoding
        ["--encoding", "utf8", "a.html"],  # falta el resultado
        ["--encoding", "invalid", "a.html", "b.md"],  # sin Texto_Decodificado
        ["--encoding", "latin1", "a.html", "b.md"],
        ["--encoding", "utf8", "a.html", "b.md", "otro"],
    ],
)
def test_parse_args_rejects_invalid_arguments_with_exit_code_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        parse_args(argv)

    assert error.value.code == EXIT_USAGE


def test_main_writes_the_result_and_the_event(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "origen.html"
    output = tmp_path / "resultado.md"
    source.write_text('<h1>Hola</h1><img src="http://x.example/a.png">', encoding="utf-8")

    code = main(["--encoding", "utf8", "--", str(source), str(output)])

    assert code == 0
    assert output.read_text(encoding="utf-8") == "# Hola\n"
    assert load_event(capsys.readouterr().out.strip()) == DoneEvent(1)


def test_running_the_module_with_bad_arguments_exits_with_2() -> None:
    completed = subprocess.run(
        [sys.executable, "-I", "-m", MODULE, "--encoding", "nada", "a", "b"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == EXIT_USAGE
    assert completed.stdout == ""
