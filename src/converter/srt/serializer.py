"""Serializador_SRT (Req 12.4, 12.5, 12.7, 12.8).

`serialize_srt` escribe un Bloque_SRT por cada segmento que tiene al menos una línea que no es
Línea_en_Blanco, en el orden de la lista recibida:

    <índice>\\n
    <inicio> --> <fin>\\n
    <línea de texto>\\n   (una por cada línea no blanca, en su orden original)
    \\n

Los índices empiezan en 1 y aumentan en 1 por bloque escrito, de modo que los segmentos omitidos
no dejan huecos (Req 12.8). La lista puede no ser una Lista_de_Segmentos_Válida, así que los
tiempos se acotan a [0, 359 999 999] ms, el rango de una Línea_de_Tiempos_SRT. La salida está en
UTF-8 sin BOM, con "\\n" como fin de línea, y una lista sin bloques produce 0 bytes.
"""

from __future__ import annotations

from collections.abc import Sequence

from converter.srt.model import Segment, non_blank_lines

# 99:59:59,999: el mayor tiempo que cabe en una Línea_de_Tiempos_SRT
MAX_TIMESTAMP_MS = 359_999_999
TIMING_SEPARATOR = " --> "

_MS_PER_SECOND = 1000
_MS_PER_MINUTE = 60 * _MS_PER_SECOND
_MS_PER_HOUR = 60 * _MS_PER_MINUTE


def format_timestamp(ms: int) -> str:
    """Escribe `ms` como "HH:MM:SS,mmm", con cada campo completado con ceros a la izquierda.

    Lanza `ValueError` si `ms` está fuera de [0, MAX_TIMESTAMP_MS], porque no cabe en una
    Línea_de_Tiempos_SRT; `serialize_srt` acota los tiempos antes de llamarla.
    """
    if not 0 <= ms <= MAX_TIMESTAMP_MS:
        raise ValueError(f"ms debe estar entre 0 y {MAX_TIMESTAMP_MS}: {ms}")
    hours, rest = divmod(ms, _MS_PER_HOUR)
    minutes, rest = divmod(rest, _MS_PER_MINUTE)
    seconds, millis = divmod(rest, _MS_PER_SECOND)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _clamp(ms: int) -> int:
    return min(max(ms, 0), MAX_TIMESTAMP_MS)


def _timing_line(start_ms: int, end_ms: int) -> str:
    # Línea_de_Tiempos_SRT del segmento, con los tiempos acotados al rango de SRT
    start = format_timestamp(_clamp(start_ms))
    end = format_timestamp(_clamp(end_ms))
    return f"{start}{TIMING_SEPARATOR}{end}"


def serialize_srt(segments: Sequence[Segment]) -> bytes:
    """Escribe `segments` en formato SRT (Req 12.4, 12.5, 12.8)."""
    parts: list[str] = []
    index = 0
    for segment in segments:
        lines = non_blank_lines(segment.text)
        if not lines:  # texto vacío o solo con Líneas_en_Blanco: se omite (Req 12.8)
            continue
        index += 1
        parts.append(f"{index}\n{_timing_line(segment.start_ms, segment.end_ms)}\n")
        parts.extend(f"{line}\n" for line in lines)
        parts.append("\n")
    return "".join(parts).encode("utf-8")
