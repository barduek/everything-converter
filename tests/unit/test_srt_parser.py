"""Pruebas de src/converter/srt/parser.py: lectura de Bloques_SRT, número de la primera línea
inválida y ejemplos de ida y vuelta con el Serializador_SRT (Req 12.6, 12.7).
"""

from __future__ import annotations

import pytest

from converter.srt.model import Segment
from converter.srt.parser import SrtParseError, parse_srt
from converter.srt.serializer import serialize_srt

_BOM_UTF8 = b"\xef\xbb\xbf"
_TIMING = "00:00:00,000 --> 00:00:01,000"

_VALID = (
    "1\n00:00:00,000 --> 00:00:01,500\nHola\n\n"
    "2\n00:00:01,500 --> 01:02:03,004\n¿Qué tal?\n  Bien  \n\n"
    "3\n01:02:03,004 --> 99:59:59,999\nAdiós\n\n"
)
_VALID_SEGMENTS = [
    Segment(0, 1_500, "Hola"),
    Segment(1_500, 3_723_004, "¿Qué tal?\n  Bien  "),
    Segment(3_723_004, 359_999_999, "Adiós"),
]


@pytest.mark.req("12.6")
def test_parses_consecutive_blocks() -> None:
    assert parse_srt(_VALID.encode()) == _VALID_SEGMENTS


@pytest.mark.req("12.6")
@pytest.mark.parametrize("data", [b"", _BOM_UTF8])
def test_content_without_blocks_has_no_segments(data: bytes) -> None:
    assert parse_srt(data) == []


@pytest.mark.req("12.6")
def test_accepts_initial_bom_and_crlf_line_endings() -> None:
    data = _BOM_UTF8 + _VALID.replace("\n", "\r\n").encode()
    assert parse_srt(data) == _VALID_SEGMENTS


@pytest.mark.req("12.6")
def test_text_lines_that_look_like_an_index_or_timing_line_are_text() -> None:
    data = f"1\n{_TIMING}\n2\n{_TIMING}\n\n".encode()
    assert parse_srt(data) == [Segment(0, 1_000, f"2\n{_TIMING}")]


@pytest.mark.req("12.6")
def test_start_after_end_is_accepted_because_only_the_format_is_checked() -> None:
    data = b"1\n00:00:05,000 --> 00:00:01,000\nx\n\n"
    assert parse_srt(data) == [Segment(5_000, 1_000, "x")]


def _block(index: str, timing: str = _TIMING, text: str = "A") -> str:
    return f"{index}\n{timing}\n{text}\n\n"


@pytest.mark.req("12.6")
@pytest.mark.parametrize(
    ("data", "line_number"),
    [
        # Índice
        (b"x\n", 1),
        (b"\n", 1),
        (_block("2").encode(), 1),
        (_block("01").encode(), 1),
        (_block("+1").encode(), 1),
        (_block(" 1").encode(), 1),
        (_block("1 ").encode(), 1),
        (_block("\u0661").encode(), 1),  # dígito árabe-índico "1"
        (_BOM_UTF8 + _BOM_UTF8 + _block("1").encode(), 1),  # solo se acepta una BOM
        (b"1\r\r\n" + f"{_TIMING}\nA\n\n".encode(), 1),  # se quita un solo "\r"
        ((_block("1") + _block("3")).encode(), 5),
        ((_block("1") + _block("1")).encode(), 5),
        ((_block("1") + "\n").encode(), 5),
        # Línea_de_Tiempos_SRT
        (_block("1", "00:00:00.000 --> 00:00:01,000").encode(), 2),
        (_block("1", "00:60:00,000 --> 00:00:01,000").encode(), 2),
        (_block("1", "00:00:60,000 --> 00:00:01,000").encode(), 2),
        (_block("1", "0:00:00,000 --> 00:00:01,000").encode(), 2),
        (_block("1", "100:00:00,000 --> 00:00:01,000").encode(), 2),
        (_block("1", "00:00:00,00 --> 00:00:01,000").encode(), 2),
        (_block("1", "00:00:00,0000 --> 00:00:01,000").encode(), 2),
        (_block("1", "00:00:00,000 -> 00:00:01,000").encode(), 2),
        (_block("1", "00:00:00,000  --> 00:00:01,000").encode(), 2),
        (_block("1", "00:00:00,000 --> 00:00:01,000 ").encode(), 2),
        (_block("1", "00:00:00,000 --> 00:00:0\u0661,000").encode(), 2),
        (_block("1", "").encode(), 2),
        # Texto
        (f"1\n{_TIMING}\n\n".encode(), 3),  # la línea vacía llega antes que el texto
        (f"1\n{_TIMING}\n \t\n\n".encode(), 3),
        (f"1\n{_TIMING}\nA\n  \n\n".encode(), 4),
        (f"1\n{_TIMING}\nA\nB\n\t\n\n".encode(), 5),
        # Fin del archivo en medio de un bloque: se informa la línea que falta
        (b"1\n", 2),
        (f"1\n{_TIMING}\n".encode(), 3),
        (f"1\n{_TIMING}\nA\n".encode(), 4),
        (f"1\n{_TIMING}\nA".encode(), 4),
        (f"1\n{_TIMING}\nA\nB".encode(), 5),
        # UTF-8 inválido
        (f"1\n{_TIMING}\n".encode() + b"\xff\n\n", 3),
        (_block("1").encode() + f"2\n{_TIMING}\n".encode() + b"ok\xc3\n\n", 7),
        # La primera línea inválida gana aunque una posterior tampoco sea UTF-8
        (b"1\nmal\n\xff\n\n", 2),
        # Con "\r\n" se cuentan las mismas líneas
        (f"1\r\n{_TIMING}\r\n\r\n".encode(), 3),
    ],
)
def test_reports_first_invalid_line(data: bytes, line_number: int) -> None:
    with pytest.raises(SrtParseError) as excinfo:
        parse_srt(data)
    assert excinfo.value.line_number == line_number
    assert f"línea {line_number}" in str(excinfo.value)


@pytest.mark.req("12.6")
def test_parse_error_is_a_value_error() -> None:
    assert issubclass(SrtParseError, ValueError)


@pytest.mark.req("12.7")
@pytest.mark.parametrize(
    "segments",
    [
        [],
        [Segment(0, 1, "a")],
        [
            Segment(0, 1_000, "línea 1\n  línea 2 \nñ 😀"),
            Segment(1_000, 2_500, f"2\n{_TIMING}"),
            Segment(2_500, 359_999_999, "\u00a0\u3000\fcon espacios Unicode\u2028"),
        ],
    ],
)
def test_round_trip_examples(segments: list[Segment]) -> None:
    assert parse_srt(serialize_srt(segments)) == segments
