"""Cabeceras de seguridad y validación de las solicitudes de la API HTTP (Req 18.15, 19.5).

Controles de la tabla "Seguridad" del diseño:

- `SecurityHeadersMiddleware` agrega `SECURITY_HEADERS` a toda respuesta HTTP: la CSP de la
  Interfaz_de_Usuario, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
  `Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Resource-Policy: same-origin` y
  `X-Content-Type-Options: nosniff`. Con esa CSP el navegador solo pide recursos al propio
  Conversor (Req 18.15, 20.3) y no ejecuta scripts ni estilos en línea; con `nosniff` tampoco
  ejecuta como script una respuesta que no se declara JavaScript. Una cabecera que la ruta ya
  fijó no se reemplaza: la descarga envía su propia `Content-Security-Policy: sandbox`
  (Req 18.10).
- Sin cabeceras CORS: ningún componente las agrega, así que un sitio ajeno no puede leer las
  respuestas y el servidor no aprueba ningún preflight.
- `RequestMarkerMiddleware` exige exactamente una cabecera `X-Converter-Request: 1` en toda
  solicitud con un método distinto de GET, HEAD y OPTIONS. Un formulario o un `fetch` simple de
  otro sitio no puede agregarla sin un preflight, que el servidor no aprueba. Sin ella la
  respuesta es 400 `recepcion_solicitud_invalida` en el envío de archivos (`POST /api/tasks`) y
  400 `solicitud_invalida` en las demás rutas, antes de enrutar la solicitud y sin leer su
  cuerpo.
- `read_json_object` lee el cuerpo JSON de una solicitud: exige `Content-Type:
  application/json`, que un formulario de otro sitio tampoco puede enviar sin preflight.
- `is_task_id` y `require_task_id` validan la forma del Identificador_de_Tarea: un UUID versión
  4 en forma canónica, en minúsculas. Un valor mal formado produce el mismo `TaskUnavailable`
  que un identificador desconocido (Req 19.5).
"""

from __future__ import annotations

import json
import re
from typing import TypeGuard

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from converter.api.errors import JSON_MEDIA_TYPE, TASKS_PATH, ApiError, api_error_response
from converter.messages_es import ReceptionError, RequestError
from converter.tasks.manager import TaskUnavailable

# CSP de la Interfaz_de_Usuario, tal como la fija el diseño
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; form-action 'self'; base-uri 'none'; "
    "frame-ancestors 'none'"
)

# Cabeceras de toda respuesta, en minúsculas como las envía ASGI
SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("content-security-policy", CONTENT_SECURITY_POLICY),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "no-referrer"),
    ("cross-origin-opener-policy", "same-origin"),
    ("cross-origin-resource-policy", "same-origin"),
    ("x-content-type-options", "nosniff"),
)

# Métodos que no modifican estado y no exigen la cabecera de la Interfaz_de_Usuario
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
REQUEST_MARKER_HEADER = "x-converter-request"
REQUEST_MARKER_VALUE = "1"
_MARKER_NAME = REQUEST_MARKER_HEADER.encode("latin-1")
_MARKER_VALUE = REQUEST_MARKER_VALUE.encode("latin-1")

# Un cuerpo JSON de la API es un objeto chico, p. ej. {"target": "JPG", "language": null,
# "acknowledge_loss": true}; el tope evita leer a memoria un cuerpo arbitrario
MAX_JSON_BODY_BYTES = 16 * 1024
_LENGTH = re.compile(r"[0-9]{1,12}")  # Content-Length en dígitos ASCII (se usa con fullmatch)

# Identificador_de_Tarea: UUID versión 4, variante RFC 4122, en minúsculas (se usa con fullmatch)
_TASK_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


class SecurityHeadersMiddleware:
    """Middleware ASGI que agrega `SECURITY_HEADERS` a toda respuesta HTTP sin reemplazar las
    que ya fijó la ruta."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestMarkerMiddleware:
    """Middleware ASGI que rechaza con 400 las solicitudes que modifican estado sin
    `X-Converter-Request: 1`."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            values = [value for name, value in scope["headers"] if name.lower() == _MARKER_NAME]
            if values != [_MARKER_VALUE]:
                # La solicitud no llega al enrutador: no se lee el cuerpo ni se busca la Tarea
                code = (
                    ReceptionError.INVALID_REQUEST
                    if scope["path"] == TASKS_PATH
                    else RequestError.INVALID_REQUEST
                )
                response = api_error_response(ApiError(code))
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def is_json_content_type(value: str | None) -> bool:
    """`value` es `application/json`, con parámetros opcionales y, si declara charset, UTF-8."""
    if value is None:
        return False
    media_type, _, parameters = value.partition(";")
    if media_type.strip().lower() != JSON_MEDIA_TYPE:
        return False
    for parameter in parameters.split(";") if parameters else ():
        name, _, raw = parameter.partition("=")
        charset = raw.strip().strip('"').lower()
        if name.strip().lower() == "charset" and charset not in {"utf-8", "utf8"}:
            return False
    return True


async def read_json_object(request: Request) -> dict[str, object]:
    """Cuerpo de `request` como objeto JSON.

    Lanza `ApiError` con `solicitud_invalida` (400) si el `Content-Type` no es
    `application/json`, si el cuerpo supera `MAX_JSON_BODY_BYTES`, no es UTF-8 o no es JSON, o
    si no es un objeto. Lee el cuerpo en partes y deja de leer al superar el tope.
    """
    invalid = ApiError(RequestError.INVALID_REQUEST)
    if not is_json_content_type(request.headers.get("content-type")):
        raise invalid
    declared = request.headers.get("content-length")
    if declared is not None and (
        _LENGTH.fullmatch(declared) is None or int(declared) > MAX_JSON_BODY_BYTES
    ):
        raise invalid
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_JSON_BODY_BYTES:
            raise invalid
    try:
        document = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):  # UTF-8 o JSON inválidos, o anidamiento excesivo
        raise invalid from None
    if not isinstance(document, dict):
        raise invalid
    return document


def is_task_id(value: object) -> TypeGuard[str]:
    """`value` tiene la forma de un Identificador_de_Tarea."""
    return isinstance(value, str) and _TASK_ID.fullmatch(value) is not None


def require_task_id(value: object) -> str:
    """Devuelve `value` si tiene la forma de un Identificador_de_Tarea; si no, lanza
    `TaskUnavailable`, el mismo error que un identificador desconocido (Req 19.5)."""
    if not is_task_id(value):
        raise TaskUnavailable()
    return value
