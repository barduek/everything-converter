"""Pruebas de src/converter/srt/serializer.py y de las Líneas_en_Blanco de srt/model.py (Req 12.4,
12.5, 12.8).

Los valores esperados se escriben a mano a partir de las definiciones de Bloque_SRT,
Línea_de_Tiempos_SRT y Línea_en_Blanco del glosario de requirements.md.
"""

from __future__ import annotations

import pytest

from converter.srt.model import Segment, is_blank_line, non_blank_lines
from converter.srt.serializer import format_timestamp, serialize_srt

_BOM_UTF8 = b"\xef\xbb\xbf"


@pytest.mark.req("12.4")
@pytest.mark.parametrize(
    ("ms", "expected"),
    [
        (0, "00:00:00,000"),
        (1, "00:00:00,001"),
        (999, "00:00:00,999"),
        (1_000, "00:00:01,000"),
        (59_999, "00:00:59,999"),
        (60_000, "00:01:00,000"),
        (3_599_999, "00:59:59,999"),
        (3_600_000, "01:00:00,000"),
        (3_723_004, "01:02:03,004"),
        (359_999_999, "99:59:59,999"),
    ],
)
def test_format_timestamp_pads_each_field(ms: int, expected: str) -> None:
    assert format_timestamp(ms) == expected


@pytest.mark.req("12.4")
@pytest.mark.parametrize("ms", [-1, 360_000_000])
def test_format_timestamp_rejects_values_outside_srt_range(ms: int) -> None:
    with pytest.raises(ValueError):
        format_timestamp(ms)


@pytest.mark.req("12.4")
def test_empty_list_produces_zero_bytes() -> None:
    assert serialize_srt([]) == b""


@pytest.mark.req("12.4")
def test_writes_one_block_per_segment_with_consecutive_indices() -> None:
    segments = [
        Segment(0, 1_500, "Hola"),
        Segment(1_500, 3_723_004, "¿Qué tal?\nBien"),
        Segment(3_723_004, 359_999_999, "Adiós"),
    ]
    assert serialize_srt(segments).decode("utf-8") == (
        "1\n00:00:00,000 --> 00:00:01,500\nHola\n\n"
        "2\n00:00:01,500 --> 01:02:03,004\n¿Qué tal?\nBien\n\n"
        "3\n01:02:03,004 --> 99:59:59,999\nAdiós\n\n"
    )


@pytest.mark.req("12.4")
def test_output_is_utf8_without_bom_with_lf_line_endings() -> None:
    data = serialize_srt([Segment(0, 1, "ñandú\r\nüber 漢字 😀\rfin")])
    assert not data.startswith(_BOM_UTF8)
    assert b"\r" not in data
    assert data == "1\n00:00:00,000 --> 00:00:00,001\nñandú\nüber 漢字 😀\nfin\n\n".encode()


@pytest.mark.req("12.4")
def test_invalid_list_is_written_in_the_order_received() -> None:
    # Desordenada, con superposición, con duración cero y con inicio mayor que el fin
    segments = [
        Segment(5_000, 6_000, "b"),
        Segment(1_000, 9_000, "a"),
        Segment(7_000, 7_000, "c"),
        Segment(8_000, 2_000, "d"),
    ]
    assert serialize_srt(segments).decode("utf-8") == (
        "1\n00:00:05,000 --> 00:00:06,000\nb\n\n"
        "2\n00:00:01,000 --> 00:00:09,000\na\n\n"
        "3\n00:00:07,000 --> 00:00:07,000\nc\n\n"
        "4\n00:00:08,000 --> 00:00:02,000\nd\n\n"
    )


@pytest.mark.req("12.4")
@pytest.mark.parametrize(
    ("start_ms", "end_ms", "timing"),
    [
        (-500, 400_000_000, "00:00:00,000 --> 99:59:59,999"),
        (-10, -1, "00:00:00,000 --> 00:00:00,000"),
        (360_000_000, 360_000_001, "99:59:59,999 --> 99:59:59,999"),
        (359_999_999, 359_999_999, "99:59:59,999 --> 99:59:59,999"),
    ],
)
def test_times_are_clamped_to_srt_range(start_ms: int, end_ms: int, timing: str) -> None:
    data = serialize_srt([Segment(start_ms, end_ms, "x")])
    assert data.decode("utf-8") == f"1\n{timing}\nx\n\n"


@pytest.mark.req("12.5")
def test_blank_lines_are_removed_and_remaining_lines_keep_their_order() -> None:
    text = "\n  \nuno\n\t\n  dos  \r\n\r\ntres\r \t \r"
    data = serialize_srt([Segment(0, 1_000, text)])
    # Las líneas no blancas conservan sus espacios iniciales y finales
    assert data.decode("utf-8") == "1\n00:00:00,000 --> 00:00:01,000\nuno\n  dos  \ntres\n\n"


@pytest.mark.req("12.8")
def test_segments_without_non_blank_lines_are_omitted_without_gaps() -> None:
    segments = [
        Segment(0, 1, ""),
        Segment(1, 2, "a"),
        Segment(2, 3, " \t\n\r\n "),
        Segment(3, 4, "b"),
        Segment(4, 5, "\n"),
    ]
    assert serialize_srt(segments).decode("utf-8") == (
        "1\n00:00:00,001 --> 00:00:00,002\na\n\n2\n00:00:00,003 --> 00:00:00,004\nb\n\n"
    )


@pytest.mark.req("12.8")
def test_list_with_only_blank_segments_produces_zero_bytes() -> None:
    assert serialize_srt([Segment(0, 1, ""), Segment(1, 2, " \t\r\n\t")]) == b""


@pytest.mark.req("12.5")
@pytest.mark.parametrize("line", ["", " ", "\t", " \t  \t"])
def test_blank_line_is_empty_or_only_spaces_and_tabs(line: str) -> None:
    assert is_blank_line(line)


@pytest.mark.req("12.5")
@pytest.mark.parametrize("line", ["x", " x ", "\u00a0", "\u3000", "\f", "\u200b"])
def test_other_characters_make_a_line_non_blank(line: str) -> None:
    # El glosario solo cuenta espacios y tabulaciones: otros espacios Unicode son texto
    assert not is_blank_line(line)


@pytest.mark.req("12.5")
def test_lines_are_split_only_at_crlf_cr_and_lf() -> None:
    assert non_blank_lines("a\r\nb\rc\nd") == ["a", "b", "c", "d"]
    # Separadores que `str.splitlines` también usa quedan dentro de la línea
    assert non_blank_lines("a\u2028b\x85c\fd\ve") == ["a\u2028b\x85c\fd\ve"]
