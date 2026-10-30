"""Aplicación FastAPI del Conversor (Req 11.3, 18.1, 18.15, 19.1, 19.5, 19.6, 20.9, 20.10).

`create_app(deps)` arma la aplicación con las dependencias que construye el arranque
(`AppDeps`). No crea objetos de dominio ni lee Variables_de_Configuración: todo llega en `deps`.

| Método y ruta | Respuesta |
|---|---|
| `GET /` | `index.html` de la Interfaz_de_Usuario |
| `GET /static/{name}` | un archivo de la carpeta `web/` |
| `GET /api/health` | Indicador_de_Salud: 200 `{"ready": true}` o 503 `{"ready": false}` |
| `GET /api/config` | `client_config`: datos que la Interfaz_de_Usuario necesita al cargar |
| `POST /api/tasks` | recepción de un archivo (`upload`): 201 con la `TaskView` de la Tarea creada |
| `GET /api/tasks/{id}` | `TaskView` de la Tarea (`tasks`) |
| `POST /api/tasks/{id}/confirm` | confirmación del Formato_de_Destino (`tasks`) |
| `POST /api/tasks/{id}/decline` | rechazo de la confirmación de pérdida (`tasks`) |
| `POST /api/tasks/{id}/cancel` | cancelación (`tasks`) |
| `GET /api/tasks/{id}/result` | descarga del Archivo_Resultado (`download`) |

Comportamiento común:

- Toda respuesta lleva las cabeceras de `security.SECURITY_HEADERS`, también los errores 500,
  porque `SecurityHeadersMiddleware` envuelve toda la pila de middlewares. No hay cabeceras CORS.
- Toda solicitud que modifica estado necesita `X-Converter-Request: 1`
  (`security.RequestMarkerMiddleware`).
- Los errores se responden con el mapeo de `errors.install_error_handlers`; en particular, todo
  404 o 405 bajo `/api/tasks/` es el mismo 404 `tarea_no_disponible` (Req 19.5). Las barras
  finales no se redirigen, así `/api/tasks/` también es ese 404 y no una redirección a
  `/api/tasks`.
- No hay documentación OpenAPI (`/docs`, `/redoc`, `/openapi.json`): la página de FastAPI pide
  recursos a un CDN (Req 18.15, 20.3) y no hace falta.
- Ninguna ruta pide cuentas, credenciales ni cookies (Req 19.1).

Archivos estáticos. Al crear la aplicación se leen a memoria los archivos de `web_dir` que se
pueden servir: archivos regulares del primer nivel, sin seguir enlaces simbólicos, sin los
ocultos (que empiezan con ".") y con una extensión de `MEDIA_TYPES`. `/static/{name}` solo
responde con uno de esos nombres exactos, así ninguna solicitud arma una ruta del sistema de
archivos y no hay recorrido de directorios. Las respuestas llevan `Cache-Control: no-cache`, para
que el navegador no use una Interfaz_de_Usuario vieja después de actualizar la imagen. Si falta
`index.html`, `create_app` lanza `FileNotFoundError`: es un error del build.

Indicador_de_Salud (Req 20.9, 20.10). `/api/health` informa listo solo después de que el
arranque llama a `Readiness.mark_ready()`, una vez abierto el Puerto_de_la_Interfaz. La otra
mitad de la condición, que `GET /` responda en 5 segundos o menos, la comprueba
`converter.healthcheck` pidiendo las dos rutas. La respuesta no tiene Datos_de_Tarea (Req 19.6).

Rutas de otros módulos (`upload`, `tasks` y `download`): cada módulo expone una función
`router(deps)` que recibe `AppDeps` y devuelve un `APIRouter`, y `create_app` lo agrega con
`app.include_router`. `POST /api/tasks` es una ruta exacta, así que no se cruza con las rutas
`/api/tasks/{id}…`. Los errores se lanzan como excepciones
(`TaskUnavailable`, las `TaskRequestError` del Gestor_de_Tareas o `errors.ApiError`) y los
manejadores instalados arman la respuesta.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response

from converter.api import download, tasks, upload
from converter.api.errors import install_error_handlers
from converter.api.security import RequestMarkerMiddleware, SecurityHeadersMiddleware

if TYPE_CHECKING:
    from starlette.types import ASGIApp

    from converter.config import Config
    from converter.eventlog import JsonValue
    from converter.languages import LanguageCatalog
    from converter.registry.registry import ConversionRegistry
    from converter.storage.sweeper import Sweeper
    from converter.storage.workspace import WorkspaceManager
    from converter.tasks.manager import TaskManager

# Carpeta de la Interfaz_de_Usuario dentro del paquete
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
INDEX_FILE = "index.html"

# Archivos que se sirven, por extensión en minúsculas, con su Content-Type
MEDIA_TYPES: Mapping[str, str] = MappingProxyType(
    {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
    }
)
STATIC_CACHE_CONTROL = "no-cache"


class Readiness:
    """Estado del Indicador_de_Salud: no listo hasta que el arranque termina (Req 20.10)."""

    __slots__ = ("_ready",)

    def __init__(self) -> None:
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    def mark_ready(self) -> None:
        """El arranque terminó y el Puerto_de_la_Interfaz está abierto (Req 20.9)."""
        self._ready = True


@dataclass(frozen=True, slots=True, kw_only=True)
class AppDeps:
    """Dependencias de la API, ya construidas por el arranque."""

    config: Config
    registry: ConversionRegistry  # Registro_de_Conversiones ya cargado
    languages: LanguageCatalog  # Idiomas_Soportados; vacío sin Modelo_de_Transcripción
    manager: TaskManager  # Gestor_de_Tareas: creación, vista, confirmación, descarga
    storage: WorkspaceManager  # Espacios_de_Trabajo de las recepciones
    sweeper: Sweeper  # borrado de las recepciones que terminan sin Tarea
    readiness: Readiness = field(default_factory=Readiness)
    web_dir: Path = WEB_DIR  # archivos de la Interfaz_de_Usuario


@dataclass(frozen=True, slots=True)
class StaticAsset:
    """Archivo de la Interfaz_de_Usuario leído a memoria."""

    body: bytes = field(repr=False)
    media_type: str


def load_web_assets(web_dir: Path) -> Mapping[str, StaticAsset]:
    """Archivos que se sirven de `web_dir`, por nombre; ver el docstring del módulo.

    Lanza `FileNotFoundError` si `web_dir` no existe o no tiene `index.html`.
    """
    assets: dict[str, StaticAsset] = {}
    with os.scandir(web_dir) as entries:
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_file(follow_symlinks=False):
                continue
            media_type = MEDIA_TYPES.get(Path(entry.name).suffix.lower())
            if media_type is not None:
                assets[entry.name] = StaticAsset(Path(entry.path).read_bytes(), media_type)
    if INDEX_FILE not in assets:
        raise FileNotFoundError(f"falta {INDEX_FILE} en la carpeta de la Interfaz_de_Usuario")
    return MappingProxyType(assets)


def client_config(deps: AppDeps) -> dict[str, JsonValue]:
    """Cuerpo de `GET /api/config` (Req 11.3, 18.13).

    El Intervalo_de_Progreso en segundos, el Límite_de_Tamaño_Global en MB y en bytes, los
    Idiomas_Soportados con su código y su nombre en español, en el orden en que se ofrecen, y
    los Formatos_de_Origen del Mapa_de_Conversión, en orden de registro. No tiene
    Datos_de_Tarea (Req 19.6).
    """
    return {
        "progress_interval_seconds": deps.config.progress_interval_seconds,
        "max_upload_mb": deps.config.max_upload_mb,
        "max_upload_bytes": deps.config.max_upload_bytes,
        "languages": [
            {"code": language.code, "name": language.name} for language in deps.languages.languages
        ],
        "source_formats": list(deps.registry.source_formats()),
    }


class _ConverterApp(FastAPI):
    def build_middleware_stack(self) -> ASGIApp:
        # Por fuera de toda la pila, incluido el ServerErrorMiddleware de Starlette, para que
        # las cabeceras de seguridad también lleguen en las respuestas 500
        return SecurityHeadersMiddleware(super().build_middleware_stack())


def _asset_response(asset: StaticAsset) -> Response:
    return Response(
        asset.body,
        media_type=asset.media_type,
        headers={"cache-control": STATIC_CACHE_CONTROL},
    )


def _add_ui_routes(app: FastAPI, assets: Mapping[str, StaticAsset]) -> None:
    @app.get("/")
    async def index() -> Response:
        return _asset_response(assets[INDEX_FILE])

    @app.get("/static/{name}")
    async def static_file(name: str) -> Response:
        asset = assets.get(name)
        if asset is None:
            raise HTTPException(status_code=404)
        return _asset_response(asset)


def _add_info_routes(app: FastAPI, deps: AppDeps) -> None:
    @app.get("/api/health")
    async def health() -> Response:
        ready = deps.readiness.ready
        return JSONResponse({"ready": ready}, status_code=200 if ready else 503)

    @app.get("/api/config")
    async def config() -> Response:
        return JSONResponse(client_config(deps))


def create_app(deps: AppDeps) -> FastAPI:
    """Aplicación FastAPI del Conversor con las rutas de la tabla del módulo.

    Lanza `FileNotFoundError` si la carpeta de la Interfaz_de_Usuario no tiene `index.html`.
    """
    assets = load_web_assets(deps.web_dir)
    app = _ConverterApp(
        title="Conversor",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        redirect_slashes=False,
    )
    app.add_middleware(RequestMarkerMiddleware)
    install_error_handlers(app)
    _add_ui_routes(app, assets)
    _add_info_routes(app, deps)
    app.include_router(upload.router(deps))
    app.include_router(tasks.router(deps))
    app.include_router(download.router(deps))
    return app
