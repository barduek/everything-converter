"""Descarga del Archivo_Resultado: `GET /api/tasks/{id}/result` (Req 15.8, 15.9, 15.11, 16.6,
18.10, 19.4, 19.5).

La ruta pide al Gestor_de_Tareas el `ResultInfo` de la Tarea y responde con el archivo:

- 200 con el Archivo_Resultado si la Tarea está completada y no venció su Período_de_Retención.
  `FileResponse` lee `result/output`, que no cambia desde la entrega, así que cada descarga trae
  el mismo contenido byte a byte (Req 15.9). El archivo llega ahí con un `os.replace` al final de
  la conversión, antes de que la Tarea pase a completada: nunca se descarga un resultado parcial
  (Req 16.6).
- 404 `tarea_no_disponible` si el identificador está mal formado, falta o no es de ninguna Tarea
  (Req 19.5), con la misma respuesta que las demás rutas de Tareas.
- 409 `descarga_no_disponible` con el Estado_de_Tarea actual si la Tarea no está completada,
  también si falló con resultados parciales (Req 15.11, 16.6).
- 410 `resultado_expirado` si venció el Período_de_Retención desde que se completó (Req 15.8).
  `TaskManager.result` marca el vencimiento en ese momento si la retención todavía no lo hizo.

Los errores son las excepciones del Gestor_de_Tareas, que responden los manejadores de
`errors.py`, sin contenido del archivo.

Cabeceras de la respuesta 200 (tabla "Seguridad" del diseño, Req 18.10):

| Cabecera | Valor |
|---|---|
| `Content-Type` | el del Formato_de_Destino (`mime_type` del catálogo), o `application/zip` |
| `Content-Disposition` | `attachment; filename="<respaldo ASCII>"; filename*=UTF-8''<nombre>` |
| `X-Content-Type-Options` | `nosniff` |
| `Content-Security-Policy` | `sandbox` |
| `Cache-Control` | `no-store` |

Con `attachment` el navegador guarda el archivo en lugar de mostrarlo; `nosniff` evita que
interprete el contenido con otro tipo, y si igual se abriera en el origen del Conversor,
`sandbox` le impide ejecutar scripts. `no-store` evita que quede una copia en la caché del
navegador o de un proxy después de que el resultado expiró. `SecurityHeadersMiddleware` agrega
las demás cabeceras de seguridad sin reemplazar estas.

`Content-Disposition` sigue RFC 6266. El nombre de descarga ya pasó por la
Normalización_de_Nombre (`naming.result_name`). `filename*` lo lleva completo en UTF-8 con
codificación porcentual: solo quedan sin codificar los `attr-char` de RFC 8187 (letras y dígitos
ASCII y ``!#$&+-.^_`|~``). `filename` es el respaldo para clientes que no entienden `filename*`:
un `quoted-string` en el que cada carácter fuera de U+0020 a U+007E se reemplaza por "_", y
también `"` y `\\`, que cerrarían o escaparían la cadena, y `%`, que algunos clientes decodifican
(RFC 6266, apéndice D). Así la cabecera es ASCII imprimible, sin saltos de línea ni comillas
sueltas, para cualquier nombre: no se puede inyectar otra cabecera ni otro parámetro.

`FileResponse` también envía `Content-Length`, `ETag` y `Last-Modified`, y atiende `Range`
(206): el resultado no cambia, así que un navegador puede reanudar una descarga interrumpida.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import quote

from fastapi import APIRouter
from starlette.responses import FileResponse, Response

from converter.api.security import require_task_id
from converter.api.tasks import TASK_PATH

if TYPE_CHECKING:
    from converter.api.app import AppDeps

RESULT_PATH = TASK_PATH + "/result"

# Cabeceras de la descarga, además de Content-Type y Content-Disposition
DOWNLOAD_HEADERS: tuple[tuple[str, str], ...] = (
    ("x-content-type-options", "nosniff"),
    ("content-security-policy", "sandbox"),
    ("cache-control", "no-store"),
)

FALLBACK_CHAR = "_"
# Caracteres que no van en el respaldo ASCII: fuera de U+0020 a U+007E, comillas, barra
# invertida y "%"
_NOT_IN_FALLBACK = re.compile(r'[^\x20-\x7e]|["\\%]')
# attr-char de RFC 8187 que `quote` codificaría; letras, dígitos y "-._~" nunca los codifica
_ATTR_CHAR_PUNCTUATION = "!#$&+^`|"


def ascii_fallback(name: str) -> str:
    """Respaldo ASCII de `name` para el parámetro `filename` de `Content-Disposition`."""
    return _NOT_IN_FALLBACK.sub(FALLBACK_CHAR, name)


def content_disposition(name: str) -> str:
    """Valor de `Content-Disposition` para descargar un archivo con el nombre `name` (RFC 6266).

    Lanza `UnicodeEncodeError` si `name` no se puede codificar en UTF-8 (un sustituto suelto).
    """
    encoded = quote(name, safe=_ATTR_CHAR_PUNCTUATION, encoding="utf-8", errors="strict")
    return f"attachment; filename=\"{ascii_fallback(name)}\"; filename*=UTF-8''{encoded}"


def router(deps: AppDeps) -> APIRouter:
    """Ruta `GET /api/tasks/{id}/result` con las dependencias `deps`."""
    routes = APIRouter()

    @routes.get(RESULT_PATH)
    async def result(task_id: str) -> Response:
        info = deps.manager.result(require_task_id(task_id))
        headers = {"content-disposition": content_disposition(info.download_name)}
        headers.update(DOWNLOAD_HEADERS)
        return FileResponse(info.path, media_type=info.media_type, headers=headers)

    return routes
