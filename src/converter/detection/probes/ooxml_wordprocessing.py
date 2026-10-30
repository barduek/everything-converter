"""Sonda `ooxml_wordprocessing`: firma de DOCX (Req 2.1).

Coincide si el archivo es un paquete ZIP (empieza con la cabecera local `PK\\x03\\x04`) cuyo
`[Content_Types].xml` declara, con un elemento `Override`, la parte principal de un documento de
Word: `application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml`.
Ese tipo es el mismo en OOXML de transición y estricto. Las plantillas (`.dotx`) y los
documentos con macros (`.docm`) declaran otros tipos y no coinciden.

La sonda corre en el proceso principal sobre un archivo no confiable, así que todo está acotado:

- El directorio central se ubica con `zipcheck.read_central_directory` (registro de fin, ZIP64 y
  datos antepuestos, como `zipfile`) y se recorren como mucho `MAX_SCANNED_ENTRIES` registros,
  sin cargarlos todos como hace `zipfile.ZipFile`. Un paquete con más registros antes de
  `[Content_Types].xml` supera además el máximo de Entradas_ZIP de los Límites_de_Descompresión.
- Los nombres se comparan sin distinguir mayúsculas, como pide OPC. La cabecera local debe tener
  el mismo nombre que el registro del directorio central.
- De `[Content_Types].xml` se leen como mucho `MAX_CONTENT_TYPES_BYTES` bytes comprimidos y se
  descomprimen como mucho `MAX_CONTENT_TYPES_BYTES` bytes; si produce más, no coincide. Solo se
  admiten los métodos sin compresión y deflate, sin cifrado. No se lee ninguna otra entrada.
- El XML se analiza con lxml sin resolver entidades, sin red, sin cargar DTD y con los límites
  de libxml2 (`huge_tree=False`). Un XML con declaración DOCTYPE no coincide: OPC no la admite.

Un paquete dañado en otras entradas coincide igual; los Límites_de_Descompresión y el daño los
comprueba después el inspector `docx`, en un subproceso.
"""

from __future__ import annotations

import struct
import zlib
from itertools import islice
from typing import Any

# lxml no publica anotaciones de tipos; `etree` queda como Any
from lxml import etree  # type: ignore[import-untyped]

from converter.detection.probes.base import ProbeInput, register_probe
from converter.detection.zipcheck import (
    DEFLATED,
    STORED,
    ZipEntryReader,
    ZipFormatError,
    read_central_directory,
)

ZIP_LOCAL_SIGNATURE = b"PK\x03\x04"
CONTENT_TYPES_NAME = b"[content_types].xml"  # en minúsculas; se compara sin distinguirlas
CONTENT_TYPES_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/content-types"
WORDPROCESSING_MAIN = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)

# Registros del directorio central que se recorren buscando `[Content_Types].xml`: uno más que
# el máximo de Entradas_ZIP de los Límites_de_Descompresión (Req 7.7)
MAX_SCANNED_ENTRIES = 10_001
# Tope de bytes comprimidos y descomprimidos de `[Content_Types].xml`. Uno real ocupa pocos KB:
# cada `Override` tiene unos 150 bytes
MAX_CONTENT_TYPES_BYTES = 1024 * 1024

_LOCAL = struct.Struct("<4s5H3L2H")  # cabecera local de APPNOTE.TXT, 30 bytes
_ENCRYPTED_FLAG = 0x0001  # bit 0 de los indicadores generales

_TYPES_TAG = f"{{{CONTENT_TYPES_NAMESPACE}}}Types"
_OVERRIDE_TAG = f"{{{CONTENT_TYPES_NAMESPACE}}}Override"


def _xml_parser() -> Any:
    # Un analizador por llamada: los de lxml no se comparten entre hilos
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


def declares_wordprocessing_main(xml: bytes) -> bool:
    """True si `xml` es un `[Content_Types].xml` con un `Override` del tipo `WORDPROCESSING_MAIN`.

    Los tipos se comparan sin distinguir mayúsculas ASCII, como pide OPC.
    """
    try:
        root = etree.fromstring(xml, _xml_parser())
    except (etree.LxmlError, ValueError):
        return False
    if root.getroottree().docinfo.doctype or root.tag != _TYPES_TAG:
        return False
    for override in root.iterchildren(_OVERRIDE_TAG):
        content_type = override.get("ContentType")
        if (
            isinstance(content_type, str)
            and content_type.isascii()
            and content_type.lower() == WORDPROCESSING_MAIN
        ):
            return True
    return False


def _data_offset(source: ProbeInput, entry: ZipEntryReader) -> int | None:
    """Posición de los datos de `entry` si su cabecera local es `[Content_Types].xml`."""
    header = source.read(entry.header_offset, _LOCAL.size)
    if len(header) < _LOCAL.size:
        return None
    signature, *_, name_length, extra_length = _LOCAL.unpack(header)
    if signature != ZIP_LOCAL_SIGNATURE or name_length != entry.name_length:
        return None
    name = source.read(entry.header_offset + _LOCAL.size, name_length)
    # El CRC del nombre confirma que la cabecera local nombra la misma entrada que el registro
    if len(name) < name_length or zlib.crc32(name) != entry.name_crc:
        return None
    if name.lower() != CONTENT_TYPES_NAME:
        return None
    return entry.header_offset + _LOCAL.size + int(name_length) + int(extra_length)


def _entry_data(source: ProbeInput, entry: ZipEntryReader, data_offset: int) -> bytes | None:
    """Contenido descomprimido de `entry`, o None si no se puede leer dentro de los topes."""
    if entry.flags & _ENCRYPTED_FLAG or entry.compressed_size > MAX_CONTENT_TYPES_BYTES:
        return None
    raw = source.read(data_offset, entry.compressed_size)
    if len(raw) < entry.compressed_size:
        return None  # el archivo termina antes que los datos
    if entry.method == STORED:
        return raw
    if entry.method != DEFLATED:
        return None
    try:
        # La salida se corta en el tope más uno, así que una bomba no se expande en memoria
        data = zlib.decompressobj(-15).decompress(raw, MAX_CONTENT_TYPES_BYTES + 1)
    except zlib.error:
        return None
    return data if len(data) <= MAX_CONTENT_TYPES_BYTES else None


def read_content_types(source: ProbeInput) -> bytes | None:
    """Contenido de `[Content_Types].xml` del paquete ZIP de `source`, dentro de los topes.

    None si no es un paquete ZIP legible, si la entrada no está entre los primeros
    `MAX_SCANNED_ENTRIES` registros o si no se puede leer dentro de los topes.
    """
    try:
        directory = read_central_directory(source.stream)
        for entry in islice(directory.entries(), MAX_SCANNED_ENTRIES):
            if entry.name_length != len(CONTENT_TYPES_NAME):
                continue
            data_offset = _data_offset(source, entry)
            if data_offset is not None:
                return _entry_data(source, entry, data_offset)
    except ZipFormatError:
        return None
    return None


@register_probe("ooxml_wordprocessing")
def ooxml_wordprocessing(source: ProbeInput) -> bool:
    if not source.head.startswith(ZIP_LOCAL_SIGNATURE):
        return False
    content_types = read_content_types(source)
    return content_types is not None and declares_wordprocessing_main(content_types)
