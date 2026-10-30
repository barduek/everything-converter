"""Cliente del Indicador_de_Salud para Docker (Req 20.9, 20.10, 20.11).

Uso:

    python -m converter.healthcheck

El `HEALTHCHECK` de la Imagen_de_Contenedor lo ejecuta con el Python del venv. Pide
`GET /` y `GET /api/health` a `127.0.0.1:<puerto>`, donde `<puerto>` es el valor de
`CONVERTER_PORT` (8080 si no está definida), una después de la otra. Termina con 0 (listo) solo si
las dos respuestas llegan en 5 segundos o menos y la segunda indica listo; en cualquier otro caso
termina con 1 (no listo) y escribe el motivo en stderr, que Docker guarda en `docker inspect`.

- **Listo.** `GET /api/health` responde 200 con el cuerpo JSON `{"ready": true}`, que el
  arranque habilita con `Readiness.mark_ready()` cuando el Puerto_de_la_Interfaz ya está abierto
  y la limpieza terminó (Req 20.9). Un 503 `{"ready": false}`, otro estado HTTP o un cuerpo que
  no es ese JSON es no listo (Req 20.10).
- **`GET /`.** Comprueba que la Interfaz_de_Usuario responde. Cuenta cualquier respuesta HTTP
  completa y a tiempo: el criterio 20.9 exige que responda, no que el cuerpo tenga una forma
  determinada.
- **Plazo de 5 segundos por solicitud.** Es un plazo total desde que se abre la conexión hasta
  que llega la respuesta completa, no un plazo por lectura: un servidor que entrega la respuesta
  de a pocos bytes también agota el plazo. Un servidor que no responde, que rechaza la conexión o
  que responde algo que no es HTTP es no listo.
- **Solo biblioteca estándar.** Habla HTTP/1.0 sobre un `socket`: el servidor cierra la conexión
  al terminar y la respuesta nunca viene en trozos (`Transfer-Encoding: chunked`). No importa
  FastAPI ni Uvicorn, así que arranca en pocos milisegundos. Lee como máximo `MAX_RESPONSE_BYTES`
  de cada respuesta: de `GET /` no necesita el cuerpo completo y el de `GET /api/health` es
  pequeño.
- **Configuración.** Lee el puerto con `load_config`, igual que el servidor. Si alguna
  Variable_de_Configuración es inválida el servidor no arrancó, y el resultado es no listo.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass

from converter.config import load_config

MODULE = "converter.healthcheck"  # para `python -m`
HOST = "127.0.0.1"
INDEX_PATH = "/"
HEALTH_PATH = "/api/health"
TIMEOUT_SECONDS = 5.0  # plazo de cada solicitud (Req 20.9, 20.10)
MAX_RESPONSE_BYTES = 1_048_576
RECV_BYTES = 65_536

EXIT_READY = 0
EXIT_NOT_READY = 1

_HEADER_END = b"\r\n\r\n"
_STATUS_LINE = re.compile(rb"HTTP/[0-9]\.[0-9] ([0-9]{3})(?: .*)?", re.DOTALL)


class InvalidResponse(ValueError):
    """Lo que respondió el servidor no es una respuesta HTTP."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Estado y cuerpo de una respuesta HTTP."""

    status: int
    body: bytes


def _remaining(deadline: float) -> float:
    """Segundos que faltan para `deadline`; lanza `TimeoutError` si ya pasó."""
    remaining = deadline - time.monotonic()
    # `settimeout(0)` pone el socket en modo no bloqueante, así que 0 también es plazo vencido
    if remaining <= 0:
        raise TimeoutError("se agotó el plazo")
    return remaining


def _split(data: bytes) -> tuple[bytes, bytes] | None:
    """Divide una respuesta en cabecera (línea de estado y cabeceras) y cuerpo."""
    head, separator, body = data.partition(_HEADER_END)
    return (head, body) if separator else None


def _content_length(head: bytes) -> int | None:
    """Valor de `Content-Length` en la cabecera, o None si falta o no es un entero."""
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            text = value.strip()
            return int(text) if text.isascii() and text.isdigit() else None
    return None


def _is_complete(data: bytes) -> bool:
    """La respuesta ya tiene la cabecera y, si declara su largo, todo el cuerpo."""
    parts = _split(data)
    if parts is None:
        return False
    head, body = parts
    length = _content_length(head)
    return length is not None and len(body) >= length


def _read_response(sock: socket.socket, deadline: float) -> bytes:
    """Lee hasta que la respuesta está completa, el servidor cierra o se llena el tope."""
    data = bytearray()
    while len(data) < MAX_RESPONSE_BYTES:
        sock.settimeout(_remaining(deadline))
        chunk = sock.recv(min(RECV_BYTES, MAX_RESPONSE_BYTES - len(data)))
        if not chunk:
            break
        data += chunk
        if _is_complete(bytes(data)):
            break
    return bytes(data)


def parse_response(data: bytes) -> HttpResponse:
    """Interpreta los bytes de una respuesta HTTP. Lanza `InvalidResponse` si no lo es."""
    parts = _split(data)
    if parts is None:
        raise InvalidResponse("la respuesta no tiene cabecera completa")
    head, body = parts
    match = _STATUS_LINE.fullmatch(head.split(b"\r\n", 1)[0])
    if match is None:
        raise InvalidResponse("la línea de estado no es HTTP")
    length = _content_length(head)
    return HttpResponse(status=int(match.group(1)), body=body if length is None else body[:length])


def fetch(port: int, path: str, *, timeout: float = TIMEOUT_SECONDS) -> HttpResponse:
    """Pide `GET path` a `127.0.0.1:port` y devuelve la respuesta.

    `timeout` es el plazo total en segundos, desde la conexión hasta la respuesta completa.
    Lanza `TimeoutError` si se agota, otro `OSError` si falla la conexión o la lectura e
    `InvalidResponse` si la respuesta no es HTTP.
    """
    request = (
        f"GET {path} HTTP/1.0\r\n"
        f"Host: {HOST}:{port}\r\n"
        "User-Agent: converter-healthcheck\r\n"
        "Accept: */*\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    deadline = time.monotonic() + timeout
    with socket.create_connection((HOST, port), timeout=timeout) as sock:
        sock.settimeout(_remaining(deadline))
        sock.sendall(request)
        return parse_response(_read_response(sock, deadline))


def is_ready(response: HttpResponse) -> bool:
    """La respuesta de `GET /api/health` es 200 con `{"ready": true}` (Req 20.9)."""
    if response.status != 200:
        return False
    try:
        payload = json.loads(response.body)
    except (ValueError, RecursionError):  # JSON inválido, UTF-8 inválido o anidado en exceso
        return False
    return isinstance(payload, dict) and payload.get("ready") is True


def _failure(path: str, exc: Exception, timeout: float) -> str:
    if isinstance(exc, TimeoutError):
        return f"GET {path} no respondió en {timeout:g} segundos"
    if isinstance(exc, InvalidResponse):
        return f"GET {path} dio una respuesta que no es HTTP: {exc}"
    return f"GET {path} falló: {type(exc).__name__}"


def check(port: int, *, timeout: float = TIMEOUT_SECONDS) -> str | None:
    """Comprueba el Indicador_de_Salud del servidor en `port`.

    Devuelve None si está listo o, si no, el motivo en español. `GET /api/health` se pide solo
    si `GET /` respondió a tiempo. `timeout` es el plazo de cada solicitud, en segundos.
    """
    try:
        fetch(port, INDEX_PATH, timeout=timeout)
    except (OSError, InvalidResponse) as exc:
        return _failure(INDEX_PATH, exc, timeout)
    try:
        health = fetch(port, HEALTH_PATH, timeout=timeout)
    except (OSError, InvalidResponse) as exc:
        return _failure(HEALTH_PATH, exc, timeout)
    if not is_ready(health):
        return f"GET {HEALTH_PATH} informa no listo (estado HTTP {health.status})"
    return None


def _report(message: str) -> None:
    sys.stderr.write(f"{MODULE}: {message}\n")
    sys.stderr.flush()


def main(env: Mapping[str, str] | None = None) -> int:
    """Ejecuta la comprobación y devuelve el código de salida: 0 si está listo, 1 si no.

    `env` son las Variables_de_Configuración; si se omite, `os.environ`.
    """
    config = load_config(os.environ if env is None else env)
    if isinstance(config, list):
        _report("configuración inválida: " + "; ".join(f"{e.variable} {e.reason}" for e in config))
        return EXIT_NOT_READY
    reason = check(config.port)
    if reason is not None:
        _report(reason)
        return EXIT_NOT_READY
    return EXIT_READY


if __name__ == "__main__":
    sys.exit(main())
