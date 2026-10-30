"""Sonda `isobmff_mp4`: firma de MP4 (Req 2.1).

Coincide si el archivo empieza con una caja `ftyp` (tipo en el desplazamiento 4) cuya marca
principal o alguna de sus marcas compatibles está en `ADMITTED_BRANDS`, salvo que la marca
principal esté en `EXCLUDED_MAJOR_BRANDS`: QuickTime (MOV), audio M4A y M4B, e imágenes HEIF y
AVIF, que comparten el contenedor ISO BMFF pero no son MP4.

La caja debe estar completa dentro de `ProbeInput.head`; una `ftyp` real ocupa unas decenas de
bytes. Se admite el tamaño de 64 bits (tamaño 1); el tamaño 0 (hasta el final del archivo) no
es una `ftyp` de un MP4 y no coincide.
"""

from __future__ import annotations

from converter.detection.probes.base import ProbeInput, register_probe

FTYP = b"ftyp"

ADMITTED_BRANDS: frozenset[bytes] = frozenset(
    (b"isom", *(b"iso%d" % n for n in range(2, 10)), b"mp41", b"mp42", b"avc1", b"dash", b"M4V ")
)
EXCLUDED_MAJOR_BRANDS: frozenset[bytes] = frozenset(
    {b"qt  ", b"M4A ", b"M4B ", b"heic", b"heix", b"mif1", b"avif"}
)

_BRAND_SIZE = 4
_LARGE_SIZE = 1  # el tamaño real va en 64 bits después del tipo


def ftyp_brands(head: bytes) -> tuple[bytes, tuple[bytes, ...]] | None:
    """Marca principal y marcas compatibles de la caja `ftyp` al principio de `head`.

    None si `head` no empieza con una caja `ftyp` completa.
    """
    if len(head) < 8 or head[4:8] != FTYP:
        return None
    size = int.from_bytes(head[0:4], "big")
    start = 8
    if size == _LARGE_SIZE:
        if len(head) < 16:
            return None
        size = int.from_bytes(head[8:16], "big")
        start = 16
    # Marca principal y versión menor, de 4 bytes cada una
    brands_start = start + 2 * _BRAND_SIZE
    if size < brands_start or size > len(head):
        return None
    major = head[start : start + _BRAND_SIZE]
    compatible = tuple(
        head[i : i + _BRAND_SIZE] for i in range(brands_start, size - _BRAND_SIZE + 1, _BRAND_SIZE)
    )
    return major, compatible


@register_probe("isobmff_mp4")
def isobmff_mp4(source: ProbeInput) -> bool:
    brands = ftyp_brands(source.head)
    if brands is None:
        return False
    major, compatible = brands
    if major in EXCLUDED_MAJOR_BRANDS:
        return False
    return major in ADMITTED_BRANDS or any(brand in ADMITTED_BRANDS for brand in compatible)
