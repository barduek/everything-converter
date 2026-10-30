"""Coincidencia de Firmas_de_Formato y elección por especificidad (Req 2.1, 2.8, 2.10, 4.4).

Una firma es una de dos formas (`registry.model`):

- `MagicSignature`: todos sus pares (desplazamiento, bytes) coinciden dentro de los primeros
  `SIGNATURE_WINDOW` bytes (64 KiB). Un par que termina después no coincide nunca.
- `ProbeSignature`: la decide la sonda registrada con su nombre (`detection.probes`).

`match_signature` recibe los Formatos en orden de registro, como los devuelve
`ConversionRegistry.formats()`, y devuelve el que tiene la firma presente más específica: una
sonda estructural está por encima de cualquier firma de bytes y, entre firmas de bytes, gana la
que compara más bytes fijos (posiciones distintas del archivo). A igual especificidad gana el
Formato registrado antes. Las firmas se evalúan de la más específica a la menos y la búsqueda
termina en la primera que coincide.

Ninguna firma depende de la Extensión_del_Nombre (Req 2.8). Solo la usan las sondas de variante,
que `match_variant` ejecuta cuando ninguna firma coincide: un archivo que cumple una variante
tiene como Tipo_Real el Formato que la declara, por ejemplo un DOCX_Cifrado con la sonda
`docx_encrypted` de DOCX (Req 2.10, 7.2).

Un nombre de sonda que no está registrado lanza `UnknownProbeError`: es un error del catálogo,
no del archivo.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from converter.detection.probes import ProbeInput, run_probe
from converter.registry.model import FormatDef, MagicSignature, ProbeSignature, Signature
from converter.registry.registry import SIGNATURE_WINDOW

# (es estructural, bytes fijos comparados): se compara como tupla
type Specificity = tuple[int, int]

_STRUCTURAL: Specificity = (1, 0)


@dataclass(frozen=True, slots=True)
class VariantMatch:
    """Sonda de variante que coincidió y el Formato que la declara."""

    format: FormatDef
    probe: str  # p. ej. "docx_encrypted"


def fixed_byte_count(signature: MagicSignature) -> int:
    """Cantidad de posiciones distintas del archivo que compara `signature`."""
    count = 0
    covered_until = 0  # fin de la unión de los pares ya contados
    for offset, data in sorted(signature.parts):
        end = offset + len(data)
        if end > covered_until:
            count += end - max(offset, covered_until)
            covered_until = end
    return count


def specificity(signature: Signature) -> Specificity:
    """Especificidad de `signature`; la mayor gana cuando coinciden varias firmas."""
    if isinstance(signature, ProbeSignature):
        return _STRUCTURAL
    return (0, fixed_byte_count(signature))


def magic_matches(signature: MagicSignature, head: bytes) -> bool:
    """True si cada par de `signature` coincide con `head` dentro de `SIGNATURE_WINDOW`."""
    return all(head.startswith(data, offset, SIGNATURE_WINDOW) for offset, data in signature.parts)


def signature_matches(signature: Signature, source: ProbeInput) -> bool:
    """True si el contenido de `source` presenta `signature`."""
    if isinstance(signature, MagicSignature):
        return magic_matches(signature, source.head)
    return run_probe(signature.probe, source)


def match_signature(formats: Iterable[FormatDef], source: ProbeInput) -> FormatDef | None:
    """Formato con la firma presente más específica en `source`; None si ninguna coincide.

    `formats` va en orden de registro, que decide entre firmas de igual especificidad.
    """
    candidates = [(fmt, fmt.signature) for fmt in formats if fmt.signature is not None]
    # `sorted` es estable también con reverse=True: a igual especificidad conserva el orden
    ranked = sorted(candidates, key=lambda candidate: specificity(candidate[1]), reverse=True)
    for fmt, signature in ranked:
        if signature_matches(signature, source):
            return fmt
    return None


def match_variant(formats: Iterable[FormatDef], source: ProbeInput) -> VariantMatch | None:
    """Primera sonda de variante que coincide con `source`, en orden de registro de los Formatos
    y, dentro de cada Formato, en el orden en que declara sus sondas."""
    for fmt in formats:
        for probe in fmt.variant_probes:
            if run_probe(probe, source):
                return VariantMatch(format=fmt, probe=probe)
    return None
