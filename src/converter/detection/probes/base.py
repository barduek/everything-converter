"""Interfaz y registro de las sondas de Firma_de_Formato (Req 2.1, 4.4).

Una sonda decide si el contenido de un archivo presenta una firma estructural, la que una
`MagicSignature` no puede expresar (por ejemplo, un ZIP cuyo `[Content_Types].xml` declara un
documento de Word). Es una función `(ProbeInput) -> bool` registrada con un nombre mediante
`@register_probe("nombre")`; el catálogo la referencia con `signature = { probe = "nombre" }` o en
`variant_probes`.

Las sondas corren en el proceso principal sobre contenido no confiable, así que:

- leen solo cabeceras y estructuras, con una cantidad de bytes acotada, y nunca decodifican
  contenido multimedia ni descomprimen más que un tope pequeño;
- devuelven False ante un contenido mal formado o truncado, sin lanzar excepciones;
- no escriben en el archivo. Pueden mover la posición de `ProbeInput.stream`: quien las llama no
  debe suponer ninguna posición después.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import BinaryIO

from converter.registry.registry import SIGNATURE_WINDOW


@dataclass(frozen=True, slots=True)
class ProbeInput:
    """Lo que recibe una sonda: los primeros bytes, el archivo completo y la extensión."""

    head: bytes  # primeros SIGNATURE_WINDOW bytes del archivo (todo el archivo si es más corto)
    stream: BinaryIO = field(repr=False, compare=False)  # binario, posicionable, solo lectura
    extension: str | None  # Extensión_del_Nombre tal como llega; None si el nombre no tiene

    @classmethod
    def from_stream(cls, stream: BinaryIO, extension: str | None) -> ProbeInput:
        """Lee los primeros `SIGNATURE_WINDOW` bytes de `stream` desde el principio."""
        stream.seek(0)
        return cls(head=stream.read(SIGNATURE_WINDOW), stream=stream, extension=extension)

    def read(self, offset: int, size: int) -> bytes:
        """Hasta `size` bytes desde `offset`; menos si el archivo termina antes.

        Usa `head` si el rango está dentro y si no lee de `stream`.
        """
        if offset < 0 or size < 0:
            raise ValueError(f"offset y size no pueden ser negativos: {offset}, {size}")
        end = offset + size
        if end <= len(self.head):
            return self.head[offset:end]
        try:
            self.stream.seek(offset)
        except OverflowError:
            return b""  # posición declarada por el archivo que ningún archivo real alcanza
        return self.stream.read(size)


type Probe = Callable[[ProbeInput], bool]


class UnknownProbeError(LookupError):
    """No hay ninguna sonda registrada con ese nombre."""

    def __init__(self, name: str) -> None:
        super().__init__(f"sonda no registrada: {name!r}")
        self.name = name


# Sondas por nombre, en orden de registro
_REGISTRY: dict[str, Probe] = {}


def register_probe(name: str) -> Callable[[Probe], Probe]:
    """Decorador que registra una sonda con `name`.

    Lanza `ValueError` si `name` está vacío o ya pertenece a otra sonda.
    """
    if not name or name != name.strip():
        raise ValueError(f"nombre de sonda inválido: {name!r}")

    def decorator(probe: Probe) -> Probe:
        if name in _REGISTRY:
            raise ValueError(f"ya hay una sonda registrada con el nombre {name!r}")
        _REGISTRY[name] = probe
        return probe

    return decorator


def get_probe(name: str) -> Probe:
    """Sonda registrada con `name`. Lanza `UnknownProbeError` si no existe."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownProbeError(name) from None


def registered_probes() -> tuple[str, ...]:
    """Nombres de las sondas registradas, en orden de registro."""
    return tuple(_REGISTRY)


def run_probe(name: str, source: ProbeInput) -> bool:
    """Ejecuta la sonda `name` sobre `source`. Lanza `UnknownProbeError` si no existe."""
    return get_probe(name)(source)
