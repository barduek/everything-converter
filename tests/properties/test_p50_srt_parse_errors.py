"""Propiedad 50: el Intérprete_SRT informa la primera línea inválida (Req 12.6).

La prueba serializa una Lista_de_Segmentos_Válida, le aplica una mutación y compara el número de
línea de `SrtParseError` con el de un oráculo propio. El oráculo lee el archivo bloque por bloque
con las definiciones de Bloque_SRT, Línea_de_Tiempos_SRT y Línea_en_Blanco del glosario, sin usar
`converter.srt`: revisa cada campo de la Línea_de_Tiempos_SRT por su posición en lugar de con una
expresión regular y compara el índice como entero decimal sin ceros a la izquierda.

De design.md toma cómo se cortan las líneas: se quita una BOM_UTF8 inicial, cada "\\n" termina
una línea (el "\\n" final no abre otra), se quita un "\\r" final de cada línea y una línea que no
es UTF-8 válido no cumple el formato. Si el archivo termina en medio de un bloque, la primera
línea que no cumple es la que falta, la siguiente a la última.

Las mutaciones con línea conocida vuelven inválida su línea k en su contexto y dejan iguales las
anteriores, así que la prueba verifica también que el oráculo informe k:

- el índice de un bloque cambiado por otra línea: con ceros a la izquierda, con signo, con
  espacios, con dígitos que no son ASCII, otro entero (el anterior, el siguiente o 0) o cualquier
  otra línea;
- un bloque omitido o repetido, que deja mal el índice del bloque que sigue;
- una Línea_de_Tiempos_SRT mal formada: minutos o segundos de 60 a 99, un dígito escrito en otra
  escritura o cambiado por otro carácter, un separador cambiado, un carácter de menos o de más, o
  una línea vacía;
- una línea de texto cambiada por una Línea_en_Blanco y un bloque sin líneas de texto;
- la línea vacía que cierra un bloque cambiada por una Línea_en_Blanco no vacía;
- bytes que no son UTF-8 válido en cualquier línea.

En otras mutaciones solo el oráculo sabe cuál es la primera línea inválida o si el archivo sigue
siendo SRT válido: quitar la línea vacía que cierra un bloque, que une el bloque con los
siguientes porque sus líneas se leen como texto, y truncar el archivo en cualquier byte. Si el
oráculo no encuentra ninguna línea inválida, `parse_srt` no debe lanzar error, porque cualquier
número que informara sería el de una línea que cumple el formato.

Las mutaciones se aplican al archivo con o sin BOM_UTF8 inicial, con "\\n" o "\\r\\n" como fin de
línea y, cuando la última línea no es vacía, con o sin fin de línea final.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from converter.srt.model import Segment
from converter.srt.parser import SrtParseError, parse_srt
from converter.srt.serializer import serialize_srt
from tests.strategies.segments import BLANK_LINE_CHARS, valid_segment_lists

_BOM_UTF8 = b"\xef\xbb\xbf"
_ASCII_DIGITS = "0123456789"

# Forma de una Línea_de_Tiempos_SRT: cada "0" es un dígito y el resto son separadores fijos
_TIMING_TEMPLATE = "00:00:00,000 --> 00:00:00,000"
_DIGIT_POSITIONS = tuple(i for i, char in enumerate(_TIMING_TEMPLATE) if char == "0")
_SEPARATOR_POSITIONS = tuple(i for i, char in enumerate(_TIMING_TEMPLATE) if char != "0")
# Posición de los minutos y de los segundos del inicio y del fin
_MINUTE_SECOND_POSITIONS = (3, 6, 20, 23)


# --- Oráculo ---------------------------------------------------------------------------------


def _lines_of(data: bytes) -> list[bytes]:
    """Líneas de `data` según design.md, todavía sin decodificar."""
    if data[: len(_BOM_UTF8)] == _BOM_UTF8:
        data = data[len(_BOM_UTF8) :]
    lines: list[bytes] = []
    start = 0
    while start < len(data):
        end = data.find(b"\n", start)
        if end < 0:  # la última línea no termina en "\n"
            end = len(data)
        line = data[start:end]
        # El byte "\r" no forma parte de ninguna secuencia de varios bytes de UTF-8, así que
        # quitarlo antes de decodificar da lo mismo que quitarlo después
        lines.append(line[:-1] if line.endswith(b"\r") else line)
        start = end + 1
    return lines


def _decode(raw: bytes) -> str | None:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_index(line: str, expected: int) -> bool:
    # Entero positivo decimal sin ceros a la izquierda, igual al índice que toca
    if not line or line[0] == "0" or any(char not in _ASCII_DIGITS for char in line):
        return False
    return int(line) == expected


def _is_timestamp(field: str) -> bool:
    # HH:MM:SS,mmm con HH de 00 a 99, MM y SS de 00 a 59 y mmm de 000 a 999
    if len(field) != 12 or (field[2], field[5], field[8]) != (":", ":", ","):
        return False
    hours, minutes, seconds, millis = field[0:2], field[3:5], field[6:8], field[9:12]
    if any(char not in _ASCII_DIGITS for char in hours + minutes + seconds + millis):
        return False
    return int(minutes) <= 59 and int(seconds) <= 59


def _is_timing_line(line: str) -> bool:
    return line[12:17] == " --> " and _is_timestamp(line[:12]) and _is_timestamp(line[17:])


def _is_blank(line: str) -> bool:
    # Línea_en_Blanco: vacía o compuesta solo por espacios o tabulaciones
    return all(char in BLANK_LINE_CHARS for char in line)


def _first_invalid_line(data: bytes) -> int | None:
    """Número de la primera línea que no cumple el formato, o None si `data` es SRT válido."""
    lines = [_decode(raw) for raw in _lines_of(data)]

    def at(number: int) -> str | None:
        # La línea `number`, contando desde 1; None si no es UTF-8 o si falta porque el archivo
        # terminó antes
        return lines[number - 1] if number <= len(lines) else None

    number = expected = 1
    while number <= len(lines):  # cada vuelta lee el Bloque_SRT `expected` desde `number`
        index, timing, first_text = at(number), at(number + 1), at(number + 2)
        if index is None or not _is_index(index, expected):
            return number
        if timing is None or not _is_timing_line(timing):
            return number + 1
        if first_text is None or _is_blank(first_text):
            return number + 2
        number += 3
        # Más líneas de texto, hasta la línea vacía que cierra el bloque
        while (line := at(number)) != "":
            if line is None or _is_blank(line):
                return number
            number += 1
        number += 1
        expected += 1
    return None


# --- Archivo válido y mutaciones ------------------------------------------------------------


@dataclass(frozen=True)
class _Block:
    """Posiciones de un Bloque_SRT en la lista de líneas, contando desde 0."""

    number: int  # índice del bloque
    first: int  # línea del índice
    text_lines: int

    @property
    def timing(self) -> int:
        return self.first + 1

    @property
    def first_text(self) -> int:
        return self.first + 2

    @property
    def closing(self) -> int:  # línea vacía que cierra el bloque
        return self.first + 2 + self.text_lines

    @property
    def end(self) -> int:  # primera línea después del bloque
        return self.closing + 1


def _serialized(segments: list[Segment]) -> tuple[list[str], list[_Block]]:
    """Líneas del archivo que escribe `serialize_srt` y las posiciones de cada Bloque_SRT."""
    *lines, last = serialize_srt(segments).decode("utf-8").split("\n")
    assert last == ""  # cada línea termina en "\n"
    # El texto de una Lista_de_Segmentos_Válida no tiene Líneas_en_Blanco ni "\r": cada segmento
    # es un bloque con una línea de texto por cada línea de su texto
    blocks: list[_Block] = []
    first = 0
    for number, segment in enumerate(segments, start=1):
        blocks.append(_Block(number, first, len(segment.text.split("\n"))))
        first = blocks[-1].end
    assert first == len(lines)
    return lines, blocks


def _encoded(lines: list[str]) -> list[bytes]:
    return [line.encode("utf-8") for line in lines]


def _replaced(lines: list[str], position: int, line: str) -> list[bytes]:
    return _encoded([*lines[:position], line, *lines[position + 1 :]])


def _line_chars_except(excluded: str) -> st.SearchStrategy[str]:
    """Caracteres de una línea que no están en `excluded`: cualquiera menos "\\r" y "\\n", que
    cortan líneas."""
    return st.characters(codec="utf-8", exclude_characters="\r\n" + excluded)


_LINE_CHARS = _line_chars_except("")
_BLANK_CHARS = st.sampled_from(BLANK_LINE_CHARS)
_blank_lines = st.text(_BLANK_CHARS, max_size=4)  # Líneas_en_Blanco, incluida la vacía
_some_blank = st.text(_BLANK_CHARS, min_size=1, max_size=4)  # Líneas_en_Blanco no vacías

# El cero de una escritura con dígitos decimales que no son ASCII. Unicode asigna los dígitos
# decimales de cada escritura seguidos y en orden, del 0 al 9
_other_zeros = st.characters(categories=["Nd"], exclude_characters=_ASCII_DIGITS).map(
    lambda digit: chr(ord(digit) - unicodedata.decimal(digit))
)


def _with_other_digit(text: str, position: int, zero: str) -> str:
    """`text` con su dígito ASCII de `position` escrito con el mismo valor en la escritura de
    `zero`, como "١" en lugar de "1"."""
    return text[:position] + chr(ord(zero) + int(text[position])) + text[position + 1 :]


def _bad_indices(number: int) -> st.SearchStrategy[str]:
    """Líneas distintas del índice `number`: parecidas a él o cualquier otra."""
    index = str(number)
    return st.one_of(
        st.integers(1, 3).map(lambda zeros: "0" * zeros + index),
        st.sampled_from(("+", "-")).map(lambda sign: sign + index),
        # Espacios o tabulaciones antes, después o a ambos lados
        st.tuples(_blank_lines, _some_blank, st.booleans()).map(
            lambda pad: pad[0] + index + pad[1] if pad[2] else pad[1] + index + pad[0]
        ),
        st.builds(
            lambda position, zero: _with_other_digit(index, position, zero),
            st.integers(0, len(index) - 1),
            _other_zeros,
        ),
        st.sampled_from((number - 1, number + 1, 0)).map(str),
        st.integers(0, 10**6).map(str),
        st.just(_TIMING_TEMPLATE),
        st.text(_LINE_CHARS, max_size=12),  # vacía, blanca, de texto, numérica...
    ).filter(
        # Si el archivo no tiene BOM_UTF8, una U+FEFF al principio de la línea 1 se lee como BOM
        lambda line: line.removeprefix("\ufeff") != index
    )


def _bad_timings(timing: str) -> st.SearchStrategy[str]:
    """Variantes de `timing` que no son Líneas_de_Tiempos_SRT."""

    def put(position: int, new: str, length: int = 1) -> str:
        return timing[:position] + new + timing[position + length :]

    return st.one_of(
        # Minutos o segundos de 60 a 99
        st.builds(
            lambda position, value: put(position, f"{value:02d}", 2),
            st.sampled_from(_MINUTE_SECOND_POSITIONS),
            st.integers(60, 99),
        ),
        # Un dígito escrito en otra escritura o cambiado por un carácter que no es dígito ASCII
        st.builds(
            lambda position, zero: _with_other_digit(timing, position, zero),
            st.sampled_from(_DIGIT_POSITIONS),
            _other_zeros,
        ),
        st.builds(put, st.sampled_from(_DIGIT_POSITIONS), _line_chars_except(_ASCII_DIGITS)),
        # Un separador cambiado por otro carácter
        st.sampled_from(_SEPARATOR_POSITIONS).flatmap(
            lambda position: _line_chars_except(timing[position]).map(
                lambda char: put(position, char)
            )
        ),
        # Un carácter de menos o de más: la línea deja de tener 29 caracteres
        st.integers(0, len(timing) - 1).map(lambda position: put(position, "")),
        st.builds(
            lambda position, char: put(position, char, 0),
            st.integers(0, len(timing)),
            _LINE_CHARS,
        ),
        st.just(""),
    )


# Bytes que no son UTF-8 válido aunque los siga cualquier carácter: un byte de continuación
# suelto, secuencias incompletas, una secuencia demasiado larga, un sustituto, un valor mayor que
# U+10FFFF y bytes que UTF-8 no usa
_INVALID_UTF8 = (
    b"\x80",
    b"\xbf",
    b"\xc3",
    b"\xe2\x82",
    b"\xf0\x9f\x98",
    b"\xc0\xaf",
    b"\xed\xa0\x80",
    b"\xf4\x90\x80\x80",
    b"\xfe",
    b"\xff",
)

# Una mutación recibe las líneas del archivo válido y sus bloques, y devuelve las líneas mutadas
# y el número de la primera línea inválida, o None si solo el oráculo puede calcularlo
_MutationResult = tuple[list[bytes], int | None]
_Mutation = Callable[[st.DrawFn, list[str], list[_Block]], _MutationResult]


def _bad_index(draw: st.DrawFn, lines: list[str], blocks: list[_Block]) -> _MutationResult:
    block = draw(st.sampled_from(blocks))
    return _replaced(lines, block.first, draw(_bad_indices(block.number))), block.first + 1


def _dropped_block(draw: st.DrawFn, lines: list[str], blocks: list[_Block]) -> _MutationResult:
    # El bloque que sigue al omitido queda en su lugar con un índice de más
    block = draw(st.sampled_from(blocks[:-1]))
    return _encoded(lines[: block.first] + lines[block.end :]), block.first + 1


def _repeated_block(draw: st.DrawFn, lines: list[str], blocks: list[_Block]) -> _MutationResult:
    # La copia repite el índice del bloque en lugar de tener el siguiente
    block = draw(st.sampled_from(blocks))
    copy = lines[block.first : block.end]
    return _encoded(lines[: block.end] + copy + lines[block.end :]), block.end + 1


def _bad_timing(draw: st.DrawFn, lines: list[str], blocks: list[_Block]) -> _MutationResult:
    block = draw(st.sampled_from(blocks))
    line = draw(_bad_timings(lines[block.timing]))
    return _replaced(lines, block.timing, line), block.timing + 1


def _blank_text_line(draw: st.DrawFn, lines: list[str], blocks: list[_Block]) -> _MutationResult:
    block = draw(st.sampled_from(blocks))
    offset = draw(st.integers(0, block.text_lines - 1))
    # Una línea vacía después de la primera línea de texto cerraría el bloque, así que solo la
    # primera puede quedar vacía; las demás quedan con espacios o tabulaciones
    line = draw(_blank_lines if offset == 0 else _some_blank)
    position = block.first_text + offset
    return _replaced(lines, position, line), position + 1


def _missing_text(draw: st.DrawFn, lines: list[str], blocks: list[_Block]) -> _MutationResult:
    # La línea vacía que cierra el bloque llega en lugar de la primera línea de texto
    block = draw(st.sampled_from(blocks))
    return _encoded(lines[: block.first_text] + lines[block.closing :]), block.first_text + 1


def _blank_closing(draw: st.DrawFn, lines: list[str], blocks: list[_Block]) -> _MutationResult:
    # La línea vacía que cierra el bloque pasa a tener espacios o tabulaciones. Con texto que no
    # es Línea_en_Blanco se leería como otra línea de texto del bloque
    block = draw(st.sampled_from(blocks))
    return _replaced(lines, block.closing, draw(_some_blank)), block.closing + 1


def _invalid_utf8(draw: st.DrawFn, lines: list[str], blocks: list[_Block]) -> _MutationResult:
    position = draw(st.integers(0, len(lines) - 1))
    line = lines[position]
    cut = draw(st.integers(0, len(line)))
    # Los bytes van entre dos caracteres, así que no completan una secuencia de la línea
    mutated = _encoded(lines)
    mutated[position] = (
        line[:cut].encode("utf-8")
        + draw(st.sampled_from(_INVALID_UTF8))
        + line[cut:].encode("utf-8")
    )
    return mutated, position + 1


def _no_closing(draw: st.DrawFn, lines: list[str], blocks: list[_Block]) -> _MutationResult:
    # Las líneas siguientes se leen como texto del bloque, así que el error, si lo hay, queda más
    # adelante: al final del archivo o donde se espera un índice
    block = draw(st.sampled_from(blocks))
    return _encoded(lines[: block.closing] + lines[block.end :]), None


_MUTATIONS: dict[str, _Mutation] = {
    "índice distinto del esperado": _bad_index,
    "bloque omitido": _dropped_block,
    "bloque repetido": _repeated_block,
    "Línea_de_Tiempos_SRT mal formada": _bad_timing,
    "línea de texto en blanco": _blank_text_line,
    "bloque sin texto": _missing_text,
    "línea de cierre en blanco": _blank_closing,
    "UTF-8 inválido": _invalid_utf8,
    "sin línea de cierre": _no_closing,
}
# Mutaciones que necesitan un bloque después del que cambian
_NEED_NEXT_BLOCK = frozenset({"bloque omitido"})
_TRUNCATION = "archivo truncado"


@dataclass(frozen=True)
class _Framing:
    bom: bool
    eol: bytes
    final_eol: bool


_framings = st.builds(_Framing, st.booleans(), st.sampled_from((b"\n", b"\r\n")), st.booleans())


def _framed(lines: list[bytes], framing: _Framing) -> bytes:
    """Une las líneas con o sin BOM_UTF8 y con el fin de línea de `framing`. Solo omite el último
    fin de línea si la última línea no es vacía, porque si no esa línea desaparecería."""
    data = b"".join(line + framing.eol for line in lines)
    if lines and lines[-1] and not framing.final_eol:
        data = data[: -len(framing.eol)]
    return _BOM_UTF8 + data if framing.bom else data


@dataclass(frozen=True)
class _Case:
    kind: str
    data: bytes
    line: int | None  # primera línea inválida según la mutación; None si solo la sabe el oráculo


@st.composite
def _cases(draw: st.DrawFn) -> _Case:
    kind = draw(st.sampled_from([*_MUTATIONS, _TRUNCATION]))
    min_size = 2 if kind in _NEED_NEXT_BLOCK else 1
    lines, blocks = _serialized(draw(valid_segment_lists(min_size=min_size, max_size=6)))
    if kind != _TRUNCATION:
        mutated, line = _MUTATIONS[kind](draw, lines, blocks)
        return _Case(kind, _framed(mutated, draw(_framings)), line)
    # Corta en cualquier byte o justo después de un fin de línea que no es el último
    data = _framed(_encoded(lines), draw(_framings))
    line_ends = [position + 1 for position, byte in enumerate(data) if byte == ord("\n")]
    cut = draw(st.one_of(st.integers(0, len(data) - 1), st.sampled_from(line_ends[:-1])))
    return _Case(kind, data[:cut], None)


# Feature: file-converter, Property 50: El Intérprete_SRT informa la primera línea inválida
@pytest.mark.req("12.6")
@given(case=_cases())
def test_parser_reports_first_invalid_line(case: _Case) -> None:
    expected = _first_invalid_line(case.data)
    if case.line is not None:
        # La mutación vuelve inválida su línea y deja como estaban las anteriores
        assert expected == case.line
    event(case.kind)

    if expected is None:
        event("sigue siendo SRT válido")
        parse_srt(case.data)  # no lanza error
        return
    if expected > len(_lines_of(case.data)):
        event("el archivo termina en medio de un bloque")
    # Req 12.6: informa la primera línea que no cumple el formato y no devuelve segmentos
    with pytest.raises(SrtParseError) as excinfo:
        parse_srt(case.data)
    assert excinfo.value.line_number == expected


# Feature: file-converter, Property 50: El Intérprete_SRT informa la primera línea inválida
@pytest.mark.req("12.6")
@given(segments=valid_segment_lists(), framing=_framings)
def test_unmutated_file_parses(segments: list[Segment], framing: _Framing) -> None:
    # Sin mutación, el archivo y sus variantes con BOM_UTF8 o "\r\n" no tienen líneas inválidas
    lines, _ = _serialized(segments)
    data = _framed(_encoded(lines), framing)
    assert _first_invalid_line(data) is None
    assert len(parse_srt(data)) == len(segments)
