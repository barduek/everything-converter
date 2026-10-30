"""PDF de prueba con texto para las pruebas de PDF a TXT (`pdf.text`).

`build_pdf` escribe a mano un PDF mínimo: una fuente Helvetica estándar con codificación
WinAnsi y, por página, una línea de texto por cada elemento de la lista, de arriba hacia abajo.
PDFium separa cada línea con `\\r\\n`, y un guion al final de una línea seguido de otra línea
es para PDFium un guion de fin de línea. Una página sin líneas no tiene Capa_de_Texto.

Con `owner_password` el PDF se cifra con el manejador de seguridad estándar (RC4 de 40 bits,
V 1 R 2) y permisos restringidos: se abre con la contraseña de usuario vacía y PDFium ignora las
restricciones, como en un PDF_con_Permisos_Restringidos. Con una `user_password` no vacía, en
cambio, pide contraseña para abrirlo: es un PDF_Protegido.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Sequence
from dataclasses import dataclass

PAGE_WIDTH = 400
PAGE_HEIGHT = 400
MARGIN = 20
LINE_HEIGHT = 14
FONT_SIZE = 12

# Cadena de relleno del manejador de seguridad estándar (PDF 1.7, algoritmo 3.2)
_PADDING = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")
_PERMISSIONS = -3904  # sin impresión, edición, copia ni anotaciones
_FILE_ID = bytes(range(16))


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


def _padded(password: bytes) -> bytes:
    return (password + _PADDING)[:32]


@dataclass(frozen=True)
class _Security:
    """Valores del manejador de seguridad estándar de un PDF cifrado."""

    key: bytes  # clave de cifrado de 5 bytes
    owner: bytes  # valor /O
    user: bytes  # valor /U

    def encrypt_stream(self, number: int, data: bytes) -> bytes:
        object_key = hashlib.md5(self.key + number.to_bytes(3, "little") + b"\0\0").digest()
        return _rc4(object_key[:10], data)

    def dictionary(self) -> bytes:
        return b"<< /Filter /Standard /V 1 /R 2 /Length 40 /P %d /O <%s> /U <%s> >>" % (
            _PERMISSIONS,
            self.owner.hex().encode(),
            self.user.hex().encode(),
        )


def _security(user_password: bytes, owner_password: bytes) -> _Security:
    owner_key = hashlib.md5(_padded(owner_password)).digest()[:5]
    owner = _rc4(owner_key, _padded(user_password))
    material = _padded(user_password) + owner + struct.pack("<i", _PERMISSIONS) + _FILE_ID
    key = hashlib.md5(material).digest()[:5]
    return _Security(key=key, owner=owner, user=_rc4(key, _PADDING))


def _escape(text: str) -> bytes:
    data = text.encode("cp1252")
    return data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _content(lines: Sequence[str]) -> bytes:
    parts = [b"BT /F1 %d Tf" % FONT_SIZE]
    for index, line in enumerate(lines):
        y = PAGE_HEIGHT - MARGIN - LINE_HEIGHT * (index + 1)
        parts.append(b"1 0 0 1 %d %d Tm (%s) Tj" % (MARGIN, y, _escape(line)))
    parts.append(b"ET")
    return b"\n".join(parts)


def build_pdf(
    pages: Sequence[Sequence[str]],
    *,
    owner_password: bytes | None = None,
    user_password: bytes = b"",
) -> bytes:
    """PDF con una página por elemento de `pages`, cada una con sus líneas de texto.

    Las líneas solo pueden tener caracteres de Windows-1252. Con `owner_password` el PDF queda
    cifrado y con permisos restringidos; `user_password` es la contraseña para abrirlo.
    """
    security = _security(user_password, owner_password) if owner_password is not None else None
    first_page = 4  # objetos: 1 catálogo, 2 páginas, 3 fuente, y por página la página y su flujo
    kids = b" ".join(b"%d 0 R" % (first_page + 2 * index) for index in range(len(pages)))
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(pages)),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for index, lines in enumerate(pages):
        number = first_page + 2 * index
        data = _content(lines)
        if security is not None:
            data = security.encrypt_stream(number + 1, data)
        bodies.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>"
            % (PAGE_WIDTH, PAGE_HEIGHT, number + 1)
        )
        bodies.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(data), data))
    trailer = b"/Root 1 0 R"
    if security is not None:
        bodies.append(security.dictionary())
        file_id = _FILE_ID.hex().encode()
        trailer += b" /Encrypt %d 0 R /ID [<%s> <%s>]" % (len(bodies), file_id, file_id)

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(bodies) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d %s >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(bodies) + 1,
        trailer,
        xref,
    )
    return bytes(out)
