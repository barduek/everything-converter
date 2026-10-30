"""Sonda `ebml_webm`: firma de WEBM (Req 2.1).

Coincide si el archivo empieza con la cabecera EBML (identificador `1A 45 DF A3`) y su elemento
`DocType` (identificador `42 82`) vale "webm", sin los bytes nulos de relleno que admite EBML.
Matroska usa la misma cabecera con `DocType` "matroska" y no coincide.

Solo se recorren los elementos hijos de la cabecera EBML que están dentro de `ProbeInput.head`;
una cabecera real ocupa unas decenas de bytes. Cada paso avanza al menos dos bytes, así que el
recorrido termina.
"""

from __future__ import annotations

from converter.detection.probes.base import ProbeInput, register_probe

EBML_HEADER_ID = 0x1A45DFA3
DOCTYPE_ID = 0x4282
WEBM_DOCTYPE = b"webm"

_MAX_ID_LENGTH = 4  # EBMLMaxIDLength predeterminado
_MAX_SIZE_LENGTH = 8  # EBMLMaxSizeLength predeterminado


def _vint_length(first: int, max_length: int) -> int | None:
    """Longitud de un entero de longitud variable según su primer byte (ceros iniciales + 1)."""
    if first == 0:
        return None
    length = 9 - first.bit_length()
    return length if length <= max_length else None


def _read_id(data: bytes, position: int) -> tuple[int, int] | None:
    """Identificador de elemento (con su marcador de longitud) y la posición siguiente."""
    if position >= len(data):
        return None
    length = _vint_length(data[position], _MAX_ID_LENGTH)
    if length is None or position + length > len(data):
        return None
    end = position + length
    return int.from_bytes(data[position:end], "big"), end


def _read_size(data: bytes, position: int) -> tuple[int | None, int] | None:
    """Tamaño de datos de un elemento (None si es desconocido) y la posición siguiente."""
    if position >= len(data):
        return None
    length = _vint_length(data[position], _MAX_SIZE_LENGTH)
    if length is None or position + length > len(data):
        return None
    end = position + length
    value_mask = (1 << (7 * length)) - 1
    value = int.from_bytes(data[position:end], "big") & value_mask
    # Todos los bits de valor en 1 indican un tamaño desconocido
    return (None if value == value_mask else value), end


def ebml_doctype(head: bytes) -> bytes | None:
    """Valor de `DocType` de la cabecera EBML al principio de `head`, sin el relleno nulo.

    None si `head` no empieza con una cabecera EBML o si no se encuentra un `DocType` legible.
    """
    element = _read_id(head, 0)
    if element is None or element[0] != EBML_HEADER_ID:
        return None
    header_size = _read_size(head, element[1])
    if header_size is None:
        return None
    body_size, position = header_size
    end = len(head) if body_size is None else min(position + body_size, len(head))
    while position < end:
        child = _read_id(head, position)
        if child is None:
            return None
        child_id, position = child
        child_size = _read_size(head, position)
        if child_size is None:
            return None
        size, position = child_size
        if size is None or position + size > end:
            return None
        if child_id == DOCTYPE_ID:
            return head[position : position + size].rstrip(b"\x00")
        position += size
    return None


@register_probe("ebml_webm")
def ebml_webm(source: ProbeInput) -> bool:
    return ebml_doctype(source.head) == WEBM_DOCTYPE
