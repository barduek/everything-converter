"""Dependencias de la API para las pruebas de `converter.api`.

`build_deps` arma un `AppDeps` con objetos reales: la configuración de `load_config`, un
Registro_de_Conversiones con los Formatos de `catalog/formats.toml` y los pares que pide la
prueba, el Gestor_de_Archivos_Temporales y el barrendero sobre un directorio temporal y un
Gestor_de_Tareas sin Tareas. El Detector_de_Tipo no ejecuta inspectores: las pruebas que crean
Tareas suben texto, que se clasifica en el proceso principal, o archivos cuyo Tipo_Real no es
Formato_de_Origen de los pares registrados, que se rechazan antes de inspeccionar. `client` da
un cliente HTTP que llama a la aplicación ASGI en el mismo proceso, sin abrir puertos, y
`upload_headers` da las cabeceras con las que la Interfaz_de_Usuario envía un archivo.

Los pares de prueba usan el convertidor `CONVERTER`. Sin `converters`, el Gestor_de_Tareas no
lo tiene y una confirmación lleva la Tarea a fallida; las pruebas que ejecutan pares le pasan un
`WritingConverter`, que escribe un Archivo_Resultado real en `work/`. `create_task` sube un
archivo por la API y `wait_for` espera a que la vista de una Tarea cumpla una condición.
"""

from __future__ import annotations

import asyncio
import io
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

import httpx
from fastapi import FastAPI

from converter.api.app import WEB_DIR, AppDeps, Readiness
from converter.config import Config, load_config
from converter.converters.base import ConversionContext, ConversionOutcome, ConversionRequest
from converter.converters.base import Converter as ConverterProtocol
from converter.detection.detector import InspectionRequest, TypeDetector
from converter.detection.source_info import SourceInfo, SourceRejection
from converter.eventlog import EventLog
from converter.languages import LanguageCatalog
from converter.models import DestinationRejection, LossClass
from converter.registry.loader import load_catalog
from converter.registry.registry import ConversionRegistry
from converter.sandbox.runner import SandboxRunner
from converter.storage.sweeper import Sweeper
from converter.storage.workspace import WorkspaceManager
from converter.tasks.manager import TaskManager
from converter.tasks.view import TaskView

CONVERTER = "test.copy"
BASE_URL = "http://testserver"
# Cabecera que envía la Interfaz_de_Usuario en las solicitudes que modifican estado
MARKER = {"X-Converter-Request": "1"}
# Caracteres que `encodeURIComponent` deja sin codificar además de los que `quote` nunca
# codifica (letras, dígitos y "_.-~")
_URI_COMPONENT_SAFE = "!*'()"

# Par de prueba: (origen, destino) Sin_Pérdida, o (origen, destino, Clasificación_de_Pérdida)
type PairSpec = tuple[str, str] | tuple[str, str, LossClass]


def encode_file_name(name: str) -> str:
    """`name` codificado como lo hace `encodeURIComponent` en la Interfaz_de_Usuario."""
    return urllib.parse.quote(name, safe=_URI_COMPONENT_SAFE)


def upload_headers(name: str) -> dict[str, str]:
    """Cabeceras de un envío de la Interfaz_de_Usuario con el Nombre_Original `name`."""
    return {**MARKER, "X-File-Name": encode_file_name(name)}


def config(env: Mapping[str, str] | None = None) -> Config:
    loaded = load_config(env or {})
    assert isinstance(loaded, Config), loaded
    return loaded


def registry(directory: Path, pairs: Iterable[PairSpec] = ()) -> ConversionRegistry:
    """Registro con los Formatos del catálogo y los pares `pairs`, en ese orden."""
    empty = directory / "pairs.toml"
    empty.write_text("", encoding="utf-8")
    loaded = load_catalog(
        [CONVERTER],
        event_log=EventLog(io.StringIO()),
        pairs_path=empty,
        extra_pairs_dir=directory / "pairs.d",
    )
    for source, target, *loss in pairs:
        loss_class = loss[0] if loss else LossClass.LOSSLESS
        loaded.register_pair(
            {
                "source": source,
                "target": target,
                "loss_class": loss_class.value,
                "loss_description": "Par de prueba.",
                "converter": CONVERTER,
            }
        )
    return loaded


class WritingConverter:
    """Convertidor `CONVERTER` que escribe `content` en `work/output` y lo devuelve.

    Con `gate`, espera a que la prueba lo active antes de escribir: la Tarea sigue en_proceso
    mientras tanto. Con `error`, escribe el archivo y después lanza `error`, como un par que
    falla con un resultado parcial.
    """

    id = CONVERTER

    def __init__(
        self,
        content: bytes = b"resultado",
        *,
        gate: asyncio.Event | None = None,
        error: Exception | None = None,
    ) -> None:
        self.content = content
        self.gate = gate
        self.error = error
        self.runs: list[str] = []  # Tareas que empezaron a ejecutar el par

    def preflight(
        self, source: SourceInfo, request: ConversionRequest, config: Config
    ) -> DestinationRejection | None:
        return None

    async def run(self, ctx: ConversionContext) -> ConversionOutcome:
        self.runs.append(ctx.task_id)
        if self.gate is not None:
            await self.gate.wait()
        path = ctx.workspace.work_dir / "output"
        path.write_bytes(self.content)
        if self.error is not None:
            raise self.error
        return ConversionOutcome(path, "single")


async def _no_inspection(request: InspectionRequest) -> SourceInfo | SourceRejection:
    raise AssertionError("las pruebas de la API no inspeccionan archivos")


def build_deps(
    directory: Path,
    *,
    env: Mapping[str, str] | None = None,
    languages: LanguageCatalog | None = None,
    pairs: Iterable[PairSpec] = (),
    web_dir: Path = WEB_DIR,
    readiness: Readiness | None = None,
    converters: Mapping[str, ConverterProtocol] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AppDeps:
    """`AppDeps` con objetos reales sobre `directory`, un directorio temporal de la prueba.

    `converters` son los convertidores del Gestor_de_Tareas (ninguno si es None) y `clock` su
    reloj, que mide el Período_de_Retención.
    """
    settings = config(env)
    catalog = languages if languages is not None else LanguageCatalog()
    conversions = registry(directory, pairs)
    root = directory / "storage"
    root.mkdir()
    storage = WorkspaceManager(root)
    log = EventLog(io.StringIO())
    sweeper = Sweeper(storage, log)
    manager = TaskManager(
        config=settings,
        registry=conversions,
        detector=TypeDetector(conversions, settings, _no_inspection),
        runner=SandboxRunner(),
        sweeper=sweeper,
        event_log=log,
        converters={} if converters is None else converters,
        languages=catalog,
        clock=clock,
    )
    return AppDeps(
        config=settings,
        registry=conversions,
        languages=catalog,
        manager=manager,
        storage=storage,
        sweeper=sweeper,
        readiness=readiness if readiness is not None else Readiness(),
        web_dir=web_dir,
    )


def client(app: FastAPI, *, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
    """Cliente HTTP que llama a la aplicación ASGI `app` sin abrir puertos."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    return httpx.AsyncClient(transport=transport, base_url=BASE_URL)


async def create_task(api: httpx.AsyncClient, name: str, content: bytes) -> str:
    """Sube `content` con el Nombre_Original `name` y devuelve el Identificador_de_Tarea."""
    response = await api.post("/api/tasks", content=content, headers=upload_headers(name))
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]
    assert isinstance(task_id, str)
    return task_id


async def wait_for(
    manager: TaskManager,
    task_id: str,
    ready: Callable[[TaskView], bool],
    *,
    timeout: float = 5.0,
) -> TaskView:
    """Vista de la Tarea `task_id` en cuanto cumple `ready`; falla si no la cumple en `timeout`
    segundos."""
    async with asyncio.timeout(timeout):
        while not ready(view := manager.view(task_id)):
            await asyncio.sleep(0.005)
    return view
