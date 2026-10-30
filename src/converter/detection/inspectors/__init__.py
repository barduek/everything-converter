"""Inspectores en subproceso con descubrimiento automático (Req 2.11, 4.4, 16.2).

Importar este paquete importa cada módulo que contiene, salvo `base` y los que empiezan con "_".
Cada módulo registra sus inspectores con `@register_inspector` y sus tipos de `SourceInfo` con
`@register_source_info`, así que agregar el inspector de un Formato es agregar aquí un módulo y
nombrarlo en el catálogo, sin cambiar el Detector_de_Tipo (Req 4.4). Lo importan el worker
`converter.workers.inspect`, que ejecuta los inspectores, y el proceso principal
(`detection/inspect_runner.py`), que valida sus nombres y reconstruye su salida JSON.

Inspectores del Mapa_de_Conversión_Inicial (tabla "Inspectores" de design.md), que agregan las
familias de convertidores:

- `image`: PNG, JPG y WEBP, con Pillow.
- `pdf`: PDF, con pypdfium2.
- `docx`: DOCX, con el lector ZIP propio y lxml.
- `media`: MP3, WAV, OGG, MP4 y WEBM, con ffprobe.

El inspector `text` corre en el proceso principal y no está en este paquete. Ver `base.py` para
las reglas de un inspector, entre ellas importar sus bibliotecas pesadas dentro de la función.
"""

from __future__ import annotations

import importlib
import pkgutil

from converter.detection.inspectors.base import (
    InspectorFunction,
    InspectorInput,
    InspectorSpec,
    UnknownInspectorError,
    get_inspector,
    register_inspector,
    registered_inspectors,
)

__all__ = [
    "InspectorFunction",
    "InspectorInput",
    "InspectorSpec",
    "UnknownInspectorError",
    "get_inspector",
    "register_inspector",
    "registered_inspectors",
]


def _discover() -> None:
    # pkgutil devuelve los módulos ordenados por nombre, así que el orden no depende del sistema
    for module in pkgutil.iter_modules(__path__):
        if module.name == "base" or module.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{module.name}")


_discover()
