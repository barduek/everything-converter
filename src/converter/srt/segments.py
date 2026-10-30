"""Normalización de segmentos y TXT de transcripción (Req 11.1, 11.7, 12.2, 12.3, 12.10).

`normalize_segments` convierte los segmentos que informa whisper-cli (`RawSegment`, sin
garantías) en una Lista_de_Segmentos_Válida con todos los tiempos entre 0 y la duración del
Audio_a_Transcribir. Recorre los segmentos ordenados por tiempo de inicio y, para cada uno:

1. divide el texto en líneas y elimina las Líneas_en_Blanco; si no queda ninguna línea, descarta
   el segmento antes de mirar sus tiempos, para que un segmento sin texto no le quite tiempo al
   siguiente al recortar superposiciones;
2. convierte las centésimas de segundo a milisegundos y acota cada tiempo a [0, duración];
3. recorta la superposición moviendo el inicio al fin del último segmento conservado;
4. descarta el segmento si queda sin duración positiva (fin menor o igual al inicio), como pasa
   con un segmento invertido, fuera de la duración o contenido por completo en el anterior.

El orden entre segmentos con el mismo inicio es el de whisper-cli, porque el ordenamiento es
estable. Las líneas que quedan conservan su orden y su contenido, incluidos los espacios iniciales
y finales, y se unen con "\\n", así que la lista vuelve igual de `parse_srt(serialize_srt(...))`
(Req 12.7) mientras la duración no supere 359 999 999 ms; la Duración_Máxima_de_Transcripción
admite como máximo 5999 minutos, que caben en ese rango.

`render_transcript_txt` escribe el TXT de transcripción: una línea por segmento, sin marcas de
tiempo, en el orden de la lista (Req 11.1). Sin palabras reconocidas, es decir, sin segmentos con
alguna línea que no es Línea_en_Blanco, el resultado tiene 0 bytes, igual que el SRT que escribe
`serialize_srt` para la misma lista (Req 11.7, 12.10).
"""

from __future__ import annotations

from collections.abc import Iterable

from converter.srt.model import BLANK_CHARS, RawSegment, Segment, non_blank_lines

_MS_PER_CS = 10


def _clamp(ms: int, duration_ms: int) -> int:
    return min(max(ms, 0), duration_ms)


def normalize_segments(raw: Iterable[RawSegment], duration_ms: int) -> list[Segment]:
    """Devuelve una Lista_de_Segmentos_Válida dentro de [0, `duration_ms`] (Req 12.2, 12.3).

    Una duración negativa se trata como 0, y entonces ningún segmento tiene duración positiva.
    """
    duration = max(duration_ms, 0)
    segments: list[Segment] = []
    previous_end = 0
    for item in sorted(raw, key=lambda segment: segment.t0_cs):
        lines = non_blank_lines(item.text)
        if not lines:  # sin texto: no ocupa tiempo ni recorta al siguiente
            continue
        start = max(_clamp(item.t0_cs * _MS_PER_CS, duration), previous_end)
        end = _clamp(item.t1_cs * _MS_PER_CS, duration)
        if end <= start:  # sin duración positiva después de acotar y recortar
            continue
        segments.append(Segment(start, end, "\n".join(lines)))
        previous_end = end
    return segments


def render_transcript_txt(segments: Iterable[Segment]) -> bytes:
    """Escribe el TXT de transcripción en UTF-8 sin BOM, con "\\n" como fin de línea (Req 11.1).

    Cada segmento con alguna línea que no es Línea_en_Blanco ocupa una línea: sus líneas no
    blancas, sin los espacios y tabulaciones de los extremos (como el espacio inicial con que
    whisper-cli escribe cada segmento), unidas por un espacio. Los demás segmentos se omiten, de
    modo que una lista sin palabras produce 0 bytes (Req 11.7, 12.10).
    """
    lines: list[str] = []
    for segment in segments:
        parts = [line.strip(BLANK_CHARS) for line in non_blank_lines(segment.text)]
        if parts:
            lines.append(" ".join(parts) + "\n")
    return "".join(lines).encode("utf-8")
