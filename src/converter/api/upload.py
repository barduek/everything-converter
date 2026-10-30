"""Recepción de archivos: `POST /api/tasks` (Req 1.1–1.6, 1.8–1.10, 16.13, 17.1, 17.10, 19.3,
19.9).

La Interfaz_de_Usuario envía el archivo como cuerpo de la solicitud, sin multipart, con estas
cabeceras:

- `X-File-Name`: el Nombre_Original en UTF-8 codificado con `encodeURIComponent`. El valor solo
  puede tener los caracteres que esa función deja sin codificar (`A-Z a-z 0-9 - _ . ! ~ * ' ( )`)
  y secuencias `%XX`, y los bytes que resultan deben ser UTF-8 válido. No se decodifica dos
  veces: "%2541.txt" es el nombre "%41.txt". Un valor vacío es el nombre vacío.
- `Content-Length`, si el cliente la envía; un envío por partes no la lleva.
- `X-Converter-Request: 1`, que `security.RequestMarkerMiddleware` exige antes de enrutar.

La recepción sigue estos pasos, en este orden. Si se cumplen varias causas de rechazo, responde
la del primer paso que la detecta. Cada rechazo es un error de recepción de `messages_es`, con
la etapa "recepción" (Req 16.13), y no crea una Tarea:

1. Cabeceras: si `X-File-Name` falta, está repetida o su codificación es inválida, o si
   `Content-Length` está repetida o no es un número, 400 `recepcion_solicitud_invalida`.
2. Nombre_Original de más de 255 caracteres: 400 `recepcion_nombre_largo` (Req 1.10).
3. `Content-Length` mayor al Límite_de_Tamaño_Global: 413 `recepcion_excede_limite` (Req 1.3).
4. Espacio libre del Almacenamiento_Temporal menor al Espacio_Libre_Mínimo: 507
   `recepcion_sin_espacio` (Req 1.5).
5. Creación del Espacio_de_Trabajo (Req 17.1). Si falla, o si falla la medición del paso 4,
   500 `recepcion_error_escritura`.
6. Copia del cuerpo a `source/original`. Más bytes que el Límite_de_Tamaño_Global: 413
   `recepcion_excede_limite` (Req 1.3). Error de escritura: 500 `recepcion_error_escritura`
   (Req 1.9). Desconexión del cliente: sin respuesta (Req 1.8).
7. Cero bytes recibidos: 400 `recepcion_vacia` (Req 1.4).
8. Creación de la Tarea en recibida: 201 con su `TaskView` (Req 1.1, 19.3).

Los pasos 1 a 4 no leen el cuerpo ni escriben en el Almacenamiento_Temporal. Así un nombre
demasiado largo o un tamaño declarado mayor al límite se rechazan sin recibir datos que haya que
borrar, y sin espacio suficiente no se almacena nada (Req 1.5).

Copia del cuerpo. El Archivo_de_Origen se crea con `O_EXCL` y `O_NOFOLLOW`, modo 0600, con el
nombre interno fijo `source/original` del Espacio_de_Trabajo, que no depende del Nombre_Original
(Req 1.6). Cada parte recibida se cuenta antes de escribirla: si la cuenta supera el
Límite_de_Tamaño_Global, la recepción se corta en ese momento, sin escribir esa parte ni leer
las siguientes (Req 1.3). Las partes se juntan hasta `WRITE_BUFFER_BYTES` y se escriben en un
hilo (`anyio.to_thread`); todas las operaciones de archivo de la recepción corren en hilos con un
límite de `UPLOAD_THREADS` compartido por las recepciones de la aplicación. Una recepción solo
ocupa un hilo mientras escribe, así que las recepciones simultáneas no se esperan entre sí y
ninguna espera a las Tareas en_cola o en_proceso (Req 19.9). Cualquier `OSError` al crear,
escribir o cerrar el archivo, incluido `ENOSPC`, es `recepcion_error_escritura` (Req 1.9).

Desconexión (Req 1.8). Si el cliente pierde la conexión o cancela el envío, el servidor lo
informa con `http.disconnect` y Starlette lanza `ClientDisconnect`. No hay a quién responder: la
ruta devuelve una respuesta vacía que el servidor descarta. El mensaje `recepcion_interrumpida`
lo muestra solo la Interfaz_de_Usuario.

Limpieza (Req 1.3, 1.4, 1.8, 1.10, 17.10). Toda recepción que creó su Espacio_de_Trabajo y
termina sin crear una Tarea encola su eliminación en el barrendero con `task_id=None`, también
ante un error inesperado o una cancelación. El barrendero lo borra en su próxima pasada, dentro
de los 5 segundos.

Respuesta. Con la recepción completa, el Gestor_de_Tareas crea la Tarea en recibida con el
Espacio_de_Trabajo de la recepción, el Nombre_Original sin modificar y el tamaño recibido, y
lanza la detección en segundo plano. La respuesta 201 es la `TaskView` de esa Tarea, con su
Identificador_de_Tarea y el estado recibida, que es el aviso de archivo recibido (Req 1.1,
19.3). Ninguna otra respuesta de esta ruta tiene Datos_de_Tarea.

El Log_de_Eventos no registra las recepciones; en particular, nunca registra Nombres_Originales.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING
from urllib.parse import unquote_to_bytes

import anyio
from anyio import CapacityLimiter, to_thread
from fastapi import APIRouter
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response

from converter.api.errors import TASKS_PATH, ApiError
from converter.messages_es import ReceptionError
from converter.tasks.model import MAX_ORIGINAL_NAME_CHARS, CompletedUpload

if TYPE_CHECKING:
    from pathlib import Path

    from converter.api.app import AppDeps
    from converter.config import Config
    from converter.storage.workspace import Workspace, WorkspaceManager

FILE_NAME_HEADER = "x-file-name"
CONTENT_LENGTH_HEADER = "content-length"

WRITE_BUFFER_BYTES = 1024 * 1024  # bytes que se juntan antes de cada escritura (1 MiB)
UPLOAD_THREADS = 4  # hilos de archivo simultáneos para todas las recepciones de la aplicación

SOURCE_FILE_MODE = 0o600
_SOURCE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW

# Valor de X-File-Name: caracteres que encodeURIComponent no codifica y secuencias %XX (se usa
# con fullmatch)
_ENCODED_NAME = re.compile(r"(?:[A-Za-z0-9\-_.!~*'()]|%[0-9A-Fa-f]{2})*")
_LENGTH = re.compile(r"[0-9]{1,20}")  # Content-Length en dígitos ASCII (se usa con fullmatch)

# Estado de la respuesta a una recepción que el cliente interrumpió; el servidor la descarta
DISCONNECTED_STATUS = 400
CREATED_STATUS = 201


def decode_file_name(values: Sequence[str]) -> str:
    """Nombre_Original de los valores de la cabecera `X-File-Name` de una solicitud.

    Lanza `ApiError` con `recepcion_solicitud_invalida` si no hay exactamente un valor, si el
    valor tiene caracteres que `encodeURIComponent` no produce o un `%` que no sigue de dos
    dígitos hexadecimales, o si los bytes decodificados no son UTF-8 válido.
    """
    if len(values) != 1 or _ENCODED_NAME.fullmatch(values[0]) is None:
        raise ApiError(ReceptionError.INVALID_REQUEST)
    try:
        # El valor es ASCII y cada "%" sigue de dos dígitos hexadecimales. UTF-8 estricto:
        # rechaza secuencias truncadas, formas largas y sustitutos codificados
        return unquote_to_bytes(values[0]).decode("utf-8")
    except UnicodeDecodeError:
        raise ApiError(ReceptionError.INVALID_REQUEST) from None


def declared_length(values: Sequence[str]) -> int | None:
    """Tamaño que declara `Content-Length`, o None si la solicitud no la tiene.

    Lanza `ApiError` con `recepcion_solicitud_invalida` si la cabecera está repetida o su valor
    no es un número decimal.
    """
    if not values:
        return None
    if len(values) != 1 or _LENGTH.fullmatch(values[0]) is None:
        raise ApiError(ReceptionError.INVALID_REQUEST)
    return int(values[0])


def _too_large(config: Config) -> ApiError:
    return ApiError(ReceptionError.TOO_LARGE, {"limite_mb": config.max_upload_mb})


def _write_error() -> ApiError:
    return ApiError(ReceptionError.WRITE_ERROR)


# Operaciones bloqueantes: corren en hilos


def _create_if_space(storage: WorkspaceManager, min_free_bytes: int) -> Workspace | None:
    """Espacio_de_Trabajo nuevo, o None si el espacio libre es menor al Espacio_Libre_Mínimo."""
    if storage.free_bytes() < min_free_bytes:
        return None
    return storage.create()


def _open_source(path: Path) -> int:
    return os.open(path, _SOURCE_FLAGS, SOURCE_FILE_MODE)


def _write_all(fd: int, data: bytes | bytearray) -> None:
    """Escribe `data` completo en `fd`; lanza `OSError` si una escritura falla."""
    with memoryview(data) as view:
        offset = 0
        while offset < len(view):
            offset += os.write(fd, view[offset:])


def _close_quietly(fd: int) -> None:
    with contextlib.suppress(OSError):
        os.close(fd)


# Pasos de la recepción


async def _new_workspace(
    storage: WorkspaceManager, min_free_bytes: int, limiter: CapacityLimiter
) -> Workspace:
    """Pasos 4 y 5: comprueba el Espacio_Libre_Mínimo y crea el Espacio_de_Trabajo."""
    try:
        ws = await to_thread.run_sync(_create_if_space, storage, min_free_bytes, limiter=limiter)
    except OSError:
        raise _write_error() from None
    if ws is None:
        raise ApiError(ReceptionError.NO_SPACE)
    return ws


async def _write(fd: int, data: bytearray, limiter: CapacityLimiter) -> None:
    try:
        await to_thread.run_sync(_write_all, fd, data, limiter=limiter)
    except OSError:
        raise _write_error() from None


async def _copy_body(request: Request, fd: int, config: Config, limiter: CapacityLimiter) -> int:
    """Copia el cuerpo de `request` en `fd` y devuelve los bytes recibidos.

    Lanza `ApiError` si la cuenta supera el Límite_de_Tamaño_Global o si una escritura falla, y
    `ClientDisconnect` si el cliente se desconecta; en esos casos deja de leer.
    """
    limit = config.max_upload_bytes
    size = 0
    pending = bytearray()
    async with contextlib.aclosing(request.stream()) as chunks:
        async for chunk in chunks:
            size += len(chunk)
            if size > limit:
                raise _too_large(config)  # se corta en este momento, sin escribir esta parte
            pending += chunk
            if len(pending) >= WRITE_BUFFER_BYTES:
                await _write(fd, pending, limiter)
                pending = bytearray()
    if pending:
        await _write(fd, pending, limiter)
    return size


async def _store_source(
    request: Request, ws: Workspace, config: Config, limiter: CapacityLimiter
) -> int:
    """Paso 6: guarda el cuerpo en `source/original` de `ws` y devuelve los bytes recibidos."""
    try:
        fd = await to_thread.run_sync(_open_source, ws.source_file, limiter=limiter)
    except OSError:
        raise _write_error() from None
    try:
        size = await _copy_body(request, fd, config, limiter)
    except BaseException:
        # El archivo se cierra aunque se cancele la solicitud; su borrado ya está pedido
        with anyio.CancelScope(shield=True):
            await to_thread.run_sync(_close_quietly, fd, limiter=limiter)
        raise
    try:
        await to_thread.run_sync(os.close, fd, limiter=limiter)
    except OSError:
        raise _write_error() from None
    return size


async def receive_upload(request: Request, deps: AppDeps, limiter: CapacityLimiter) -> Response:
    """Atiende `POST /api/tasks` con los pasos del docstring del módulo.

    Lanza `ApiError` con el error de recepción de cada rechazo.
    """
    config = deps.config
    name = decode_file_name(request.headers.getlist(FILE_NAME_HEADER))
    declared = declared_length(request.headers.getlist(CONTENT_LENGTH_HEADER))
    if len(name) > MAX_ORIGINAL_NAME_CHARS:
        raise ApiError(ReceptionError.NAME_TOO_LONG)
    if declared is not None and declared > config.max_upload_bytes:
        raise _too_large(config)
    ws = await _new_workspace(deps.storage, config.min_free_space_bytes, limiter)
    created = False
    try:
        size = await _store_source(request, ws, config, limiter)
        if size == 0:
            raise ApiError(ReceptionError.EMPTY)
        task = await deps.manager.create(CompletedUpload(ws, name, size))
        created = True  # desde aquí el Espacio_de_Trabajo es de la Tarea
        view = deps.manager.view(task.id)
        return JSONResponse(view.to_json(), status_code=CREATED_STATUS)
    except ClientDisconnect:
        return Response(status_code=DISCONNECTED_STATUS)
    finally:
        if not created:
            deps.sweeper.enqueue_remove(ws, task_id=None)


def router(deps: AppDeps) -> APIRouter:
    """Ruta `POST /api/tasks` con las dependencias `deps`."""
    limiter = CapacityLimiter(UPLOAD_THREADS)
    routes = APIRouter()

    @routes.post(TASKS_PATH)
    async def upload(request: Request) -> Response:
        return await receive_upload(request, deps, limiter)

    return routes
