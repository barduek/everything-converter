"""Segmentos_de_Transcripción y Líneas_en_Blanco (Req 12.2 a 12.8).

- `RawSegment` es un segmento tal como lo informa whisper-cli: tiempos en centésimas de segundo
  y texto sin garantías (desordenado, con superposiciones, vacío o con Líneas_en_Blanco).
- `Segment` es un Segmento_de_Transcripción con tiempos en milisegundos. No valida sus campos,
  porque el Serializador_SRT acepta listas que no son Listas_de_Segmentos_Válidas (Req 12.4);
  `normalize_segments` es quien produce las listas válidas (Req 12.2, 12.3).
- `is_blank_line` y `non_blank_lines` aplican la definición de Línea_en_Blanco del glosario:
  una línea vacía o compuesta solo por espacios (U+0020) o tabulaciones (U+0009).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Solo estos tres separan líneas: dividir también en U+2028, U+0085 o avance de página, como hace
# `str.splitlines`, cambiaría el texto en la ida y vuelta SRT, que separa líneas solo por "\n"
_LINE_BREAK = re.compile(r"\r\n|\r|\n")

# Caracteres que puede tener una Línea_en_Blanco según el glosario
BLANK_CHARS = " \t"


@dataclass(frozen=True)
class RawSegment:
    """Segmento de la salida JSON de whisper-cli, sin garantías de orden, rango ni texto."""

    t0_cs: int  # centésimas de segundo
    t1_cs: int
    text: str


@dataclass(frozen=True)
class Segment:
    """Segmento_de_Transcripción: tiempos en milisegundos y texto de una o más líneas."""

    start_ms: int
    end_ms: int
    text: str  # una o más líneas separadas por "\n"


def is_blank_line(line: str) -> bool:
    """Indica si `line`, que no contiene saltos de línea, es una Línea_en_Blanco."""
    return not line.strip(BLANK_CHARS)


def non_blank_lines(text: str) -> list[str]:
    """Divide `text` en líneas por "\\r\\n", "\\r" o "\\n" y descarta las Líneas_en_Blanco.

    Las líneas que quedan conservan su orden y su contenido, incluidos los espacios iniciales y
    finales (Req 12.5).
    """
    return [line for line in _LINE_BREAK.split(text) if not is_blank_line(line)]
