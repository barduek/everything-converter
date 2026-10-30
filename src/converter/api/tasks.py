"""Rutas de una Tarea: vista, confirmación, rechazo de la confirmación y cancelación (Req 3.3,
3.4, 3.7, 14.1, 14.2, 14.6, 14.11, 19.4, 19.5, 19.11).

| Método y ruta | Cuerpo | Respuestas |
|---|---|---|
| `GET /api/tasks/{id}` | — | 200 `TaskView`; 404 |
| `POST /api/tasks/{id}/confirm` | ver "Cuerpos JSON" | 200 `TaskView`; 400; 404; 409; 422 |
| `POST /api/tasks/{id}/decline` | `{"target": "JPG"}` | 200 `TaskView`; 400; 404; 409 |
| `POST /api/tasks/{id}/cancel` | — | 200 `TaskView`; 404; 409 |

El cuerpo de `confirm` es `{"target": "JPG", "language": "es" | "auto" | null,
"acknowledge_loss": true}`. Todas las respuestas 200 traen la `TaskView` con el estado que dejó
la solicitud: `confirm`, la Tarea en_cola o ya en_proceso si había lugar (o fallida si el
convertidor no pudo evaluar el par); `decline`, la Tarea en recibida, sin cambios, con los mismos
Formatos_de_Destino (Req 3.7); `cancel`, la Tarea cancelada (Req 14.6).

La descarga, `GET /api/tasks/{id}/result`, está en `download.py`.

Identificador_de_Tarea (Req 19.4, 19.5). `{id}` es un parámetro de texto sin restricciones, para
que el enrutador no responda con un error de validación: cada ruta llama primero a
`security.require_task_id`, que lanza `TaskUnavailable` si el valor no tiene la forma de un UUID
versión 4 en minúsculas, igual que el Gestor_de_Tareas para un identificador desconocido. Un
identificador ausente (`/api/tasks//cancel`) no coincide con ninguna ruta y recibe el 404 del
enrutador. Las tres respuestas son la misma 404 `tarea_no_disponible` de `errors.py`.

Cuerpos JSON. `security.read_json_object` exige `Content-Type: application/json` y un objeto
JSON de hasta 16 KiB. Después se valida la forma; cualquier otra cosa es 400
`solicitud_invalida` y no llega al Gestor_de_Tareas:

- `confirm`: `target` es obligatorio y es un texto; `language` es un texto o null, y si falta es
  null (español en un Par_de_Transcripción, Req 11.10); `acknowledge_loss` es un booleano, y si
  falta es false. Sin `acknowledge_loss: true`, un par que no es Sin_Pérdida responde 409
  `confirmacion_requerida` (Req 3.3).
- `decline`: `target` es obligatorio y es un texto.
- Una clave que no es de la lista también es `solicitud_invalida`: un cliente con un error de
  escritura en el nombre de una clave no confirma algo distinto de lo que quiso.

Los valores no se comparan con el registro: el Gestor_de_Tareas decide si el destino forma un
par, si falta aceptar la pérdida y si el idioma es un Idioma_Soportado. `cancel` no lee el
cuerpo.

Orden de las comprobaciones. La forma del identificador y la existencia de la Tarea se
comprueban antes de leer el cuerpo: una solicitud con un cuerpo inválido para una Tarea que no
existe recibe el mismo 404 que con un cuerpo válido (Req 19.5). Ninguna solicitud rechazada
modifica una Tarea.

Errores. Las rutas no arman respuestas de error: dejan pasar las excepciones del
Gestor_de_Tareas (`TaskUnavailable` 404; `StateForbids`, `ConfirmationRequired` 409 con el
estado actual o el pedido de confirmación; `TargetRejected` 422 con el Rechazo_de_Destino) y
`errors.ApiError` (400), y los manejadores de `errors.install_error_handlers` responden.
Varias solicitudes simultáneas sobre la misma Tarea se atienden de a una con el lock de la
Tarea, en el Gestor_de_Tareas (Req 19.11).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from converter.api.errors import TASKS_PREFIX, ApiError
from converter.api.security import read_json_object, require_task_id
from converter.messages_es import RequestError
from converter.tasks.model import ConfirmRequest

if TYPE_CHECKING:
    from converter.api.app import AppDeps
    from converter.tasks.view import TaskView

TASK_PATH = TASKS_PREFIX + "{task_id}"
CONFIRM_PATH = TASK_PATH + "/confirm"
DECLINE_PATH = TASK_PATH + "/decline"
CANCEL_PATH = TASK_PATH + "/cancel"

# Claves admitidas en cada cuerpo
CONFIRM_KEYS = frozenset({"target", "language", "acknowledge_loss"})
DECLINE_KEYS = frozenset({"target"})


def _invalid() -> ApiError:
    return ApiError(RequestError.INVALID_REQUEST)


def parse_confirm(body: Mapping[str, object]) -> ConfirmRequest:
    """Confirmación del Formato_de_Destino de un cuerpo JSON ya leído.

    Lanza `ApiError` con `solicitud_invalida` si falta `target`, si hay claves que no son de
    `CONFIRM_KEYS` o si un valor no es del tipo que se indica en el docstring del módulo.
    """
    if not body.keys() <= CONFIRM_KEYS:
        raise _invalid()
    target = body.get("target")
    language = body.get("language")
    acknowledge_loss = body.get("acknowledge_loss", False)
    if (
        not isinstance(target, str)
        or (language is not None and not isinstance(language, str))
        or not isinstance(acknowledge_loss, bool)
    ):
        raise _invalid()
    return ConfirmRequest(target=target, language=language, acknowledge_loss=acknowledge_loss)


def parse_decline(body: Mapping[str, object]) -> str:
    """Formato_de_Destino de un rechazo de la confirmación, de un cuerpo JSON ya leído.

    Lanza `ApiError` con `solicitud_invalida` si falta `target`, si no es un texto o si hay
    claves que no son de `DECLINE_KEYS`.
    """
    target = body.get("target")
    if not body.keys() <= DECLINE_KEYS or not isinstance(target, str):
        raise _invalid()
    return target


def _view_response(view: TaskView) -> Response:
    return JSONResponse(view.to_json())


async def _existing_task_body(deps: AppDeps, task_id: str, request: Request) -> dict[str, object]:
    """Cuerpo JSON de una solicitud sobre la Tarea `task_id`.

    Lanza `TaskUnavailable` si el identificador está mal formado o la Tarea no existe, antes de
    leer el cuerpo, y `ApiError` si el cuerpo no es un objeto JSON válido.
    """
    deps.manager.view(require_task_id(task_id))  # solo comprueba que la Tarea existe
    return await read_json_object(request)


def router(deps: AppDeps) -> APIRouter:
    """Rutas de vista, confirmación, rechazo de la confirmación y cancelación de una Tarea."""
    routes = APIRouter()

    @routes.get(TASK_PATH)
    async def view(task_id: str) -> Response:
        return _view_response(deps.manager.view(require_task_id(task_id)))

    @routes.post(CONFIRM_PATH)
    async def confirm(task_id: str, request: Request) -> Response:
        confirmation = parse_confirm(await _existing_task_body(deps, task_id, request))
        return _view_response(await deps.manager.confirm(task_id, confirmation))

    @routes.post(DECLINE_PATH)
    async def decline(task_id: str, request: Request) -> Response:
        target = parse_decline(await _existing_task_body(deps, task_id, request))
        return _view_response(await deps.manager.decline(task_id, target))

    @routes.post(CANCEL_PATH)
    async def cancel(task_id: str) -> Response:
        return _view_response(await deps.manager.cancel(require_task_id(task_id)))

    return routes
