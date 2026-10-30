"""Sonda de variante `docx_encrypted`: DOCX_Cifrado (Req 2.10, 7.2).

Office guarda un documento cifrado con contraseña de apertura como un archivo compuesto OLE (CFB,
firma `D0 CF 11 E0 A1 B1 1A E1`) con los flujos `EncryptionInfo` (parámetros de cifrado) y
`EncryptedPackage` (el paquete ZIP cifrado), según MS-OFFCRYPTO. La sonda coincide si la
Extensión_del_Nombre es ".docx", sin distinguir mayúsculas, y el contenido es ese archivo OLE
con ambos flujos en la raíz. Sin la extensión ".docx" el archivo no cumple la definición de
DOCX_Cifrado (Req 2.5). Un DOCX con restricción de edición y sin contraseña de apertura es un
ZIP común y no coincide.

La extensión y la firma OLE se comprueban antes de abrir el archivo, así que el resto de los
archivos no se analiza. El archivo OLE se lee con olefile, que carga la cabecera, la FAT y el
directorio, pero no los flujos. olefile no acota esa carga: con un DIFAT circular lee y
concatena el mismo sector millones de veces (un archivo de 1536 bytes ocupa cientos de MB de
memoria en segundos), y con una cadena de directorio circular lee tantos sectores como tiene el
archivo. Por eso olefile lee a través de `_BudgetedReader`, que corta la lectura después de
`MAX_OLE_READ_BYTES` bytes en total. Con sectores de 512 bytes, ese tope alcanza para la FAT y el
directorio de un archivo OLE de hasta unos 120 MiB, muy por encima del Límite_de_Tamaño de
documento predeterminado; un DOCX_Cifrado más grande no coincide y se rechaza como formato no
reconocido.

Un archivo OLE mal formado no coincide: olefile puede lanzar muchos tipos de excepción con un
contenido hostil (incluido `RecursionError` con un árbol de directorio degenerado) y todos
cuentan como "no coincide".
"""

from __future__ import annotations

import os
from typing import BinaryIO

# olefile no publica anotaciones de tipos
import olefile  # type: ignore[import-untyped]

from converter.detection.probes.base import ProbeInput, register_probe

DOCX_EXTENSION = ".docx"
OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
OLE_HEADER_SIZE = 512
ENCRYPTION_STREAMS = ("EncryptionInfo", "EncryptedPackage")

# Bytes que olefile puede leer en total: cabecera, FAT, DIFAT y directorio. Con sectores de
# 512 bytes, la FAT ocupa 1/128 del archivo
MAX_OLE_READ_BYTES = 1024 * 1024


class _ReadBudgetExceeded(Exception):
    """olefile intentó leer más de lo que permite `_BudgetedReader`."""


class _BudgetedReader:
    """Lector de `stream` que entrega como mucho `budget` bytes en total.

    Una lectura que pediría más de lo que queda lanza `_ReadBudgetExceeded` sin leer. Solo
    implementa lo que usa olefile: `read`, `seek`, `tell` y `closed`.
    """

    def __init__(self, stream: BinaryIO, budget: int) -> None:
        self._stream = stream
        self._left = budget

    @property
    def closed(self) -> bool:
        return self._stream.closed

    def read(self, size: int = -1) -> bytes:
        if size < 0 or size > self._left:
            raise _ReadBudgetExceeded
        data = self._stream.read(size)
        self._left -= len(data)
        return data

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._stream.seek(offset, whence)

    def tell(self) -> int:
        return self._stream.tell()


@register_probe("docx_encrypted")
def docx_encrypted(source: ProbeInput) -> bool:
    if source.extension is None or source.extension.lower() != DOCX_EXTENSION:
        return False
    if len(source.head) < OLE_HEADER_SIZE or not source.head.startswith(OLE_SIGNATURE):
        return False
    reader = _BudgetedReader(source.stream, MAX_OLE_READ_BYTES)
    try:
        # Con un objeto de archivo, olefile no lo cierra al salir
        with olefile.OleFileIO(reader) as ole:
            return all(ole.get_type(name) == olefile.STGTY_STREAM for name in ENCRYPTION_STREAMS)
    except Exception:  # contenido hostil: cualquier error de olefile es "no coincide"
        return False
