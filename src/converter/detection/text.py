"""Clasificación de texto y Texto_Decodificado (Req 2.2 a 2.5, 2.12).

- `classify_text` recorre el contenido una sola vez, bloque por bloque y sin juntarlo en
  memoria, y dice si es Texto_UTF8_Válido, Texto_Windows_1252 o ninguno de los dos. El
  Detector_de_Tipo le pasa el archivo en bloques de 1 MiB con `read_chunks`.
- `decode_text` devuelve el Texto_Decodificado: en UTF-8 sin la BOM_UTF8 inicial y en
  Windows-1252 con los bytes sin carácter asignado convertidos en el carácter Unicode de igual
  valor. Los convertidores de Formatos_de_Texto trabajan sobre ese texto, por lo que ignoran la
  codificación que declare el documento, como `<meta charset>` en HTML (Req 2.12).
"""

from __future__ import annotations

import codecs
from collections.abc import Iterable, Iterator
from enum import Enum
from typing import BinaryIO

CHUNK_SIZE = 1024 * 1024  # 1 MiB por bloque leído del archivo

BOM_UTF8: bytes = codecs.BOM_UTF8  # EF BB BF

# Bytes de control que cuentan para Texto_Windows_1252: 0x01 a 0x1F y 0x7F, salvo tabulación
# (0x09), salto de línea (0x0A), avance de página (0x0C) y retorno de carro (0x0D)
CONTROL_BYTES: bytes = bytes(b for b in (*range(0x01, 0x20), 0x7F) if b not in b"\t\n\f\r")

# Los controles deben ser menos de este porcentaje del total de bytes
MAX_CONTROL_PERCENT = 1

# Bytes sin carácter asignado en Windows-1252; se interpretan como el carácter Unicode de igual
# valor (U+0081, U+008D, U+008F, U+0090 y U+009D)
WINDOWS_1252_UNDEFINED: frozenset[int] = frozenset({0x81, 0x8D, 0x8F, 0x90, 0x9D})

_UNDEFINED_HANDLER = "converter.windows1252_undefined"


class TextKind(Enum):
    UTF8 = "utf8"  # Texto_UTF8_Válido (con o sin BOM_UTF8)
    WINDOWS_1252 = "windows1252"  # Texto_Windows_1252
    INVALID = "invalid"  # no es Texto_Válido


def read_chunks(stream: BinaryIO, size: int = CHUNK_SIZE) -> Iterator[bytes]:
    """Lee `stream` hasta el final en bloques de `size` bytes (el último puede ser menor)."""
    if size < 1:
        raise ValueError(f"size debe ser 1 o mayor: {size}")
    while chunk := stream.read(size):
        yield chunk


def classify_text(chunks: Iterable[bytes]) -> TextKind:
    """Clasifica el contenido formado por `chunks`, en orden, según el glosario.

    Texto_UTF8_Válido: el decodificador UTF-8 incremental estricto acepta todos los bloques y el
    final sin secuencias inválidas ni incompletas, y no aparece U+0000. Si no lo es,
    Texto_Windows_1252: no aparece el byte 0x00 y los `CONTROL_BYTES` son menos del 1 % del total.
    El resultado no depende de dónde se corten los bloques.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    is_utf8 = True
    total = 0
    controls = 0
    for chunk in chunks:
        # U+0000 solo se codifica en UTF-8 con el byte 0x00 (el decodificador estricto rechaza
        # las formas sobrelargas como C0 80), y ese byte también excluye Texto_Windows_1252
        if b"\x00" in chunk:
            return TextKind.INVALID
        total += len(chunk)
        controls += len(chunk) - len(chunk.translate(None, CONTROL_BYTES))
        if is_utf8:
            try:
                decoder.decode(chunk)
            except UnicodeDecodeError:
                is_utf8 = False
    if is_utf8:
        try:
            # Una secuencia multibyte cortada al final del contenido es inválida
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            is_utf8 = False
    if is_utf8:
        return TextKind.UTF8
    if controls * 100 < total * MAX_CONTROL_PERCENT:
        return TextKind.WINDOWS_1252
    return TextKind.INVALID


def decode_text(data: bytes, kind: TextKind) -> str:
    """Devuelve el Texto_Decodificado de `data`, ya clasificado como `kind` por `classify_text`.

    Lanza `ValueError` si `kind` es `TextKind.INVALID`, porque ese contenido no es Texto_Válido.
    """
    if kind is TextKind.UTF8:
        # Solo la BOM_UTF8 inicial; una U+FEFF posterior es parte del texto
        return data.removeprefix(BOM_UTF8).decode("utf-8", errors="strict")
    if kind is TextKind.WINDOWS_1252:
        # La BOM_UTF8 no se quita: en Windows-1252 esos bytes son "ï»¿"
        return data.decode("cp1252", errors=_UNDEFINED_HANDLER)
    raise ValueError("el contenido no es Texto_Válido y no tiene Texto_Decodificado")


def _undefined_as_same_value(error: UnicodeError) -> tuple[str, int]:
    """Manejador de errores del códec cp1252 para los `WINDOWS_1252_UNDEFINED`.

    Cualquier otro error se vuelve a lanzar, para no ocultar un uso indebido.
    """
    if not isinstance(error, UnicodeDecodeError):
        raise error
    undefined = error.object[error.start : error.end]
    if not undefined or any(b not in WINDOWS_1252_UNDEFINED for b in undefined):
        raise error
    return "".join(map(chr, undefined)), error.end


codecs.register_error(_UNDEFINED_HANDLER, _undefined_as_same_value)
