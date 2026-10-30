"""Pruebas de src/converter/workers/pdf_text.py: limpieza del texto, eventos, argumentos y
extracción de PDF reales con pypdfium2 (Req 8.1, 8.2, 8.3, 8.10).

El recorrido completo a través del Ejecutor aislado y del convertidor `pdf.text` está en
`test_converters_pdf_text.py`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from converter.workers.pdf_text import (
    EXIT_USAGE,
    MODULE,
    DoneEvent,
    PageEvent,
    WorkerArgs,
    WorkerOutputError,
    clean_page_text,
    dump_event,
    extract_text,
    format_args,
    is_blank_page,
    load_event,
    parse_args,
)
from tests.pdf_text_support import build_pdf

# --- Limpieza del texto de una página ---


@pytest.mark.req("8.1")
def test_clean_page_text_normalizes_line_breaks_to_lf() -> None:
    assert clean_page_text("uno\r\ndos\r\n\r\ntres") == "uno\ndos\n\ntres"
    # Un CR suelto tampoco queda en el resultado
    assert clean_page_text("uno\rdos") == "uno\ndos"
    assert clean_page_text("\r\r\n") == "\n\n"


@pytest.mark.req("8.1")
def test_clean_page_text_removes_word_joiner_and_bom_only() -> None:
    assert clean_page_text("pala\u2060bra\ufeff, otra\u2060") == "palabra, otra"
    # Lo demás se conserva: espacios, tabulaciones, guiones, guion suave, ligaduras y U+FFFE
    kept = "a \t b-\nc\u00ad d\ufb01 e\ufffe f\u00a0g"
    assert clean_page_text(kept) == kept


@pytest.mark.req("8.2", "8.3")
@pytest.mark.parametrize("text", ["", " ", "\t", "\n", "\r\n", " \t\n \n\t "])
def test_a_page_with_only_spaces_tabs_and_line_breaks_is_blank(text: str) -> None:
    assert is_blank_page(text)


@pytest.mark.req("8.2", "8.3")
@pytest.mark.parametrize("text", ["a", " . ", "\n0\n", "\u00a0", "\ufffe", "\x0c"])
def test_a_page_with_any_other_character_is_not_blank(text: str) -> None:
    assert not is_blank_page(text)


# --- Eventos de la salida estándar ---


@pytest.mark.parametrize(
    "event",
    [
        PageEvent(page=1, total=1, has_text=True),
        PageEvent(page=3, total=12, has_text=False),
        DoneEvent(pages=0, pages_with_text=0),
        DoneEvent(pages=12, pages_with_text=7),
    ],
)
def test_events_round_trip_as_single_ascii_lines(event: PageEvent | DoneEvent) -> None:
    line = dump_event(event)

    assert "\n" not in line
    assert line.isascii()
    assert load_event(line) == event


def test_events_have_a_fixed_json_form() -> None:
    assert dump_event(PageEvent(2, 5, True)) == '{"event":"page","page":2,"total":5,"text":true}'
    assert dump_event(DoneEvent(5, 4)) == '{"event":"done","pages":5,"with_text":4}'


@pytest.mark.parametrize(
    "line",
    [
        "",
        "no es JSON",
        "[]",
        "3",
        '{"event":"otro"}',
        '{"event":"page","page":1,"total":1}',  # falta text
        '{"event":"page","page":1,"total":1,"text":true,"extra":1}',
        '{"event":"page","page":0,"total":1,"text":true}',
        '{"event":"page","page":2,"total":1,"text":true}',
        '{"event":"page","page":true,"total":1,"text":true}',
        '{"event":"page","page":1.0,"total":1,"text":true}',
        '{"event":"page","page":1,"total":1,"text":1}',
        '{"event":"done","pages":-1,"with_text":0}',
        '{"event":"done","pages":2,"with_text":3}',
        '{"event":"done","pages":2}',
    ],
)
def test_load_event_rejects_invalid_lines(line: str) -> None:
    with pytest.raises(WorkerOutputError):
        load_event(line)


# --- Argumentos ---


@pytest.mark.parametrize(
    "args",
    [
        WorkerArgs(source=Path("/ws/source/original"), output=Path("/ws/work/output.txt")),
        WorkerArgs(source=Path("-raro"), output=Path("--otro")),
    ],
)
def test_format_args_round_trips(args: WorkerArgs) -> None:
    formatted = format_args(args)

    # Los posicionales van después de "--": una ruta que empieza con "-" no es una opción
    assert formatted[0] == "--"
    assert parse_args(formatted) == args


@pytest.mark.parametrize("args", [[], ["origen.pdf"], ["a", "b", "c"], ["--otra", "a", "b"]])
def test_parse_args_rejects_invalid_arguments(args: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        parse_args(args)
    assert error.value.code == EXIT_USAGE


# --- Extracción de PDF reales ---


def _extract(tmp_path: Path, data: bytes) -> tuple[str, DoneEvent, list[PageEvent]]:
    """Texto escrito, resumen y eventos de página de `extract_text` sobre el PDF `data`."""
    source = tmp_path / "origen.pdf"
    output = tmp_path / "salida.txt"
    source.write_bytes(data)
    pages: list[PageEvent] = []
    done = extract_text(source, output, pages.append)
    return output.read_bytes().decode("utf-8"), done, pages


@pytest.mark.req("8.1")
def test_extract_text_writes_every_page_in_order_separated_by_a_blank_line(
    tmp_path: Path,
) -> None:
    pdf = build_pdf([["Primera página", "con dos líneas"], ["Segunda"], ["Tercera", "y última"]])

    text, done, pages = _extract(tmp_path, pdf)

    assert text == "Primera página\ncon dos líneas\n\nSegunda\n\nTercera\ny última"
    assert done == DoneEvent(pages=3, pages_with_text=3)
    assert pages == [PageEvent(1, 3, True), PageEvent(2, 3, True), PageEvent(3, 3, True)]


@pytest.mark.req("8.1")
def test_extract_text_is_utf8_with_lf_only_and_keeps_accents_and_punctuation(
    tmp_path: Path,
) -> None:
    pdf = build_pdf([["Camión, niño y pingüino (a\\b)", "¿Qué? ¡Sí! €5"]])

    text, _, _ = _extract(tmp_path, pdf)

    assert text == "Camión, niño y pingüino (a\\b)\n¿Qué? ¡Sí! €5"
    assert "\r" not in text


@pytest.mark.req("8.1")
def test_extract_text_keeps_the_end_of_line_hyphen_and_never_joins_lines(
    tmp_path: Path,
) -> None:
    # PDFium marca el guion de "inter-" y omite el salto de línea; el worker los conserva
    pdf = build_pdf([["Una palabra inter-", "nacional y otra", "línea con - guion suelto"]])

    text, _, _ = _extract(tmp_path, pdf)

    assert text == "Una palabra inter-\nnacional y otra\nlínea con - guion suelto"


@pytest.mark.req("8.1")
def test_extract_text_keeps_several_hyphens_on_the_same_page(tmp_path: Path) -> None:
    pdf = build_pdf([["uno-", "dos-", "tres", "cuatro-", "cinco"]])

    text, _, _ = _extract(tmp_path, pdf)

    assert text == "uno-\ndos-\ntres\ncuatro-\ncinco"


@pytest.mark.req("8.3")
def test_pages_without_text_add_nothing_and_are_reported(tmp_path: Path) -> None:
    # Una página vacía y otras con solo espacios o tabulaciones no tienen texto
    pdf = build_pdf([[], ["Uno"], ["   "], ["\t "], ["Dos"], []])

    text, done, pages = _extract(tmp_path, pdf)

    assert text == "Uno\n\nDos"
    assert done == DoneEvent(pages=6, pages_with_text=2)
    assert [(event.page, event.has_text) for event in pages] == [
        (1, False),
        (2, True),
        (3, False),
        (4, False),
        (5, True),
        (6, False),
    ]
    assert {event.total for event in pages} == {6}


@pytest.mark.req("8.2")
def test_a_pdf_with_no_text_in_any_page_leaves_an_empty_output(tmp_path: Path) -> None:
    text, done, pages = _extract(tmp_path, build_pdf([[], ["  "], []]))

    assert text == ""
    assert done == DoneEvent(pages=3, pages_with_text=0)
    assert [event.has_text for event in pages] == [False, False, False]


@pytest.mark.req("8.10")
def test_a_pdf_with_restricted_permissions_gives_the_same_text_without_a_password(
    tmp_path: Path,
) -> None:
    pages = [["Texto", "protegido-", "contra copia"], ["Segunda página"]]

    plain, _, _ = _extract(tmp_path, build_pdf(pages))
    restricted, done, _ = _extract(tmp_path, build_pdf(pages, owner_password=b"propietario"))

    assert restricted == plain == "Texto\nprotegido-\ncontra copia\n\nSegunda página"
    assert done == DoneEvent(pages=2, pages_with_text=2)


# --- El worker como proceso ---


def _run_worker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-m", MODULE, *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.req("8.1", "8.3")
def test_the_worker_process_writes_the_text_and_one_event_line_per_page(tmp_path: Path) -> None:
    source = tmp_path / "origen.pdf"
    output = tmp_path / "salida.txt"
    source.write_bytes(build_pdf([["Hola"], [], ["Mundo"]]))

    completed = _run_worker("--", str(source), str(output))

    assert completed.returncode == 0, completed.stderr
    assert output.read_text(encoding="utf-8") == "Hola\n\nMundo"
    assert [load_event(line) for line in completed.stdout.splitlines()] == [
        PageEvent(1, 3, True),
        PageEvent(2, 3, False),
        PageEvent(3, 3, True),
        DoneEvent(pages=3, pages_with_text=2),
    ]


def test_the_worker_exits_with_the_pdfium_error_on_stderr_and_no_events(tmp_path: Path) -> None:
    source = tmp_path / "origen.pdf"
    output = tmp_path / "salida.txt"
    source.write_bytes(b"%PDF-1.4\nno es un PDF")

    completed = _run_worker("--", str(source), str(output))

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "Failed to load document" in completed.stderr
    assert not output.exists()


def test_the_worker_rejects_invalid_arguments_with_exit_code_2() -> None:
    completed = _run_worker("solo-uno")

    assert completed.returncode == EXIT_USAGE
    assert completed.stdout == ""
