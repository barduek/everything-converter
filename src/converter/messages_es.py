"""Catálogo único de mensajes al usuario en español (Req 16.4, 16.5, 16.9, 16.13, 18.1).

Cada código de las tablas de "Error Handling" del diseño tiene una entrada en `CATALOG` con su
Etapa_de_Procesamiento, una plantilla del motivo en español y exactamente una acción sugerida no
vacía. Los grupos son:

- Errores de recepción (`ReceptionError`): rechazos sin Tarea, siempre en la etapa recepción
  (Req 1.3, 1.4, 1.5, 1.9, 1.10, 16.13, 18.13).
- Motivos_de_Fallo (`FailureReason`): los Rechazos_de_Origen, que ocurren en la detección, y los
  fallos durante el procesamiento, que ocurren en la conversión, la transcripción o la entrega.
  `archivo_danado` y `error_interno` aparecen en las dos tablas y son un único código. Cada
  entrada lista las etapas en que puede ocurrir; `Failure.stage` indica la del caso.
- Rechazos_de_Destino (`RejectionCode`), errores de solicitudes sobre Tareas (`RequestError`) y
  motivos de cancelación (`CancelReason`, que no son Motivos_de_Fallo). No ocurren en una
  Etapa_de_Procesamiento: la API los envía con `stage: null`.

`NOTICES` tiene las plantillas de los avisos al usuario y `STATE_LABELS` el nombre en español de
cada Estado_de_Tarea.

Mensajes sin detalle técnico (Req 16.5): `render_failure`, `render_error` y `render_notice` arman
el texto solo con plantillas fijas y con los parámetros que declara cada código. Cada parámetro
tiene un tipo (`ParamKind`) y se valida antes de usarlo: números enteros, nombres de Formato o de
Códec con un patrón cerrado que no admite separadores de ruta, Números_de_Página, caracteres
citados uno por uno con su punto de código, y Estados_de_Tarea. Un parámetro que falta, que
sobra o que no cumple su tipo lanza `InvalidMessageError`. Ninguna función recibe el detalle
técnico del error ni una excepción, así que un mensaje no puede contener rutas ni trazas; ese
detalle va solo al Log_de_Eventos.

Los únicos textos libres que se citan son los que el propio usuario envió o que vienen de su
archivo y que los requisitos piden mostrar: el destino o el idioma solicitados (Req 3.4, 11.9),
la Extensión_del_Nombre (Req 2.6), los nombres de fuentes del DOCX (Req 7.4) y los nombres de
idiomas. Se muestran con los caracteres no imprimibles reemplazados por U+FFFD y recortados a
una longitud máxima.
"""

from __future__ import annotations

import string
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from types import MappingProxyType

from converter.models import Category, Failure, Notice, Stage, TaskState

# --- Códigos ---


class FailureReason(StrEnum):
    """Motivo_de_Fallo de una Tarea que pasa a fallida."""

    # Rechazos_de_Origen: recibida → fallida, etapa detección (Req 16.1)
    UNRECOGNIZED_FORMAT = "formato_no_reconocido"  # Req 2.5
    NO_TARGETS = "tipo_sin_destinos"  # Req 2.7, 3.6
    CATEGORY_SIZE_LIMIT = "excede_limite_categoria"  # Req 2.9
    ANIMATED_IMAGE = "imagen_animada"  # Req 5.6
    PIXEL_LIMIT = "excede_limite_pixeles"  # Req 5.9
    ENCRYPTED_DOCX = "docx_cifrado"  # Req 7.2
    PROTECTED_PDF = "pdf_protegido"  # Req 8.7
    DECOMPRESSION_LIMITS = "docx_excede_limites_descompresion"  # Req 7.7
    # En la detección o durante el procesamiento
    DAMAGED = "archivo_danado"  # Req 16.2
    INTERNAL_ERROR = "error_interno"  # Req 16.9
    # Durante el procesamiento: en_proceso → fallida
    UNSUPPORTED_CODEC = "codec_no_soportado"  # Req 9.6, 10.8
    UNREPRESENTABLE_CHARACTERS = "caracteres_no_representables"  # Req 6.11
    PDF_WITHOUT_TEXT = "pdf_sin_texto"  # Req 8.2
    PAGE_PIXEL_LIMIT = "pagina_excede_pixeles"  # Req 8.11
    MODEL_UNAVAILABLE = "modelo_no_disponible"  # Req 11.6
    TIMED_OUT = "tiempo_excedido"  # Req 14.7
    NO_SPACE = "sin_espacio_proceso"  # Req 16.10


class ReceptionError(StrEnum):
    """Error de recepción: el archivo se rechaza o se interrumpe sin crear una Tarea."""

    TOO_LARGE = "recepcion_excede_limite"  # Req 1.3
    EMPTY = "recepcion_vacia"  # Req 1.4
    NO_SPACE = "recepcion_sin_espacio"  # Req 1.5
    WRITE_ERROR = "recepcion_error_escritura"  # Req 1.9
    NAME_TOO_LONG = "recepcion_nombre_largo"  # Req 1.10
    INVALID_REQUEST = "recepcion_solicitud_invalida"  # falta X-File-Name o es inválido
    INTERRUPTED = "recepcion_interrumpida"  # solo Interfaz_de_Usuario (Req 18.13)


class RejectionCode(StrEnum):
    """Rechazo_de_Destino: la Tarea sigue en recibida (Req 16.12)."""

    TARGET_UNAVAILABLE = "destino_no_disponible"  # Req 3.4
    DIMENSIONS_EXCEEDED = "dimensiones_excedidas"  # Req 5.5
    VIDEO_WITHOUT_AUDIO = "video_sin_audio"  # Req 10.3
    TRANSCRIPTION_TOO_LONG = "transcripcion_excede_duracion"  # Req 11.5
    UNSUPPORTED_LANGUAGE = "idioma_no_soportado"  # Req 11.9, 13.8


class RequestError(StrEnum):
    """Error de una solicitud sobre una Tarea."""

    TASK_UNAVAILABLE = "tarea_no_disponible"  # Req 19.5, 18.14
    STATE_FORBIDS = "estado_no_permite"  # Req 14.11
    CONFIRMATION_REQUIRED = "confirmacion_requerida"  # Req 3.3
    DOWNLOAD_UNAVAILABLE = "descarga_no_disponible"  # Req 15.11, 16.6
    RESULT_EXPIRED = "resultado_expirado"  # Req 15.8
    INVALID_REQUEST = "solicitud_invalida"  # validación de entrada


class CancelReason(StrEnum):
    """Motivo de cancelación de una Tarea; no es un Motivo_de_Fallo."""

    BY_USER = "cancelada_por_usuario"  # Req 14.6
    RETENTION_EXPIRED = "sin_destino_en_retencion"  # Req 14.8


class NoticeCode(StrEnum):
    """Aviso al usuario sobre la detección o el resultado."""

    EXTENSION_MISMATCH = "extension_no_coincide"  # Req 2.6
    BITS_REDUCED = "bits_reducidos"  # Req 5.8
    EXTERNAL_RESOURCES_SKIPPED = "recursos_externos_omitidos"  # Req 6.6, 7.5
    FONTS_REPLACED = "fuentes_reemplazadas"  # Req 7.4
    COMMENTS_SKIPPED = "comentarios_omitidos"  # Req 7.6
    CHANGES_APPLIED = "cambios_aplicados"  # Req 7.6
    MACROS_SKIPPED = "macros_omitidas"  # Req 7.5
    PAGES_WITHOUT_TEXT = "paginas_sin_texto"  # Req 8.3
    SAMPLE_RATE_CHANGED = "frecuencia_cambiada"  # Req 9.4, 10.9
    CHANNELS_MIXED = "canales_mezclados"  # Req 9.3, 10.9
    UNUSED_TRACKS = "pistas_no_usadas"  # Req 10.4
    DIMENSIONS_PADDED = "dimensiones_rellenadas"  # Req 10.11
    NO_SPEECH = "sin_voz"  # Req 11.7, 12.10


class CodecTrack(StrEnum):
    """Pista a la que corresponde un Códec no soportado (Req 9.6, 10.8)."""

    VIDEO = "video"  # Pista_de_Video
    DEFAULT_AUDIO = "audio_predeterminada"  # Pista_de_Audio_Predeterminada de un video
    AUDIO = "audio"  # archivo de audio


class MessageGroup(StrEnum):
    """Tabla de "Error Handling" a la que pertenece un código."""

    RECEPTION = "recepcion"
    FAILURE = "fallo"
    DESTINATION_REJECTION = "rechazo_de_destino"
    TASK_REQUEST = "solicitud"
    CANCELLATION = "cancelacion"


# --- Errores de este módulo ---


class UnknownCodeError(LookupError):
    """El código no está en el catálogo."""


class InvalidMessageError(ValueError):
    """Los parámetros o la etapa no corresponden al código.

    El texto nombra el código, el parámetro y la regla incumplida, nunca el valor recibido, que
    puede venir del contenido del archivo.
    """


# --- Nombres de los Estados_de_Tarea (Req 18.1) ---

STATE_LABELS: Mapping[TaskState, str] = MappingProxyType(
    {
        TaskState.RECEIVED: "Recibida",
        TaskState.QUEUED: "En cola",
        TaskState.PROCESSING: "En proceso",
        TaskState.COMPLETED: "Completada",
        TaskState.FAILED: "Fallida",
        TaskState.CANCELLED: "Cancelada",
    }
)


def state_label(state: TaskState | str) -> str:
    """Nombre en español de un Estado_de_Tarea, p. ej. "en_cola" da "En cola"."""
    try:
        return STATE_LABELS[TaskState(state)]
    except ValueError:
        raise InvalidMessageError("el valor no es un Estado_de_Tarea") from None


# --- Tipos de parámetro ---


class ParamKind(StrEnum):
    """Tipo de un parámetro de plantilla. Define cómo se valida y cómo se muestra."""

    NUMBER = "numero"  # entero positivo: límites, dimensiones, Hz, canales, cantidades, páginas
    DURATION_MS = "duracion_ms"  # entero ≥ 0 en ms; se muestra en minutos y segundos
    FORMAT = "formato"  # nombre de Formato, p. ej. "PNG"
    FORMATS = "formatos"  # lista no vacía de nombres de Formato
    CATEGORY = "categoria"  # Categoría
    CODEC = "codec"  # nombre de Códec que declara el archivo, p. ej. "h264"
    TRACK = "pista"  # CodecTrack
    PAGES = "paginas"  # Números_de_Página distintos y en orden creciente
    CHARACTERS = "caracteres"  # de 1 a 10 caracteres distintos citados de un archivo
    STATE = "estado"  # Estado_de_Tarea, que se muestra con su nombre en español
    USER_VALUE = "valor_del_usuario"  # texto que envió el usuario, p. ej. el destino pedido
    EXTENSION = "extension"  # Extensión_del_Nombre del Nombre_Original
    LANGUAGES = "idiomas"  # lista no vacía de nombres de idioma en español
    FONT_REPLACEMENTS = "reemplazos_de_fuentes"  # lista no vacía de (fuente, reemplazo o None)


MAX_CITED_CHARACTERS = 10  # Req 6.11
USER_TEXT_LIMIT = 40  # caracteres de un texto del usuario o de una Extensión_del_Nombre
NAME_TEXT_LIMIT = 64  # caracteres de un nombre de fuente o de idioma

# Nombres de Formato y de Códec: ASCII, sin espacios ni separadores de ruta
_NAME_CHARS = frozenset(string.ascii_letters + string.digits + "._+-")
_FORMAT_NAME_LIMIT = 32
_CODEC_NAME_LIMIT = 64
# `find_unrepresentable` no informa estos caracteres (Req 6.11)
_NOT_CITED = frozenset(" \t\n\r")

_TRACK_TEXT: Mapping[CodecTrack, str] = MappingProxyType(
    {
        CodecTrack.VIDEO: "de la pista de video",
        CodecTrack.DEFAULT_AUDIO: "de la pista de audio predeterminada",
        CodecTrack.AUDIO: "del audio",
    }
)
_NO_REPLACEMENT = "otra fuente instalada"


def _integer(value: object, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidMessageError("debe ser un número entero")
    if value < minimum:
        raise InvalidMessageError(f"debe ser un entero mayor o igual a {minimum}")
    return int(value)


def _items(value: object, maximum: int | None = None) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise InvalidMessageError("debe ser una lista")
    if not value:
        raise InvalidMessageError("la lista no puede estar vacía")
    if maximum is not None and len(value) > maximum:
        raise InvalidMessageError(f"la lista admite como máximo {maximum} elementos")
    return tuple(value)


def _text(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise InvalidMessageError("debe ser un texto")
    if not value and not allow_empty:
        raise InvalidMessageError("el texto no puede estar vacío")
    return str(value)


def _display(text: str, limit: int) -> str:
    """Texto libre listo para mostrar: sin caracteres no imprimibles y con longitud acotada.

    Los espacios Unicode se muestran como un espacio; los demás caracteres no imprimibles
    (control, formato, como las marcas de dirección, y sustitutos) como U+FFFD.
    """
    shown = "".join(
        ch if ch.isprintable() else (" " if unicodedata.category(ch) == "Zs" else "\ufffd")
        for ch in text
    )
    return shown if len(shown) <= limit else shown[:limit] + "…"


def _name(value: object, limit: int) -> str:
    name = _text(value)
    if len(name) > limit or not set(name) <= _NAME_CHARS or not name[0].isalnum():
        raise InvalidMessageError(
            f"debe tener hasta {limit} letras o dígitos ASCII, puntos, guiones o '+' y "
            "empezar con letra o dígito"
        )
    return name


def _render_number(value: object) -> str:
    return str(_integer(value, 1))


def _render_duration(value: object) -> str:
    # Hacia arriba: una duración que supera un máximo en minutos enteros nunca se muestra igual
    seconds = -(-_integer(value, 0) // 1000)
    minutes, rest = divmod(seconds, 60)
    if minutes == 0:
        return f"{rest} s"
    return f"{minutes} min" if rest == 0 else f"{minutes} min {rest} s"


def _render_format(value: object) -> str:
    return _name(value, _FORMAT_NAME_LIMIT)


def _render_formats(value: object) -> str:
    return ", ".join(_render_format(item) for item in _items(value))


def _render_category(value: object) -> str:
    try:
        return Category(_text(value)).value
    except ValueError:
        raise InvalidMessageError("debe ser una Categoría") from None


def _render_codec(value: object) -> str:
    return _name(value, _CODEC_NAME_LIMIT)


def _render_track(value: object) -> str:
    try:
        return _TRACK_TEXT[CodecTrack(_text(value))]
    except ValueError:
        raise InvalidMessageError("debe ser una pista de CodecTrack") from None


def _render_pages(value: object) -> str:
    pages = [_integer(item, 1) for item in _items(value)]
    if any(a >= b for a, b in pairwise(pages)):
        raise InvalidMessageError("los Números_de_Página deben ser distintos y crecientes")
    return ", ".join(str(page) for page in pages)


def _cite_character(ch: str) -> str:
    code = f"U+{ord(ch):04X}"
    # Una marca combinable sola se uniría a las comillas; un no imprimible no se ve
    if ch.isprintable() and not unicodedata.category(ch).startswith("M"):
        return f"«{ch}» ({code})"
    return code


def _render_characters(value: object) -> str:
    chars: list[str] = []
    for item in _items(value, MAX_CITED_CHARACTERS):
        if not isinstance(item, str) or len(item) != 1:
            raise InvalidMessageError("cada elemento debe ser un único carácter")
        if item in _NOT_CITED:
            raise InvalidMessageError("no se citan espacios, tabulaciones ni saltos de línea")
        if item in chars:
            raise InvalidMessageError("los caracteres deben ser distintos")
        chars.append(item)
    return ", ".join(_cite_character(ch) for ch in chars)


def _render_state(value: object) -> str:
    return state_label(_text(value))


def _render_user_value(value: object) -> str:
    return _display(_text(value, allow_empty=True), USER_TEXT_LIMIT)


def _render_extension(value: object) -> str:
    extension = _text(value)
    if len(extension) < 2 or not extension.startswith("."):
        raise InvalidMessageError("debe empezar con un punto seguido de al menos un carácter")
    return _display(extension, USER_TEXT_LIMIT)


def _render_languages(value: object) -> str:
    return ", ".join(_display(_text(item), NAME_TEXT_LIMIT) for item in _items(value))


def _render_font_replacements(value: object) -> str:
    shown: list[str] = []
    for item in _items(value):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise InvalidMessageError("cada elemento debe ser un par (fuente, reemplazo)")
        font = _display(_text(item[0]), NAME_TEXT_LIMIT)
        replacement = (
            _NO_REPLACEMENT if item[1] is None else _display(_text(item[1]), NAME_TEXT_LIMIT)
        )
        shown.append(f"{font} → {replacement}")
    return ", ".join(shown)


_RENDERERS: Mapping[ParamKind, Callable[[object], str]] = MappingProxyType(
    {
        ParamKind.NUMBER: _render_number,
        ParamKind.DURATION_MS: _render_duration,
        ParamKind.FORMAT: _render_format,
        ParamKind.FORMATS: _render_formats,
        ParamKind.CATEGORY: _render_category,
        ParamKind.CODEC: _render_codec,
        ParamKind.TRACK: _render_track,
        ParamKind.PAGES: _render_pages,
        ParamKind.CHARACTERS: _render_characters,
        ParamKind.STATE: _render_state,
        ParamKind.USER_VALUE: _render_user_value,
        ParamKind.EXTENSION: _render_extension,
        ParamKind.LANGUAGES: _render_languages,
        ParamKind.FONT_REPLACEMENTS: _render_font_replacements,
    }
)


# --- Catálogo ---


@dataclass(frozen=True, slots=True)
class MessageDef:
    """Entrada del catálogo: un código con su etapa, su plantilla y su acción sugerida."""

    code: str
    group: MessageGroup
    # Etapas en que puede ocurrir; vacío si el código no ocurre en una Etapa_de_Procesamiento
    stages: tuple[Stage, ...]
    template: str  # motivo, con campos {nombre} de `params`
    action: str  # la única acción sugerida; también puede usar campos de `params`
    params: Mapping[str, ParamKind]
    http_status: int | None  # None: no es una respuesta de error de la API


@dataclass(frozen=True, slots=True)
class NoticeDef:
    """Plantilla de un aviso al usuario."""

    code: str
    template: str
    params: Mapping[str, ParamKind]


_RECEPTION = (Stage.RECEPTION,)
_DETECTION = (Stage.DETECTION,)
_CONVERSION = (Stage.CONVERSION,)
_TRANSCRIPTION = (Stage.TRANSCRIPTION,)
_DECODING = (Stage.DETECTION, Stage.CONVERSION, Stage.TRANSCRIPTION)
_TOOL_DECODING = (Stage.CONVERSION, Stage.TRANSCRIPTION)
# "La etapa en curso" de una Tarea en_proceso; el empaquetado del ZIP y el paso a result/ son la
# etapa entrega
_RUNNING = (Stage.CONVERSION, Stage.TRANSCRIPTION, Stage.DELIVERY)
_ANY_TASK_STAGE = (Stage.DETECTION, Stage.CONVERSION, Stage.TRANSCRIPTION, Stage.DELIVERY)

_N = ParamKind.NUMBER
_RETRY_UPLOAD = "Envíe el archivo de nuevo."
_REUPLOAD_AND_RETRY = "Vuelva a subir el archivo e intente la conversión de nuevo."
_REMOVE_PASSWORD = "Quite la contraseña en el programa de origen y envíelo de nuevo."
_CHECK_FILE = "Verifique que el archivo abra en otro programa y envíelo de nuevo."


def _def(
    code: StrEnum,
    group: MessageGroup,
    template: str,
    action: str,
    *,
    stages: tuple[Stage, ...] = (),
    params: Mapping[str, ParamKind] | None = None,
    http_status: int | None = None,
) -> MessageDef:
    return MessageDef(
        code=code.value,
        group=group,
        stages=stages,
        template=template,
        action=action,
        params=MappingProxyType(dict(params or {})),
        http_status=http_status,
    )


_R = MessageGroup.RECEPTION
_F = MessageGroup.FAILURE
_D = MessageGroup.DESTINATION_REJECTION
_T = MessageGroup.TASK_REQUEST
_C = MessageGroup.CANCELLATION

_DEFINITIONS: tuple[MessageDef, ...] = (
    # Errores de recepción, sin Tarea (Req 16.13)
    _def(
        ReceptionError.TOO_LARGE,
        _R,
        "El archivo excede el límite de {limite_mb} MB.",
        "Envíe un archivo más pequeño.",
        stages=_RECEPTION,
        params={"limite_mb": _N},
        http_status=413,
    ),
    _def(
        ReceptionError.EMPTY,
        _R,
        "El archivo está vacío.",
        "Elija un archivo con contenido.",
        stages=_RECEPTION,
        http_status=400,
    ),
    _def(
        ReceptionError.NO_SPACE,
        _R,
        "No hay espacio de almacenamiento suficiente.",
        "Intente más tarde, cuando terminen otras conversiones.",
        stages=_RECEPTION,
        http_status=507,
    ),
    _def(
        ReceptionError.WRITE_ERROR,
        _R,
        "El archivo no pudo almacenarse.",
        _RETRY_UPLOAD,
        stages=_RECEPTION,
        http_status=500,
    ),
    _def(
        ReceptionError.NAME_TOO_LONG,
        _R,
        "El nombre del archivo supera los 255 caracteres.",
        "Acorte el nombre y envíe el archivo de nuevo.",
        stages=_RECEPTION,
        http_status=400,
    ),
    _def(
        ReceptionError.INVALID_REQUEST,
        _R,
        "La solicitud de envío no es válida.",
        "Recargue la página y envíe el archivo de nuevo.",
        stages=_RECEPTION,
        http_status=400,
    ),
    _def(
        ReceptionError.INTERRUPTED,
        _R,
        "El archivo no se recibió.",
        _RETRY_UPLOAD,
        stages=_RECEPTION,
    ),
    # Rechazos_de_Origen (Req 16.1)
    _def(
        FailureReason.UNRECOGNIZED_FORMAT,
        _F,
        "El formato no es reconocido. Los archivos de texto se aceptan solo con codificación "
        "UTF-8 o Windows-1252 (ISO-8859-1).",
        "Verifique el archivo o guárdelo como texto UTF-8.",
        stages=_DETECTION,
    ),
    _def(
        FailureReason.NO_TARGETS,
        _F,
        "Se detectó el formato {tipo_real}, que no tiene formatos de destino disponibles. "
        "Formatos de origen admitidos: {formatos_origen}.",
        "Envíe un archivo de alguno de esos formatos.",
        stages=_DETECTION,
        params={"tipo_real": ParamKind.FORMAT, "formatos_origen": ParamKind.FORMATS},
    ),
    _def(
        FailureReason.CATEGORY_SIZE_LIMIT,
        _F,
        "El archivo es de la categoría {categoria} y excede el límite de {limite_mb} MB de "
        "esa categoría.",
        "Envíe un archivo más pequeño.",
        stages=_DETECTION,
        params={"categoria": ParamKind.CATEGORY, "limite_mb": _N},
    ),
    _def(
        FailureReason.ANIMATED_IMAGE,
        _F,
        "Las imágenes animadas están fuera del alcance.",
        "Envíe una imagen estática.",
        stages=_DETECTION,
    ),
    _def(
        FailureReason.PIXEL_LIMIT,
        _F,
        "La imagen mide {ancho} × {alto} píxeles tal como se muestra y supera el límite de "
        "{limite_pixeles} píxeles (ancho × alto).",
        "Reduzca la imagen y envíela de nuevo.",
        stages=_DETECTION,
        params={"ancho": _N, "alto": _N, "limite_pixeles": _N},
    ),
    _def(
        FailureReason.ENCRYPTED_DOCX,
        _F,
        "El documento está protegido con contraseña de apertura.",
        _REMOVE_PASSWORD,
        stages=_DETECTION,
    ),
    _def(
        FailureReason.PROTECTED_PDF,
        _F,
        "El documento está protegido; los PDF protegidos no se admiten.",
        _REMOVE_PASSWORD,
        stages=_DETECTION,
    ),
    _def(
        FailureReason.DECOMPRESSION_LIMITS,
        _F,
        "Documento demasiado complejo o potencialmente malicioso.",
        "Simplifique el documento o divídalo en varios y envíelos de nuevo.",
        stages=_DETECTION,
    ),
    _def(
        FailureReason.DAMAGED,
        _F,
        "Archivo dañado o ilegible.",
        _CHECK_FILE,
        stages=_DECODING,
    ),
    _def(
        FailureReason.INTERNAL_ERROR,
        _F,
        "Error interno del Conversor.",
        _REUPLOAD_AND_RETRY,
        stages=_ANY_TASK_STAGE,
    ),
    # Fallos durante el procesamiento (Req 16.3)
    _def(
        FailureReason.UNSUPPORTED_CODEC,
        _F,
        "El códec «{codec}» {pista} no está soportado.",
        "Convierta el archivo a un códec común con otro programa y envíelo de nuevo.",
        stages=_TOOL_DECODING,
        params={"codec": ParamKind.CODEC, "pista": ParamKind.TRACK},
    ),
    _def(
        FailureReason.UNREPRESENTABLE_CHARACTERS,
        _F,
        "El texto contiene caracteres que la fuente del PDF no puede representar: {caracteres}.",
        "Elija HTML como destino o quite esos caracteres.",
        stages=_CONVERSION,
        params={"caracteres": ParamKind.CHARACTERS},
    ),
    _def(
        FailureReason.PDF_WITHOUT_TEXT,
        _F,
        "El PDF parece escaneado: no tiene texto extraíble. El reconocimiento de texto en "
        "imágenes (OCR) está fuera del alcance.",
        "Elija PNG como destino para obtener las páginas como imágenes.",
        stages=_CONVERSION,
    ),
    _def(
        FailureReason.PAGE_PIXEL_LIMIT,
        _F,
        # {pagina} es la primera página, en orden de Número_de_Página, que supera el límite
        "La página {pagina} es demasiado grande: rasterizada a {ppp} ppp mediría {ancho} × "
        "{alto} píxeles y supera el límite de {limite_pixeles} píxeles (ancho × alto).",
        "Elija TXT como destino o use un PDF con páginas más pequeñas.",
        stages=_CONVERSION,
        params={"pagina": _N, "ppp": _N, "ancho": _N, "alto": _N, "limite_pixeles": _N},
    ),
    _def(
        FailureReason.MODEL_UNAVAILABLE,
        _F,
        "El modelo de transcripción no está disponible.",
        "Avise a quien administra el Conversor; las demás conversiones siguen disponibles.",
        stages=_TRANSCRIPTION,
    ),
    _def(
        FailureReason.TIMED_OUT,
        _F,
        "Tiempo de procesamiento excedido.",
        "Intente con un archivo más corto o más pequeño.",
        stages=_RUNNING,
    ),
    _def(
        FailureReason.NO_SPACE,
        _F,
        "No hay espacio de almacenamiento suficiente en el equipo del Conversor.",
        "Intente más tarde.",
        stages=_RUNNING,
    ),
    # Rechazos_de_Destino (Req 16.12)
    _def(
        RejectionCode.TARGET_UNAVAILABLE,
        _D,
        "El destino «{destino}» no está disponible para archivos {tipo_real}.",
        "Elija uno de los formatos de destino disponibles: {destinos}.",
        params={
            "destino": ParamKind.USER_VALUE,
            "tipo_real": ParamKind.FORMAT,
            "destinos": ParamKind.FORMATS,
        },
        http_status=422,
    ),
    _def(
        RejectionCode.DIMENSIONS_EXCEEDED,
        _D,
        "La imagen mide {ancho} × {alto} píxeles tal como se muestra; {destino} admite como "
        "máximo {maximo} × {maximo} píxeles.",
        "Elija otro formato de destino.",
        params={"ancho": _N, "alto": _N, "destino": ParamKind.FORMAT, "maximo": _N},
        http_status=422,
    ),
    _def(
        RejectionCode.VIDEO_WITHOUT_AUDIO,
        _D,
        "El video no tiene audio.",
        "Elija un formato de destino de video.",
        http_status=422,
    ),
    _def(
        RejectionCode.TRANSCRIPTION_TOO_LONG,
        _D,
        "El audio dura {duracion}; el máximo para transcribir es de {maximo_min} min.",
        "Recorte el audio o elija otro destino.",
        params={"duracion": ParamKind.DURATION_MS, "maximo_min": _N},
        http_status=422,
    ),
    _def(
        RejectionCode.UNSUPPORTED_LANGUAGE,
        _D,
        "El idioma «{valor}» no está disponible. Idiomas disponibles: {idiomas}, o detección "
        "automática.",
        "Elija uno de los idiomas disponibles.",
        params={"valor": ParamKind.USER_VALUE, "idiomas": ParamKind.LANGUAGES},
        http_status=422,
    ),
    # Errores de solicitudes sobre Tareas
    _def(
        RequestError.TASK_UNAVAILABLE,
        _T,
        # Idéntico para identificador ausente, mal formado o inexistente (Req 19.5)
        "La tarea no existe o ya no está disponible.",
        "Envíe el archivo de nuevo para convertirlo.",
        http_status=404,
    ),
    _def(
        RequestError.STATE_FORBIDS,
        _T,
        "La solicitud no se puede atender: la tarea está en estado «{estado}».",
        "Revise el estado de la tarea; si necesita otra conversión, envíe el archivo de nuevo.",
        params={"estado": ParamKind.STATE},
        http_status=409,
    ),
    _def(
        RequestError.CONFIRMATION_REQUIRED,
        _T,
        "Este destino pierde información y requiere su confirmación.",
        "Confirme la conversión o elija otro formato de destino.",
        http_status=409,
    ),
    _def(
        RequestError.DOWNLOAD_UNAVAILABLE,
        _T,
        "La tarea está en estado «{estado}»; el resultado no está disponible.",
        "Espere a que la tarea se complete; si falló o se canceló, envíe el archivo de nuevo.",
        params={"estado": ParamKind.STATE},
        http_status=409,
    ),
    _def(
        RequestError.RESULT_EXPIRED,
        _T,
        "El resultado expiró.",
        "Envíe el archivo de nuevo para convertirlo.",
        http_status=410,
    ),
    _def(
        RequestError.INVALID_REQUEST,
        _T,
        "La solicitud no es válida.",
        "Recargue la página e intente de nuevo.",
        http_status=400,
    ),
    # Motivos de cancelación
    _def(
        CancelReason.BY_USER,
        _C,
        "La tarea se canceló a pedido del usuario.",
        "Envíe el archivo de nuevo si quiere convertirlo.",
    ),
    _def(
        CancelReason.RETENTION_EXPIRED,
        _C,
        "No se eligió un formato de destino dentro de {retencion_min} minutos.",
        "Envíe el archivo de nuevo y elija un formato de destino.",
        params={"retencion_min": _N},
    ),
)


def _notice(
    code: NoticeCode, template: str, params: Mapping[str, ParamKind] | None = None
) -> NoticeDef:
    return NoticeDef(
        code=code.value, template=template, params=MappingProxyType(dict(params or {}))
    )


_NOTICE_DEFINITIONS: tuple[NoticeDef, ...] = (
    _notice(
        NoticeCode.EXTENSION_MISMATCH,
        "La extensión «{extension}» del nombre no corresponde al formato detectado, "
        "{tipo_real}; el archivo se procesa como {tipo_real}.",
        {"extension": ParamKind.EXTENSION, "tipo_real": ParamKind.FORMAT},
    ),
    _notice(
        NoticeCode.BITS_REDUCED,
        "La profundidad de color se redujo de 16 a 8 bits por canal.",
    ),
    _notice(
        NoticeCode.EXTERNAL_RESOURCES_SKIPPED,
        "Se omitieron, sin acceder a ellos, los recursos externos que el documento referencia "
        "fuera del archivo. Recursos omitidos: {cantidad}.",
        {"cantidad": _N},
    ),
    _notice(
        NoticeCode.FONTS_REPLACED,
        "Algunas fuentes del documento no están disponibles y se reemplazaron por fuentes "
        "instaladas: {reemplazos}.",
        {"reemplazos": ParamKind.FONT_REPLACEMENTS},
    ),
    _notice(NoticeCode.COMMENTS_SKIPPED, "Se omitieron los comentarios del documento."),
    _notice(
        NoticeCode.CHANGES_APPLIED,
        "Se aplicaron los cambios pendientes del control de cambios; el PDF muestra la versión "
        "final.",
    ),
    _notice(NoticeCode.MACROS_SKIPPED, "Se omitieron las macros del documento sin ejecutarlas."),
    _notice(
        NoticeCode.PAGES_WITHOUT_TEXT,
        "Algunas páginas no tienen texto extraíble y no aportan texto al resultado. Páginas "
        "sin texto: {paginas}.",
        {"paginas": ParamKind.PAGES},
    ),
    _notice(
        NoticeCode.SAMPLE_RATE_CHANGED,
        "La frecuencia de muestreo cambió de {origen_hz} Hz a {resultado_hz} Hz porque el "
        "formato de destino no admite la del origen.",
        {"origen_hz": _N, "resultado_hz": _N},
    ),
    _notice(
        NoticeCode.CHANNELS_MIXED,
        "El audio tenía {canales_origen} canales y el resultado tiene {canales_resultado} "
        "canales, el máximo del formato de destino.",
        {"canales_origen": _N, "canales_resultado": _N},
    ),
    _notice(
        NoticeCode.UNUSED_TRACKS,
        "Se usó solo la pista de audio predeterminada. Pistas de audio no usadas: {cantidad}.",
        {"cantidad": _N},
    ),
    _notice(
        NoticeCode.DIMENSIONS_PADDED,
        "El video mide {ancho_origen} × {alto_origen} píxeles y el resultado mide "
        "{ancho_resultado} × {alto_resultado} píxeles: MP4 requiere ancho y alto pares, por "
        "lo que se agregaron píxeles negros a la derecha o abajo.",
        {
            "ancho_origen": _N,
            "alto_origen": _N,
            "ancho_resultado": _N,
            "alto_resultado": _N,
        },
    ),
    _notice(NoticeCode.NO_SPEECH, "No se detectó voz en el audio; el resultado está vacío."),
)


def _fields(template: str) -> set[str]:
    """Campos {nombre} de una plantilla; rechaza índices, formatos y conversiones."""
    names: set[str] = set()
    for _, field, spec, conversion in string.Formatter().parse(template):
        if field is None:
            continue
        if not field.isidentifier() or spec or conversion is not None:
            raise ValueError(f"campo inválido en la plantilla: {{{field}}}")
        names.add(field)
    return names


def _check_fields(code: str, texts: tuple[str, ...], params: Mapping[str, ParamKind]) -> None:
    used: set[str] = set()
    for text in texts:
        used |= _fields(text)
    if used != set(params):
        raise ValueError(f"{code}: los campos {sorted(used)} no coinciden con {sorted(params)}")


_GROUP_CODES: Mapping[MessageGroup, type[StrEnum]] = MappingProxyType(
    {
        MessageGroup.RECEPTION: ReceptionError,
        MessageGroup.FAILURE: FailureReason,
        MessageGroup.DESTINATION_REJECTION: RejectionCode,
        MessageGroup.TASK_REQUEST: RequestError,
        MessageGroup.CANCELLATION: CancelReason,
    }
)


def _build_catalog() -> Mapping[str, MessageDef]:
    catalog: dict[str, MessageDef] = {}
    for definition in _DEFINITIONS:
        code = definition.code
        if code in catalog:
            raise ValueError(f"código repetido en el catálogo: {code}")
        if code not in {member.value for member in _GROUP_CODES[definition.group]}:
            raise ValueError(f"{code}: el código no pertenece al grupo {definition.group}")
        if not definition.template.strip() or not definition.action.strip():
            raise ValueError(f"{code}: la plantilla y la acción sugerida no pueden estar vacías")
        if definition.group is MessageGroup.FAILURE:
            if not definition.stages or Stage.RECEPTION in definition.stages:
                raise ValueError(f"{code}: un Motivo_de_Fallo ocurre en detección o después")
        elif definition.group is MessageGroup.RECEPTION:
            if definition.stages != _RECEPTION:
                raise ValueError(f"{code}: un error de recepción ocurre en la etapa recepción")
        elif definition.stages:
            raise ValueError(f"{code}: este grupo no ocurre en una Etapa_de_Procesamiento")
        _check_fields(code, (definition.template, definition.action), definition.params)
        catalog[code] = definition
    missing = {m.value for codes in _GROUP_CODES.values() for m in codes} - catalog.keys()
    if missing:
        raise ValueError(f"códigos sin entrada en el catálogo: {sorted(missing)}")
    return MappingProxyType(catalog)


def _build_notices() -> Mapping[str, NoticeDef]:
    notices: dict[str, NoticeDef] = {}
    for definition in _NOTICE_DEFINITIONS:
        if definition.code in notices or definition.code in CATALOG:
            raise ValueError(f"código de aviso repetido: {definition.code}")
        if not definition.template.strip():
            raise ValueError(f"{definition.code}: la plantilla no puede estar vacía")
        _check_fields(definition.code, (definition.template,), definition.params)
        notices[definition.code] = definition
    missing = {code.value for code in NoticeCode} - notices.keys()
    if missing:
        raise ValueError(f"avisos sin plantilla: {sorted(missing)}")
    return MappingProxyType(notices)


# Se validan al importar el módulo: un catálogo incompleto o inconsistente es un error de
# programación y no debe llegar a ejecutarse
CATALOG: Mapping[str, MessageDef] = _build_catalog()
NOTICES: Mapping[str, NoticeDef] = _build_notices()
if set(STATE_LABELS) != set(TaskState):
    raise ValueError("cada Estado_de_Tarea necesita su nombre en español")


# --- Armado de mensajes ---


@dataclass(frozen=True, slots=True)
class UserMessage:
    """Mensaje al usuario: Etapa_de_Procesamiento, motivo y acción sugerida (Req 16.4, 16.13)."""

    code: str
    stage: Stage | None  # None en Rechazos_de_Destino, solicitudes y cancelaciones
    message: str  # el motivo
    action: str  # la acción sugerida

    @property
    def text(self) -> str:
        """El mensaje completo, solo con la etapa, el motivo y la acción sugerida."""
        parts: list[str] = [] if self.stage is None else [f"Etapa: {self.stage.value}."]
        parts += [f"Motivo: {self.message}", f"Acción sugerida: {self.action}"]
        return " ".join(parts)


def _render_params(
    code: str, spec: Mapping[str, ParamKind], params: Mapping[str, object]
) -> dict[str, str]:
    missing = sorted(set(spec) - set(params))
    extra = sorted(str(name) for name in set(params) - set(spec))
    if missing or extra:
        raise InvalidMessageError(f"{code}: faltan los parámetros {missing}, sobran {extra}")
    rendered: dict[str, str] = {}
    for name, kind in spec.items():
        try:
            rendered[name] = _RENDERERS[kind](params[name])
        except InvalidMessageError as error:
            raise InvalidMessageError(f"{code}: el parámetro {name!r} {error}") from None
    return rendered


def render_failure(failure: Failure) -> UserMessage:
    """Mensaje de una Tarea fallida con su Etapa_de_Procesamiento (Req 16.4).

    Lanza `UnknownCodeError` si el motivo no es un `FailureReason` e `InvalidMessageError` si la
    etapa no es una de las del motivo o los parámetros no son los que declara.
    """
    try:
        reason = FailureReason(failure.reason)
    except ValueError:
        raise UnknownCodeError("el motivo no es un FailureReason") from None
    definition = CATALOG[reason]
    try:
        stage = Stage(failure.stage)
    except ValueError:
        raise InvalidMessageError(f"{reason.value}: la etapa no es una Stage") from None
    if stage not in definition.stages:
        raise InvalidMessageError(f"{reason.value}: no ocurre en la etapa {stage.value}")
    rendered = _render_params(reason.value, definition.params, failure.params)
    return UserMessage(
        code=reason.value,
        stage=stage,
        message=definition.template.format_map(rendered),
        action=definition.action.format_map(rendered),
    )


def render_error(code: str, params: Mapping[str, object] | None = None) -> UserMessage:
    """Mensaje de un error de recepción, un Rechazo_de_Destino, un error de una solicitud sobre
    una Tarea o un motivo de cancelación.

    Los errores de recepción llevan la etapa recepción (Req 16.13); los demás no tienen etapa.
    Los Motivos_de_Fallo se arman con `render_failure`, porque su etapa depende del caso.
    """
    definition = CATALOG.get(code) if isinstance(code, str) else None
    if definition is None:
        raise UnknownCodeError("el código no está en el catálogo")
    if definition.group is MessageGroup.FAILURE:
        raise InvalidMessageError(f"{definition.code}: los Motivos_de_Fallo usan render_failure")
    rendered = _render_params(definition.code, definition.params, params or {})
    return UserMessage(
        code=definition.code,
        stage=definition.stages[0] if definition.stages else None,
        message=definition.template.format_map(rendered),
        action=definition.action.format_map(rendered),
    )


def render_notice(notice: Notice) -> str:
    """Texto en español de un aviso al usuario."""
    definition = NOTICES.get(notice.code) if isinstance(notice.code, str) else None
    if definition is None:
        raise UnknownCodeError("el código de aviso no está en el catálogo")
    rendered = _render_params(definition.code, definition.params, notice.params)
    return definition.template.format_map(rendered)
