"""Carga del catálogo declarativo en el Registro_de_Conversiones (Req 4.3, 4.4, 4.9).

El catálogo inicial son `catalog/formats.toml`, con tablas `[[format]]`, y `catalog/pairs.toml`,
con tablas `[[pair]]`. Tiene que cargar completo: un archivo ausente o ilegible, un TOML
inválido, una clave de primer nivel que no es la del archivo o una entrada que el registro
rechaza lanzan `CatalogError`. Es un error de programación y el arranque termina con código 70.

Los catálogos adicionales son los `catalog/pairs.d/*.toml`, en orden de nombre de archivo, con
la misma forma que `pairs.toml`. Una entrada inválida se rechaza: el registro la anota como
`registry_rejected` y el resto del Mapa_de_Conversión queda igual (Req 4.5 a 4.7, 4.12). Un
archivo adicional que no se puede leer o no tiene esa forma se descarta entero y se anota como
`catalog_file_rejected`; los demás archivos se cargan igual.

Agregar un Formato o un par es editar estos archivos: el Detector_de_Tipo, el Gestor_de_Tareas,
el Gestor_de_Archivos_Temporales y la Interfaz_de_Usuario lo toman desde el siguiente inicio
(Req 4.3, 4.4). Las rutas son parámetros para que las pruebas usen directorios temporales.
"""

from __future__ import annotations

import contextlib
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from converter.eventlog import EventLog, Level
from converter.registry.registry import ConversionRegistry, RegistrationError

CATALOG_DIR = Path(__file__).resolve().parent.parent / "catalog"
FORMATS_PATH = CATALOG_DIR / "formats.toml"  # catálogo inicial: Formatos
PAIRS_PATH = CATALOG_DIR / "pairs.toml"  # catálogo inicial: Pares_de_Conversión
EXTRA_PAIRS_DIR = CATALOG_DIR / "pairs.d"  # catálogos adicionales; puede no existir

FORMAT_KEY = "format"
PAIR_KEY = "pair"

# Evento del Log_de_Eventos para un catálogo adicional descartado entero
CATALOG_FILE_REJECTED = "catalog_file_rejected"


class CatalogError(Exception):
    """El catálogo inicial no cargó completo; el Conversor no debe arrancar.

    `path` es el archivo con el problema y `reason`, el problema. Si el registro rechazó una
    entrada, `__cause__` es su `RegistrationError`.
    """

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


class _FileProblem(Exception):
    """Un archivo del catálogo no se pudo leer o no tiene la forma esperada."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def load_catalog(
    converter_ids: Iterable[str],
    *,
    event_log: EventLog | None = None,
    formats_path: Path = FORMATS_PATH,
    pairs_path: Path = PAIRS_PATH,
    extra_pairs_dir: Path = EXTRA_PAIRS_DIR,
) -> ConversionRegistry:
    """Crea el Registro_de_Conversiones y le carga el catálogo inicial y los adicionales.

    `converter_ids` son los convertidores registrados. Registra primero los Formatos de
    `formats_path`, después los pares de `pairs_path` y al final los de cada
    `extra_pairs_dir/*.toml`. Lanza `CatalogError` si el catálogo inicial no carga completo.
    """
    log = event_log if event_log is not None else EventLog()
    registry = ConversionRegistry(converter_ids, event_log=log)

    for index, spec in enumerate(_initial_entries(formats_path, FORMAT_KEY), start=1):
        try:
            registry.register_format(spec)
        except RegistrationError as error:
            raise CatalogError(formats_path, f"[[{FORMAT_KEY}]] n.º {index}: {error}") from error

    for index, spec in enumerate(_initial_entries(pairs_path, PAIR_KEY), start=1):
        try:
            registry.register_pair(spec)
        except RegistrationError as error:
            raise CatalogError(pairs_path, f"[[{PAIR_KEY}]] n.º {index}: {error}") from error

    for path in sorted(extra_pairs_dir.glob("*.toml")):
        try:
            entries = _read_entries(path, PAIR_KEY)
        except _FileProblem as problem:
            log.emit(
                Level.WARNING, CATALOG_FILE_REJECTED, {"file": str(path), "reason": problem.reason}
            )
            continue
        for spec in entries:
            # El registro ya anotó el rechazo como `registry_rejected` y no cambió
            with contextlib.suppress(RegistrationError):
                registry.register_pair(spec)

    return registry


def _initial_entries(path: Path, key: str) -> list[dict[str, Any]]:
    try:
        return _read_entries(path, key)
    except _FileProblem as problem:
        raise CatalogError(path, problem.reason) from problem


def _read_entries(path: Path, key: str) -> list[dict[str, Any]]:
    """Tablas `[[key]]` del archivo, en orden. Un archivo sin ellas no tiene entradas."""
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except OSError as error:
        raise _FileProblem(
            f"no se pudo leer el archivo: {error.strerror or type(error).__name__}"
        ) from error
    except UnicodeDecodeError as error:
        raise _FileProblem("el archivo no está codificado en UTF-8") from error
    except tomllib.TOMLDecodeError as error:
        raise _FileProblem(f"TOML inválido: {error}") from error

    # Una clave mal escrita, como [[pairs]], dejaría el catálogo incompleto sin aviso
    unknown = sorted(document.keys() - {key})
    if unknown:
        raise _FileProblem(f"claves desconocidas {unknown}; se esperaban tablas [[{key}]]")
    entries = document.get(key, [])
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise _FileProblem(f"'{key}' debe ser una lista de tablas [[{key}]]")
    return entries
