"""Pruebas de src/converter/detection/text.py: clasificación de texto según el glosario y
Texto_Decodificado (Req 2.2 a 2.5, 2.12).

Los bytes esperados se escriben a mano a partir de las definiciones de Texto_UTF8_Válido,
Texto_Windows_1252, Texto_Decodificado y BOM_UTF8, no a partir de las constantes del módulo.
"""

from __future__ import annotations

import io

import pytest

from converter.detection.text import (
    CHUNK_SIZE,
    TextKind,
    classify_text,
    decode_text,
    read_chunks,
)

_BOM = b"\xef\xbb\xbf"
_UNDEFINED = (0x81, 0x8D, 0x8F, 0x90, 0x9D)
# 0x01-0x1F y 0x7F salvo tabulación, salto de línea, avance de página y retorno de carro
_COUNTED_CONTROLS = [b for b in (*range(0x01, 0x20), 0x7F) if b not in (0x09, 0x0A, 0x0C, 0x0D)]


def _classify(data: bytes) -> TextKind:
    return classify_text([data])


# --- classify_text: Texto_UTF8_Válido ---


@pytest.mark.req("2.2", "2.3")
@pytest.mark.parametrize(
    "data",
    [
        b"hola mundo\n",
        "año, pingüino, canción: €".encode(),
        "日本語 и кириллица 🙂".encode(),
        b"",  # se decodifica completo sin secuencias inválidas
    ],
)
def test_valid_utf8_is_utf8(data: bytes) -> None:
    assert _classify(data) is TextKind.UTF8


@pytest.mark.req("2.4")
def test_bom_does_not_change_the_classification() -> None:
    content = "título\r\nlínea".encode()
    assert _classify(_BOM + content) is _classify(content) is TextKind.UTF8
    assert _classify(_BOM) is TextKind.UTF8


@pytest.mark.req("2.2")
def test_controls_do_not_restrict_utf8() -> None:
    # La regla del 1 % es solo de Texto_Windows_1252
    assert _classify(bytes(_COUNTED_CONTROLS)) is TextKind.UTF8


@pytest.mark.req("2.2", "2.4")
def test_chunk_boundaries_do_not_change_the_result() -> None:
    data = _BOM + "ñandú 🙂 €".encode()
    for cut in range(len(data) + 1):
        assert classify_text([data[:cut], data[cut:]]) is TextKind.UTF8, cut
    assert classify_text(bytes([b]) for b in data) is TextKind.UTF8


# --- classify_text: Texto_Windows_1252 ---


@pytest.mark.req("2.2", "2.5")
@pytest.mark.parametrize(
    "data",
    [
        b"a\xf1o de la canci\xf3n",  # Latin-1: "año de la canción"
        b"\x93comillas\x94 y \x80",  # caracteres propios de Windows-1252
        b"caf\xc3",  # secuencia UTF-8 cortada al final
        b"\xed\xa0\x80",  # sustituto codificado: UTF-8 inválido
        b"\xc0\x80",  # forma sobrelarga de U+0000: UTF-8 inválido y sin el byte 0x00
        bytes(_UNDEFINED),
    ],
)
def test_invalid_utf8_without_controls_is_windows_1252(data: bytes) -> None:
    assert _classify(data) is TextKind.WINDOWS_1252


@pytest.mark.req("2.5")
def test_tab_newline_form_feed_and_carriage_return_are_not_counted() -> None:
    assert _classify(b"\xe9" + b"\t\n\x0c\r" * 100) is TextKind.WINDOWS_1252


@pytest.mark.req("2.5")
@pytest.mark.parametrize("control", _COUNTED_CONTROLS)
def test_one_percent_of_controls_is_invalid(control: int) -> None:
    # 1 control en 100 bytes es el 1 %: no es menos del 1 %
    assert _classify(b"\xe9" + bytes([control]) + b"a" * 98) is TextKind.INVALID
    # 1 control en 101 bytes queda por debajo del 1 %
    assert _classify(b"\xe9" + bytes([control]) + b"a" * 99) is TextKind.WINDOWS_1252


@pytest.mark.req("2.5")
def test_controls_are_counted_across_chunks() -> None:
    chunks = [b"\xe9" + b"a" * 49, b"\x01" + b"a" * 49]  # 1 control en 100 bytes
    assert classify_text(chunks) is TextKind.INVALID
    assert classify_text([*chunks, b"a"]) is TextKind.WINDOWS_1252


# --- classify_text: no es Texto_Válido ---


@pytest.mark.req("2.5")
@pytest.mark.parametrize(
    "data",
    [
        b"\x00",
        b"texto UTF-8 v\xc3\xa1lido con nulo\x00",
        b"a\xf1o con nulo\x00" + b"a" * 1000,
    ],
)
def test_null_byte_is_invalid(data: bytes) -> None:
    assert _classify(data) is TextKind.INVALID


@pytest.mark.req("2.5")
def test_null_byte_in_a_later_chunk_is_invalid() -> None:
    assert classify_text([b"hola " * 1000, b"\x00"]) is TextKind.INVALID


# --- read_chunks ---


def test_read_chunks_uses_one_mib_blocks() -> None:
    assert CHUNK_SIZE == 1024 * 1024
    data = b"a" * (2 * CHUNK_SIZE + CHUNK_SIZE // 2)
    chunks = list(read_chunks(io.BytesIO(data)))
    assert [len(c) for c in chunks] == [CHUNK_SIZE, CHUNK_SIZE, CHUNK_SIZE // 2]
    assert b"".join(chunks) == data
    assert list(read_chunks(io.BytesIO(b""))) == []


@pytest.mark.req("2.5")
def test_classify_a_stream_read_in_chunks() -> None:
    data = "ñ".encode() * CHUNK_SIZE  # cada bloque corta una secuencia de 2 bytes al medio
    assert classify_text(read_chunks(io.BytesIO(data), CHUNK_SIZE + 1)) is TextKind.UTF8


# --- decode_text ---


@pytest.mark.req("2.4", "2.12")
def test_decode_utf8_removes_only_the_initial_bom() -> None:
    assert decode_text(_BOM + "canción".encode(), TextKind.UTF8) == "canción"
    assert decode_text("canción".encode(), TextKind.UTF8) == "canción"
    assert decode_text(_BOM + _BOM + b"x", TextKind.UTF8) == "\ufeffx"
    assert decode_text(b"a" + _BOM, TextKind.UTF8) == "a\ufeff"


@pytest.mark.req("2.12")
def test_decode_windows_1252() -> None:
    assert decode_text(b"a\xf1o \x93ok\x94 \x80", TextKind.WINDOWS_1252) == "año “ok” €"
    # La BOM_UTF8 solo se quita en UTF-8
    assert decode_text(_BOM + b"\xe9", TextKind.WINDOWS_1252) == "ï»¿é"


@pytest.mark.req("2.12")
def test_decode_windows_1252_undefined_bytes_as_same_value() -> None:
    data = b"x".join(bytes([b]) for b in _UNDEFINED)
    expected = "x".join(chr(b) for b in _UNDEFINED)
    assert decode_text(data, TextKind.WINDOWS_1252) == expected


@pytest.mark.req("2.12")
@pytest.mark.parametrize("byte", range(0x01, 0x100))
def test_decode_windows_1252_every_byte_gives_one_character(byte: int) -> None:
    expected = chr(byte) if byte in _UNDEFINED else bytes([byte]).decode("cp1252")
    assert decode_text(bytes([byte]), TextKind.WINDOWS_1252) == expected


@pytest.mark.req("2.12")
def test_decode_ignores_the_charset_declared_by_html() -> None:
    data = '<meta charset="iso-8859-1"><p>año</p>'.encode()
    assert _classify(data) is TextKind.UTF8
    assert decode_text(data, TextKind.UTF8) == '<meta charset="iso-8859-1"><p>año</p>'


def test_decode_invalid_raises() -> None:
    with pytest.raises(ValueError):
        decode_text(b"\x00", TextKind.INVALID)


def test_undefined_handler_does_not_hide_other_codec_errors() -> None:
    # El manejador registrado solo acepta los bytes sin carácter asignado de Windows-1252
    with pytest.raises(UnicodeDecodeError):
        b"\xff".decode("ascii", errors="converter.windows1252_undefined")
