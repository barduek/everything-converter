"""Pruebas de src/converter/healthcheck.py: el cliente del Indicador_de_Salud que ejecuta el
`HEALTHCHECK` de Docker (Req 20.9, 20.10).

Las pruebas hablan con servidores reales en `127.0.0.1` y un puerto libre: un servidor de prueba
de sockets, que permite fijar el estado, el cuerpo, la demora y el goteo de cada ruta, y, para el
contrato con la API, la aplicación de `converter.api.app` servida por Uvicorn. Los plazos de las
pruebas son de fracciones de segundo para que sean rápidas; el valor de 5 segundos de la
especificación se comprueba como constante y en las llamadas sin plazo explícito.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn

from converter import healthcheck
from converter.api.app import AppDeps, create_app
from converter.healthcheck import (
    EXIT_NOT_READY,
    EXIT_READY,
    HEALTH_PATH,
    HOST,
    INDEX_PATH,
    MODULE,
    TIMEOUT_SECONDS,
    HttpResponse,
    InvalidResponse,
    check,
    fetch,
    is_ready,
    main,
    parse_response,
)
from tests.api_support import build_deps

_TIMEOUT = 0.4  # plazo de las pruebas, en segundos
_SLACK = 1.5  # lo que puede tardar una prueba más allá de su plazo, en máquinas lentas
_SUBPROCESS_TIMEOUT_S = 60
_REASONS = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}


@dataclass(frozen=True, slots=True, kw_only=True)
class Route:
    """Cómo responde el servidor de prueba a una ruta."""

    status: int = 200
    body: bytes = b""
    delay: float = 0.0  # espera antes de empezar a responder, en segundos
    drip: float = 0.0  # pausa entre cada byte de la respuesta, en segundos
    raw: bytes | None = None  # bytes que se envían tal cual, en lugar de una respuesta HTTP
    keep_open: bool = False  # no cierra la conexión después de responder


READY_ROUTES: Mapping[str, Route] = {
    INDEX_PATH: Route(body=b"<!doctype html><title>Conversor</title>"),
    HEALTH_PATH: Route(body=b'{"ready": true}'),
}
NOT_READY_ROUTES: Mapping[str, Route] = {
    **READY_ROUTES,
    HEALTH_PATH: Route(status=503, body=b'{"ready": false}'),
}


def _response_bytes(route: Route) -> bytes:
    if route.raw is not None:
        return route.raw
    head = (
        f"HTTP/1.0 {route.status} {_REASONS.get(route.status, 'Status')}\r\n"
        f"Content-Length: {len(route.body)}\r\n"
        "Content-Type: application/json\r\n"
        "\r\n"
    )
    return head.encode("ascii") + route.body


class _Handler(socketserver.StreamRequestHandler):
    server: FakeServer

    def handle(self) -> None:
        request_line = self.rfile.readline()
        while self.rfile.readline() not in (b"\r\n", b"\n", b""):  # cabeceras de la solicitud
            pass
        parts = request_line.split()
        if len(parts) < 2:
            return
        path = parts[1].decode("ascii", "replace")
        self.server.requests.append(path)
        route = self.server.routes.get(path, Route(status=404))
        stop = self.server.stop
        if route.delay and stop.wait(route.delay):
            return
        payload = _response_bytes(route)
        if route.drip:
            for i in range(len(payload)):
                self.wfile.write(payload[i : i + 1])
                self.wfile.flush()
                if stop.wait(route.drip):
                    return
        else:
            self.wfile.write(payload)
            self.wfile.flush()
        if route.keep_open:
            stop.wait(30)


class FakeServer(socketserver.ThreadingTCPServer):
    """Servidor HTTP mínimo en `127.0.0.1` que responde según `routes`."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, routes: Mapping[str, Route]) -> None:
        super().__init__((HOST, 0), _Handler)
        self.routes = routes
        self.requests: list[str] = []  # rutas pedidas, en orden
        self.stop = threading.Event()  # libera a los manejadores que esperan

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def handle_error(self, request: object, client_address: object) -> None:
        # El cliente corta la conexión cuando se agota su plazo: no es un error de la prueba
        pass


type Serve = Callable[[Mapping[str, Route]], FakeServer]


@pytest.fixture
def serve() -> Iterator[Serve]:
    servers: list[FakeServer] = []

    def start(routes: Mapping[str, Route]) -> FakeServer:
        server = FakeServer(routes)
        # Un sondeo corto hace que `shutdown` no espere el medio segundo predeterminado
        thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
        thread.start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.stop.set()
        server.shutdown()
        server.server_close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def _timed[T](call: Callable[[], T]) -> tuple[T, float]:
    start = time.monotonic()
    result = call()
    return result, time.monotonic() - start


# --- Contrato con el diseño ---


@pytest.mark.req("20.9", "20.10")
def test_each_request_has_a_five_second_timeout_by_default() -> None:
    assert TIMEOUT_SECONDS == 5.0
    for function in (fetch, check):
        assert inspect.signature(function).parameters["timeout"].default == 5.0


# --- Respuesta de /api/health ---


@pytest.mark.req("20.9")
def test_ready_when_both_respond_and_health_says_ready(serve: Serve) -> None:
    server = serve(READY_ROUTES)
    assert check(server.port, timeout=_TIMEOUT) is None
    assert server.requests == [INDEX_PATH, HEALTH_PATH]  # primero la página, después el estado


@pytest.mark.req("20.10")
def test_not_ready_when_health_reports_not_ready(serve: Serve) -> None:
    server = serve(NOT_READY_ROUTES)
    reason = check(server.port, timeout=_TIMEOUT)
    assert reason is not None
    assert "no listo" in reason
    assert "503" in reason
    assert server.requests == [INDEX_PATH, HEALTH_PATH]


@pytest.mark.req("20.9", "20.10")
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, b'{"ready": true}', True),
        (200, b'{"ready":true,"extra":1}', True),
        (503, b'{"ready": false}', False),
        (503, b'{"ready": true}', False),  # el estado HTTP también cuenta
        (500, b'{"ready": true}', False),
        (204, b"", False),
        (200, b'{"ready": false}', False),
        (200, b"{}", False),
        (200, b'{"ready": "true"}', False),
        (200, b'{"ready": 1}', False),
        (200, b'{"ready": null}', False),
        (200, b"[true]", False),
        (200, b"true", False),
        (200, b"listo", False),
        (200, b"", False),
        (200, b"\xff\xfe", False),  # no es UTF-8
        (200, b"[" * 100_000, False),  # anidado en exceso
    ],
)
def test_is_ready_needs_status_200_and_ready_true(status: int, body: bytes, expected: bool) -> None:
    assert is_ready(HttpResponse(status=status, body=body)) is expected


# --- Plazo de 5 segundos ---


@pytest.mark.req("20.10")
def test_not_ready_when_index_does_not_respond_in_time(serve: Serve) -> None:
    server = serve({**READY_ROUTES, INDEX_PATH: Route(delay=30)})
    reason, elapsed = _timed(lambda: check(server.port, timeout=_TIMEOUT))
    assert reason is not None
    assert reason.startswith(f"GET {INDEX_PATH} no respondió")
    assert _TIMEOUT * 0.9 <= elapsed < _TIMEOUT + _SLACK
    assert server.requests == [INDEX_PATH]  # no pide el estado si la página no responde


@pytest.mark.req("20.10")
def test_not_ready_when_health_does_not_respond_in_time(serve: Serve) -> None:
    server = serve({**READY_ROUTES, HEALTH_PATH: Route(body=b'{"ready": true}', delay=30)})
    reason, elapsed = _timed(lambda: check(server.port, timeout=_TIMEOUT))
    assert reason is not None
    assert reason.startswith(f"GET {HEALTH_PATH} no respondió")
    assert _TIMEOUT * 0.9 <= elapsed < _TIMEOUT + _SLACK


@pytest.mark.req("20.10")
def test_timeout_covers_the_whole_response_not_each_read(serve: Serve) -> None:
    # Un byte cada 50 ms: ninguna lectura tarda más que el plazo, pero la respuesta completa sí
    dripping = Route(body=b'{"ready": true}', drip=0.05)
    server = serve({**READY_ROUTES, HEALTH_PATH: dripping})
    reason, elapsed = _timed(lambda: check(server.port, timeout=_TIMEOUT))
    assert reason is not None
    assert reason.startswith(f"GET {HEALTH_PATH} no respondió")
    assert elapsed < _TIMEOUT + _SLACK


@pytest.mark.req("20.9")
def test_each_request_gets_its_own_timeout(serve: Serve) -> None:
    # Cada respuesta llega a tiempo, pero juntas tardan más que un solo plazo
    timeout = 1.0
    delay = 0.55
    server = serve(
        {
            INDEX_PATH: Route(body=b"<html>", delay=delay),
            HEALTH_PATH: Route(body=b'{"ready": true}', delay=delay),
        }
    )
    reason, elapsed = _timed(lambda: check(server.port, timeout=timeout))
    assert reason is None
    assert elapsed >= 2 * delay > timeout


@pytest.mark.req("20.9")
def test_response_with_content_length_does_not_wait_for_the_server_to_close(
    serve: Serve,
) -> None:
    server = serve({**READY_ROUTES, HEALTH_PATH: Route(body=b'{"ready": true}', keep_open=True)})
    reason, elapsed = _timed(lambda: check(server.port, timeout=_TIMEOUT * 5))
    assert reason is None
    assert elapsed < _TIMEOUT * 2


# --- Fallas de conexión y de protocolo ---


@pytest.mark.req("20.10")
def test_not_ready_when_nothing_listens_on_the_port() -> None:
    reason = check(_free_port(), timeout=_TIMEOUT)
    assert reason is not None
    assert reason.startswith(f"GET {INDEX_PATH} falló")


@pytest.mark.req("20.10")
@pytest.mark.parametrize(
    "raw",
    [b"", b"hola\r\n\r\ncuerpo", b"HTTP/1.0 OK\r\n\r\n", b"sin fin de cabecera"],
    ids=["cierra-sin-responder", "no-es-http", "estado-invalido", "cabecera-incompleta"],
)
def test_not_ready_when_the_server_does_not_speak_http(serve: Serve, raw: bytes) -> None:
    server = serve({INDEX_PATH: Route(raw=raw)})
    reason = check(server.port, timeout=_TIMEOUT)
    assert reason is not None
    assert reason.startswith(f"GET {INDEX_PATH} dio una respuesta que no es HTTP")


@pytest.mark.req("20.9")
def test_any_complete_http_response_counts_for_the_index(serve: Serve) -> None:
    # La página solo debe responder; que `/api/health` diga listo decide el resultado
    server = serve({**READY_ROUTES, INDEX_PATH: Route(status=404)})
    assert check(server.port, timeout=_TIMEOUT) is None


@pytest.mark.req("20.9")
def test_fetch_reads_only_up_to_the_response_size_cap(serve: Serve) -> None:
    big = b"x" * (healthcheck.MAX_RESPONSE_BYTES * 2)
    server = serve({INDEX_PATH: Route(body=big)})
    response = fetch(server.port, INDEX_PATH, timeout=5)
    assert response.status == 200
    assert 0 < len(response.body) <= healthcheck.MAX_RESPONSE_BYTES


# --- Interpretación de la respuesta ---


@pytest.mark.parametrize(
    ("data", "status", "body"),
    [
        (b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi", 200, b"hi"),
        (b"HTTP/1.0 503 Service Unavailable\r\n\r\n{}", 503, b"{}"),
        (b"HTTP/1.1 204\r\n\r\n", 204, b""),  # sin frase de motivo
        (b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nhiextra", 200, b"hi"),
        (b"HTTP/1.1 200 OK\r\nContent-Length: x\r\n\r\nabc", 200, b"abc"),
    ],
)
def test_parse_response(data: bytes, status: int, body: bytes) -> None:
    assert parse_response(data) == HttpResponse(status=status, body=body)


@pytest.mark.parametrize(
    "data",
    [b"", b"HTTP/1.1 200 OK\r\n", b"ICY 200 OK\r\n\r\n", b"HTTP/1.1 20 OK\r\n\r\n"],
)
def test_parse_response_rejects_what_is_not_http(data: bytes) -> None:
    with pytest.raises(InvalidResponse):
        parse_response(data)


# --- Puerto y código de salida ---


@pytest.mark.req("20.9", "20.10")
def test_main_takes_the_port_from_converter_port(serve: Serve) -> None:
    ready = serve(READY_ROUTES)
    not_ready = serve(NOT_READY_ROUTES)
    assert main({"CONVERTER_PORT": str(ready.port)}) == EXIT_READY
    assert main({"CONVERTER_PORT": str(not_ready.port)}) == EXIT_NOT_READY


@pytest.mark.req("20.10")
def test_main_reports_the_reason_on_stderr(
    serve: Serve, capsys: pytest.CaptureFixture[str]
) -> None:
    server = serve(NOT_READY_ROUTES)
    assert main({"CONVERTER_PORT": str(server.port)}) == EXIT_NOT_READY
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"{MODULE}: GET {HEALTH_PATH} informa no listo")


@pytest.mark.req("20.10")
def test_main_is_not_ready_when_the_configuration_is_invalid(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main({"CONVERTER_PORT": "0"}) == EXIT_NOT_READY
    err = capsys.readouterr().err
    assert "configuración inválida" in err
    assert "CONVERTER_PORT" in err


@pytest.mark.req("20.9", "20.10")
def test_python_dash_m_exit_codes(serve: Serve) -> None:
    """Como lo ejecuta Docker: `python -m converter.healthcheck` con el entorno del Contenedor."""

    def run(port: int) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "CONVERTER_PORT": str(port)}
        return subprocess.run(
            [sys.executable, "-m", MODULE],
            env=env,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            check=False,
        )

    ready = run(serve(READY_ROUTES).port)
    assert (ready.returncode, ready.stdout, ready.stderr) == (0, "", "")

    not_ready = run(serve(NOT_READY_ROUTES).port)
    assert not_ready.returncode == 1
    assert "informa no listo" in not_ready.stderr

    refused = run(_free_port())
    assert refused.returncode == 1


# --- Contrato con la API real ---


@pytest.fixture
def real_server(tmp_path: Path) -> Iterator[tuple[int, AppDeps]]:
    """La aplicación de `converter.api.app` servida por Uvicorn en un puerto libre."""
    deps = build_deps(tmp_path)
    server = uvicorn.Server(
        uvicorn.Config(create_app(deps), host=HOST, port=0, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    start = time.monotonic()
    while not server.started:
        assert thread.is_alive(), "Uvicorn terminó antes de arrancar"
        assert time.monotonic() - start < 30, "Uvicorn no arrancó a tiempo"
        time.sleep(0.01)
    port = int(server.servers[0].sockets[0].getsockname()[1])
    yield port, deps
    server.should_exit = True
    thread.join(timeout=30)
    asyncio.run(deps.manager.aclose())


@pytest.mark.req("20.9", "20.10")
def test_real_app_is_not_ready_until_startup_marks_it(real_server: tuple[int, AppDeps]) -> None:
    port, deps = real_server

    before = check(port)
    assert before is not None
    assert "503" in before

    deps.readiness.mark_ready()
    assert check(port) is None
    assert main({"CONVERTER_PORT": str(port)}) == EXIT_READY
