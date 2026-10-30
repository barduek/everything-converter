"""Propiedad 3: la clasificación de texto sigue las definiciones del glosario (Req 2.5).

El oráculo es el predicado de referencia de `tests.strategies.text_bytes`, escrito a partir de
las definiciones de Texto_UTF8_Válido, Texto_Windows_1252, Texto_Decodificado y BOM_UTF8 sin usar
`converter.detection.text`. `classify_text` recibe el contenido cortado en bloques de formas
distintas, porque su resultado no puede depender de dónde caen los cortes de 1 MiB.

Para el contenido que no es Texto_Válido la prueba verifica que `classify_text` lo clasifica como
tal, que `decode_text` no le da un Texto_Decodificado y que el mensaje del Motivo_de_Fallo
`formato_no_reconocido`, con el que el Detector_de_Tipo lo rechaza, menciona UTF-8 y
Windows-1252.
"""

from __future__ import annotations

from itertools import pairwise

import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from converter.detection.text import TextKind, classify_text, decode_text
from converter.messages_es import FailureReason, render_failure
from converter.models import Failure, Stage
from tests.strategies.text_bytes import (
    BOM_UTF8,
    COUNTED_CONTROLS,
    WINDOWS_1252_UNDEFINED,
    arbitrary_bytes,
    decoded_text,
    is_valid_utf8_text,
    is_windows1252_text,
)


def _expected_kind(content: bytes) -> TextKind:
    if is_valid_utf8_text(content):
        return TextKind.UTF8
    if is_windows1252_text(content):
        return TextKind.WINDOWS_1252
    return TextKind.INVALID


@st.composite
def _chunked_contents(draw: st.DrawFn) -> tuple[bytes, list[bytes]]:
    """Un contenido y una forma de cortarlo en bloques que, unidos en orden, lo reconstruyen."""
    content = draw(arbitrary_bytes())
    how = draw(st.sampled_from(("entero", "byte a byte", "tamaño fijo", "cortes al azar")))
    if how == "entero":
        chunks = [content]
    elif how == "byte a byte":
        chunks = [content[index : index + 1] for index in range(len(content))]
    elif how == "tamaño fijo":
        # Como `read_chunks` con bloques de 1 MiB, en escala: el último bloque puede ser menor
        size = draw(st.integers(1, max(1, len(content))))
        chunks = [content[start : start + size] for start in range(0, len(content), size)]
    else:
        # Cortes repetidos dejan bloques vacíos, que no cambian el contenido
        cuts = sorted(draw(st.lists(st.integers(0, len(content)), max_size=12)))
        chunks = [content[start:end] for start, end in pairwise([0, *cuts, len(content)])]
    event(f"bloques: {how}")
    return content, chunks


def _record_case(content: bytes, kind: TextKind) -> None:
    if kind is TextKind.UTF8:
        with_bom = content.startswith(BOM_UTF8)
        event("Texto_UTF8_Válido con BOM_UTF8" if with_bom else "Texto_UTF8_Válido")
    elif kind is TextKind.WINDOWS_1252:
        event("Texto_Windows_1252")
        if WINDOWS_1252_UNDEFINED.intersection(content):
            event("Texto_Windows_1252 con bytes sin carácter asignado")
        if not COUNTED_CONTROLS.isdisjoint(content):
            event("Texto_Windows_1252 con controles contados")
    elif 0x00 in content:
        event("no es Texto_Válido: byte 0x00")
    else:
        event("no es Texto_Válido: 1 % o más de controles")


# Feature: file-converter, Property 3: La clasificación de texto sigue las definiciones del glosario  # noqa: E501
@pytest.mark.req("2.5")
@given(sample=_chunked_contents())
def test_text_classification_follows_glossary(sample: tuple[bytes, list[bytes]]) -> None:
    content, chunks = sample
    assert b"".join(chunks) == content
    expected = _expected_kind(content)
    _record_case(content, expected)

    # Un iterador de una sola pasada, como los bloques que lee el Detector_de_Tipo
    assert classify_text(iter(chunks)) is expected

    if expected is TextKind.INVALID:
        with pytest.raises(ValueError):
            decode_text(content, expected)
        # Req 2.5: el rechazo informa que el formato no es reconocido y las codificaciones
        # admitidas, en un mismo mensaje
        message = render_failure(Failure(FailureReason.UNRECOGNIZED_FORMAT, Stage.DETECTION, {}))
        assert "UTF-8" in message.message and "Windows-1252" in message.message, message
    else:
        # UTF-8 sin la BOM_UTF8 inicial; Windows-1252 con 0x81, 0x8D, 0x8F, 0x90 y 0x9D como el
        # carácter Unicode de igual valor
        assert decode_text(content, expected) == decoded_text(content)
