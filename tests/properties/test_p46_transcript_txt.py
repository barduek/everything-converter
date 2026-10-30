"""Propiedad 46: el TXT de transcripción respeta el orden y queda vacío sin palabras (Req 11.1,
11.7, 12.10).

La prueba revisa sobre la salida de `render_transcript_txt`:

- que es UTF-8 estricto y que no agrega una BOM_UTF8;
- que su Secuencia_de_Palabras es la concatenación de las palabras de cada segmento, en el orden
  de la lista (Req 11.1);
- que tiene una línea terminada en "\\n" por cada segmento con texto, con las palabras de ese
  segmento, y ninguna línea más;
- que no agrega marcas de tiempo: tiene tantos tiempos con forma de Línea_de_Tiempos_SRT y tantos
  "-->" como los textos de los segmentos;
- que tiene 0 bytes exactamente cuando la lista no tiene palabras, y que entonces el SRT que
  escribe `serialize_srt` para la misma lista también tiene 0 bytes (Req 11.7, 12.10).

Las listas son Listas_de_Segmentos_Válidas de `valid_segment_lists()` y las que produce
`normalize_segments` a partir de `raw_segments()`, que es la lista que recibe
`render_transcript_txt` en una transcripción y que a menudo queda vacía. También hay listas sin
garantías, con segmentos vacíos, solo con Líneas_en_Blanco o con saltos "\\r", y listas sin
ninguna palabra, para ejercitar los segmentos que se omiten.

El oráculo usa su propia división en líneas, su propia definición de Línea_en_Blanco y su propia
división en palabras, sin `non_blank_lines` ni `is_blank_line`, para que un error de srt/ haga
fallar la prueba en lugar de pasar al valor esperado. Según el glosario, una palabra es una
secuencia máxima de caracteres distintos de espacio, tabulación, retorno de carro y salto de
línea, así que U+00A0, U+2028 y similares forman parte de las palabras.

El aviso de que no se detectó voz en la Tarea completada depende del Transcriptor y del
Gestor_de_Tareas, y no forma parte de esta prueba del núcleo puro.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from converter.srt.model import Segment
from converter.srt.segments import normalize_segments, render_transcript_txt
from converter.srt.serializer import serialize_srt
from tests.strategies.segments import (
    BLANK_LINE_CHARS,
    SRT_MAX_MS,
    raw_segments,
    segment_texts,
    valid_segment_lists,
)

_BOM_UTF8 = b"\xef\xbb\xbf"

# Palabra según la Secuencia_de_Palabras del glosario
_WORD = re.compile(r"[^ \t\r\n]+")

# Lo que parece una marca de tiempo: un tiempo de Línea_de_Tiempos_SRT (también con punto, como
# en VTT o en la salida de whisper-cli) o el separador "-->"
_TIMING = re.compile(r"[0-9]+:[0-9]{2}:[0-9]{2}[,.][0-9]{3}|-->")

# Duraciones de `normalize_segments`: del orden de los tiempos crudos habituales (hasta 60 000 ms),
# de todo el rango de SRT y 0, que deja la lista vacía
_durations = st.one_of(
    st.integers(1, 70_000),
    st.integers(1, SRT_MAX_MS),
    st.just(0),
)

# Tiempos de las listas sin garantías; el TXT no los usa
_times = st.integers(-1_000, SRT_MAX_MS + 1)

# Textos sin palabras: vacíos o solo con espacios, tabulaciones y saltos de línea
_wordless_texts = st.text(st.sampled_from(BLANK_LINE_CHARS + "\r\n"), max_size=8)

_Case = tuple[str, list[Segment]]

_cases: st.SearchStrategy[_Case] = st.one_of(
    valid_segment_lists().map(lambda segments: ("válida", segments)),
    st.tuples(raw_segments(), _durations).map(
        lambda case: ("de normalize_segments", normalize_segments(*case))
    ),
    st.lists(st.builds(Segment, _times, _times, segment_texts()), max_size=12).map(
        lambda segments: ("sin garantías", segments)
    ),
    st.lists(st.builds(Segment, _times, _times, _wordless_texts), max_size=6).map(
        lambda segments: ("sin palabras", segments)
    ),
)


def _lines(text: str) -> list[str]:
    # Solo "\r\n", "\r" y "\n" separan líneas; U+2028, U+0085 y similares quedan dentro de la línea
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _is_blank(line: str) -> bool:
    # Línea_en_Blanco: vacía o compuesta solo por espacios y tabulaciones
    return all(char in BLANK_LINE_CHARS for char in line)


def _has_text(segment: Segment) -> bool:
    # El segmento tiene al menos una línea que no es Línea_en_Blanco
    return any(not _is_blank(line) for line in _lines(segment.text))


def _words(text: str) -> list[str]:
    return _WORD.findall(text)


# Feature: file-converter, Property 46: El TXT de transcripción respeta el orden y queda vacío sin palabras  # noqa: E501
@pytest.mark.req("11.1", "11.7", "12.10")
@given(case=_cases)
def test_transcript_txt_keeps_order_and_is_empty_without_words(case: _Case) -> None:
    kind, segments = case

    data = render_transcript_txt(segments)

    assert isinstance(data, bytes)
    # Req 11.1: UTF-8 estricto; "utf-8" y no "utf-8-sig", para que una BOM_UTF8 agregada quede
    # como un U+FEFF al principio de la primera palabra y haga fallar la comparación de palabras
    text = data.decode("utf-8")
    expected_words = [word for segment in segments for word in _words(segment.text)]
    # Req 11.1: las palabras de los segmentos, en el orden de la lista
    assert _words(text) == expected_words
    # Solo hay BOM_UTF8 si la primera palabra reconocida empieza con U+FEFF
    if not expected_words or not expected_words[0].startswith("\ufeff"):
        assert not data.startswith(_BOM_UTF8)

    # Una línea terminada en "\n" por cada segmento con texto, con las palabras de ese segmento
    with_text = [segment for segment in segments if _has_text(segment)]
    assert not text or text.endswith("\n"), f"la última línea no termina en \\n: {text!r}"
    txt_lines = _lines(text)[:-1] if text else []
    assert len(txt_lines) == len(with_text), f"{len(txt_lines)} líneas: {text!r}"
    assert [_words(line) for line in txt_lines] == [_words(s.text) for s in with_text]

    # Req 11.1: sin marcas de tiempo agregadas; solo las que dicen los textos reconocidos
    source_timings = sum(len(_TIMING.findall(segment.text)) for segment in segments)
    assert len(_TIMING.findall(text)) == source_timings

    # Req 11.7 y 12.10: sin palabras, el TXT y el SRT tienen 0 bytes; con palabras, ninguno
    srt = serialize_srt(segments)
    assert (data == b"") == (not expected_words)
    assert (data == b"") == (srt == b"")

    event(f"lista {kind}")
    if not expected_words:
        event("sin palabras: TXT y SRT de 0 bytes")
    if segments and not expected_words:
        event("sin palabras con segmentos en la lista")
    if len(with_text) < len(segments):
        event("con segmentos sin texto omitidos")
    if any(len(_lines(s.text)) > 1 for s in with_text):
        event("con segmentos de varias líneas")
    if len(with_text) >= 3:
        event("con 3 o más líneas en el TXT")
    if source_timings:
        event("con textos que parecen marcas de tiempo")
