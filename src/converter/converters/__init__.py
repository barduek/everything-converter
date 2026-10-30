"""Convertidores por familia con descubrimiento automático (Req 4.3, 4.4).

`load_converters()` importa con `pkgutil.walk_packages` cada módulo y subpaquete de este paquete,
en orden por nombre, salvo `base` y los módulos cuyo nombre empieza con "_". Cada familia
registra sus convertidores con `@register_converter("familia.nombre")` y sus funciones de
arranque con `@on_startup` (ver `base.py`), así que agregar una familia es agregar aquí un
módulo, sin cambiar el Detector_de_Tipo, el Gestor_de_Tareas, el Gestor_de_Archivos_Temporales
ni la Interfaz_de_Usuario (Req 4.3). El arranque llama a `load_converters()` antes de cargar el
catálogo, que solo acepta pares cuyo `converter` está registrado.

A diferencia de `detection.probes`, importar este paquete no importa las familias. Los workers y
los inspectores corren en subprocesos e importan módulos sueltos, como `orientation` o
`media_rules`; si el paquete cargara todas las familias, cada subproceso pagaría el tiempo y la
memoria de dependencias que no usa.

Reglas para los módulos del paquete, porque el descubrimiento los importa a todos en el proceso
principal:

- Importarlos no hace E/S, no lanza subprocesos y no depende de herramientas externas: la
  consulta a una herramienta va en una función `@on_startup` o en `run`, así la importación
  funciona también fuera del contenedor.
- Las bibliotecas que solo usa un worker se importan en el worker, no aquí.
- Un módulo que no se puede importar hace fallar `load_converters()` con su excepción: un
  convertidor ausente en silencio dejaría sus pares fuera del catálogo.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from collections.abc import Mapping
from types import ModuleType
from typing import NoReturn

from converter.converters.base import Converter, registered_converters

# Módulos que el descubrimiento omite: `base` es la interfaz y ya lo importa este módulo
_SKIPPED = frozenset({"base"})


def _import_error(name: str) -> NoReturn:
    # `walk_packages` llama a `onerror` dentro del `except` del import fallido, así que esta
    # excepción lleva la original como contexto. Sin `onerror`, un ImportError de un
    # subpaquete se ignoraría en silencio.
    raise ImportError(f"no se pudo importar el paquete {name}")


def import_submodules(package: ModuleType) -> tuple[str, ...]:
    """Importa los módulos y subpaquetes de `package` y devuelve sus nombres, en orden.

    Omite los módulos cuyo último componente está en `_SKIPPED` o empieza con "_". Propaga la
    excepción del primer módulo que no se puede importar.
    """
    imported: list[str] = []
    prefix = f"{package.__name__}."
    for module in pkgutil.walk_packages(package.__path__, prefix, onerror=_import_error):
        last = module.name.rpartition(".")[2]
        if last in _SKIPPED or last.startswith("_"):
            continue
        importlib.import_module(module.name)
        imported.append(module.name)
    return tuple(imported)


def load_converters() -> Mapping[str, Converter]:
    """Importa las familias de este paquete y devuelve los convertidores registrados.

    Llamarla otra vez no vuelve a importar nada y devuelve los mismos convertidores.
    """
    import_submodules(sys.modules[__name__])
    return registered_converters()


__all__ = ["import_submodules", "load_converters"]
