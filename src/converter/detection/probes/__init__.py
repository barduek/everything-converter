"""Sondas de Firma_de_Formato con descubrimiento automático (Req 2.1, 2.10, 4.4, 7.2).

Importar este paquete importa cada módulo que contiene, salvo `base` y los que empiezan con "_".
Cada módulo registra sus sondas con `@register_probe`, así que agregar un Formato con firma
estructural es agregar aquí un módulo con su sonda y la entrada en el catálogo, sin cambiar el
Detector_de_Tipo (Req 4.4).

Sondas del Mapa_de_Conversión_Inicial (tabla "Firmas" de design.md):

- `pdf_header`: PDF.
- `ooxml_wordprocessing`: DOCX.
- `mpeg_audio`: MP3.
- `isobmff_mp4`: MP4.
- `ebml_webm`: WEBM.
- `docx_encrypted`: sonda de variante de DOCX que reconoce un DOCX_Cifrado.
"""

from __future__ import annotations

import importlib
import pkgutil

from converter.detection.probes.base import (
    Probe,
    ProbeInput,
    UnknownProbeError,
    get_probe,
    register_probe,
    registered_probes,
    run_probe,
)

__all__ = [
    "Probe",
    "ProbeInput",
    "UnknownProbeError",
    "get_probe",
    "register_probe",
    "registered_probes",
    "run_probe",
]


def _discover() -> None:
    # pkgutil devuelve los módulos ordenados por nombre, así que el orden no depende del sistema
    for module in pkgutil.iter_modules(__path__):
        if module.name == "base" or module.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{module.name}")


_discover()
