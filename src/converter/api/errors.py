"""Respuestas de error de la API HTTP (Req 16.4, 16.5, 16.13, 19.5).

Toda respuesta de error lleva el cuerpo JSON del diseño, en UTF-8:

```json
{"error": {"code": "estado_no_permite", "stage": null, "message": "…", "action": "…"}}
```

`stage` es la Etapa_de_Procesamiento ("recepción" en los errores de recepción, Req 16.13) o
null. El motivo y la acción salen de `messages_es.render_error`, así que llegan en español y sin
detalle técnico (Req 16.5). El Rechazo_de_Destino `destino_no_disponible` agrega `targets`, la
lista de los Formatos_de_Destino ofrecidos, como en el ejemplo del diseño.

`install_error_handlers` instala este mapeo de excepciones a respuestas:

| Excepción | HTTP | Código |
|---|---|---|
| `TaskUnavailable`, y todo 404 o 405 de una ruta bajo `/api/tasks/` | 404 | `tarea_no_disponible` |
| `TaskRequestError` del Gestor_de_Tareas | el del catálogo: 409, 422 o 410 | el de la excepción |
| `ApiError` | el del catálogo | el de la excepción |
| `RequestValidationError` de FastAPI | 400 | `solicitud_invalida` |
| cualquier otro `HTTPException` | el suyo | respuesta predeterminada de FastAPI |

La respuesta 404 de una Tarea no disponible es una sola, armada al importar el módulo: el mismo
estado, las mismas cabeceras y el mismo cuerpo, byte a byte, para un identificador ausente (por
ejemplo `/api/tasks//cancel`), mal formado o desconocido, y para cualquier ruta inexistente bajo
`/api/tasks/`, también un método que la ruta no admite (por ejemplo `GET …/cancel`, que el
enrutador respondería con 405). Así ninguna respuesta indica cuál de esos casos ocurrió
(Req 19.5).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from converter.messages_es import CATALOG, RejectionCode, RequestError, UserMessage, render_error
from converter.tasks.manager import TaskRequestError, TaskUnavailable

if TYPE_CHECKING:
    from converter.eventlog import JsonValue

TASKS_PATH = "/api/tasks"  # envío de archivos (POST)
TASKS_PREFIX = TASKS_PATH + "/"  # rutas de una Tarea: /api/tasks/{id}…
JSON_MEDIA_TYPE = "application/json"


def _http_status(code: str) -> int:
    """Código HTTP de la respuesta de error `code`, según el catálogo."""
    status = CATALOG[code].http_status
    if status is None:
        raise ValueError(f"{code}: no es una respuesta de error de la API")
    return status


class ApiError(Exception):
    """Solicitud rechazada con un código del catálogo de `messages_es` (Req 16.4, 16.13).

    Sirve para los errores que no vienen del Gestor_de_Tareas: los de recepción
    (`ReceptionError`) y `solicitud_invalida`. El mensaje se arma y se valida al crear la
    excepción: lanza `UnknownCodeError` si el código no está en el catálogo,
    `InvalidMessageError` si los parámetros no son los que declara y `ValueError` si el código
    no es una respuesta de la API (por ejemplo `recepcion_interrumpida`).
    """

    message: UserMessage
    status_code: int

    def __init__(self, code: StrEnum | str, params: Mapping[str, object] | None = None) -> None:
        message = render_error(code, params)
        status_code = _http_status(message.code)
        super().__init__(message.code)  # sin los valores que envió el usuario
        self.message = message
        self.status_code = status_code


def error_body(
    message: UserMessage, extra: Mapping[str, JsonValue] | None = None
) -> dict[str, JsonValue]:
    """Cuerpo JSON de una respuesta de error; `extra` agrega campos a `error`."""
    error: dict[str, JsonValue] = {
        "code": message.code,
        "stage": None if message.stage is None else message.stage.value,
        "message": message.message,
        "action": message.action,
    }
    error.update(extra or {})
    return {"error": error}


def error_response(
    message: UserMessage, status_code: int, extra: Mapping[str, JsonValue] | None = None
) -> Response:
    """Respuesta de error con el cuerpo de `error_body`."""
    return JSONResponse(error_body(message, extra), status_code=status_code)


def api_error_response(error: ApiError) -> Response:
    """Respuesta de un `ApiError`."""
    return error_response(error.message, error.status_code)


# Cuerpo único de `tarea_no_disponible` (Req 19.5)
TASK_UNAVAILABLE_BODY: bytes = json.dumps(
    error_body(render_error(RequestError.TASK_UNAVAILABLE)),
    ensure_ascii=False,
    separators=(",", ":"),
).encode("utf-8")
TASK_UNAVAILABLE_STATUS = _http_status(RequestError.TASK_UNAVAILABLE)  # 404


def task_unavailable_response() -> Response:
    """La respuesta 404 idéntica para cualquier Tarea no disponible (Req 19.5)."""
    return Response(
        TASK_UNAVAILABLE_BODY, status_code=TASK_UNAVAILABLE_STATUS, media_type=JSON_MEDIA_TYPE
    )


def task_request_error_response(error: TaskRequestError) -> Response:
    """Respuesta de una solicitud sobre una Tarea que el Gestor_de_Tareas no atendió.

    `estado_no_permite`, `confirmacion_requerida` y `descarga_no_disponible` dan 409, los
    Rechazos_de_Destino 422 y `resultado_expirado` 410, según el catálogo.
    """
    message = render_error(error.code, error.params)
    extra: dict[str, JsonValue] = {}
    if error.code == RejectionCode.TARGET_UNAVAILABLE:
        targets = error.params["destinos"]
        if isinstance(targets, list | tuple):
            extra["targets"] = [str(target) for target in targets]
    return error_response(message, _http_status(error.code), extra)


async def _on_task_unavailable(request: Request, exc: Exception) -> Response:
    return task_unavailable_response()


async def _on_task_request_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, TaskRequestError):  # el manejador solo se instala para esa clase
        raise exc
    return task_request_error_response(exc)


async def _on_api_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, ApiError):  # el manejador solo se instala para esa clase
        raise exc
    return api_error_response(exc)


async def _on_validation_error(request: Request, exc: Exception) -> Response:
    # Sin el detalle de FastAPI, que repite en inglés los valores recibidos
    return api_error_response(ApiError(RequestError.INVALID_REQUEST))


async def _on_no_route(request: Request, exc: Exception) -> Response:
    # Bajo /api/tasks/ el enrutador no responde con otro cuerpo, ni por una ruta inexistente
    # (404) ni por un método que la ruta no admite (405) (Req 19.5). `path` es la ruta ya
    # decodificada, la misma con la que se eligió la ruta
    path: str = request.scope["path"]
    if path.startswith(TASKS_PREFIX):
        return task_unavailable_response()
    if not isinstance(exc, HTTPException):  # el manejador solo se instala para 404 y 405
        raise exc
    return await http_exception_handler(request, exc)


def install_error_handlers(app: FastAPI) -> None:
    """Instala en `app` el mapeo de excepciones a respuestas de la tabla del módulo."""
    app.add_exception_handler(TaskUnavailable, _on_task_unavailable)
    app.add_exception_handler(TaskRequestError, _on_task_request_error)
    app.add_exception_handler(ApiError, _on_api_error)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    app.add_exception_handler(404, _on_no_route)
    app.add_exception_handler(405, _on_no_route)
