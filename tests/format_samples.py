"""Contenidos mínimos con la Firma_de_Formato de cada Formato, construidos en código.

Sirven para las pruebas de firmas, sondas y detección. Cada función devuelve los bytes de un
archivo; los que necesitan una estructura (MP3, MP4, WEBM, DOCX, OLE) la arman campo por campo
con las posiciones de su especificación, sin herramientas externas.
"""

from __future__ import annotations

import io
import struct
import zipfile

# --- Firmas por bytes ---

PNG = bytes.fromhex("89504E470D0A1A0A") + bytes.fromhex("0000000D49484452") + bytes(64)
JPG = bytes.fromhex("FFD8FFE000104A464946") + bytes(64)
WEBP = b"RIFF" + (64).to_bytes(4, "little") + b"WEBPVP8 " + bytes(64)
WAV = b"RIFF" + (64).to_bytes(4, "little") + b"WAVEfmt " + bytes(64)
OGG = b"OggS" + bytes(64)
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n"


# --- MP3 ---

# Cabeceras MPEG Audio Layer III y la longitud de su cuadro
MPEG1_128K = bytes.fromhex("FFFB9000")  # MPEG-1, 128 kbit/s, 44100 Hz: 144*128000/44100 = 417
MPEG1_128K_LENGTH = 417
MPEG1_128K_PADDED = bytes.fromhex("FFFB9200")  # igual, con el byte de relleno: 418
MPEG2_64K = bytes.fromhex("FFF38000")  # MPEG-2, 64 kbit/s, 22050 Hz: 72*64000/22050 = 208
MPEG2_64K_LENGTH = 208
MPEG25_8K = bytes.fromhex("FFE31000")  # MPEG-2.5, 8 kbit/s, 11025 Hz: 72*8000/11025 = 52
MPEG25_8K_LENGTH = 52


def mpeg_frame(header: bytes, length: int) -> bytes:
    """Cuadro de `length` bytes con `header` y datos nulos."""
    return header + bytes(length - len(header))


def id3v2_tag(body_size: int, *, major: int = 4, flags: int = 0) -> bytes:
    """Etiqueta ID3v2 con `body_size` bytes después de la cabecera (y el pie si `flags` lo pide)."""
    syncsafe = bytes((body_size >> shift) & 0x7F for shift in (21, 14, 7, 0))
    footer = b"3DI" + bytes([major, 0, flags]) + syncsafe if major == 4 and flags & 0x10 else b""
    return b"ID3" + bytes([major, 0, flags]) + syncsafe + bytes(body_size) + footer


def mp3(*, tag: bytes = b"", frames: int = 3) -> bytes:
    """MP3 con `tag` delante y `frames` cuadros MPEG-1 Layer III de 128 kbit/s."""
    return tag + mpeg_frame(MPEG1_128K, MPEG1_128K_LENGTH) * frames


# --- MP4 ---


def ftyp(major: bytes, *compatible: bytes, minor_version: int = 0x200) -> bytes:
    """Caja `ftyp` con tamaño de 32 bits."""
    body = major + minor_version.to_bytes(4, "big") + b"".join(compatible)
    return (8 + len(body)).to_bytes(4, "big") + b"ftyp" + body


def mp4(major: bytes = b"isom", *compatible: bytes) -> bytes:
    """`ftyp` seguida de una caja `free` y una `mdat` vacías."""
    if not compatible:
        compatible = (b"isom", b"iso2", b"avc1", b"mp41")
    return ftyp(major, *compatible) + b"\x00\x00\x00\x08free" + b"\x00\x00\x00\x08mdat"


# --- WEBM ---


def ebml_element(element_id: bytes, data: bytes) -> bytes:
    """Elemento EBML con tamaño de 1 byte (datos de hasta 126 bytes)."""
    assert len(data) < 0x7F
    return element_id + bytes([0x80 | len(data)]) + data


def ebml_header(doctype: bytes = b"webm", *, size: bytes | None = None) -> bytes:
    """Cabecera EBML con los elementos que escribe un muxer y `DocType` igual a `doctype`.

    `size` reemplaza la codificación del tamaño de la cabecera (por ejemplo, 8 bytes o
    desconocido).
    """
    children = b"".join(
        (
            ebml_element(b"\x42\x86", b"\x01"),  # EBMLVersion
            ebml_element(b"\x42\xf7", b"\x01"),  # EBMLReadVersion
            ebml_element(b"\x42\xf2", b"\x04"),  # EBMLMaxIDLength
            ebml_element(b"\x42\xf3", b"\x08"),  # EBMLMaxSizeLength
            ebml_element(b"\x42\x82", doctype),  # DocType
            ebml_element(b"\x42\x87", b"\x04"),  # DocTypeVersion
            ebml_element(b"\x42\x85", b"\x02"),  # DocTypeReadVersion
        )
    )
    encoded_size = bytes([0x80 | len(children)]) if size is None else size
    return b"\x1a\x45\xdf\xa3" + encoded_size + children


def webm(doctype: bytes = b"webm") -> bytes:
    """Cabecera EBML seguida del comienzo de un Segment de tamaño desconocido."""
    return ebml_header(doctype) + bytes.fromhex("18538067") + b"\x01\xff\xff\xff\xff\xff\xff\xff"


# --- DOCX ---

CONTENT_TYPES_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/content-types"
WORDPROCESSING_MAIN = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
SPREADSHEET_MAIN = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
WORD_TEMPLATE_MAIN = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"
)
WORD_MACRO_MAIN = "application/vnd.ms-word.document.macroEnabled.main+xml"


def content_types(*overrides: str, prolog: str = "") -> bytes:
    """`[Content_Types].xml` con un `Override` por tipo de `overrides`."""
    items = "".join(
        f'<Override PartName="/word/part{i}.xml" ContentType="{content_type}"/>'
        for i, content_type in enumerate(overrides)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'{prolog}<Types xmlns="{CONTENT_TYPES_NAMESPACE}">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f"{items}</Types>"
    ).encode()


def zip_package(*entries: tuple[str, bytes], compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    """Paquete ZIP con `entries` en ese orden."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def docx(*, content_types_name: str = "[Content_Types].xml") -> bytes:
    """DOCX mínimo: `[Content_Types].xml`, relaciones y la parte principal."""
    return zip_package(
        (content_types_name, content_types(WORDPROCESSING_MAIN)),
        ("_rels/.rels", b'<?xml version="1.0"?><Relationships/>'),
        ("word/document.xml", b'<?xml version="1.0"?><w:document/>'),
    )


# --- Archivo compuesto OLE (CFB) ---

OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
OLE_SECTOR = 512
OLE_MINI_STREAM_CUTOFF = 4096
_FREESECT = 0xFFFFFFFF
_ENDOFCHAIN = 0xFFFFFFFE
_FATSECT = 0xFFFFFFFD
_NOSTREAM = 0xFFFFFFFF
_STORAGE, _STREAM, _ROOT = 1, 2, 5
_DIRENTRY = struct.Struct("<64sHBBIII16sIQQIII")  # 128 bytes, MS-CFB 2.6
_HEADER = struct.Struct("<8s16s6H10I")  # 76 bytes, seguidos del DIFAT de 109 entradas


def _directory_entry(
    name: str,
    kind: int,
    *,
    right: int = _NOSTREAM,
    child: int = _NOSTREAM,
    start: int = _ENDOFCHAIN,
    size: int = 0,
) -> bytes:
    encoded = (name + "\x00").encode("utf-16-le")
    return _DIRENTRY.pack(
        encoded,
        len(encoded),
        kind,
        1,
        _NOSTREAM,
        right,
        child,
        bytes(16),
        0,
        0,
        0,
        start,
        size,
        0,
    )


def ole_file(*streams: tuple[str, bytes], storages: tuple[str, ...] = ()) -> bytes:
    """Archivo compuesto OLE v3 (sectores de 512 bytes) con `streams` y `storages` en la raíz.

    Cada flujo se completa con ceros hasta 4096 bytes para quedar fuera del mini flujo, así el
    archivo no necesita MiniFAT. Los almacenamientos quedan vacíos. Caben tres entradas además
    de la raíz, en un solo sector de directorio.
    """
    children = [(name, _STREAM, data) for name, data in streams]
    children += [(name, _STORAGE, b"") for name in storages]
    assert len(children) <= 3
    fat = [_FATSECT, _ENDOFCHAIN]  # sector 0: la FAT; sector 1: el directorio
    entries: list[bytes] = []
    sectors = b""
    for index, (name, kind, data) in enumerate(children, start=1):
        right = index + 1 if index < len(children) else _NOSTREAM  # árbol degenerado a derecha
        if kind == _STORAGE:
            entries.append(_directory_entry(name, kind, right=right))
            continue
        data = data.ljust(OLE_MINI_STREAM_CUTOFF, b"\x00")
        padded = data.ljust(-(-len(data) // OLE_SECTOR) * OLE_SECTOR, b"\x00")
        first = len(fat)
        count = len(padded) // OLE_SECTOR
        fat += [*range(first + 1, first + count), _ENDOFCHAIN]
        entries.append(_directory_entry(name, kind, right=right, start=first, size=len(data)))
        sectors += padded
    root = _directory_entry("Root Entry", _ROOT, child=1 if children else _NOSTREAM)
    directory = (root + b"".join(entries)).ljust(OLE_SECTOR, b"\x00")
    assert len(fat) <= OLE_SECTOR // 4
    fat_sector = struct.pack("<128I", *fat, *[_FREESECT] * (128 - len(fat)))
    header = _HEADER.pack(
        OLE_SIGNATURE,
        bytes(16),
        0x3E,
        3,
        0xFFFE,
        9,
        6,
        0,  # versión 3, sectores de 512 y mini sectores de 64 bytes
        0,
        0,
        1,
        1,
        0,
        OLE_MINI_STREAM_CUTOFF,  # 1 sector de FAT; el directorio en el sector 1
        _ENDOFCHAIN,
        0,
        _ENDOFCHAIN,
        0,  # sin MiniFAT ni DIFAT
    )
    difat = struct.pack("<109I", 0, *[_FREESECT] * 108)
    return header + difat + fat_sector + directory + sectors


def encrypted_docx() -> bytes:
    """DOCX_Cifrado: archivo OLE con `EncryptionInfo` y `EncryptedPackage` (MS-OFFCRYPTO)."""
    return ole_file(
        ("EncryptionInfo", b'\x04\x00\x04\x00\x40\x00\x00\x00<?xml version="1.0"?>'),
        ("EncryptedPackage", (6000).to_bytes(8, "little") + bytes(range(256)) * 24),
    )


def ole_circular_difat() -> bytes:
    """Cabecera OLE que declara el máximo de sectores de FAT con un DIFAT que apunta a sí mismo.

    Ocupa 1536 bytes; un lector sin topes lee el mismo sector millones de veces.
    """
    fat_sectors = 0xFFFFFFFF
    difat_sectors = (fat_sectors - 109 + 126) // 127
    header = _HEADER.pack(
        OLE_SIGNATURE,
        bytes(16),
        0x3E,
        3,
        0xFFFE,
        9,
        6,
        0,
        0,
        0,
        fat_sectors,
        0,
        0,
        OLE_MINI_STREAM_CUTOFF,
        _ENDOFCHAIN,
        0,
        0,
        difat_sectors,
    )
    difat = struct.pack("<109I", *[0] * 109)
    return header + difat + struct.pack("<128I", *[0] * 128) + bytes(OLE_SECTOR)
