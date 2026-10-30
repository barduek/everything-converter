"""Sonda `mpeg_audio`: firma de MP3 (Req 2.1).

Coincide si el archivo empieza con una etiqueta ID3v2 opcional seguida de dos cabeceras MPEG
Audio Layer III válidas y consecutivas: la segunda está justo donde termina el primer cuadro,
según la longitud que indica su cabecera. La etiqueta se salta según su tamaño, aunque termine
después de los primeros 64 KiB (por ejemplo, con una carátula), así que la sonda lee como mucho
dos cabeceras de 4 bytes fuera de `ProbeInput.head`.

Exigir dos cabeceras evita los falsos positivos con un Texto_Windows_1252 que empieza con "ÿû"
(bytes `FF FB`, que también son el comienzo de una cabecera MPEG-1 Layer III).

Una cabecera es válida, como en FFmpeg, si tiene la sincronización de 11 bits, una versión y una
frecuencia de muestreo no reservadas y un índice de tasa de bits distinto de "inválido". El
formato libre (índice 0) no indica la longitud del cuadro y por eso no coincide.
"""

from __future__ import annotations

from converter.detection.probes.base import ProbeInput, register_probe

ID3V2_MARKER = b"ID3"
ID3V2_HEADER_SIZE = 10  # también el tamaño del pie opcional de ID3v2.4
ID3V2_VERSIONS = frozenset({2, 3, 4})
_ID3V2_FOOTER_FLAG = 0x10  # solo en ID3v2.4

FRAME_HEADER_SIZE = 4

# Versión de MPEG (bits 20 y 19 de la cabecera)
_MPEG_2_5, _MPEG_RESERVED, _MPEG_2, _MPEG_1 = 0, 1, 2, 3
_LAYER_III = 1  # bits 18 y 17

# Tasas de bits de Layer III en kbit/s por índice; 0 es formato libre y 15 es inválido
_BITRATES_MPEG1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_BITRATES_MPEG2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)

# Frecuencias de muestreo en Hz por versión; el índice 3 está reservado
_SAMPLE_RATES = {
    _MPEG_1: (44100, 48000, 32000),
    _MPEG_2: (22050, 24000, 16000),
    _MPEG_2_5: (11025, 12000, 8000),
}


def id3v2_size(header: bytes) -> int | None:
    """Tamaño total de la etiqueta ID3v2 que empieza en `header`, con cabecera y pie.

    0 si `header` no empieza con "ID3"; None si empieza con "ID3" pero la cabecera es inválida.
    """
    if not header.startswith(ID3V2_MARKER):
        return 0
    if len(header) < ID3V2_HEADER_SIZE:
        return None
    major, revision, flags = header[3], header[4], header[5]
    size_bytes = header[6:10]
    if major not in ID3V2_VERSIONS or revision == 0xFF or any(b & 0x80 for b in size_bytes):
        return None
    # Entero "syncsafe": 7 bits por byte
    size = 0
    for byte in size_bytes:
        size = (size << 7) | byte
    footer = ID3V2_HEADER_SIZE if major == 4 and flags & _ID3V2_FOOTER_FLAG else 0
    return ID3V2_HEADER_SIZE + size + footer


def layer3_frame_length(header: bytes) -> int | None:
    """Longitud en bytes del cuadro cuya cabecera MPEG Audio Layer III es `header`.

    None si `header` no es una cabecera Layer III válida o si es de formato libre.
    """
    if len(header) < FRAME_HEADER_SIZE:
        return None
    b0, b1, b2 = header[0], header[1], header[2]
    if b0 != 0xFF or b1 & 0xE0 != 0xE0:
        return None  # sin los 11 bits de sincronización
    version = (b1 >> 3) & 0b11
    layer = (b1 >> 1) & 0b11
    if version == _MPEG_RESERVED or layer != _LAYER_III:
        return None
    bitrate_index = b2 >> 4
    rate_index = (b2 >> 2) & 0b11
    padding = (b2 >> 1) & 1
    if rate_index == 3:
        return None
    bitrates = _BITRATES_MPEG1 if version == _MPEG_1 else _BITRATES_MPEG2
    kbps = bitrates[bitrate_index]
    if kbps == 0:
        return None  # formato libre o índice inválido
    sample_rate = _SAMPLE_RATES[version][rate_index]
    # 1152 muestras por cuadro en MPEG-1 y 576 en MPEG-2 y 2.5: 144 o 72 bytes por bit/s y Hz
    coefficient = 144 if version == _MPEG_1 else 72
    return coefficient * kbps * 1000 // sample_rate + padding


@register_probe("mpeg_audio")
def mpeg_audio(source: ProbeInput) -> bool:
    start = id3v2_size(source.head)
    if start is None:
        return False
    first = layer3_frame_length(source.read(start, FRAME_HEADER_SIZE))
    if first is None:
        return False
    return layer3_frame_length(source.read(start + first, FRAME_HEADER_SIZE)) is not None
