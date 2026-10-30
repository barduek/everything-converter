"""Propiedad 47: la normalización produce una Lista_de_Segmentos_Válida dentro de la duración
(Req 12.2, 12.3).

La prueba revisa tres cosas sobre la salida de `normalize_segments`:

- que cumple la definición de Lista_de_Segmentos_Válida del glosario y que todos sus tiempos
  quedan entre 0 y la duración (una duración negativa cuenta como 0);
- que cada segmento sale de un segmento crudo distinto, en el orden de los inicios, con el mismo
  texto sin Líneas_en_Blanco, el mismo fin acotado y un inicio igual o posterior al crudo;
- que coincide con un oráculo propio del algoritmo de design.md.

El oráculo usa su propia división en líneas y su propia definición de Línea_en_Blanco, sin
`non_blank_lines` ni `is_blank_line`, para que un error de srt/ haga fallar la prueba en lugar de
pasar al valor esperado. También formula el acotado de otra manera: en vez de acotar cada tiempo
a [0, duración], toma el inicio como el mayor entre el inicio crudo, 0 y el fin anterior, y el fin
como el menor entre el fin crudo y la duración. Si el inicio queda antes del fin, ambas formas dan
los mismos tiempos; si no, ambas descartan el segmento, también con una duración negativa.
"""

from __future__ import annotations

import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from converter.srt.model import RawSegment, Segment
from converter.srt.segments import normalize_segments
from tests.strategies.segments import BLANK_LINE_CHARS, SRT_MAX_MS, raw_segments, segment_texts

_MS_PER_CS = 10

# Duraciones positivas pequeñas, del orden de los tiempos crudos habituales (hasta 60 000 ms), y
# de todo el rango de SRT; 0, negativas y bordes de una centésima. Las positivas pesan más porque
# con 0 o una duración negativa la salida siempre está vacía
_durations = st.one_of(
    st.integers(1, 70_000),
    st.integers(1, 70_000),
    st.integers(1, SRT_MAX_MS),
    st.sampled_from((0, 1, 9, 10, 11, SRT_MAX_MS - 1, SRT_MAX_MS)),
    st.integers(-(10**10), -1),
)


# Textos de `_overlapping_raw`: casi siempre con alguna línea no blanca
_texts = st.one_of(st.sampled_from(("a", " b ", "c\r\n\td", "e\n \nf")), segment_texts())


@st.composite
def _overlapping_raw(draw: st.DrawFn) -> list[RawSegment]:
    """Segmentos crudos seguidos, casi todos con texto y duración positiva, que se superponen a
    menudo: el inicio queda en el fin anterior, hasta 1 s antes o después de él o en el inicio
    anterior, y un segmento corto que empieza antes de ese fin queda contenido en el anterior.

    `raw_segments()` rara vez deja más de uno o dos segmentos conservados; esta lista ejercita el
    recorte de superposiciones, los empates de inicio y los segmentos contenidos en uno anterior.
    """
    count = draw(st.integers(2, 10))
    segments: list[RawSegment] = []
    previous_t0 = cursor = draw(st.integers(0, 500))
    for _ in range(count):
        t0 = draw(
            st.one_of(
                st.just(cursor),
                st.integers(max(cursor - 100, 0), cursor + 100),
                st.just(previous_t0),  # empate: el orden estable decide cuál se recorta
            )
        )
        t1 = draw(st.integers(t0 + 1, t0 + 500))
        segments.append(RawSegment(t0, t1, draw(_texts)))
        previous_t0, cursor = t0, t1
    if draw(st.booleans()):
        segments = draw(st.permutations(segments))
    return segments


@st.composite
def _cases(draw: st.DrawFn) -> tuple[list[RawSegment], int]:
    """Una lista cruda y una duración, a veces justo en un tiempo crudo, a 1 ms de él o después
    de todos los fines."""
    raw = draw(st.one_of(raw_segments(), _overlapping_raw()))
    near_times = sorted(
        {
            cs * _MS_PER_CS + offset
            for segment in raw
            for cs in (segment.t0_cs, segment.t1_cs)
            for offset in (-1, 0, 1)
            if cs * _MS_PER_CS + offset <= SRT_MAX_MS
        }
    )
    options = [_durations]
    if near_times:
        options.append(st.sampled_from(near_times))
    # Una duración que alcanza todos los fines crudos, para que se conserven más segmentos
    last_end = max((segment.t1_cs * _MS_PER_CS for segment in raw), default=0)
    if 0 < last_end <= SRT_MAX_MS:
        options.append(st.integers(last_end, SRT_MAX_MS))
    return raw, draw(st.one_of(options))


def _lines(text: str) -> list[str]:
    # Solo "\r\n", "\r" y "\n" separan líneas; U+2028, U+0085 y similares quedan dentro de la línea
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _is_blank(line: str) -> bool:
    # Línea_en_Blanco: vacía o compuesta solo por espacios y tabulaciones
    return all(char in BLANK_LINE_CHARS for char in line)


def _kept_text(text: str) -> str:
    # El texto de un segmento conservado: sus líneas no blancas, sin cambios, unidas por "\n"
    return "\n".join(line for line in _lines(text) if not _is_blank(line))


def _sorted_by_start(raw: list[RawSegment]) -> list[tuple[int, RawSegment]]:
    # Ordenamiento estable: ante el mismo inicio, el orden en que llegaron
    return sorted(enumerate(raw), key=lambda pair: (pair[1].t0_cs, pair[0]))


def _expected(raw: list[RawSegment], duration_ms: int) -> list[tuple[int, Segment]]:
    """Oráculo: los segmentos que conserva el algoritmo, con la posición del segmento crudo."""
    kept: list[tuple[int, Segment]] = []
    previous_end = 0
    for position, item in _sorted_by_start(raw):
        text = _kept_text(item.text)
        if not text:  # sin texto: se descarta antes de mirar sus tiempos
            continue
        start = max(item.t0_cs * _MS_PER_CS, 0, previous_end)
        end = min(item.t1_cs * _MS_PER_CS, duration_ms)
        if start >= end:  # invertido, fuera de la duración o contenido en el anterior
            continue
        kept.append((position, Segment(start, end, text)))
        previous_end = end
    return kept


def _comes_from(segment: Segment, item: RawSegment, duration_ms: int) -> bool:
    # Mismo texto sin Líneas_en_Blanco, mismo fin acotado e inicio igual o posterior al crudo
    return (
        segment.text == _kept_text(item.text)
        and segment.end_ms == min(max(item.t1_cs * _MS_PER_CS, 0), max(duration_ms, 0))
        and segment.start_ms >= max(item.t0_cs * _MS_PER_CS, 0)
    )


def _assert_valid_list(segments: list[Segment], duration_ms: int) -> None:
    duration = max(duration_ms, 0)
    previous_end = 0
    for number, segment in enumerate(segments, start=1):
        assert isinstance(segment, Segment), f"segmento {number}: {segment!r}"
        # Req 12.3 y glosario: 0 <= inicio < fin <= duración
        assert 0 <= segment.start_ms < segment.end_ms <= duration, f"segmento {number}: {segment}"
        # Ordenada por inicio y sin superposición: cada segmento empieza en o después del fin del
        # anterior, lo que con inicio < fin también deja los inicios en orden creciente
        assert segment.start_ms >= previous_end, f"segmento {number} superpuesto: {segment}"
        # Una o más líneas, ninguna Línea_en_Blanco ni con "\r"
        lines = segment.text.split("\n")
        assert all("\r" not in line and not _is_blank(line) for line in lines), (
            f"segmento {number} con una línea inválida: {segment.text!r}"
        )
        previous_end = segment.end_ms


def _assert_each_comes_from_its_own_raw_segment(
    segments: list[Segment], raw: list[RawSegment], duration_ms: int
) -> None:
    # La salida es una subsecuencia de los segmentos crudos ordenados por inicio. Asignar cada
    # segmento al primer crudo posible que queda alcanza para saber si existe esa subsecuencia
    matched = 0
    for _, item in _sorted_by_start(raw):
        if matched < len(segments) and _comes_from(segments[matched], item, duration_ms):
            matched += 1
    assert matched == len(segments), (
        f"el segmento {matched + 1} no sale de un segmento crudo posterior a los anteriores: "
        f"{segments[matched]}"
    )


# Feature: file-converter, Property 47: La normalización produce una Lista_de_Segmentos_Válida dentro de la duración  # noqa: E501
@pytest.mark.req("12.2", "12.3")
@given(case=_cases())
def test_normalization_produces_valid_list_within_duration(
    case: tuple[list[RawSegment], int],
) -> None:
    raw, duration_ms = case

    segments = normalize_segments(raw, duration_ms)

    assert isinstance(segments, list)
    # Req 12.2 y 12.3: Lista_de_Segmentos_Válida con todos los tiempos entre 0 y la duración
    _assert_valid_list(segments, duration_ms)
    # Cada segmento sale de un segmento crudo con el mismo texto, sin repetirlos ni reordenarlos
    _assert_each_comes_from_its_own_raw_segment(segments, raw, duration_ms)
    # El resultado es el del algoritmo de design.md
    expected = _expected(raw, duration_ms)
    assert segments == [segment for _, segment in expected]

    with_text = [item for item in raw if _kept_text(item.text)]
    if duration_ms < 0:
        event("duración negativa")
    elif duration_ms == 0:
        event("duración 0")
    if not segments:
        event("sin segmentos")
    if len(with_text) < len(raw):
        event("con segmentos sin texto descartados")
    if len(segments) < len(with_text):
        event("con segmentos con texto descartados por sus tiempos")
    if any(s.start_ms > max(raw[i].t0_cs * _MS_PER_CS, 0) for i, s in expected):
        event("con superposiciones recortadas")
    if any(raw[i].t1_cs * _MS_PER_CS > duration_ms for i, _ in expected):
        event("con fines acotados a la duración")
    if any(s.text != raw[i].text for i, s in expected):
        event("con Líneas_en_Blanco o saltos \\r eliminados del texto")
    if len(segments) >= 3:
        event("con 3 o más segmentos")
    kept_starts = [raw[i].t0_cs for i, _ in expected]
    if len(set(kept_starts)) < len(kept_starts):
        event("con segmentos conservados del mismo inicio crudo")
