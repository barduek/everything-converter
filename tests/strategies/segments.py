"""Generadores de Segmentos_de_Transcripción que usan las propiedades 46 a 50.

- `segment_texts()` genera textos sin garantías, como los que puede informar whisper-cli: vacíos,
  solo con Líneas_en_Blanco, con líneas no blancas mezcladas con Líneas_en_Blanco y separadas por
  "\\r\\n", "\\r" o "\\n" (también al principio y al final), y texto Unicode arbitrario.
- `raw_segments()` genera listas crudas de `RawSegment`, con tiempos en centésimas de segundo:
  casi ordenadas, como las de whisper-cli, o desordenadas, con superposiciones, segmentos con el
  mismo inicio que el anterior o contenidos en él, invertidos o sin duración, tiempos negativos o
  muy grandes y textos de `segment_texts()`.
- `valid_segment_lists()` genera Listas_de_Segmentos_Válidas con tiempos de fin menores o iguales
  a `max_end_ms`: ordenadas, sin superposición (un segmento puede empezar justo donde termina el
  anterior), con inicio mayor o igual a 0 y menor al fin, y con un texto de una o más líneas
  unidas por "\\n". Ninguna línea es Línea_en_Blanco ni contiene "\\r" o "\\n", así que la lista
  vuelve igual de `parse_srt(serialize_srt(lista))` (Req 12.7). Los tiempos pasan por los bordes
  de los campos de la Línea_de_Tiempos_SRT, como 59 999 y 60 000 ms.

Las líneas no blancas conservan espacios y tabulaciones en los extremos e incluyen caracteres que
`str.splitlines` toma como saltos de línea (U+000B, U+000C, U+001C a U+001E, U+0085, U+2028 y
U+2029) o que `str.strip` toma como espacios (U+00A0 y U+3000). Según el glosario no separan
líneas ni forman Líneas_en_Blanco. También hay líneas que se parecen a un índice o a una
Línea_de_Tiempos_SRT.

Los textos no tienen sustitutos sueltos (U+D800 a U+DFFF), que no se pueden codificar en UTF-8.
"""

from __future__ import annotations

from hypothesis import strategies as st

from converter.srt.model import RawSegment, Segment

# Caracteres que puede tener una Línea_en_Blanco según el glosario. Es una copia propia y no
# `model.BLANK_CHARS`, para que un carácter que falte o sobre en model.py haga fallar las pruebas
BLANK_LINE_CHARS = " \t"

# 99:59:59,999: el mayor tiempo que cabe en una Línea_de_Tiempos_SRT (Req 12.7)
SRT_MAX_MS = 359_999_999

# Saltos de línea que reconoce el Serializador_SRT (design.md)
LINE_BREAKS: tuple[str, ...] = ("\r\n", "\r", "\n")

# Caracteres que no son saltos de línea ni caracteres de una Línea_en_Blanco según el glosario,
# aunque `str.splitlines` o `str.strip` los traten así
LOOKALIKE_CHARS: tuple[str, ...] = (
    "\x0b",
    "\x0c",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\u2028",
    "\u2029",
    "\u00a0",
    "\u3000",
    "\u200b",
    "\ufeff",
)

# Bordes de los campos de la Línea_de_Tiempos_SRT, en milisegundos
_FIELD_EDGES: tuple[int, ...] = (
    0,
    1,
    999,
    1_000,
    59_999,
    60_000,
    3_599_999,
    3_600_000,
    35_999_999,
    36_000_000,
    SRT_MAX_MS - 1,
    SRT_MAX_MS,
)

# Distancia máxima de un instante "cercano" al anterior, en milisegundos
_NEAR_MS = 5_000

# Líneas no blancas conocidas: el espacio inicial con que whisper-cli escribe cada segmento,
# espacios y tabulaciones en los extremos, acentos, emojis y líneas que parecen de un Bloque_SRT
_SPECIAL_LINES: tuple[str, ...] = (
    " Hola",
    "Hola ",
    "\tTabulado\t",
    "  dos espacios  ",
    "¿Qué tal?",
    "Ñandú y pingüino",
    "Ελληνικά",
    "日本語",
    "😀",
    "👩\u200d💻",
    "a\u2028b",
    "\ufeffcon BOM",
    "1",
    "2",
    "01",
    "-->",
    "00:00:00,000 --> 00:00:01,000",
)

# Caracteres de una línea de texto y, entre ellos, los que impiden que sea Línea_en_Blanco
_LINE_CHARS = st.characters(codec="utf-8", exclude_characters="\r\n")
_NON_BLANK_CHARS = st.characters(codec="utf-8", exclude_characters="\r\n" + BLANK_LINE_CHARS)

# Líneas vacías o solo con espacios y tabulaciones
_blank_lines = st.text(st.sampled_from(BLANK_LINE_CHARS), max_size=4)

_breaks = st.sampled_from(LINE_BREAKS)

# Líneas que no son Líneas_en_Blanco y no tienen "\r" ni "\n"
_text_lines = st.one_of(
    # Texto arbitrario con al menos un carácter que no es espacio ni tabulación
    st.tuples(
        st.text(_LINE_CHARS, max_size=10), _NON_BLANK_CHARS, st.text(_LINE_CHARS, max_size=10)
    ).map("".join),
    st.sampled_from(_SPECIAL_LINES + LOOKALIKE_CHARS),
    # Una palabra rodeada de espacios y tabulaciones
    st.tuples(_blank_lines, st.sampled_from(_SPECIAL_LINES), _blank_lines).map("".join),
)


_optional_breaks = st.sampled_from(("", *LINE_BREAKS))


@st.composite
def _mixed_texts(draw: st.DrawFn) -> str:
    """Líneas no blancas y Líneas_en_Blanco unidas por saltos de línea elegidos al azar, con o sin
    saltos al principio y al final.

    Un "\\r" seguido de una línea vacía y un "\\n" forma un solo "\\r\\n".
    """
    lines = draw(st.lists(st.one_of(_text_lines, _blank_lines), min_size=1, max_size=6))
    pieces = [draw(_optional_breaks), lines[0]]
    for line in lines[1:]:
        pieces += [draw(_breaks), line]
    pieces.append(draw(_optional_breaks))
    return "".join(pieces)


@st.composite
def _blank_texts(draw: st.DrawFn) -> str:
    """Textos vacíos o solo con Líneas_en_Blanco, con o sin saltos de línea."""
    return "".join(draw(st.lists(st.one_of(_blank_lines, _breaks), max_size=8)))


def segment_texts() -> st.SearchStrategy[str]:
    """Textos de segmento sin garantías: vacíos, en blanco, mezclados o arbitrarios."""
    return st.one_of(
        _blank_texts(),
        _mixed_texts(),
        st.text(st.characters(codec="utf-8"), max_size=30),
    )


# Tiempos crudos en centésimas de segundo: pequeños, negativos, muy grandes y bordes como
# 35 999 999 y 36 000 000 cs, que pasan a 359 999 990 y 360 000 000 ms
_raw_times = st.one_of(
    st.integers(-500, 6_000),
    st.integers(-(10**10), 10**10),
    st.sampled_from((-1, 0, 1, 35_999_999, 36_000_000)),
)


@st.composite
def _raw_lists(draw: st.DrawFn, max_size: int) -> list[RawSegment]:
    count = draw(st.integers(0, max_size))
    segments: list[RawSegment] = []
    previous_t0 = cursor = draw(st.integers(-100, 100))
    for _ in range(count):
        # Como en whisper-cli, el inicio suele quedar cerca del fin anterior: antes (superposición),
        # justo en él o después; también puede repetir el inicio anterior o ser cualquier valor
        t0 = draw(
            st.one_of(
                st.just(cursor),
                st.just(previous_t0),
                st.integers(cursor - 300, cursor + 300),
                _raw_times,
            )
        )
        # El fin puede ser posterior al inicio, igual (sin duración), anterior (invertido) o
        # cualquier valor
        t1 = draw(st.one_of(st.integers(t0 - 50, t0 + 1_000), st.just(t0), _raw_times))
        segments.append(RawSegment(t0, t1, draw(segment_texts())))
        previous_t0, cursor = t0, t1
    if draw(st.booleans()):
        segments = draw(st.permutations(segments))
    return segments


def raw_segments(*, max_size: int = 12) -> st.SearchStrategy[list[RawSegment]]:
    """Listas crudas de 0 a `max_size` segmentos, como las informa whisper-cli o peores."""
    if max_size < 0:
        raise ValueError(f"max_size debe ser mayor o igual a 0: {max_size}")
    return _raw_lists(max_size)


def _instants(low: int, high: int, *, share: int, include_high: bool) -> st.SearchStrategy[int]:
    """Instantes de [`low`, `high`] en ms: `low`, uno cercano, uno lejano dentro de la fracción
    1/`share` del intervalo, un borde de campo de la Línea_de_Tiempos_SRT y, si `include_high`,
    `high`.

    Limitar el salto lejano a 1/`share` deja lugar a los segmentos que faltan, que de otro modo
    quedarían a menudo apretados en 1 ms cada uno al final del intervalo.
    """
    options = [
        st.just(low),
        st.integers(low, min(low + _NEAR_MS, high)),
        st.integers(low, low + (high - low) // share),
    ]
    edges = [ms for ms in _FIELD_EDGES if low <= ms <= high]
    if edges:
        options.append(st.sampled_from(edges))
    if include_high:
        options.append(st.just(high))
    return st.one_of(options)


@st.composite
def _valid_texts(draw: st.DrawFn) -> str:
    return "\n".join(draw(st.lists(_text_lines, min_size=1, max_size=4)))


@st.composite
def _valid_lists(draw: st.DrawFn, max_end_ms: int, min_size: int, max_size: int) -> list[Segment]:
    # Cada segmento dura al menos 1 ms, así que caben como mucho `max_end_ms` segmentos
    count = draw(st.integers(min_size, min(max_size, max_end_ms)))
    segments: list[Segment] = []
    cursor = 0  # fin del segmento anterior
    for position in range(count):
        # Se deja 1 ms por cada segmento que falta, para que todos quepan antes de `max_end_ms`,
        # y el salto lejano se reparte entre los inicios y fines que faltan. Solo el último
        # segmento puede terminar justo en `max_end_ms`
        remaining = count - position
        start = draw(
            _instants(cursor, max_end_ms - remaining, share=2 * remaining, include_high=False)
        )
        end = draw(
            _instants(
                start + 1,
                max_end_ms - remaining + 1,
                share=2 * remaining - 1,
                include_high=remaining == 1,
            )
        )
        segments.append(Segment(start, end, draw(_valid_texts())))
        cursor = end
    return segments


def valid_segment_lists(
    *, max_end_ms: int = SRT_MAX_MS, min_size: int = 0, max_size: int = 12
) -> st.SearchStrategy[list[Segment]]:
    """Listas_de_Segmentos_Válidas de `min_size` a `max_size` segmentos, con tiempos de fin
    menores o iguales a `max_end_ms`."""
    if not 0 <= min_size <= max_size:
        raise ValueError(f"se necesita 0 <= min_size <= max_size: {min_size}, {max_size}")
    if max_end_ms < min_size:
        raise ValueError(f"no caben {min_size} segmentos de 1 ms o más antes de {max_end_ms} ms")
    return _valid_lists(max_end_ms, min_size, max_size)
