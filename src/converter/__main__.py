"""Arranque del Conversor: `python -m converter` (Req 4.9, 14.13, 17.5, 20.2, 20.5, 20.6, 20.9,
20.13).

Secuencia del diagrama «Arranque e Indicador_de_Salud» del diseño. Los pasos 1 y 2 pueden
terminar el proceso y no hacen trabajo pesado, así la salida con código distinto de 0 ocurre en
menos de 10 segundos (Req 20.6, 20.13):

1. Variables_de_Configuración: `load_config(os.environ)`. Si alguna es inválida, el Log_de_Eventos
   registra un evento `config_invalid` por variable, con su nombre y el motivo, y el proceso
   termina con el código 78 (EX_CONFIG). No toca el almacenamiento ni abre el puerto.
2. Almacenamiento_Temporal: comprueba que se pueden crear archivos y elimina todos los
   Espacios_de_Trabajo (Req 17.5). Si no se puede, registra `storage_not_writable` y termina con
   el código 73 (EX_CANTCREAT). La purga conserva todo lo que no es un Espacio_de_Trabajo del
   volumen (Req 20.8); los que no puede eliminar los toma el barrendero para reintentar.
3. Convertidores y catálogo: `load_converters()` y `load_catalog()`. Un catálogo inicial inválido
   es un error de programación: registra `catalog_invalid` y termina con el código 70
   (EX_SOFTWARE) (Req 4.9).
4. Funciones `@on_startup` de las familias de convertidores, por ejemplo la lista de
   decodificadores de FFmpeg. Una que falla se registra y no impide el arranque.
5. `LanguageCatalog`. Un `languages.es.json` que existe pero no cumple el formato es un error del
   build: registra `catalog_invalid` y termina con el código 70.
6. Apertura del Puerto_de_la_Interfaz con Uvicorn: un worker y sin log de accesos, para que los
   Identificadores_de_Tarea no queden en registros. El puerto se abre después de la purga, así
   que el Conversor no acepta archivos antes de ella (Req 17.5). Al abrirlo, el
   Indicador_de_Salud pasa a listo (Req 20.9) y el log registra `service_ready`.

El Gestor_de_Tareas empieza sin Tareas: no recupera las de un inicio anterior, y la purga del
paso 2 elimina sus archivos (Req 14.13). Las CPU efectivas del Contenedor se calculan una vez al
empezar el paso 3 y no cambian durante el ciclo de vida del proceso (Req 11.8, 13.4).

Mientras el servidor atiende, el barrendero del Gestor_de_Archivos_Temporales y la retención del
Gestor_de_Tareas corren como tareas de fondo en el mismo bucle asyncio. Al apagarse, después de
que Uvicorn atiende las solicitudes en curso, se detienen esas tareas y las conversiones en
curso. Eso ocurre dentro del apagado de Uvicorn y no después: al terminar, Uvicorn vuelve a
emitir la señal que recibió (SIGTERM o SIGINT) y el proceso puede terminar en ese instante.

Las rutas del arranque son fijas y ninguna Variable_de_Configuración las cambia (Req 20.7).
`Locations` y `on_server` existen solo para que las pruebas usen directorios temporales y
alcancen el servidor.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import uvicorn

from converter.api.app import AppDeps, create_app
from converter.config import Config, load_config
from converter.converters import load_converters
from converter.converters.base import StartupContext, run_startup_hooks
from converter.detection.detector import TypeDetector
from converter.detection.inspect_runner import InspectRunner
from converter.eventlog import EventLog, Level, describe_exception
from converter.languages import DEFAULT_LANGUAGES_PATH, LanguageCatalog, LanguageCatalogError
from converter.registry.loader import (
    EXTRA_PAIRS_DIR,
    FORMATS_PATH,
    PAIRS_PATH,
    CatalogError,
    load_catalog,
)
from converter.sandbox.cpus import effective_cpus
from converter.sandbox.runner import SandboxRunner
from converter.storage.sweeper import Sweeper
from converter.storage.workspace import STORAGE_ROOT, WorkspaceManager
from converter.tasks.manager import TaskManager

if TYPE_CHECKING:
    import socket

    from fastapi import FastAPI

    from converter.config import ConfigError

# Códigos de salida de sysexits.h (los de `os.EX_*` no existen en todas las plataformas)
EXIT_SOFTWARE = 70  # catálogo inicial inválido (Req 4.9)
EXIT_CANTCREAT = 73  # el Almacenamiento_Temporal no permite crear archivos (Req 20.13)
EXIT_CONFIG = 78  # Variables_de_Configuración inválidas (Req 20.6)

# El Contenedor atiende en todas sus interfaces; quien lo ejecuta decide a cuáles publica el
# Puerto_de_la_Interfaz con `-p`
HOST = "0.0.0.0"
# Tiempo que Uvicorn espera a que terminen las solicitudes en curso al apagarse, dentro de los
# 10 segundos que `docker stop` espera antes de enviar SIGKILL
GRACEFUL_SHUTDOWN_SECONDS = 5

# Eventos del Log_de_Eventos del arranque
CONFIG_INVALID = "config_invalid"
STORAGE_NOT_WRITABLE = "storage_not_writable"
CATALOG_INVALID = "catalog_invalid"
SERVICE_READY = "service_ready"
BACKGROUND_TASK_FAILED = "background_task_failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class Locations:
    """Rutas que lee el arranque. Los valores predeterminados son los del Contenedor."""

    storage: Path = STORAGE_ROOT  # Almacenamiento_Temporal (Req 20.7)
    formats: Path = FORMATS_PATH  # catálogo inicial: Formatos
    pairs: Path = PAIRS_PATH  # catálogo inicial: Pares_de_Conversión
    extra_pairs: Path = EXTRA_PAIRS_DIR  # catálogos adicionales
    languages: Path = DEFAULT_LANGUAGES_PATH  # languages.es.json


def server_config(app: FastAPI, config: Config) -> uvicorn.Config:
    """Configuración de Uvicorn: el Puerto_de_la_Interfaz, un worker y sin log de accesos."""
    return uvicorn.Config(
        app,
        host=HOST,
        port=config.port,
        workers=1,
        access_log=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


class Server(uvicorn.Server):
    """Servidor de Uvicorn que avisa cuándo abrió el puerto y cuándo terminó de apagarse."""

    def __init__(
        self,
        config: uvicorn.Config,
        *,
        on_started: Callable[[], None],
        on_stopped: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(config)
        self._on_started = on_started
        self._on_stopped = on_stopped

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        # `started` se activa cuando el socket ya está abierto y escuchando
        if self.started:
            self._on_started()

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        try:
            await super().shutdown(sockets=sockets)
        finally:
            await self._on_stopped()


class Background:
    """Tareas de fondo del proceso y el cierre del Gestor_de_Tareas.

    `start` lanza el barrendero y la retención. `stop` los detiene y después interrumpe los
    trabajos de las Tareas; se puede llamar más de una vez.
    """

    def __init__(self, sweeper: Sweeper, manager: TaskManager, log: EventLog) -> None:
        self._sweeper = sweeper
        self._manager = manager
        self._log = log
        self._tasks: list[asyncio.Task[NoReturn]] = []

    def start(self) -> None:
        for name, work in (
            ("sweeper", self._sweeper.run()),
            ("retention", self._manager.run_retention()),
        ):
            task = asyncio.create_task(work, name=name)
            task.add_done_callback(self._failed)
            self._tasks.append(task)

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._manager.aclose()

    def _failed(self, task: asyncio.Task[NoReturn]) -> None:
        # Sin esto, un fallo inesperado detendría la limpieza sin dejar rastro. Se registra la
        # clase y la ubicación de la excepción, nunca su mensaje (Req 16.8)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._log.emit(
                Level.ERROR,
                BACKGROUND_TASK_FAILED,
                {"task": task.get_name(), **describe_exception(error).as_json()},
            )


def _log_invalid_config(log: EventLog, errors: list[ConfigError]) -> None:
    for error in errors:
        log.emit(Level.ERROR, CONFIG_INVALID, {"variable": error.variable, "reason": error.reason})


def _prepare_storage(storage: WorkspaceManager, sweeper: Sweeper, log: EventLog) -> bool:
    """Comprueba que se pueden crear archivos y elimina los Espacios_de_Trabajo.

    Devuelve False, tras registrar el motivo, si el Almacenamiento_Temporal no se puede usar.
    Los Espacios_de_Trabajo que la purga no pudo eliminar quedan para los reintentos del
    barrendero (Req 17.9).
    """
    try:
        storage.check_writable()
        failures = storage.purge_all_workspaces()
    except OSError as error:
        reason = os.strerror(error.errno) if isinstance(error.errno, int) else type(error).__name__
        log.emit(
            Level.ERROR,
            STORAGE_NOT_WRITABLE,
            {
                "path": str(storage.root),
                "reason": reason,
                "detail": describe_exception(error).as_json(),
            },
        )
        return False
    sweeper.enqueue_purge_failures(failures)
    return True


async def _run(
    config: Config,
    storage: WorkspaceManager,
    sweeper: Sweeper,
    log: EventLog,
    locations: Locations,
    on_server: Callable[[Server], None] | None,
) -> int:
    """Pasos 3 a 6 de la secuencia. Devuelve el código de salida."""
    cpus = effective_cpus()  # se fija aquí para todo el ciclo de vida (Req 11.8, 13.4)
    runner = SandboxRunner()

    try:
        converters = load_converters()
        registry = load_catalog(
            converters,
            event_log=log,
            formats_path=locations.formats,
            pairs_path=locations.pairs,
            extra_pairs_dir=locations.extra_pairs,
        )
    except CatalogError as error:
        log.emit(Level.ERROR, CATALOG_INVALID, {"file": str(error.path), "reason": error.reason})
        return EXIT_SOFTWARE

    await run_startup_hooks(
        StartupContext(config=config, runner=runner, storage=storage, event_log=log)
    )

    try:
        languages = LanguageCatalog.load(locations.languages)
    except LanguageCatalogError as error:
        log.emit(
            Level.ERROR, CATALOG_INVALID, {"file": str(locations.languages), "reason": str(error)}
        )
        return EXIT_SOFTWARE

    manager = TaskManager(
        config=config,
        registry=registry,
        detector=TypeDetector(
            registry,
            config,
            InspectRunner(runner, max_image_pixels=config.max_image_pixels, cpus=cpus),
        ),
        runner=runner,
        sweeper=sweeper,
        event_log=log,
        converters=converters,
        languages=languages,
    )
    deps = AppDeps(
        config=config,
        registry=registry,
        languages=languages,
        manager=manager,
        storage=storage,
        sweeper=sweeper,
    )
    background = Background(sweeper, manager, log)

    def started() -> None:
        deps.readiness.mark_ready()
        log.emit(Level.INFO, SERVICE_READY, {"port": config.port, "cpus": cpus})

    server = Server(
        server_config(create_app(deps), config), on_started=started, on_stopped=background.stop
    )
    if on_server is not None:
        on_server(server)
    background.start()
    try:
        await server.serve()
    finally:
        # Cubre el arranque del servidor que falla (por ejemplo, con el puerto ocupado); si el
        # apagado fue normal, `stop` ya se ejecutó
        await background.stop()
    return 0


def main(
    env: Mapping[str, str] | None = None,
    *,
    locations: Locations | None = None,
    on_server: Callable[[Server], None] | None = None,
) -> int:
    """Arranca el Conversor y devuelve el código de salida cuando el servidor termina.

    `env` son las Variables_de_Configuración (`os.environ` si se omite). `locations` y
    `on_server` son para las pruebas: `on_server` recibe el servidor ya creado, con la purga
    hecha y antes de abrir el puerto.
    """
    where = locations if locations is not None else Locations()
    log = EventLog()

    loaded = load_config(os.environ if env is None else env)
    if not isinstance(loaded, Config):
        _log_invalid_config(log, loaded)
        return EXIT_CONFIG

    storage = WorkspaceManager(where.storage)
    sweeper = Sweeper(storage, log)
    if not _prepare_storage(storage, sweeper, log):
        return EXIT_CANTCREAT

    try:
        return asyncio.run(_run(loaded, storage, sweeper, log, where, on_server))
    except KeyboardInterrupt:
        # Uvicorn vuelve a emitir SIGINT cuando termina de apagarse
        return 0


if __name__ == "__main__":
    sys.exit(main())
