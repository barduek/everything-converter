"""Sonda `pdf_header`: firma de PDF (Req 2.1).

Coincide si `%PDF-` aparece completo dentro de los primeros 1024 bytes. Los lectores habituales
aceptan bytes antes de la cabecera dentro de ese margen.
"""

from __future__ import annotations

from converter.detection.probes.base import ProbeInput, register_probe

PDF_MARKER = b"%PDF-"
HEADER_WINDOW = 1024  # el marcador debe terminar dentro de estos primeros bytes


@register_probe("pdf_header")
def pdf_header(source: ProbeInput) -> bool:
    return source.head.find(PDF_MARKER, 0, HEADER_WINDOW) >= 0
