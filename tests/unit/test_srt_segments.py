"""Pruebas de src/converter/srt/segments.py (Req 11.1, 11.7, 12.2, 12.3, 12.10).

Los valores esperados se escriben a mano a partir de las definiciones de Segmento_de_Transcripción,
Lista_de_Segmentos_Válida y Línea_en_Blanco del glosario de requirements.md y de la normalización
que describe design.md: los tiempos de `RawSegment` están en centésimas de segundo.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from converter.srt.model import RawSegment, Segment
from converter.srt.parser import parse_srt
from converter.srt.segments import normalize_segments, render_transcript_txt
from converter.srt.serializer import serialize_srt

_BOM_UTF8 = b"\xef\xbb\xbf"


def _assert_valid_list(segments: list[Segment], duration_ms: int) -> None:
    # Lista_de_Segmentos_Válida dentro de la duración (glosario y Req 12.3)
    for segment in segments:
        assert 0 <= segment.start_ms < segment.end_ms <= duration_ms
        assert all(line.strip(" \t") for line in segment.text.split("\n"))
    for previous, current in pairwise(segments):
        assert previous.end_ms <= current.start_ms


@pytest.mark.req("12.2")
def test_empty_input_produces_empty_list() -> None:
    assert normalize_segments([], 60_000) == []


@pytest.mark.req("12.2")
def test_centiseconds_are_converted_to_milliseconds() -> None:
    raw = [RawSegment(0, 150, "Hola"), RawSegment(150, 372, "¿Qué tal?")]
    assert normalize_segments(raw, 10_000) == [
        Segment(0, 1_500, "Hola"),
        Segment(1_500, 3_720, "¿Qué tal?"),
    ]


@pytest.mark.req("12.2")
def test_segments_are_sorted_by_start_keeping_input_order_on_ties() -> None:
    raw = [
        RawSegment(300, 400, "c"),
        RawSegment(100, 200, "a"),
        RawSegment(200, 250, "b1"),
        RawSegment(200, 300, "b2"),
    ]
    # "b1" y "b2" empiezan juntos: "b2" queda después y su inicio pasa al fin de "b1"
    assert normalize_segments(raw, 10_000) == [
        Segment(1_000, 2_000, "a"),
        Segment(2_000, 2_500, "b1"),
        Segment(2_500, 3_000, "b2"),
        Segment(3_000, 4_000, "c"),
    ]


@pytest.mark.req("12.3")
def test_times_are_clamped_to_zero_and_duration() -> None:
    raw = [RawSegment(-50, 100, "a"), RawSegment(900, 2_000, "b")]
    assert normalize_segments(raw, 10_000) == [
        Segment(0, 1_000, "a"),
        Segment(9_000, 10_000, "b"),
    ]


@pytest.mark.req("12.3")
@pytest.mark.parametrize(
    "raw",
    [
        RawSegment(1_000, 1_200, "después del fin"),
        RawSegment(-300, -100, "antes del inicio"),
        RawSegment(-300, 0, "termina en 0"),
    ],
)
def test_segments_outside_the_duration_are_discarded(raw: RawSegment) -> None:
    assert normalize_segments([raw], 10_000) == []


@pytest.mark.req("12.3")
@pytest.mark.parametrize("duration_ms", [0, -1])
def test_zero_or_negative_duration_produces_empty_list(duration_ms: int) -> None:
    assert normalize_segments([RawSegment(0, 100, "a")], duration_ms) == []


@pytest.mark.req("12.2")
def test_overlap_is_trimmed_by_moving_start_to_previous_end() -> None:
    raw = [RawSegment(0, 200, "a"), RawSegment(150, 300, "b")]
    assert normalize_segments(raw, 10_000) == [
        Segment(0, 2_000, "a"),
        Segment(2_000, 3_000, "b"),
    ]


@pytest.mark.req("12.2")
def test_segment_contained_in_previous_one_is_discarded() -> None:
    raw = [RawSegment(0, 500, "a"), RawSegment(100, 300, "b"), RawSegment(400, 600, "c")]
    assert normalize_segments(raw, 10_000) == [
        Segment(0, 5_000, "a"),
        Segment(5_000, 6_000, "c"),
    ]


@pytest.mark.req("12.2")
@pytest.mark.parametrize(("t0_cs", "t1_cs"), [(100, 100), (200, 150)])
def test_segments_without_positive_duration_are_discarded(t0_cs: int, t1_cs: int) -> None:
    raw = [RawSegment(t0_cs, t1_cs, "x"), RawSegment(300, 400, "y")]
    assert normalize_segments(raw, 10_000) == [Segment(3_000, 4_000, "y")]


@pytest.mark.req("12.2")
@pytest.mark.parametrize("text", ["", " ", "\t", " \t\r\n \n\r"])
def test_segments_without_text_are_discarded_without_trimming_the_next(text: str) -> None:
    raw = [RawSegment(0, 300, text), RawSegment(100, 200, "a")]
    assert normalize_segments(raw, 10_000) == [Segment(1_000, 2_000, "a")]


@pytest.mark.req("12.2")
def test_blank_lines_are_removed_and_remaining_lines_keep_order_and_spaces() -> None:
    raw = [RawSegment(0, 100, "\n uno \n\t\r\ndos\r \r")]
    assert normalize_segments(raw, 10_000) == [Segment(0, 1_000, " uno \ndos")]


@pytest.mark.req("12.2")
@pytest.mark.req("12.3")
def test_messy_input_becomes_a_valid_list_that_round_trips_through_srt() -> None:
    duration_ms = 5_000
    raw = [
        RawSegment(450, 700, "fuera\nde rango"),
        RawSegment(120, 260, "  \nsegunda\r\n\t"),
        RawSegment(-20, 150, " primera"),
        RawSegment(130, 140, "contenida"),
        RawSegment(250, 250, "sin duración"),
        RawSegment(300, 200, "invertida"),
        RawSegment(240, 400, ""),
        RawSegment(380, 520, "última"),
    ]
    segments = normalize_segments(raw, duration_ms)
    assert segments == [
        Segment(0, 1_500, " primera"),
        Segment(1_500, 2_600, "segunda"),
        Segment(3_800, 5_000, "última"),
    ]
    _assert_valid_list(segments, duration_ms)
    assert parse_srt(serialize_srt(segments)) == segments


@pytest.mark.req("11.7")
@pytest.mark.req("12.10")
def test_empty_list_renders_zero_bytes() -> None:
    assert render_transcript_txt([]) == b""


@pytest.mark.req("11.1")
def test_renders_one_line_per_segment_without_timestamps() -> None:
    segments = [
        Segment(0, 1_500, " Hola"),
        Segment(1_500, 3_723_004, "¿Qué tal?\n  Bien  "),
        Segment(3_723_004, 3_800_000, "Adiós, 漢字 😀"),
    ]
    assert render_transcript_txt(segments).decode("utf-8") == (
        "Hola\n¿Qué tal? Bien\nAdiós, 漢字 😀\n"
    )


@pytest.mark.req("11.1")
def test_output_is_utf8_without_bom_with_lf_line_endings() -> None:
    data = render_transcript_txt([Segment(0, 1, "ñandú\r\nüber\rfin")])
    assert not data.startswith(_BOM_UTF8)
    assert b"\r" not in data
    assert data == "ñandú über fin\n".encode()


@pytest.mark.req("11.1")
def test_inner_spaces_and_other_unicode_spaces_are_kept() -> None:
    # Solo se quitan espacios y tabulaciones de los extremos de cada línea
    data = render_transcript_txt([Segment(0, 1, "\u00a0a  b\u2028c\t")])
    assert data.decode("utf-8") == "\u00a0a  b\u2028c\n"


@pytest.mark.req("11.7")
def test_segments_without_non_blank_lines_are_omitted() -> None:
    segments = [
        Segment(0, 1, ""),
        Segment(1, 2, "a"),
        Segment(2, 3, " \t\r\n "),
        Segment(3, 4, "b"),
    ]
    assert render_transcript_txt(segments) == b"a\nb\n"


@pytest.mark.req("11.7")
@pytest.mark.req("12.10")
def test_transcription_without_words_gives_zero_byte_txt_and_srt() -> None:
    raw = [RawSegment(0, 100, ""), RawSegment(100, 200, " \t\n")]
    segments = normalize_segments(raw, 10_000)
    assert segments == []
    assert render_transcript_txt(segments) == b""
    assert serialize_srt(segments) == b""
