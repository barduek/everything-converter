"""Interfaz de los convertidores, su registro y las funciones de arranque (Req 4.3, 4.4).

Un convertidor implementa una familia de Pares_de_Conversión y se registra con un identificador
`"familia.nombre"` (p. ej. "image.raster") que los pares del catálogo citan en `converter`. El
Gestor_de_Tareas solo conoce el protocolo `Converter`:

- `preflight(source, request, config)` corre antes de encolar y devuelve un Rechazo_de_Destino
  propio del par, o None (Req 16.12). No hace E/S ni lanza subprocesos: decide con la información
  del origen que obtuvo el inspector del Detector_de_Tipo.
- `run(ctx)` ejecuta el par en subprocesos con `ctx.runner` y devuelve un `ConversionOutcome`
  con el resultado dentro de `work/`. Si falla, lanza `ConversionFailure` con un
  Motivo_de_Fallo; el Gestor_de_Tareas trata cualquier otra excepción como `error_interno`.

`@register_converter("familia.nombre")` decora una clase: fija su atributo `id`, crea una
instancia sin argumentos y la registra. Los convertidores no guardan estado por Tarea; lo que
una familia prepara al iniciar (por ejemplo, la lista de decodificadores de FFmpeg) lo guarda su
módulo desde una función de arranque.

`@on_startup` registra una función de arranque. El arranque las ejecuta en orden de registro con
`run_startup_hooks`, después de cargar el catálogo y antes de abrir el Puerto_de_la_Interfaz.
Pueden ser síncronas o corrutinas. Una función que lanza una excepción no impide el arranque ni
las funciones siguientes: el fallo se registra en el Log_de_Eventos (`startup_hook_failed`, con
la clase y la ubicación de la excepción, sin su mensaje), y los convertidores de esa familia
deben fallar con su Motivo_de_Fallo cuando les falten los datos.

Los registros son de proceso. `converters/__init__.py` importa los módulos del paquete para que
cada familia se registre sin cambios en el núcleo (Req 4.3).
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Protocol, cast

# Información del origen que obtuvo el inspector del Detector_de_Tipo (ImageInfo, PdfInfo,
# MediaInfo, DocxInfo, TextInfo o NoInfo). Cada convertidor la reduce al tipo de su familia con
# `isinstance`. El módulo solo depende de la clasificación de texto y de los mensajes, así que no
# carga los inspectores ni sus bibliotecas.
from converter.detection.source_info import SourceInfo
from converter.eventlog import Level, describe_exception
from converter.messages_es import FailureReason, NoticeCode, render_failure, render_notice
from converter.models import DestinationRejection, Failure, Notice, Stage

if TYPE_CHECKING:
    # Solo para anotaciones: así importar este módulo no carga el ejecutor, el almacenamiento
    # ni el Gestor_de_Tareas, que pueden importar los convertidores
    from converter.config import Config
    from converter.eventlog import ErrorDetail, EventLog, ToolDetail
    from converter.registry.model import ConversionPair
    from converter.sandbox.runner import SandboxRunner
    from converter.storage.workspace import Workspace, WorkspaceManager
    from converter.tasks.progress import ProgressReporter

# Identificador de convertidor: "familia.nombre" en minúsculas, p. ej. "video.extract_audio"
CONVERTER_ID = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")  # se usa con `fullmatch`

# Etapas_de_Procesamiento de una Tarea en_proceso: la conversión, la transcripción y la entrega
# (el empaquetado del ZIP)
PROCESSING_STAGES: frozenset[Stage] = frozenset(
    {Stage.CONVERSION, Stage.TRANSCRIPTION, Stage.DELIVERY}
)

# Evento del Log_de_Eventos de una función de arranque que lanzó una excepción
STARTUP_HOOK_FAILED = "startup_hook_failed"


# --------------------------------------------------------------------------------------------
# Solicitud, contexto y resultado


@dataclass(frozen=True, slots=True)
class ConversionRequest:
    """Lo que pidió el usuario al confirmar: Formato_de_Destino e Idioma_de_Transcripción."""

    target: str  # nombre del Formato_de_Destino, p. ej. "JPG"
    # Código del Idioma_de_Transcripción o "auto"; None si el par no es de transcripción
    language: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target, str) or not self.target:
            raise ValueError("el Formato_de_Destino de la solicitud no puede estar vacío")
        if self.language is not None and (not isinstance(self.language, str) or not self.language):
            raise ValueError("el Idioma_de_Transcripción, si está, no puede estar vacío")


class NoticeCollector:
    """Avisos al usuario que produce una conversión, en el orden en que se agregan.

    `add` valida el aviso con su plantilla de `messages_es`, así un código desconocido o un
    parámetro inválido falla en el convertidor que lo produce y no al mostrar la Tarea.
    """

    __slots__ = ("_notices",)

    def __init__(self) -> None:
        self._notices: list[Notice] = []

    def add(self, code: NoticeCode, params: Mapping[str, object] | None = None) -> Notice:
        """Agrega el aviso `code` con `params` y lo devuelve.

        Lanza `ValueError` si `code` no es un `NoticeCode` e `InvalidMessageError` si los
        parámetros no son los que declara su plantilla.
        """
        notice = Notice(code=NoticeCode(code), params=MappingProxyType(dict(params or {})))
        render_notice(notice)
        self._notices.append(notice)
        return notice

    @property
    def notices(self) -> tuple[Notice, ...]:
        """Avisos agregados, en orden."""
        return tuple(self._notices)


@dataclass(frozen=True, slots=True)
class ConversionContext:
    """Lo que recibe `Converter.run` para ejecutar un par sobre una Tarea."""

    task_id: str
    pair: ConversionPair
    request: ConversionRequest  # destino e Idioma_de_Transcripción
    source: SourceInfo  # datos que obtuvo el inspector del Detector_de_Tipo
    workspace: Workspace
    config: Config
    runner: SandboxRunner  # lanza subprocesos con límites y cancelación
    progress: ProgressReporter  # Porcentaje_de_Avance monótono
    notices: NoticeCollector  # avisos al usuario


@dataclass(frozen=True, slots=True)
class ConversionOutcome:
    """Resultado de `Converter.run`."""

    result_path: Path  # dentro de work/, el Gestor_de_Tareas lo mueve a result/
    result_kind: Literal["single", "zip"]  # "zip": un ZIP con varios archivos (p. ej. páginas)
    detected_language: str | None = None  # Idioma_Detectado con Detección_Automática_de_Idioma

    def __post_init__(self) -> None:
        if self.result_kind not in ("single", "zip"):
            raise ValueError(f"result_kind debe ser 'single' o 'zip': {self.result_kind!r}")


class ConversionFailure(Exception):
    """Fallo de una conversión con su Motivo_de_Fallo (Req 16.1).

    `failure` es lo que el Gestor_de_Tareas guarda en la Tarea. `detail` es el detalle técnico
    saneado (`sanitize_tool_output` o `describe_exception`) que va solo al Log_de_Eventos. El
    texto de la excepción es el código del motivo, nunca los parámetros.
    """

    failure: Failure
    detail: ToolDetail | ErrorDetail | None

    def __init__(
        self,
        reason: FailureReason,
        *,
        stage: Stage = Stage.CONVERSION,
        params: Mapping[str, str | int | list[str]] | None = None,
        detail: ToolDetail | ErrorDetail | None = None,
    ) -> None:
        """Lanza `ValueError` si `reason` no es un `FailureReason` o `stage` no es una de
        `PROCESSING_STAGES`, e `InvalidMessageError` si el motivo no ocurre en esa etapa o los
        parámetros no son los que declara su mensaje."""
        reason = FailureReason(reason)
        stage = Stage(stage)
        if stage not in PROCESSING_STAGES:
            raise ValueError(f"un convertidor no falla en la etapa {stage.value}")
        failure = Failure(reason=reason, stage=stage, params=MappingProxyType(dict(params or {})))
        render_failure(failure)
        super().__init__(reason.value)
        self.failure = failure
        self.detail = detail


# --------------------------------------------------------------------------------------------
# Protocolo y registro de convertidores


class _Implementation(Protocol):
    """Métodos que define una clase decorada con `@register_converter`; el `id` lo fija él."""

    def preflight(
        self, source: SourceInfo, request: ConversionRequest, config: Config
    ) -> DestinationRejection | None:
        """Comprobaciones previas a encolar: Rechazo_de_Destino específico del par."""
        ...

    async def run(self, ctx: ConversionContext) -> ConversionOutcome:
        """Ejecuta el par en subprocesos; lanza ConversionFailure con un Motivo_de_Fallo."""
        ...


class Converter(_Implementation, Protocol):
    """Convertidor de una familia de pares, registrado con su identificador."""

    @property
    def id(self) -> str:
        """Identificador "familia.nombre", p. ej. "image.raster"."""
        ...


class UnknownConverterError(LookupError):
    """No hay ningún convertidor registrado con ese identificador."""

    def __init__(self, converter_id: str) -> None:
        super().__init__(f"convertidor no registrado: {converter_id!r}")
        self.converter_id = converter_id


# Convertidores por identificador, en orden de registro
_CONVERTERS: dict[str, Converter] = {}


def register_converter[T: _Implementation](converter_id: str) -> Callable[[type[T]], type[T]]:
    """Decorador de clase que registra un convertidor con `converter_id`.

    La clase define `preflight` y `run` (una corrutina) y se puede crear sin argumentos. El
    decorador le fija `id = converter_id` y registra una instancia. Lanza `ValueError` si
    `converter_id` no tiene la forma "familia.nombre", ya pertenece a otro convertidor o la
    clase define otro `id`, y `TypeError` si la clase no tiene esos métodos.
    """
    if not isinstance(converter_id, str) or CONVERTER_ID.fullmatch(converter_id) is None:
        raise ValueError(f"identificador de convertidor inválido: {converter_id!r}")

    def decorator(cls: type[T]) -> type[T]:
        if not isinstance(cls, type):
            raise TypeError(f"@register_converter decora una clase: {converter_id!r}")
        if converter_id in _CONVERTERS:
            raise ValueError(f"ya hay un convertidor registrado con el id {converter_id!r}")
        if not callable(getattr(cls, "preflight", None)):
            raise TypeError(f"{cls.__qualname__} no define preflight")
        if not inspect.iscoroutinefunction(getattr(cls, "run", None)):
            raise TypeError(f"{cls.__qualname__}.run debe ser una corrutina (async def)")
        declared = getattr(cls, "id", converter_id)
        if declared != converter_id:
            raise ValueError(f"{cls.__qualname__} define id {declared!r}, no {converter_id!r}")
        cls.id = converter_id  # type: ignore[attr-defined]
        _CONVERTERS[converter_id] = cast(Converter, cls())
        return cls

    return decorator


def get_converter(converter_id: str) -> Converter:
    """Convertidor registrado con `converter_id`. Lanza `UnknownConverterError` si no existe."""
    try:
        return _CONVERTERS[converter_id]
    except KeyError:
        raise UnknownConverterError(converter_id) from None


def registered_converters() -> Mapping[str, Converter]:
    """Convertidores registrados por identificador, en orden de registro (copia inmutable)."""
    return MappingProxyType(dict(_CONVERTERS))


# --------------------------------------------------------------------------------------------
# Funciones de arranque


@dataclass(frozen=True, slots=True)
class StartupContext:
    """Lo que recibe una función de arranque."""

    config: Config
    runner: SandboxRunner  # para consultar herramientas, p. ej. `ffmpeg -decoders`
    storage: WorkspaceManager  # para crear un Espacio_de_Trabajo donde correr esas consultas
    event_log: EventLog


type StartupHook = Callable[[StartupContext], Awaitable[None] | None]

# Funciones de arranque, en orden de registro
_STARTUP_HOOKS: list[StartupHook] = []


def on_startup[H: StartupHook](hook: H) -> H:
    """Decorador que registra una función de arranque. Lanza `ValueError` si ya está."""
    if not callable(hook):
        raise TypeError("@on_startup decora una función")
    if hook in _STARTUP_HOOKS:
        raise ValueError(f"la función de arranque {hook_name(hook)} ya está registrada")
    _STARTUP_HOOKS.append(hook)
    return hook


def startup_hooks() -> tuple[StartupHook, ...]:
    """Funciones de arranque registradas, en orden de registro."""
    return tuple(_STARTUP_HOOKS)


def hook_name(hook: StartupHook) -> str:
    """Nombre de una función de arranque en el Log_de_Eventos: módulo y nombre calificado."""
    module = getattr(hook, "__module__", None) or "?"
    name = getattr(hook, "__qualname__", None) or type(hook).__qualname__
    return f"{module}.{name}"


async def run_startup_hooks(ctx: StartupContext) -> None:
    """Ejecuta las funciones de arranque en orden de registro, esperando las corrutinas.

    Una excepción de una función se registra como `startup_hook_failed` y no detiene a las
    siguientes. La cancelación (`asyncio.CancelledError`) sí se propaga.
    """
    for hook in startup_hooks():
        try:
            result = hook(ctx)
            if inspect.isawaitable(result):
                await result
        except Exception as error:
            ctx.event_log.emit(
                Level.ERROR,
                STARTUP_HOOK_FAILED,
                {"hook": hook_name(hook), **describe_exception(error).as_json()},
            )
