"""Intérprete_SRT (Req 12.6, 12.7).

`parse_srt` acepta solo contenido formado por cero o más Bloques_SRT con índices que empiezan en
1 y aumentan en 1 por bloque. Lee el contenido como UTF-8 y acepta una BOM_UTF8 inicial. Cada
"\\n" termina una línea, de modo que el "\\n" final del archivo no abre una línea vacía más, y a
cada línea se le quita un "\\r" final.

Es una máquina de estados estricta que espera, en este orden:

1. el índice del bloque: exactamente el siguiente entero, en decimal y sin ceros a la izquierda;
2. una Línea_de_Tiempos_SRT `HH:MM:SS,mmm --> HH:MM:SS,mmm` con dígitos ASCII;
3. una o más líneas de texto que no son Líneas_en_Blanco;
4. una línea vacía, que cierra el bloque.

Las líneas de texto no pueden ser vacías ni Líneas_en_Blanco, así que la primera línea vacía
siempre cierra el bloque. La relación entre el inicio y el fin no se valida: la definición de
Bloque_SRT solo exige el formato de la Línea_de_Tiempos_SRT.

Ante la primera línea que no cumple, `parse_srt` lanza `SrtParseError` con su número contando
desde 1 y no devuelve segmentos (Req 12.6). Una línea que no es UTF-8 válido también es una línea
que no cumple. Si el archivo termina en medio de un bloque, la línea que falta es la siguiente a
la última, y ese es el número que se informa.
"""

from __future__ import annotations

import re
from enum import Enum, auto

from converter.srt.model import Segment, is_blank_line

_BOM_UTF8 = b"\xef\xbb\xbf"

# `[0-9]` en lugar de `\d`, que en `str` también acepta dígitos que no son ASCII
_TIMING = re.compile(
    r"([0-9]{2}):([0-5][0-9]):([0-5][0-9]),([0-9]{3})"
    r" --> "
    r"([0-9]{2}):([0-5][0-9]):([0-5][0-9]),([0-9]{3})"
)


class SrtParseError(ValueError):
    """El contenido no es una secuencia de Bloques_SRT; `line_number` cuenta desde 1."""

    def __init__(self, line_number: int, reason: str = "") -> None:
        self.line_number = line_number
        self.reason = reason
        detail = f": {reason}" if reason else ""
        super().__init__(f"SRT inválido en la línea {line_number}{detail}")


class _State(Enum):
    INDEX = auto()  # espera el índice del bloque siguiente o el fin del archivo
    TIMING = auto()  # espera la Línea_de_Tiempos_SRT
    FIRST_TEXT = auto()  # espera la primera línea de texto del bloque
    TEXT = auto()  # espera otra línea de texto o la línea vacía que cierra el bloque


# Motivo cuando el archivo termina en cada estado distinto de INDEX
_MISSING_AT_EOF: dict[_State, str] = {
    _State.TIMING: "falta la Línea_de_Tiempos_SRT",
    _State.FIRST_TEXT: "falta el texto del bloque",
    _State.TEXT: "falta la línea vacía que cierra el bloque",
}


def _to_ms(hours: str, minutes: str, seconds: str, millis: str) -> int:
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)


def _split_lines(data: bytes) -> list[bytes]:
    if data.startswith(_BOM_UTF8):
        data = data[len(_BOM_UTF8) :]
    # El byte 0x0A no aparece dentro de una secuencia multibyte de UTF-8, así que se puede
    # dividir antes de decodificar y ubicar en qué línea está un byte inválido
    lines = data.split(b"\n")
    if lines[-1] == b"":  # el "\n" final termina la última línea; no abre otra
        lines.pop()
    return lines


def parse_srt(data: bytes) -> list[Segment]:
    """Lee un archivo SRT y devuelve sus segmentos en orden; lanza `SrtParseError` (Req 12.6)."""
    lines = _split_lines(data)
    segments: list[Segment] = []
    state = _State.INDEX
    expected_index = 1
    start_ms = end_ms = 0
    text_lines: list[str] = []

    for number, raw in enumerate(lines, start=1):
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise SrtParseError(number, "la línea no es UTF-8 válido") from None
        line = line.removesuffix("\r")

        if state is _State.INDEX:
            if line != str(expected_index):
                raise SrtParseError(number, f"se esperaba el índice {expected_index}")
            state = _State.TIMING
        elif state is _State.TIMING:
            match = _TIMING.fullmatch(line)
            if match is None:
                raise SrtParseError(number, "se esperaba una Línea_de_Tiempos_SRT")
            start_ms = _to_ms(*match.group(1, 2, 3, 4))
            end_ms = _to_ms(*match.group(5, 6, 7, 8))
            state = _State.FIRST_TEXT
        elif state is _State.FIRST_TEXT:
            if is_blank_line(line):
                raise SrtParseError(number, "se esperaba una línea de texto no blanca")
            text_lines = [line]
            state = _State.TEXT
        elif line == "":  # _State.TEXT: la línea vacía cierra el bloque
            segments.append(Segment(start_ms, end_ms, "\n".join(text_lines)))
            expected_index += 1
            state = _State.INDEX
        elif is_blank_line(line):
            raise SrtParseError(number, "una línea de texto no puede ser Línea_en_Blanco")
        else:
            text_lines.append(line)

    if state is not _State.INDEX:
        raise SrtParseError(len(lines) + 1, _MISSING_AT_EOF[state])
    return segments
