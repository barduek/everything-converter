"""PDF mínimos escritos a mano, con el cifrado estándar RC4 del formato PDF si se pide.

Sirven para las pruebas del inspector `pdf` sin depender de otros módulos de prueba ni de
herramientas externas. `build_pdf` escribe páginas en blanco, sin contenido, con el `/MediaBox`,
el `/CropBox` y el `/Rotate` que se indiquen, así que el cifrado no tiene cadenas ni flujos que
cifrar: solo hay que escribir el diccionario `/Encrypt` con las claves `/O` y `/U` de los
algoritmos 2 a 5 de ISO 32000-1, sección 7.6.3. Es un cifrado débil, válido solo para pruebas.
Este módulo no importa pytest.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

# Relleno de contraseñas del algoritmo 2 (ISO 32000-1, 7.6.3.3)
_PADDING = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")

# Bits de permisos de /P, contados desde 1 (ISO 32000-1, tabla 22)
PRINT = 1 << 2
MODIFY = 1 << 3
COPY = 1 << 4
ANNOTATE = 1 << 5
FILL_FORMS = 1 << 8
ACCESSIBILITY = 1 << 9
ASSEMBLE = 1 << 10
PRINT_HIGH_QUALITY = 1 << 11
ALL_PERMISSIONS = -4  # todos los bits de permiso en 1 y los dos reservados de abajo en 0


@dataclass(frozen=True, slots=True)
class PageSpec:
    """Una página en blanco."""

    width: float = 612
    height: float = 792
    rotate: int | None = None  # /Rotate; None lo omite
    crop_box: tuple[float, float, float, float] | None = None  # /CropBox; None lo omite


@dataclass(frozen=True, slots=True)
class Encryption:
    """Cifrado estándar RC4: revisión 2 (40 bits) o revisión 3 (128 bits)."""

    user_password: bytes = b""  # distinta de vacía: el PDF pide contraseña para abrirse
    owner_password: bytes = b"owner"
    permissions: int = ALL_PERMISSIONS  # /P como entero con signo de 32 bits
    revision: Literal[2, 3] = 2


def permissions_without(*bits: int) -> int:
    """`ALL_PERMISSIONS` sin los permisos `bits`, como entero con signo de 32 bits."""
    value = ALL_PERMISSIONS
    for bit in bits:
        value &= ~bit
    return value


def _md5(data: bytes) -> bytes:
    return hashlib.md5(data, usedforsecurity=False).digest()


def _rc4(key: bytes, data: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        out.append(byte ^ state[(state[i] + state[j]) % 256])
    return bytes(out)


def _pad(password: bytes) -> bytes:
    return (password + _PADDING)[:32]


def _rc4_rounds(key: bytes, data: bytes, revision: int) -> bytes:
    """RC4 una vez (revisión 2) o 20 veces con la clave combinada con 0 a 19 (revisión 3)."""
    result = _rc4(key, data)
    if revision >= 3:
        for step in range(1, 20):
            result = _rc4(bytes(byte ^ step for byte in key), result)
    return result


def _owner_entry(encryption: Encryption) -> bytes:
    """Entrada /O (algoritmo 3)."""
    length = 5 if encryption.revision == 2 else 16
    digest = _md5(_pad(encryption.owner_password or encryption.user_password))
    if encryption.revision >= 3:
        for _ in range(50):
            digest = _md5(digest)
    key = digest[:length]
    return _rc4_rounds(key, _pad(encryption.user_password), encryption.revision)


def _file_key(encryption: Encryption, owner_entry: bytes, file_id: bytes) -> bytes:
    """Clave de cifrado del archivo (algoritmo 2)."""
    length = 5 if encryption.revision == 2 else 16
    permissions = (encryption.permissions & 0xFFFFFFFF).to_bytes(4, "little")
    digest = _md5(_pad(encryption.user_password) + owner_entry + permissions + file_id)
    if encryption.revision >= 3:
        for _ in range(50):
            digest = _md5(digest[:length])
    return digest[:length]


def _user_entry(encryption: Encryption, key: bytes, file_id: bytes) -> bytes:
    """Entrada /U (algoritmos 4 y 5)."""
    if encryption.revision == 2:
        return _rc4(key, _PADDING)
    seed = _md5(_PADDING + file_id)
    return _rc4_rounds(key, seed, 3) + bytes(16)


def _encrypt_dictionary(encryption: Encryption, file_id: bytes) -> bytes:
    owner = _owner_entry(encryption)
    user = _user_entry(encryption, _file_key(encryption, owner, file_id), file_id)
    version, length = (1, b"") if encryption.revision == 2 else (2, b" /Length 128")
    return b"<< /Filter /Standard /V %d%s /R %d /O <%s> /U <%s> /P %d >>" % (
        version,
        length,
        encryption.revision,
        owner.hex().encode(),
        user.hex().encode(),
        encryption.permissions,
    )


def _number(value: float) -> bytes:
    return b"%g" % value


def _page_dictionary(page: PageSpec) -> bytes:
    entries = [b"/Type /Page", b"/Parent 2 0 R"]
    entries.append(b"/MediaBox [0 0 %s %s]" % (_number(page.width), _number(page.height)))
    if page.crop_box is not None:
        entries.append(b"/CropBox [%s]" % b" ".join(_number(value) for value in page.crop_box))
    if page.rotate is not None:
        entries.append(b"/Rotate %d" % page.rotate)
    return b"<< " + b" ".join(entries) + b" >>"


def build_pdf(
    pages: Sequence[PageSpec] = (PageSpec(),),
    *,
    encryption: Encryption | None = None,
    page_count: int | None = None,
) -> bytes:
    """PDF con `pages` en blanco, cifrado si se indica `encryption`.

    `page_count`, si se indica, es el `/Count` que declara el árbol de páginas en lugar de
    `len(pages)`, para armar un PDF cuyo árbol no coincide con sus páginas.
    """
    file_id = _md5(b"pdf_samples")
    bodies: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>"
        % (
            b" ".join(b"%d 0 R" % (3 + index) for index in range(len(pages))),
            len(pages) if page_count is None else page_count,
        ),
        *(_page_dictionary(page) for page in pages),
    ]
    trailer = b"/Root 1 0 R /ID [<%s> <%s>]" % (file_id.hex().encode(), file_id.hex().encode())
    if encryption is not None:
        bodies.append(_encrypt_dictionary(encryption, file_id))
        trailer += b" /Encrypt %d 0 R" % len(bodies)

    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref_offset = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(bodies) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d %s >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(bodies) + 1,
        trailer,
        xref_offset,
    )
    return bytes(out)
