"""Pruebas de src/converter/workers/text_plain.py: Texto_Visible de MD y HTML, marcadores de lista,
saneamiento de Scripts y Recursos_Externos, codificación del resultado, evento final y argumentos
(Req 2.12, 6.4, 6.6, 6.7, 6.13, 6.14).

El recorrido completo a través del Ejecutor aislado y del convertidor `text.plain` está en
`test_converters_text_plain.py`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from converter.detection.text import BOM_UTF8, TextKind
from converter.workers.text_plain import (
    EXIT_USAGE,
    MODULE,
    DoneEvent,
    WorkerArgs,
    WorkerOutputError,
    convert_file,
    dump_event,
    format_args,
    load_event,
    parse_args,
    to_plain_text,
)

# --- to_plain_text: HTML ---


@pytest.mark.req("6.4")
def test_html_blocks_and_forced_breaks_start_new_lines() -> None:
    html = (
        "<body><h1>Título</h1><p>uno<br>dos</p><div>tres</div>"
        "<table><tr><td>a</td><td>b</td></tr></table><pre>  x\n y</pre></body>"
    )

    text, _ = to_plain_text(html, "HTML")

    assert text == "Título\nuno\ndos\ntres\na\nb\n  x\n y"


@pytest.mark.req("6.4")
def test_html_list_items_get_dash_and_number_markers() -> None:
    html = '<ul><li>a<ul><li>b</li></ul></li><li>c</li></ul><ol start="5"><li>x</li><li>y</li></ol>'

    text, _ = to_plain_text(html, "HTML")

    assert text == "- a\n  - b\n- c\n5. x\n6. y"


@pytest.mark.req("6.7", "6.13")
def test_html_scripts_do_not_reach_the_result() -> None:
    html = (
        "<head><script>alert('M1')</script></head><body onload=\"go('M2')\">"
        "<p>antes</p><script>alert('M3')</script>"
        "<a href=\"javascript:go('M4')\">enlace</a><p onclick=\"go('M5')\">después</p></body>"
    )

    text, external = to_plain_text(html, "HTML")

    assert text == "antes\nenlace\ndespués"
    assert external == 0


@pytest.mark.req("6.6")
def test_html_external_resources_are_counted_and_leave_no_trace() -> None:
    html = (
        '<body><p>texto</p><img src="http://example.com/a.png" alt="ALT">'
        '<iframe src="//example.com/f">FRAME</iframe>'
        '<link rel="stylesheet" href="estilo.css">'
        '<p style="background: url(http://example.com/b.png)">fin</p>'
        '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" alt="inline">'
        '<a href="http://example.com/">navegación</a></body>'
    )

    text, external = to_plain_text(html, "HTML")

    assert external == 4  # img, iframe, link y url() de style; ni data: ni enlaces
    assert text == "texto\nfin\nnavegación"


@pytest.mark.req("2.12")
def test_html_declared_charset_is_ignored() -> None:
    html = '<head><meta charset="iso-8859-5"></head><body><p>ñandú ¿qué?</p></body>'

    assert to_plain_text(html, "HTML")[0] == "ñandú ¿qué?"


def test_html_without_visible_text_gives_an_empty_result() -> None:
    assert to_plain_text("<script>x</script>", "HTML") == ("", 0)
    assert to_plain_text("", "HTML") == ("", 0)


# --- to_plain_text: MD ---


@pytest.mark.req("6.4")
def test_md_gives_the_visible_text_of_its_commonmark_html() -> None:
    md = (
        "# Título\n\nUn *párrafo* con [enlace](http://example.com/x).\n\n- uno\n- dos\n\n"
        "3. tres\n4. cuatro\n\n```\ncódigo\n  sangrado\n```\n\nlínea  \nforzada\n"
    )

    text, _ = to_plain_text(md, "MD")

    assert text == (
        "Título\nUn párrafo con enlace.\n- uno\n- dos\n3. tres\n4. cuatro\ncódigo\n  sangrado\n"
        "línea\nforzada"
    )


@pytest.mark.req("6.7", "6.13")
def test_md_scripts_are_removed_including_javascript_links() -> None:
    md = (
        'Texto\n\n<script>alert("M1")</script>\n\n[enlace](javascript:alert(2))\n\n'
        '<b onclick="x()">negrita</b>'
    )

    text, external = to_plain_text(md, "MD")

    assert text == "Texto\nenlace\nnegrita"
    assert external == 0


@pytest.mark.req("6.6")
def test_md_images_with_remote_urls_are_neutralized() -> None:
    text, external = to_plain_text("antes\n\n![alt](http://example.com/i.png)\n\ndespués", "MD")

    assert text == "antes\ndespués"
    assert external == 1


def test_md_empty_text_gives_an_empty_result() -> None:
    assert to_plain_text("", "MD") == ("", 0)


def test_to_plain_text_rejects_other_formats() -> None:
    with pytest.raises(ValueError, match="TXT"):
        to_plain_text("texto", "TXT")


# --- convert_file ---


def _convert(tmp_path: Path, data: bytes, source_format: str, encoding: TextKind) -> bytes:
    source = tmp_path / "original"
    output = tmp_path / "output.txt"
    source.write_bytes(data)
    convert_file(
        WorkerArgs(source_format=source_format, encoding=encoding, source=source, output=output)
    )
    return output.read_bytes()


@pytest.mark.req("6.4", "6.14")
def test_utf8_result_has_no_bom_even_if_the_source_has_one(tmp_path: Path) -> None:
    source = BOM_UTF8 + "<p>Año nuevo</p>".encode()

    result = _convert(tmp_path, source, "HTML", TextKind.UTF8)

    assert result == "Año nuevo".encode()
    assert not result.startswith(BOM_UTF8)


@pytest.mark.req("6.14")
@pytest.mark.parametrize("source_format", ["MD", "HTML"])
def test_windows_1252_source_is_written_as_utf8(tmp_path: Path, source_format: str) -> None:
    source = "¿Qué tal, ñandú? “Hola” – 5 €".encode("cp1252")

    result = _convert(tmp_path, source, source_format, TextKind.WINDOWS_1252)

    assert result == "¿Qué tal, ñandú? “Hola” – 5 €".encode()


@pytest.mark.req("6.6")
def test_convert_file_reports_the_external_resources(tmp_path: Path) -> None:
    source = tmp_path / "original"
    output = tmp_path / "output.txt"
    source.write_bytes(b'<img src="http://example.com/a.png"><img src="b.png"><p>x</p>')

    event = convert_file(
        WorkerArgs(source_format="HTML", encoding=TextKind.UTF8, source=source, output=output)
    )

    assert event == DoneEvent(external_resources=2)
    assert output.read_bytes() == b"x"


# --- Evento final ---


@pytest.mark.parametrize("count", [0, 1, 250])
def test_event_round_trips_as_a_single_ascii_line(count: int) -> None:
    line = dump_event(DoneEvent(count))

    assert "\n" not in line
    assert line.isascii()
    assert load_event(line) == DoneEvent(count)


def test_event_has_a_fixed_json_form() -> None:
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
        '{"event":"done","external_resources":true}',
        '{"event":"done","external_resources":1.0}',
        '{"event":"done","external_resources":"1"}',
    ],
)
def test_load_event_rejects_invalid_lines(line: str) -> None:
    with pytest.raises(WorkerOutputError):
        load_event(line)


def test_done_event_rejects_a_negative_count() -> None:
    with pytest.raises(ValueError, match="Recursos_Externos"):
        DoneEvent(-1)


# --- Argumentos ---


@pytest.mark.parametrize(
    "args",
    [
        WorkerArgs(
            source_format="MD",
            encoding=TextKind.UTF8,
            source=Path("/ws/source/original"),
            output=Path("/ws/work/output.txt"),
        ),
        WorkerArgs(
            source_format="HTML",
            encoding=TextKind.WINDOWS_1252,
            source=Path("-origen"),  # una ruta que empieza con "-" no es una opción
            output=Path("-salida.txt"),
        ),
    ],
)
def test_format_args_round_trips_through_parse_args(args: WorkerArgs) -> None:
    assert parse_args(format_args(args)) == args


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--", "MD", "utf8", "origen"],  # falta el resultado
        ["--", "TXT", "utf8", "origen", "salida"],  # Formato de origen desconocido
        ["--", "MD", "latin1", "origen", "salida"],  # codificación desconocida
        ["--", "MD", "invalid", "origen", "salida"],  # un texto inválido no se convierte
        ["--otra", "--", "MD", "utf8", "origen", "salida"],
    ],
)
def test_parse_args_exits_with_the_usage_code_when_the_arguments_are_invalid(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(argv)

    assert exit_info.value.code == EXIT_USAGE
    assert capsys.readouterr().err


def test_worker_args_reject_unsupported_values() -> None:
    with pytest.raises(ValueError, match="Formato"):
        WorkerArgs(source_format="TXT", encoding=TextKind.UTF8, source=Path("a"), output=Path("b"))
    with pytest.raises(ValueError, match="codificación"):
        WorkerArgs(
            source_format="MD", encoding=TextKind.INVALID, source=Path("a"), output=Path("b")
        )


# --- Proceso ---


@pytest.mark.req("6.4", "6.14")
def test_the_worker_process_writes_the_result_and_prints_one_event(tmp_path: Path) -> None:
    source = tmp_path / "original"
    output = tmp_path / "output.txt"
    source.write_text("# Hola\n\n- uno\n- dos\n", encoding="utf-8")
    args = WorkerArgs(source_format="MD", encoding=TextKind.UTF8, source=source, output=output)

    completed = subprocess.run(
        [sys.executable, "-I", "-m", MODULE, *format_args(args)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == '{"event":"done","external_resources":0}\n'
    assert output.read_bytes() == b"Hola\n- uno\n- dos"


def test_the_worker_process_fails_with_a_traceback_when_the_source_is_missing(
    tmp_path: Path,
) -> None:
    args = WorkerArgs(
        source_format="HTML",
        encoding=TextKind.UTF8,
        source=tmp_path / "no-existe",
        output=tmp_path / "output.txt",
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-m", MODULE, *format_args(args)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "FileNotFoundError" in completed.stderr
    assert not args.output.exists()
