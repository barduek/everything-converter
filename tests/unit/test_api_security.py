"""Pruebas de src/converter/api/security.py: cabeceras de seguridad, ausencia de CORS, cabecera
`X-Converter-Request` en las solicitudes que modifican estado, lectura de cuerpos JSON y forma
del Identificador_de_Tarea (Req 16.13, 18.15, 19.5).

La aplicación es la de `create_app` con dependencias reales (`tests/api_support.py`), más rutas
de prueba que registran si llegaron a ejecutarse.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from converter.api.app import AppDeps, create_app
from converter.api.security import (
    MAX_JSON_BODY_BYTES,
    SECURITY_HEADERS,
    is_task_id,
    read_json_object,
    require_task_id,
)
from converter.tasks.manager import TaskUnavailable
from tests.api_support import MARKER, build_deps, client

# CSP de la tabla "Seguridad" del diseño, copiada tal cual
DESIGN_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; form-action 'self'; base-uri 'none'; "
    "frame-ancestors 'none'"
)
EXPECTED_HEADERS = {
    "content-security-policy": DESIGN_CSP,
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "x-content-type-options": "nosniff",
}


class Probe:
    """Rutas de prueba agregadas a la aplicación; `calls` cuenta las que se ejecutaron."""

    def __init__(self, app: FastAPI) -> None:
        self.calls = 0
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            app.add_api_route("/api/prueba", self.endpoint, methods=[method])
        app.add_api_route("/api/json", self.echo_json, methods=["POST"])
        app.add_api_route("/api/falla", self.crash, methods=["GET"])
        app.add_api_route("/api/sandbox", self.sandboxed, methods=["GET"])

    async def endpoint(self) -> Response:
        self.calls += 1
        return JSONResponse({"ok": True})

    async def echo_json(self, request: Request) -> Response:
        self.calls += 1
        return JSONResponse(await read_json_object(request))

    async def crash(self) -> Response:
        raise RuntimeError("error de programación")

    async def sandboxed(self) -> Response:
        # Como la descarga de la tarea 12.3, que fija su propia CSP
        return Response(b"x", headers={"Content-Security-Policy": "sandbox"})


@pytest.fixture
def deps(tmp_path: Path) -> AppDeps:
    return build_deps(tmp_path)


@pytest.fixture
def app(deps: AppDeps) -> FastAPI:
    return create_app(deps)


@pytest.fixture
def probe(app: FastAPI) -> Probe:
    return Probe(app)


@pytest_asyncio.fixture
async def api(app: FastAPI, deps: AppDeps, probe: Probe) -> AsyncIterator[httpx.AsyncClient]:
    async with client(app) as http:
        yield http
    await deps.manager.aclose()


def _security_headers(response: httpx.Response) -> dict[str, str]:
    return {name: response.headers.get(name, "") for name in EXPECTED_HEADERS}


# --- Cabeceras de seguridad ---


def test_security_headers_are_the_ones_of_the_design() -> None:
    assert dict(SECURITY_HEADERS) == EXPECTED_HEADERS


@pytest.mark.req("18.15")
@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/", 200),
        ("GET", "/static/styles.css", 200),
        ("GET", "/api/health", 503),
        ("GET", "/api/config", 200),
        ("GET", "/api/tasks//cancel", 404),
        ("GET", "/no-existe", 404),
        ("POST", "/api/prueba", 400),  # sin X-Converter-Request
        ("OPTIONS", "/api/config", 405),
    ],
)
@pytest.mark.asyncio
async def test_every_response_has_the_security_headers(
    api: httpx.AsyncClient, method: str, path: str, status: int
) -> None:
    response = await api.request(method, path)
    assert response.status_code == status
    assert _security_headers(response) == EXPECTED_HEADERS


@pytest.mark.req("18.15")
@pytest.mark.asyncio
async def test_server_errors_also_have_the_security_headers(app: FastAPI, probe: Probe) -> None:
    async with client(app, raise_app_exceptions=False) as api:
        response = await api.get("/api/falla")
    assert response.status_code == 500
    assert b"programaci" not in response.content  # sin el detalle de la excepción
    assert _security_headers(response) == EXPECTED_HEADERS


@pytest.mark.asyncio
async def test_a_route_keeps_its_own_content_security_policy(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/sandbox")
    assert response.headers.get_list("content-security-policy") == ["sandbox"]
    others = {**EXPECTED_HEADERS, "content-security-policy": "sandbox"}
    assert _security_headers(response) == others


# --- Sin CORS ---


@pytest.mark.parametrize("path", ["/api/prueba", "/api/config", "/api/tasks"])
@pytest.mark.asyncio
async def test_cors_preflights_are_not_approved(
    api: httpx.AsyncClient, probe: Probe, path: str
) -> None:
    response = await api.options(
        path,
        headers={
            "Origin": "https://otro.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-converter-request, content-type",
        },
    )
    assert response.status_code >= 400
    assert not [name for name in response.headers if name.startswith("access-control-")]
    assert probe.calls == 0


@pytest.mark.asyncio
async def test_responses_have_no_cors_headers(api: httpx.AsyncClient) -> None:
    for path in ("/", "/api/config", "/api/health"):
        response = await api.get(path, headers={"Origin": "https://otro.example"})
        assert not [name for name in response.headers if name.startswith("access-control-")]


# --- X-Converter-Request ---


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("X-Converter-Request", "0")],
        [("X-Converter-Request", "true")],
        [("X-Converter-Request", "")],
        [("X-Converter-Request", "1"), ("X-Converter-Request", "1")],
    ],
)
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.asyncio
async def test_state_changes_without_the_marker_are_rejected(
    api: httpx.AsyncClient, probe: Probe, method: str, headers: list[tuple[str, str]]
) -> None:
    response = await api.request(method, "/api/prueba", headers=headers, content=b"{}")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "solicitud_invalida"
    assert response.json()["error"]["stage"] is None
    assert probe.calls == 0  # la solicitud no llegó a la ruta


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.asyncio
async def test_state_changes_with_the_marker_reach_the_route(
    api: httpx.AsyncClient, probe: Probe, method: str
) -> None:
    response = await api.request(method, "/api/prueba", headers=MARKER)
    assert response.status_code == 200
    assert probe.calls == 1


@pytest.mark.asyncio
async def test_safe_methods_do_not_need_the_marker(api: httpx.AsyncClient) -> None:
    assert (await api.get("/api/config")).status_code == 200
    # OPTIONS llega al enrutador, que no tiene rutas OPTIONS: 405 y no el 400 de la cabecera
    assert (await api.options("/api/prueba")).status_code == 405


@pytest.mark.req("16.13")
@pytest.mark.asyncio
async def test_an_upload_without_the_marker_is_an_invalid_reception(
    api: httpx.AsyncClient,
) -> None:
    response = await api.post("/api/tasks", content=b"contenido", headers={"X-File-Name": "a.txt"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "recepcion_solicitud_invalida"
    assert error["stage"] == "recepción"


# --- Cuerpos JSON ---


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "application/json; charset=utf-8", "Application/JSON; Charset=UTF-8"],
)
@pytest.mark.asyncio
async def test_json_bodies_with_json_content_type_are_read(
    api: httpx.AsyncClient, content_type: str
) -> None:
    body = '{"target": "JPG", "language": null, "acknowledge_loss": true, "nota": "año"}'
    response = await api.post(
        "/api/json",
        content=body.encode(),
        headers={**MARKER, "Content-Type": content_type},
    )
    assert response.status_code == 200
    assert response.json() == {
        "target": "JPG",
        "language": None,
        "acknowledge_loss": True,
        "nota": "año",
    }


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        (None, b"{}"),
        ("text/plain", b"{}"),
        ("application/x-www-form-urlencoded", b"target=JPG"),
        ("multipart/form-data; boundary=x", b"{}"),
        ("application/jsonx", b"{}"),
        ("application/json; charset=latin-1", b"{}"),
        ("application/json", b""),
        ("application/json", b"{"),
        ("application/json", b"[1, 2]"),
        ("application/json", b'"JPG"'),
        ("application/json", b'{"nota": "\xff"}'),  # no es UTF-8
        ("application/json", b'{"a": "' + b"x" * MAX_JSON_BODY_BYTES + b'"}'),  # demasiado
    ],
)
@pytest.mark.asyncio
async def test_other_bodies_are_invalid_requests(
    api: httpx.AsyncClient, probe: Probe, content_type: str | None, body: bytes
) -> None:
    headers = dict(MARKER)
    if content_type is not None:
        headers["Content-Type"] = content_type
    response = await api.post("/api/json", content=body, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "solicitud_invalida"


@pytest.mark.asyncio
async def test_long_bodies_without_content_length_are_cut(api: httpx.AsyncClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"a": "'
        for _ in range(MAX_JSON_BODY_BYTES // 1024 + 1):
            yield b"x" * 1024
        yield b'"}'

    headers = {**MARKER, "Content-Type": "application/json"}
    response = await api.post("/api/json", content=chunks(), headers=headers)
    assert "content-length" not in response.request.headers  # se envía por partes
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "solicitud_invalida"


# --- Identificador_de_Tarea ---


@pytest.mark.req("19.5")
def test_task_ids_are_canonical_lowercase_uuid4() -> None:
    for _ in range(20):
        assert is_task_id(str(uuid.uuid4()))
    valid = "6f1c2c9e-8a0b-4d7e-9a35-2f0d4b8c1e77"
    assert require_task_id(valid) == valid
    for value in (
        valid.upper(),
        "6f1c2c9e-8a0b-1d7e-9a35-2f0d4b8c1e77",  # versión 1
        "6f1c2c9e-8a0b-4d7e-ca35-2f0d4b8c1e77",  # variante que no es la de RFC 4122
        valid.replace("-", ""),
        "{" + valid + "}",
        valid + "\n",
        " " + valid,
        valid[:-1],
        "",
        None,
        uuid.UUID(valid),
    ):
        assert not is_task_id(value), value
        with pytest.raises(TaskUnavailable):
            require_task_id(value)
