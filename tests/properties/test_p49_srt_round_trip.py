"""Propiedad 49: ida y vuelta SRT (Req 12.7).

El oráculo es la lista original: `parse_srt(serialize_srt(lista))` debe devolverla igual, con la
misma cantidad de segmentos, en el mismo orden, con los mismos tiempos en milisegundos y los mismos
textos. Las listas vienen de dos fuentes:

- `valid_segment_lists()`, que genera Listas_de_Segmentos_Válidas con tiempos de fin de hasta
  359 999 999 ms y pasa por los bordes de los campos de la Línea_de_Tiempos_SRT;
- `normalize_segments`, aplicada a listas crudas de whisper-cli con duraciones de hasta
  359 999 999 ms, porque son las listas que el Conversor serializa en MP3, WAV y MP4 a SRT
  (Req 12.9). La Duración_Máxima_de_Transcripción admite como máximo 5999 minutos, que caben en
  ese rango. Que esas listas sean válidas lo prueba la Propiedad 47.

La lista vacía, que el Serializador_SRT escribe como 0 bytes, se lee siempre como ejemplo fijo.

Además del archivo tal como lo escribe el Serializador_SRT, la prueba lee las variantes que deja un
editor al guardarlo con BOM_UTF8 inicial o con "\\r\\n" como fin de línea, que el Intérprete_SRT
acepta según design.md: quita una BOM_UTF8 inicial y un "\\r" final de cada línea.
"""

from __future__ import annotations

from itertools import pairwise

import pytest
from hypothesis import event, example, given
from hypothesis import strategies as st

from converter.srt.model import Segment
from converter.srt.parser import parse_srt
from converter.srt.segments import normalize_segments
from converter.srt.serializer import serialize_srt
from tests.strategies.segments import SRT_MAX_MS, raw_segments, valid_segment_lists

_BOM_UTF8 = b"\xef\xbb\xbf"

# 5999 minutos: la mayor Duración_Máxima_de_Transcripción que admite la configuración
_MAX_TRANSCRIPTION_MS = 5_999 * 60 * 1_000

# Duraciones del Audio_a_Transcribir en ms: cercanas a los tiempos habituales de `raw_segments()`
# (hasta 60 000 cs), de todo el rango de SRT y sus bordes
_durations = st.one_of(
    st.integers(0, 120_000),
    st.integers(0, SRT_MAX_MS),
    st.sampled_from((0, 1, _MAX_TRANSCRIPTION_MS, SRT_MAX_MS - 1, SRT_MAX_MS)),
)


@st.composite
def _normalized_lists(draw: st.DrawFn) -> list[Segment]:
    return normalize_segments(draw(raw_segments()), draw(_durations))


# Listas_de_Segmentos_Válidas con tiempos de fin de hasta 359 999 999 ms, con su fuente. Cerca de
# la mitad de las listas crudas no deja ningún segmento con texto y duración positiva; se
# descartan para no gastar ejemplos en la lista vacía, que la prueba lee siempre con `@example`
_segment_lists = st.one_of(
    st.tuples(st.just("valid_segment_lists"), valid_segment_lists(max_end_ms=SRT_MAX_MS)),
    st.tuples(st.just("normalize_segments"), _normalized_lists().filter(bool)),
)


def _variants(data: bytes) -> dict[str, bytes]:
    # El archivo escrito, con BOM_UTF8 inicial, con "\r\n" y con ambas cosas. El texto de una
    # Lista_de_Segmentos_Válida no tiene "\r", así que cada "\r" que agrega "\r\n" es el único
    # "\r" final de su línea
    crlf = data.replace(b"\n", b"\r\n")
    return {"LF": data, "BOM y LF": _BOM_UTF8 + data, "CRLF": crlf, "BOM y CRLF": _BOM_UTF8 + crlf}


# Feature: file-converter, Property 49: Ida y vuelta SRT
@pytest.mark.req("12.7")
@given(source_and_segments=_segment_lists)
@example(source_and_segments=("lista vacía", []))
def test_srt_round_trip(source_and_segments: tuple[str, list[Segment]]) -> None:
    source, segments = source_and_segments
    data = serialize_srt(segments)

    # Req 12.7: misma cantidad de segmentos, mismo orden, mismos tiempos y mismos textos
    assert parse_srt(data) == segments

    # El mismo archivo guardado con BOM_UTF8 o con "\r\n" se lee igual
    for name, variant in _variants(data).items():
        assert parse_srt(variant) == segments, name

    event(f"fuente: {source}")
    if not segments:
        event("lista vacía: 0 bytes")
    if any(segment.end_ms == SRT_MAX_MS for segment in segments):
        event("con un tiempo de fin de 99:59:59,999")
    if any("\n" in segment.text for segment in segments):
        event("con segmentos de varias líneas")
    if any(a.end_ms == b.start_ms for a, b in pairwise(segments)):
        event("con un segmento que empieza donde termina el anterior")
