"""Información del origen que obtiene el Detector_de_Tipo y su forma JSON (Req 2.12, 16.1).

Un inspector analiza el Archivo_de_Origen de un Formato y devuelve un `SourceInfo`, con los datos
que necesitan los convertidores de ese Formato, o un `SourceRejection`, que es un
Rechazo_de_Origen. Los inspectores `image`, `pdf`, `docx` y `media` corren en un subproceso y
entregan su resultado en JSON por la salida estándar; el inspector `text` corre en el proceso
principal y produce `TextInfo` (design.md, "Inspectores").

- `SourceInfo` es la clase base. Cada subclase es una dataclass inmutable registrada con
  `@register_source_info("nombre")`, que le da un nombre en la forma JSON. Así el proceso
  principal reconstruye el tipo exacto sin importar el módulo del inspector. Los campos admiten
  `bool`, `int`, `float`, `str`, enumeraciones, `Literal`, `X | None`, `tuple[X, ...]` y otras
  dataclasses con campos de esos tipos (por ejemplo, las páginas de un PDF).
- `TextInfo` es la información de un Formato_de_Texto: su codificación (UTF-8 o Windows-1252) y
  si el contenido empieza con la BOM_UTF8. Con ella el convertidor obtiene el Texto_Decodificado
  y no usa la codificación que declare el documento (Req 2.12).
- `NoInfo` es la información de un Formato que no declara inspector.
- `SourceRejection` es un Rechazo_de_Origen: un Motivo_de_Fallo de la etapa detección con sus
  parámetros y el Tipo_Real, si se llegó a determinar (Req 16.1, 16.7). Puede llevar además el
  detalle técnico saneado del fallo, para el evento `task_failed` del Log_de_Eventos; ese
  detalle no cuenta al comparar rechazos y nunca llega al usuario.
- `DetectionResult` es lo que registra la Tarea cuando la detección termina sin rechazo: el
  Tipo_Real, su `SourceInfo` y los avisos al usuario (Req 2.6).

La salida de un inspector es un objeto JSON con una sola clave:
`{"info": {"type": <nombre>, "data": {<campo>: <valor>, …}}}` o
`{"rejection": {"reason": <Motivo_de_Fallo>, "params": {…}}}`. `dump_inspector_output` la
escribe y `load_inspector_output` la valida campo por campo. La escribe un subproceso que analiza
un archivo hostil, así que un nombre no registrado, un campo que falta o sobra, un valor de otro
tipo (un booleano no es un entero), un número no finito o un motivo que no es de un inspector
lanzan `InspectorOutputError`. El mensaje de ese error nombra el campo y lo que se esperaba,
nunca el valor recibido, porque puede venir del contenido del archivo.

Este módulo no importa los inspectores ni sus bibliotecas: lo importan los convertidores
(`converters/base.py`) y los subprocesos de inspección.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import math
import re
import types
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cache
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, NoReturn, Self, cast, get_args, get_origin

from converter.detection.text import TextKind, decode_text
from converter.eventlog import ErrorDetail, ToolDetail
from converter.messages_es import FailureReason, render_failure
from converter.models import Failure, Notice, Stage

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

    from converter.eventlog import JsonValue

# Rechazos de la etapa del inspector, en el orden de evaluación del diseño: cada inspector
# comprueba los suyos en este orden y devuelve el primero que corresponde. Los
# Límites_de_Descompresión van antes que "archivo dañado" porque Req 16.2 excluye de ese motivo
# a los DOCX que los superan. DOCX_Cifrado lo decide la sonda de variante del Detector_de_Tipo,
# en el mismo lugar que PDF_Protegido: son de Formatos distintos.
INSPECTION_REJECTIONS: tuple[FailureReason, ...] = (
    FailureReason.PROTECTED_PDF,
    FailureReason.ENCRYPTED_DOCX,
    FailureReason.DECOMPRESSION_LIMITS,
    FailureReason.DAMAGED,
    FailureReason.ANIMATED_IMAGE,
    FailureReason.PIXEL_LIMIT,
)

# Motivos que puede devolver un inspector: sus rechazos y `error_interno`
INSPECTOR_REASONS: tuple[FailureReason, ...] = (
    *INSPECTION_REJECTIONS,
    FailureReason.INTERNAL_ERROR,
)

# Nombre de un tipo de SourceInfo en la forma JSON, p. ej. "image"
_INFO_NAME = re.compile(r"[a-z][a-z0-9_]*")  # se usa con `fullmatch`

type RejectionParams = Mapping[str, str | int | list[str]]


class InspectorOutputError(ValueError):
    """La salida de un inspector, o un `SourceInfo` en JSON, no tiene la forma esperada."""


def _invalid(where: str, problem: str) -> NoReturn:
    raise InspectorOutputError(f"{where}: {problem}")


# --------------------------------------------------------------------------------------------
# Clase base y registro de tipos


class SourceInfo:
    """Base de la información del origen que obtiene el inspector de un Formato.

    Las subclases son dataclasses inmutables registradas con `@register_source_info`. Cada
    convertidor reduce la que recibe al tipo de su familia con `isinstance`.
    """

    __slots__ = ()

    def to_json(self) -> dict[str, JsonValue]:
        """Forma JSON: `{"type": <nombre registrado>, "data": {<campo>: <valor>, …}}`.

        Lanza `TypeError` si la clase no está registrada o un campo tiene un tipo no admitido, y
        `ValueError` si un número no es finito.
        """
        name = _NAME_OF.get(type(self))
        if name is None:
            raise TypeError(
                f"{type(self).__qualname__} no está registrado con @register_source_info"
            )
        return {"type": name, "data": _dump(self, "info.data")}

    @classmethod
    def from_json(cls, value: object) -> Self:
        """Reconstruye un `SourceInfo` desde su forma JSON ya decodificada.

        El tipo registrado con el nombre de `value["type"]` tiene que ser `cls` o una subclase.
        Lanza `InspectorOutputError` si `value` no es la forma JSON válida de ese tipo.
        """
        if not isinstance(value, dict) or set(value) != {"type", "data"}:
            _invalid("info", "debe ser un objeto con las claves type y data")
        name = value["type"]
        info_type = _BY_NAME.get(name) if isinstance(name, str) else None
        if info_type is None:
            _invalid("info.type", "no es un tipo de SourceInfo registrado")
        if not issubclass(info_type, cls):
            _invalid("info.type", f"no corresponde a {cls.__name__}")
        # `register_source_info` solo acepta dataclasses
        dataclass_type = cast("type[DataclassInstance]", info_type)
        return cast(Self, _load_dataclass(dataclass_type, value["data"], "info.data"))


# Tipos registrados por nombre, en orden de registro, y el nombre de cada tipo
_BY_NAME: dict[str, type[SourceInfo]] = {}
_NAME_OF: dict[type[SourceInfo], str] = {}


def register_source_info[T: SourceInfo](name: str) -> Callable[[type[T]], type[T]]:
    """Decorador que registra una subclase de `SourceInfo` con `name` para su forma JSON.

    Va sobre `@dataclass(frozen=True, ...)`. Lanza `ValueError` si `name` no tiene la forma
    `[a-z][a-z0-9_]*` o ya está registrado, o si la clase ya tiene otro nombre, y `TypeError` si
    la clase no es una dataclass inmutable que hereda de `SourceInfo`.
    """
    if not isinstance(name, str) or _INFO_NAME.fullmatch(name) is None:
        raise ValueError(f"nombre de SourceInfo inválido: {name!r}")

    def decorator(cls: type[T]) -> type[T]:
        if not isinstance(cls, type) or not issubclass(cls, SourceInfo):
            raise TypeError(f"@register_source_info decora una subclase de SourceInfo: {name!r}")
        # `__dataclass_fields__` propio: una subclase sin decorar heredaría el de su padre
        params = getattr(cls, "__dataclass_params__", None)
        if "__dataclass_fields__" not in vars(cls) or params is None or not params.frozen:
            raise TypeError(f"{cls.__qualname__} debe ser una dataclass con frozen=True")
        if name in _BY_NAME:
            raise ValueError(f"ya hay un SourceInfo registrado con el nombre {name!r}")
        if cls in _NAME_OF:
            raise ValueError(f"{cls.__qualname__} ya está registrado como {_NAME_OF[cls]!r}")
        _BY_NAME[name] = cls
        _NAME_OF[cls] = name
        return cls

    return decorator


def registered_source_infos() -> Mapping[str, type[SourceInfo]]:
    """Tipos de `SourceInfo` registrados por nombre, en orden de registro (copia inmutable)."""
    return MappingProxyType(dict(_BY_NAME))


# --------------------------------------------------------------------------------------------
# Conversión entre dataclasses y JSON


def _dump(value: object, where: str) -> JsonValue:
    if isinstance(value, enum.Enum):
        return _dump(value.value, where)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _dump(getattr(value, item.name), f"{where}.{item.name}")
            for item in dataclasses.fields(value)
            if item.init
        }
    if isinstance(value, tuple):
        return [_dump(item, f"{where}[{index}]") for index, item in enumerate(value)]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{where}: el número no es finito")
        return value
    raise TypeError(f"{where}: tipo no admitido en un SourceInfo: {type(value).__qualname__}")


@cache
def _init_fields(cls: type[DataclassInstance]) -> tuple[tuple[str, object], ...]:
    """Campos de `__init__` de la dataclass `cls` con su tipo ya resuelto, en orden."""
    hints = typing.get_type_hints(cls)
    return tuple((item.name, hints[item.name]) for item in dataclasses.fields(cls) if item.init)


def _load_dataclass(cls: type[DataclassInstance], value: object, where: str) -> object:
    if not isinstance(value, dict):
        _invalid(where, "debe ser un objeto")
    fields = _init_fields(cls)
    names = {name for name, _ in fields}
    if any(key not in names for key in value):
        _invalid(where, "tiene campos que no son de " + cls.__name__)
    missing = [name for name, _ in fields if name not in value]
    if missing:
        _invalid(where, "faltan los campos " + ", ".join(missing))
    arguments = {name: _load(hint, value[name], f"{where}.{name}") for name, hint in fields}
    try:
        return cast(Callable[..., object], cls)(**arguments)
    except (TypeError, ValueError):
        # La validación propia de la clase (`__post_init__`) rechazó la combinación de valores.
        # El error se lanza fuera del `except` para no encadenar el original, que puede citarlos.
        pass
    _invalid(where, "los valores no forman un " + cls.__name__ + " válido")


def _load(hint: object, value: object, where: str) -> object:
    if isinstance(hint, typing.TypeAliasType):
        return _load(hint.__value__, value, where)
    if hint is None or hint is type(None):
        if value is not None:
            _invalid(where, "debe ser null")
        return None
    origin = get_origin(hint)
    if origin is typing.Union or origin is types.UnionType:
        options = get_args(hint)
        if value is None:
            if type(None) not in options:
                _invalid(where, "no puede ser null")
            return None
        rest = [option for option in options if option is not type(None)]
        if len(rest) != 1:
            raise TypeError(f"{where}: un SourceInfo solo admite uniones de la forma X | None")
        return _load(rest[0], value, where)
    if origin is Literal:
        for option in get_args(hint):
            if type(value) is type(option) and value == option:
                return option
        _invalid(where, "no es uno de los valores admitidos")
    if origin is tuple:
        arguments = get_args(hint)
        if len(arguments) != 2 or arguments[1] is not Ellipsis:
            raise TypeError(f"{where}: un SourceInfo solo admite tuplas de la forma tuple[X, ...]")
        if not isinstance(value, list):
            _invalid(where, "debe ser una lista")
        return tuple(
            _load(arguments[0], item, f"{where}[{index}]") for index, item in enumerate(value)
        )
    if hint is bool:
        if not isinstance(value, bool):
            _invalid(where, "debe ser un booleano")
        return value
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            _invalid(where, "debe ser un número entero")
        return value
    if hint is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _invalid(where, "debe ser un número")
        if not math.isfinite(value):
            _invalid(where, "debe ser un número finito")
        return float(value)
    if hint is str:
        if not isinstance(value, str):
            _invalid(where, "debe ser un texto")
        return value
    if isinstance(hint, type) and issubclass(hint, enum.Enum):
        # Mismo tipo y valor: así `True` no pasa por el miembro de valor 1
        for member in hint:
            if type(member.value) is type(value) and member.value == value:
                return member
        _invalid(where, "no es un valor de " + hint.__name__)
    if isinstance(hint, type) and dataclasses.is_dataclass(hint):
        return _load_dataclass(hint, value, where)
    raise TypeError(f"{where}: tipo no admitido en un SourceInfo: {hint!r}")


# --------------------------------------------------------------------------------------------
# Información de los Formatos sin inspector en subproceso


@register_source_info("text")
@dataclass(frozen=True, slots=True)
class TextInfo(SourceInfo):
    """Información de un Formato_de_Texto, que produce el inspector `text` (Req 2.12)."""

    encoding: TextKind  # TextKind.UTF8 o TextKind.WINDOWS_1252
    had_bom: bool  # el contenido empieza con la BOM_UTF8; solo en un Texto_UTF8_Válido

    def __post_init__(self) -> None:
        if not isinstance(self.encoding, TextKind) or self.encoding is TextKind.INVALID:
            raise ValueError("la codificación de un Formato_de_Texto es UTF-8 o Windows-1252")
        if not isinstance(self.had_bom, bool):
            raise TypeError("had_bom debe ser un booleano")
        if self.had_bom and self.encoding is not TextKind.UTF8:
            raise ValueError("solo un Texto_UTF8_Válido se clasifica con la BOM_UTF8 inicial")

    def decode(self, data: bytes) -> str:
        """Texto_Decodificado de `data`, el contenido completo del Archivo_de_Origen.

        No depende de la codificación que declare el documento, como `<meta charset>` en HTML.
        """
        return decode_text(data, self.encoding)


@register_source_info("none")
@dataclass(frozen=True, slots=True)
class NoInfo(SourceInfo):
    """Información de un Formato que no declara inspector: no hay datos del origen."""


# --------------------------------------------------------------------------------------------
# Rechazo_de_Origen y resultado de la detección


@dataclass(frozen=True, slots=True)
class SourceRejection:
    """Rechazo_de_Origen: la Tarea pasa de recibida a fallida en la etapa detección (Req 16.1).

    Lanza `ValueError` si `reason` no es un `FailureReason` o `real_type` está vacío, e
    `InvalidMessageError` (también un `ValueError`) si el motivo no ocurre en la detección o los
    parámetros no son los que declara su mensaje. Lanza `TypeError` si `detail` no es un
    `ToolDetail` ni un `ErrorDetail`.
    """

    reason: FailureReason
    params: RejectionParams = field(default_factory=dict)  # valores del caso: límite, lista
    real_type: str | None = None  # Tipo_Real, si se determinó antes del rechazo (Req 16.7)
    # Detalle técnico saneado para el Log_de_Eventos (Req 16.7, 16.8): la salida reducida de la
    # herramienta o la clase de la excepción. No se compara, no se muestra y no viaja en JSON
    detail: ToolDetail | ErrorDetail | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", FailureReason(self.reason))
        copied = {k: list(v) if isinstance(v, list) else v for k, v in self.params.items()}
        object.__setattr__(self, "params", MappingProxyType(copied))
        if self.real_type is not None and (
            not isinstance(self.real_type, str) or not self.real_type
        ):
            raise ValueError("el Tipo_Real de un Rechazo_de_Origen, si está, no puede estar vacío")
        if self.detail is not None and not isinstance(self.detail, (ToolDetail, ErrorDetail)):
            raise TypeError("el detalle de un Rechazo_de_Origen es un ToolDetail o un ErrorDetail")
        render_failure(self.failure)

    @property
    def failure(self) -> Failure:
        """Motivo_de_Fallo que se registra en la Tarea, con la etapa detección."""
        return Failure(reason=self.reason, stage=Stage.DETECTION, params=self.params)


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """Detección terminada sin Rechazo_de_Origen: lo que se registra en la Tarea."""

    real_type: str  # Tipo_Real: nombre del Formato registrado
    info: SourceInfo  # lo que obtuvo el inspector del Formato
    notices: tuple[Notice, ...] = ()  # avisos al usuario, p. ej. extension_no_coincide (Req 2.6)

    def __post_init__(self) -> None:
        if not isinstance(self.real_type, str) or not self.real_type:
            raise ValueError("el Tipo_Real no puede estar vacío")
        if not isinstance(self.info, SourceInfo):
            raise TypeError("info debe ser un SourceInfo")
        object.__setattr__(self, "notices", tuple(self.notices))


# --------------------------------------------------------------------------------------------
# Salida de un inspector


def dump_inspector_output(result: SourceInfo | SourceRejection) -> str:
    """JSON en una línea, solo ASCII, con el resultado de un inspector.

    Lanza `ValueError` si el rechazo no es de un inspector (`INSPECTOR_REASONS`). El Tipo_Real
    del rechazo no se escribe, porque lo agrega el Detector_de_Tipo, y el detalle técnico
    tampoco: lo arma el proceso principal con la salida saneada del subproceso.
    """
    document: dict[str, JsonValue]
    if isinstance(result, SourceRejection):
        if result.reason not in INSPECTOR_REASONS:
            raise ValueError(f"un inspector no produce el rechazo {result.reason.value}")
        params: dict[str, JsonValue] = {key: value for key, value in result.params.items()}
        document = {"rejection": {"reason": result.reason.value, "params": params}}
    elif isinstance(result, SourceInfo):
        document = {"info": result.to_json()}
    else:
        raise TypeError("un inspector devuelve un SourceInfo o un SourceRejection")
    return json.dumps(document, allow_nan=False, separators=(",", ":"))


def load_inspector_output(text: str | bytes) -> SourceInfo | SourceRejection:
    """Resultado de un inspector desde el JSON que escribió `dump_inspector_output`.

    Lanza `InspectorOutputError` si `text` no es JSON válido o no tiene esa forma.
    """
    try:
        document = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        # `UnicodeDecodeError` y `JSONDecodeError` son `ValueError`; un anidamiento muy profundo
        # agota la recursión del decodificador
        raise InspectorOutputError("la salida del inspector no es JSON válido") from None
    if not isinstance(document, dict) or len(document) != 1:
        _invalid("salida", "debe ser un objeto con una sola clave, info o rejection")
    if "info" in document:
        return SourceInfo.from_json(document["info"])
    if "rejection" in document:
        return _load_rejection(document["rejection"])
    _invalid("salida", "debe ser un objeto con una sola clave, info o rejection")


def _reject_constant(name: str) -> NoReturn:
    # NaN, Infinity y -Infinity no son JSON estándar
    raise ValueError("constante no admitida")


def _load_rejection(value: object) -> SourceRejection:
    if not isinstance(value, dict) or set(value) != {"reason", "params"}:
        _invalid("rejection", "debe ser un objeto con las claves reason y params")
    code = value["reason"]
    reason = next((r for r in INSPECTOR_REASONS if isinstance(code, str) and code == r.value), None)
    if reason is None:
        _invalid("rejection.reason", "no es un rechazo de un inspector")
    params = value["params"]
    if not isinstance(params, dict):
        _invalid("rejection.params", "debe ser un objeto")
    for item in params.values():
        is_text_list = isinstance(item, list) and all(isinstance(text, str) for text in item)
        if isinstance(item, bool) or not (isinstance(item, (str, int)) or is_text_list):
            _invalid(
                "rejection.params", "cada valor debe ser un texto, un entero o una lista de textos"
            )
    try:
        return SourceRejection(reason, params)
    except ValueError:
        pass  # el error se lanza fuera del `except` para no encadenar el original
    _invalid("rejection.params", "no son los parámetros del motivo " + reason.value)
