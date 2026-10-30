"""Propiedad 48: el Serializador_SRT escribe un bloque por segmento con texto (Req 12.4, 12.5,
12.8).

El oráculo escribe el SRT esperado con su propia copia de las definiciones de Bloque_SRT,
Línea_de_Tiempos_SRT y Línea_en_Blanco del glosario, sin usar `non_blank_lines`, `is_blank_line`
ni `format_timestamp`, para que un error de srt/ haga fallar la prueba en lugar de pasar al valor
esperado. La lista puede no ser una Lista_de_Segmentos_Válida, así que los tiempos que no caben en
una Línea_de_Tiempos_SRT se acotan a [0, 359 999 999] ms, como indica design.md.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from converter.srt.model import Segment
from converter.srt.serializer import serialize_srt
from tests.strategies.segments import (
    BLANK_LINE_CHARS,
    SRT_MAX_MS,
    segment_texts,
    valid_segment_lists,
)

_BOM_UTF8 = b"\xef\xbb\xbf"

# Tiempos en ms de listas que no son válidas: pequeños, negativos, de todo el rango de SRT, muy
# grandes y los bordes de los campos de la Línea_de_Tiempos_SRT y de su rango
_times = st.one_of(
    st.integers(-2_000, 10_000),
    st.integers(0, SRT_MAX_MS),
    st.integers(-(2**63), 2**63),
    st.sampled_from((-1, 0, 59_999, 60_000, 3_599_999, 3_600_000, SRT_MAX_MS, SRT_MAX_MS + 1)),
)

# Listas válidas y listas sin garantías de orden, tiempos ni texto
_segment_lists = st.one_of(
    valid_segment_lists(),
    st.lists(st.builds(Segment, _times, _times, segment_texts()), max_size=12),
)


def _non_blank_lines(text: str) -> list[str]:
    # Cortar en cada "\r" y en cada "\n" deja las mismas líneas no blancas que tratar "\r\n" como
    # un solo salto: lo que queda entre "\r" y "\n" es una línea vacía, que es Línea_en_Blanco
    return [line for line in re.split(r"[\r\n]", text) if line.strip(BLANK_LINE_CHARS)]


def _timestamp(ms: int) -> str:
    # Línea_de_Tiempos_SRT: HH:MM:SS,mmm con ceros a la izquierda, con el tiempo acotado al rango
    ms = min(max(ms, 0), SRT_MAX_MS)
    hours, minutes = ms // 3_600_000, ms // 60_000 % 60
    seconds, millis = ms // 1_000 % 60, ms % 1_000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _expected_srt(segments: list[Segment]) -> str:
    # Un Bloque_SRT por segmento con alguna línea no blanca: índice, Línea_de_Tiempos_SRT, las
    # líneas no blancas en su orden y una línea vacía, cada una terminada en "\n"
    blocks: list[str] = []
    for segment in segments:
        lines = _non_blank_lines(segment.text)
        if not lines:  # Req 12.8: se omite sin dejar hueco en los índices
            continue
        index = len(blocks) + 1
        timing = f"{_timestamp(segment.start_ms)} --> {_timestamp(segment.end_ms)}"
        blocks.append("".join(f"{line}\n" for line in [str(index), timing, *lines, ""]))
    return "".join(blocks)


def _outside_srt_range(ms: int) -> bool:
    return not 0 <= ms <= SRT_MAX_MS


# Feature: file-converter, Property 48: El Serializador_SRT escribe un bloque por segmento con texto
@pytest.mark.req("12.4", "12.5", "12.8")
@given(segments=_segment_lists)
def test_serializer_writes_one_block_per_segment_with_text(segments: list[Segment]) -> None:
    data = serialize_srt(segments)

    # Req 12.4: UTF-8 sin BOM; una lista sin bloques produce 0 bytes
    assert not data.startswith(_BOM_UTF8)
    text = data.decode("utf-8")
    # Req 12.4, 12.5 y 12.8: los bloques, sus índices, tiempos y líneas coinciden con el oráculo
    assert text == _expected_srt(segments)

    written = [segment for segment in segments if _non_blank_lines(segment.text)]
    if not written:
        event("sin bloques: 0 bytes")
    if len(written) < len(segments):
        event("con segmentos omitidos")
    if any(_outside_srt_range(ms) for s in written for ms in (s.start_ms, s.end_ms)):
        event("con tiempos acotados al rango de SRT")
    if any(len(_non_blank_lines(s.text)) < len(re.split(r"\r\n|\r|\n", s.text)) for s in written):
        event("con Líneas_en_Blanco eliminadas dentro de un bloque")
    if any("\r" in s.text for s in written):
        event("con \\r o \\r\\n")
