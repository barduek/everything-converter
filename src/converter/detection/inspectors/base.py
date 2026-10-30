"""Interfaz y registro de los inspectores que corren en subproceso (Req 2.11, 4.4, 16.1, 16.2).

Un inspector analiza el Archivo_de_Origen de un Formato con la biblioteca o la herramienta de
ese Formato y devuelve un `SourceInfo`, con los datos que necesitan sus convertidores, o un
`SourceRejection` (tabla "Inspectores" de design.md). Es una función
`(InspectorInput) -> SourceInfo | SourceRejection` registrada con un nombre mediante
`@register_inspector("nombre", info_type=...)`; el catálogo la referencia con
`inspector = "nombre"` en el Formato.

Los inspectores leen contenido no confiable con parsers complejos, así que corren en el
subproceso `python -m converter.workers.inspect`, con los límites del Ejecutor aislado; los
lanza `detection/inspect_runner.py`. El inspector `text` es la excepción: corre en el proceso
principal, dentro del Detector_de_Tipo, y no se registra aquí.

Cada registro declara:

- `info_type`: la clase de `SourceInfo` que devuelve el inspector, o una base común de las que
  puede devolver. El proceso principal convierte en `error_interno` una información de otro
  tipo.
- `tool`: la herramienta cuya salida de error se sanea y se clasifica si el subproceso falla
  (`eventlog.Tool`, por ejemplo `Tool.PILLOW`), o None si ninguna corresponde.
- `sandbox_tool`: elige los límites predeterminados del subproceso
  (`sandbox.limits.tool_limits`). Es `SandboxTool.PYTHON` salvo que el inspector lance otra
  herramienta, como ffprobe.

Reglas para un inspector:

- Evalúa sus rechazos en el orden de `INSPECTION_REJECTIONS` y devuelve el primero que
  corresponde, con los parámetros que pide su mensaje.
- No escribe en el Archivo_de_Origen. Sus archivos temporales van a `TMPDIR`, que es `tmp/` del
  Espacio_de_Trabajo.
- Puede lanzar una excepción: el worker termina con el traceback en stderr y el proceso
  principal clasifica esa salida con `tool`.
- Importa sus bibliotecas pesadas (Pillow, pypdfium2, lxml) dentro de la función. El proceso
  principal y cada worker importan todos los inspectores, para conocer sus nombres y sus tipos
  de `SourceInfo`, y un import pesado en el módulo lo pagarían todos.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from converter.detection.source_info import SourceInfo, SourceRejection
from converter.eventlog import Tool
from converter.sandbox.limits import SandboxTool

# Nombre de un inspector, p. ej. "image"; el worker lo recibe como argumento
_INSPECTOR_NAME = re.compile(r"[a-z][a-z0-9_]*")  # se usa con `fullmatch`


@dataclass(frozen=True, slots=True, kw_only=True)
class InspectorInput:
    """Lo que recibe un inspector en el subproceso."""

    path: Path  # Archivo_de_Origen; se abre solo para lectura
    real_type: str | None  # Tipo_Real, p. ej. "MP4": `media` elige el demuxer; None si no se indicó
    max_image_pixels: int  # Límite_de_Píxeles (Req 5.9)


type InspectorFunction = Callable[[InspectorInput], SourceInfo | SourceRejection]


@dataclass(frozen=True, slots=True)
class InspectorSpec:
    """Un inspector registrado y lo que declara."""

    name: str
    function: InspectorFunction
    info_type: type[SourceInfo]  # tipo de la información que devuelve
    tool: Tool | None  # herramienta cuya salida de error se sanea y se clasifica
    sandbox_tool: SandboxTool  # límites predeterminados del subproceso


class UnknownInspectorError(LookupError):
    """No hay ningún inspector registrado con ese nombre."""

    def __init__(self, name: str) -> None:
        super().__init__(f"inspector no registrado: {name!r}")
        self.name = name


# Inspectores por nombre, en orden de registro
_REGISTRY: dict[str, InspectorSpec] = {}


def register_inspector(
    name: str,
    *,
    info_type: type[SourceInfo],
    tool: Tool | None = None,
    sandbox_tool: SandboxTool = SandboxTool.PYTHON,
) -> Callable[[InspectorFunction], InspectorFunction]:
    """Decorador que registra un inspector con `name`.

    Lanza `ValueError` si `name` no tiene la forma `[a-z][a-z0-9_]*` o ya pertenece a otro
    inspector, o si `tool` o `sandbox_tool` no son valores de sus enumeraciones, y `TypeError` si
    `info_type` no es una subclase de `SourceInfo`.
    """
    if not isinstance(name, str) or _INSPECTOR_NAME.fullmatch(name) is None:
        raise ValueError(f"nombre de inspector inválido: {name!r}")
    if not isinstance(info_type, type) or not issubclass(info_type, SourceInfo):
        raise TypeError(f"info_type del inspector {name!r} debe ser una subclase de SourceInfo")
    checked_tool = None if tool is None else Tool(tool)
    checked_sandbox_tool = SandboxTool(sandbox_tool)

    def decorator(function: InspectorFunction) -> InspectorFunction:
        if name in _REGISTRY:
            raise ValueError(f"ya hay un inspector registrado con el nombre {name!r}")
        _REGISTRY[name] = InspectorSpec(
            name=name,
            function=function,
            info_type=info_type,
            tool=checked_tool,
            sandbox_tool=checked_sandbox_tool,
        )
        return function

    return decorator


def get_inspector(name: str) -> InspectorSpec:
    """Inspector registrado con `name`. Lanza `UnknownInspectorError` si no existe."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownInspectorError(name) from None


def registered_inspectors() -> tuple[str, ...]:
    """Nombres de los inspectores registrados, en orden de registro."""
    return tuple(_REGISTRY)
