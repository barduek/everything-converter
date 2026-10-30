"""Pruebas de src/converter/api/app.py y src/converter/api/errors.py: la página y los archivos de
la Interfaz_de_Usuario, el Indicador_de_Salud, la configuración del cliente, el 404 único de las
rutas de Tareas y el mapeo de errores a respuestas HTTP (Req 3.4, 11.3, 14.11, 15.8, 15.11,
16.13, 18.1, 18.15, 19.1, 19.5, 19.6, 20.9, 20.10).

La aplicación se arma con dependencias reales (`tests/api_support.py`) y se llama en el mismo
proceso, sin abrir puertos. Las pruebas del mapeo de errores agregan rutas propias que lanzan las
excepciones del Gestor_de_Tareas; las rutas reales de las Tareas se prueban en
`test_api_tasks.py` y `test_api_download.py`.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from html.parser import HTMLParser
from pathlib import Path
from types import MappingProxyType
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.responses import JSONResponse, Response

from converter.api.app import MEDIA_TYPES, WEB_DIR, AppDeps, create_app
from converter.api.errors import TASK_UNAVAILABLE_BODY, ApiError
from converter.api.security import require_task_id
from converter.languages import Language, LanguageCatalog
from converter.messages_es import (
    InvalidMessageError,
    ReceptionError,
    RejectionCode,
    RequestError,
    UnknownCodeError,
    render_error,
)
from converter.models import DestinationRejection, TaskState
from converter.tasks.manager import (
    ConfirmationRequired,
    DownloadUnavailable,
    ResultExpired,
    StateForbids,
    TargetRejected,
    TaskRequestError,
)
from tests.api_support import MARKER, build_deps, client

VALID_ID = "6f1c2c9e-8a0b-4d7e-9a35-2f0d4b8c1e77"  # UUID v4 que no es de ninguna Tarea
UUID1_ID = "6f1c2c9e-8a0b-1d7e-9a35-2f0d4b8c1e77"  # misma forma, pero versión 1


@pytest.fixture
def deps(tmp_path: Path) -> AppDeps:
    return build_deps(tmp_path)


@pytest.fixture
def app(deps: AppDeps) -> FastAPI:
    return create_app(deps)


@pytest_asyncio.fixture
async def api(app: FastAPI, deps: AppDeps) -> AsyncIterator[httpx.AsyncClient]:
    async with client(app) as http:
        yield http
    await deps.manager.aclose()


def _error(code: str, params: Mapping[str, object] | None = None) -> dict[str, object]:
    message = render_error(code, params)
    return {
        "error": {
            "code": message.code,
            "stage": None if message.stage is None else message.stage.value,
            "message": message.message,
            "action": message.action,
        }
    }


def _raising(error: Exception) -> Callable[[], Coroutine[Any, Any, Response]]:
    """Ruta de prueba que lanza `error`."""

    async def endpoint() -> Response:
        raise error

    return endpoint


# --- Interfaz_de_Usuario ---


class _PageScan(HTMLParser):
    """Recursos que pide una página y lo que la CSP de la Interfaz_de_Usuario no permite."""

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.blocked: list[str] = []  # scripts y estilos en línea, manejadores on*

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {name for name, _ in attrs}
        if tag == "style":
            self.blocked.append("<style>")
        if tag == "script" and "src" not in names:
            self.blocked.append("<script> en línea")
        for name, value in attrs:
            if name == "style" or name.startswith("on"):
                self.blocked.append(f"atributo {name}")
            if name in {"href", "src", "srcset", "action", "poster"} and value is not None:
                self.urls.append(value)


@pytest.mark.req("18.1", "18.15")
@pytest.mark.asyncio
async def test_index_is_the_spanish_page_of_the_web_folder(api: httpx.AsyncClient) -> None:
    response = await api.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    assert response.content == (WEB_DIR / "index.html").read_bytes()
    assert '<html lang="es">' in response.text


@pytest.mark.req("18.15")
@pytest.mark.asyncio
async def test_index_only_requests_local_resources_allowed_by_the_csp(
    api: httpx.AsyncClient,
) -> None:
    scan = _PageScan()
    scan.feed((await api.get("/")).text)
    assert scan.blocked == []
    assert scan.urls  # la página usa al menos su hoja de estilos
    for url in scan.urls:
        if url.startswith("data:"):  # img-src admite data:, sin solicitudes
            continue
        assert url.startswith("/static/"), url
        response = await api.get(url)
        assert response.status_code == 200, url
        assert response.headers["content-type"] == MEDIA_TYPES[Path(url).suffix], url


@pytest.mark.req("18.15")
@pytest.mark.asyncio
async def test_static_serves_files_of_the_web_folder(api: httpx.AsyncClient) -> None:
    response = await api.get("/static/styles.css")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/css; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    assert response.content == (WEB_DIR / "styles.css").read_bytes()


@pytest.mark.asyncio
async def test_static_serves_only_listed_files_of_the_folder(tmp_path: Path) -> None:
    web = tmp_path / "web"
    (web / "sub").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (web / "app.js").write_text("export {};", encoding="utf-8")
    (web / ".oculto.js").write_text("secreto", encoding="utf-8")
    (web / "notas.txt").write_text("secreto", encoding="utf-8")
    (web / "sub" / "interno.js").write_text("secreto", encoding="utf-8")
    (tmp_path / "secreto.js").write_text("secreto", encoding="utf-8")
    os.symlink(tmp_path / "secreto.js", web / "enlace.js")
    deps = build_deps(tmp_path, web_dir=web)
    async with client(create_app(deps)) as api:
        served = await api.get("/static/app.js")
        assert served.status_code == 200
        assert served.headers["content-type"] == "text/javascript; charset=utf-8"
        assert served.content == b"export {};"
        for path in (
            "/static/..%2Fsecreto.js",
            "/static/%2e%2e%2Fsecreto.js",
            "/static/%2e%2e",
            "/static/sub/interno.js",
            "/static/sub%2Finterno.js",
            "/static/enlace.js",  # enlace simbólico a un archivo de fuera
            "/static/.oculto.js",
            "/static/notas.txt",  # extensión que no se sirve
            "/static/falta.js",
            "/static/",
        ):
            response = await api.get(path)
            assert response.status_code == 404, path
            assert b"secreto" not in response.content


def test_create_app_requires_index_html(tmp_path: Path) -> None:
    web = tmp_path / "web"
    web.mkdir()
    (web / "styles.css").write_text("", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        create_app(build_deps(tmp_path, web_dir=web))


@pytest.mark.req("18.15")
@pytest.mark.asyncio
async def test_openapi_documentation_is_not_served(api: httpx.AsyncClient) -> None:
    # La página de documentación de FastAPI pide sus recursos a un CDN
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert (await api.get(path)).status_code == 404, path


# --- Indicador_de_Salud ---


@pytest.mark.req("20.9", "20.10", "19.6")
@pytest.mark.asyncio
async def test_health_is_ready_only_after_startup_marks_it(
    api: httpx.AsyncClient, deps: AppDeps
) -> None:
    before = await api.get("/api/health")
    assert before.status_code == 503
    assert before.json() == {"ready": False}

    deps.readiness.mark_ready()
    after = await api.get("/api/health")
    assert after.status_code == 200
    assert after.json() == {"ready": True}  # sin Datos_de_Tarea ni otros campos


# --- Configuración del cliente ---


@pytest.mark.req("11.3", "19.6")
@pytest.mark.asyncio
async def test_config_has_interval_limit_languages_and_source_formats(tmp_path: Path) -> None:
    deps = build_deps(
        tmp_path,
        env={"CONVERTER_PROGRESS_INTERVAL_SECONDS": "7", "CONVERTER_MAX_UPLOAD_MB": "120"},
        languages=LanguageCatalog([Language("es", "español"), Language("pt", "portugués")]),
        # Los Formatos_de_Origen salen en el orden de registro de los Formatos
        pairs=[("TXT", "HTML"), ("MP3", "WAV"), ("PNG", "JPG"), ("TXT", "MD")],
    )
    async with client(create_app(deps)) as api:
        response = await api.get("/api/config")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "progress_interval_seconds": 7,
        "max_upload_mb": 120,
        "max_upload_bytes": 120_000_000,
        "languages": [
            {"code": "es", "name": "español"},
            {"code": "pt", "name": "portugués"},
        ],
        "source_formats": ["PNG", "TXT", "MP3"],
    }
    assert "portugués".encode() in response.content  # UTF-8, sin escapes \u


@pytest.mark.req("11.3")
@pytest.mark.asyncio
async def test_config_without_transcription_model_has_no_languages(
    api: httpx.AsyncClient,
) -> None:
    body = (await api.get("/api/config")).json()
    assert body["languages"] == []
    assert body["source_formats"] == []
    assert body["progress_interval_seconds"] == 5
    assert body["max_upload_mb"] == 500


# --- 404 único de las Tareas ---

MISSING_TASK_REQUESTS: tuple[tuple[str, str], ...] = (
    ("GET", "/api/tasks/"),
    ("GET", "/api/tasks//result"),
    ("GET", "/api/tasks/no-es-un-id"),
    ("GET", "/api/tasks/%20"),
    ("GET", f"/api/tasks/{VALID_ID.upper()}"),
    ("GET", f"/api/tasks/{UUID1_ID}"),
    ("GET", f"/api/tasks/{VALID_ID}"),
    ("GET", f"/api/tasks/{VALID_ID}/result"),
    ("GET", f"/api/tasks/{VALID_ID}/otra"),
    ("POST", "/api/tasks//cancel"),
    ("POST", "/api/tasks//confirm"),
    ("POST", f"/api/tasks/{VALID_ID}/cancel"),
)


@pytest.mark.req("19.5")
@pytest.mark.asyncio
async def test_every_missing_task_route_gets_the_same_404(api: httpx.AsyncClient) -> None:
    responses = [
        await api.request(method, path, headers=MARKER) for method, path in MISSING_TASK_REQUESTS
    ]
    for response in responses:
        assert response.status_code == 404, response.request.url
        assert response.content == TASK_UNAVAILABLE_BODY, response.request.url
        assert dict(response.headers) == dict(responses[0].headers), response.request.url
    assert json.loads(TASK_UNAVAILABLE_BODY) == _error(RequestError.TASK_UNAVAILABLE)


@pytest.mark.req("19.5")
@pytest.mark.asyncio
async def test_unavailable_tasks_in_routes_get_the_same_404(
    app: FastAPI, api: httpx.AsyncClient, deps: AppDeps
) -> None:
    # Una ruta como las de la tarea 12.3: la forma del identificador y su existencia se validan
    # con el mismo error
    async def view(task_id: str) -> Response:
        return JSONResponse(deps.manager.view(require_task_id(task_id)).to_json())

    app.add_api_route("/api/tasks/{task_id}", view, methods=["GET"])
    router_404 = await api.get("/api/tasks//cancel")
    for task_id in (VALID_ID, str(uuid.uuid4()), "no-es-un-id", VALID_ID.upper(), UUID1_ID):
        response = await api.get(f"/api/tasks/{task_id}")
        assert response.status_code == 404, task_id
        assert response.content == TASK_UNAVAILABLE_BODY, task_id
        assert dict(response.headers) == dict(router_404.headers), task_id


@pytest.mark.asyncio
async def test_other_missing_routes_keep_the_default_404(api: httpx.AsyncClient) -> None:
    for path in ("/no-existe", "/api/otra", "/api/taskss/x"):
        response = await api.get(path)
        assert response.status_code == 404, path
        assert response.content != TASK_UNAVAILABLE_BODY, path


# --- Mapeo de errores ---


@pytest.mark.parametrize(
    ("error", "status"),
    [
        pytest.param(StateForbids(TaskState.PROCESSING), 409, marks=pytest.mark.req("14.11")),
        pytest.param(ConfirmationRequired(), 409, marks=pytest.mark.req("3.3")),
        pytest.param(DownloadUnavailable(TaskState.FAILED), 409, marks=pytest.mark.req("15.11")),
        pytest.param(ResultExpired(), 410, marks=pytest.mark.req("15.8")),
        pytest.param(
            TargetRejected(
                DestinationRejection(
                    code=RejectionCode.DIMENSIONS_EXCEEDED,
                    params=MappingProxyType(
                        {"ancho": 17000, "alto": 3000, "destino": "WEBP", "maximo": 16383}
                    ),
                )
            ),
            422,
            marks=pytest.mark.req("16.12"),
        ),
    ],
)
@pytest.mark.asyncio
async def test_task_request_errors_use_the_catalog_status_and_message(
    app: FastAPI, api: httpx.AsyncClient, error: TaskRequestError, status: int
) -> None:
    app.add_api_route("/api/prueba", _raising(error), methods=["GET"])
    response = await api.get("/api/prueba")
    assert response.status_code == status
    assert response.json() == _error(error.code, dict(error.params))


@pytest.mark.req("3.4")
@pytest.mark.asyncio
async def test_target_unavailable_lists_the_offered_targets(
    app: FastAPI, api: httpx.AsyncClient
) -> None:
    params = {"destino": "MP3", "tipo_real": "PNG", "destinos": ["JPG", "WEBP", "PDF"]}
    rejection = DestinationRejection(
        code=RejectionCode.TARGET_UNAVAILABLE, params=MappingProxyType(params)
    )
    app.add_api_route("/api/prueba", _raising(TargetRejected(rejection)), methods=["GET"])
    response = await api.get("/api/prueba")
    assert response.status_code == 422
    body = response.json()
    expected = _error(RejectionCode.TARGET_UNAVAILABLE, params)["error"]
    assert isinstance(expected, dict)
    assert body == {"error": {**expected, "targets": ["JPG", "WEBP", "PDF"]}}
    assert body["error"]["message"] == "El destino «MP3» no está disponible para archivos PNG."


@pytest.mark.req("16.13", "1.3")
@pytest.mark.asyncio
async def test_api_errors_use_the_catalog_status_stage_and_message(
    app: FastAPI, api: httpx.AsyncClient
) -> None:
    error = ApiError(ReceptionError.TOO_LARGE, {"limite_mb": 500})
    app.add_api_route("/api/prueba", _raising(error), methods=["GET"])
    response = await api.get("/api/prueba")
    assert response.status_code == 413
    body = response.json()
    assert body == _error(ReceptionError.TOO_LARGE, {"limite_mb": 500})
    assert body["error"]["stage"] == "recepción"
    assert body["error"]["message"] == "El archivo excede el límite de 500 MB."


def test_api_error_validates_its_code_and_parameters() -> None:
    assert ApiError(RequestError.INVALID_REQUEST).status_code == 400
    assert ApiError(ReceptionError.NO_SPACE).status_code == 507
    with pytest.raises(UnknownCodeError):
        ApiError("no_existe")
    with pytest.raises(InvalidMessageError):
        ApiError(ReceptionError.TOO_LARGE)  # falta el límite
    with pytest.raises(ValueError, match="no es una respuesta"):
        ApiError(ReceptionError.INTERRUPTED)  # solo lo muestra la Interfaz_de_Usuario


@pytest.mark.asyncio
async def test_request_validation_errors_are_invalid_requests(
    app: FastAPI, api: httpx.AsyncClient
) -> None:
    async def endpoint(n: int) -> Response:
        return JSONResponse({"n": n})

    app.add_api_route("/api/prueba", endpoint, methods=["GET"])
    response = await api.get("/api/prueba", params={"n": "no-es-un-número"})
    assert response.status_code == 400
    assert response.json() == _error(RequestError.INVALID_REQUEST)


# --- Sin cuentas ---


@pytest.mark.req("19.1")
@pytest.mark.asyncio
async def test_routes_need_no_credentials_or_cookies(api: httpx.AsyncClient, deps: AppDeps) -> None:
    deps.readiness.mark_ready()
    for path in ("/", "/static/styles.css", "/api/health", "/api/config"):
        response = await api.get(path)  # sin credenciales ni cookies
        assert response.status_code == 200, path
        assert "set-cookie" not in response.headers, path
        assert "www-authenticate" not in response.headers, path
    assert not api.cookies
