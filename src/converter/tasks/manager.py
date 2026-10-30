"""Gestor_de_Tareas, capa asíncrona: creación, detección, vista, confirmación, rechazo de la
confirmación, cancelación, ejecución, entrega y retención de las Tareas (Req 1.1, 1.2, 1.7, 2.6,
2.11, 3.1–3.7, 11.3, 11.9, 11.10, 13.8, 14.1, 14.2, 14.5–14.8, 14.10, 14.11, 14.13, 15.2, 15.8,
15.10, 16.1, 16.3, 16.6, 16.7, 16.9–16.12, 17.2–17.4, 19.2, 19.6, 19.10, 19.11).

`TaskManager` guarda las Tareas en memoria, en el único proceso del Conversor, y recibe por
inyección la configuración, el reloj monotónico, el ejecutor aislado, el
Registro_de_Conversiones, los convertidores, los Idiomas_Soportados, el Detector_de_Tipo, el
barrendero del Gestor_de_Archivos_Temporales y el Log_de_Eventos. Empieza
sin Tareas y no recupera las de un inicio anterior (Req 14.13, D14).

Creación (`create`):

- El Identificador_de_Tarea sale de `uuid.uuid4()`, que usa el generador aleatorio del sistema
  operativo: 122 bits aleatorios en forma canónica. El conjunto de identificadores emitidos desde
  el inicio garantiza que ninguno se repita, aunque su Tarea ya no exista (Req 19.2).
- La Tarea queda en recibida con el Espacio_de_Trabajo de la recepción, que tiene su único
  Archivo_de_Origen, y con el Nombre_Original sin modificar (Req 1.1, 1.2, 1.7).
- `create` lanza la detección en segundo plano y devuelve la Tarea sin esperarla: la respuesta
  de la subida llega enseguida y la Interfaz_de_Usuario consulta la vista mientras
  `detection` es "en_curso".

Detección. Un trabajo por Tarea llama al Detector_de_Tipo y espera su resultado como máximo
`DETECTION_TIMEOUT_SECONDS` desde la creación:

- Sin rechazo, la Tarea registra el Tipo_Real, la información del origen y los avisos de la
  detección, como la extensión distinta (Req 2.6). La vista ofrece desde ese momento los
  Formatos_de_Destino (Req 3.1).
- Un Rechazo_de_Origen lleva la Tarea de recibida a fallida con su Motivo_de_Fallo, de la etapa
  detección, y con el Tipo_Real si se determinó (Req 3.6, 16.1).
- Una excepción del detector (`DetectionSetupError`, `UnknownProbeError`, `ValueError` o
  `TypeError` de la función de inspección, un error de E/S), un resultado de otro tipo y el
  vencimiento del plazo llevan la Tarea a fallida con `error_interno` (Req 16.9).
- Al vencer el plazo, el fallo se registra en ese momento y recién después se detiene el
  detector, que puede tardar hasta 2 segundos en terminar su subproceso. Así el Tipo_Real o el
  rechazo quedan registrados dentro de los 10 segundos de Req 2.11 aunque el inspector no
  responda. El plazo deja medio segundo de margen sobre esos 10 segundos.
- La detección no pasa por el planificador ni espera a las Tareas en_proceso, así que el
  Límite_de_Concurrencia no la demora (Req 19.10). Las inspecciones en subproceso tienen su
  propio semáforo en `detection/inspect_runner.py`.
- `Task.detection_done` se activa cuando la detección termina, con o sin rechazo, o se
  interrumpe; quien necesita el Tipo_Real puede esperarlo sin tomar el lock de la Tarea.

Toda Tarea que pasa a fallida registra el evento `task_failed` en el Log_de_Eventos, con el
Tipo_Real si se determinó, el par si se eligió y el detalle técnico saneado (Req 16.7), y encola
la eliminación de su Espacio_de_Trabajo en el barrendero (Req 17.4). El registro de la Tarea, sin
contenido, sigue en memoria 24 horas más (ver "Retención").

Vista (`view`): la `TaskView` de la Tarea pedida, armada solo con esa Tarea y con los
Formatos_de_Destino de su Tipo_Real en orden de registro, con Clasificación_de_Pérdida y
Descripción_de_Pérdida (Req 3.1, 3.2, 3.5, 19.6). Un identificador que no corresponde a ninguna
Tarea lanza `TaskUnavailable`, el mismo error en cualquier caso (Req 19.5).

Solicitudes que modifican una Tarea (`confirm`, `decline`, `cancel`). Cada una toma el
`asyncio.Lock` de la Tarea durante toda su evaluación, así varias solicitudes simultáneas se
atienden de a una y cada una ve el estado que dejó la anterior (Req 19.11). Las tres devuelven la
`TaskView` armada antes de soltar el lock, con el estado que dejó esa solicitud. Una solicitud
que no se atiende lanza una `TaskRequestError` con el código y los parámetros de su mensaje en
`messages_es` y no cambia la Tarea:

- `StateForbids`, `estado_no_permite` con el Estado_de_Tarea actual (Req 14.11).
- `ConfirmationRequired`, `confirmacion_requerida` (Req 3.3).
- `TargetRejected`, un Rechazo_de_Destino: la Tarea sigue en recibida con su Archivo_de_Origen y
  sin Motivo_de_Fallo (Req 16.12).

Confirmación (`confirm`), en el orden del diseño:

1. La Tarea está en recibida; si no, `StateForbids` (Req 14.11).
2. La detección terminó. Si sigue en curso, la confirmación la espera con el lock tomado; el
   registro del resultado no toma el lock, así que no se bloquean. La detección termina dentro
   de su plazo, y si llevó la Tarea a fallida la respuesta es `StateForbids` con ese estado.
3. El destino forma un Par_de_Conversión con el Tipo_Real; si no, `destino_no_disponible` con
   el destino pedido y los Formatos_de_Destino ofrecidos (Req 3.4). La comparación es exacta,
   así que el propio Tipo_Real o un nombre desconocido también se rechazan.
4. Si el par no es Sin_Pérdida, `acknowledge_loss` es verdadero; si no, `ConfirmationRequired`
   y la Tarea sigue en recibida hasta recibir la confirmación (Req 3.3).
5. En un Par_de_Transcripción, el Idioma_de_Transcripción: `LanguageCatalog.resolve` da el
   español si no se eligió (Req 11.10), "auto" o el Idioma_Soportado elegido; cualquier otro
   valor es `idioma_no_soportado` con los nombres de los Idiomas_Soportados (Req 11.9, 13.8).
   Fuera de un Par_de_Transcripción el idioma enviado no se usa y la Tarea queda sin idioma.
   Con el catálogo de idiomas vacío (sin Modelo_de_Transcripción) no hay idiomas que listar:
   ese rechazo no se puede armar y la confirmación lanza `InvalidMessageError` sin cambiar la
   Tarea; el catálogo y los Pares_de_Transcripción se instalan juntos.
6. `preflight` del convertidor del par, con la información del origen que obtuvo el inspector:
   su Rechazo_de_Destino, si lo hay, es un `TargetRejected` (Req 16.12). Si el convertidor no
   está, `preflight` lanza una excepción o devuelve algo que no es un Rechazo_de_Destino válido,
   la Tarea pasa a fallida con `error_interno`, en la etapa conversión o transcripción según el
   par (Req 16.9), y la respuesta es su vista.

Si todo pasa, la Tarea queda asociada al par y al idioma, `ConfirmAccepted` la lleva a en_cola
(Req 14.1) y entra al final de la cola única del planificador con su número de ingreso en
`queued_seq` (Req 19.8). Ningún rechazo modifica la Tarea: el par y el idioma se asocian recién
al final.

Rechazo de la confirmación (`decline`): con la Tarea en recibida y la detección terminada,
devuelve la vista sin cambios, con los mismos Formatos_de_Destino (Req 3.7). En otro estado,
`StateForbids`.

Cancelación (`cancel`): en recibida, en_cola o en_proceso, detiene el trabajo en curso de la
Tarea y espera a que termine, aplica `CancelRequested` con el motivo `cancelada_por_usuario`,
saca la Tarea del planificador y encola el borrado de su Espacio_de_Trabajo (Req 14.6, 17.4). En
recibida el trabajo es la detección y en_proceso es la ejecución: detenerlas detiene el
subproceso en curso (SIGTERM al grupo, hasta 2 segundos y SIGKILL) y su resultado ya no se
registra. Si mientras se detiene la detección el planificador inicia la ejecución de la Tarea,
la cancelación detiene también esa ejecución. En un Estado_Terminal, `StateForbids` (Req 14.11).
Si quien pidió la cancelación se interrumpe mientras se espera el trabajo, la cancelación se
aplica igual.

Planificación (`_dispatch`). Hay una única cola FIFO y un único Límite_de_Concurrencia para todas
las Tareas (Req 19.8). `_dispatch` se llama después de cada cambio que agrega o libera trabajo en
el planificador: la confirmación aceptada, la cancelación y el fin de cada ejecución. En ese
mismo paso del bucle pasa a en_proceso, en orden de ingreso a en_cola, las Tareas que tienen
lugar (Req 14.4, 14.10). Por eso la vista que devuelve una confirmación ya muestra en_proceso si
había lugar. Después de `aclose` no inicia más Tareas.

Ejecución. Al pasar a en_proceso, la Tarea recibe un `ProgressReporter` en 0 %, así la primera
actualización del Porcentaje_de_Avance está disponible de inmediato (Req 14.5), y un trabajo
propio que llama a `converter.run(ctx)` dentro de `asyncio.timeout_at`. El plazo se cuenta desde
el cambio a en_proceso y es el Tiempo_Máximo_de_Procesamiento que le corresponde: el de
transcripción en un Par_de_Transcripción y el de la Categoría del par en los demás casos
(`processing_timeout_seconds`, Req 14.7).

- Si la detección de la Tarea todavía está deteniendo su inspector, el trabajo la espera antes
  de ejecutar el par, así ningún subproceso de la Tarea queda sin seguimiento.
- Al vencer el plazo, `asyncio.timeout_at` cancela el par: el ejecutor envía SIGTERM al grupo del
  subproceso, espera 2 segundos y envía SIGKILL, y la Tarea pasa a fallida con `tiempo_excedido`
  dentro de los 10 segundos de Req 14.7.
- Una `ConversionFailure` lleva la Tarea a fallida con su Motivo_de_Fallo. Si ese motivo es
  `error_interno` y el fallo trae un `ENOSPC` o quedan menos de 1 MB libres, el motivo es
  `sin_espacio_proceso` (Req 16.10). Cualquier otra excepción se clasifica con
  `converters.errors.classify_failure`: `sin_espacio_proceso` por la misma regla y `error_interno`
  en otro caso (Req 16.9). El detalle técnico va al evento `task_failed` (Req 16.7).
- Un par que termina sin dejar su Archivo_Resultado como archivo regular dentro de `work/` falla
  como cualquier otra excepción (`MissingResultError`, Req 16.3).
- Toda excepción queda en el trabajo de su Tarea; las demás siguen y el planificador libera su
  lugar (Req 16.11).
- El trabajo no toma el lock de la Tarea: la cancelación lo tiene mientras lo detiene. Si lo
  cancelan (`cancel` o `aclose`), termina sin aplicar ningún evento, aunque el convertidor
  haya absorbido la cancelación; quien lo canceló decide el estado.

Entrega. Con el par terminado, `os.replace` mueve el resultado de `work/` a `result/output`
(atómico, en el mismo sistema de archivos), el nombre de descarga sale de `naming.result_name`
(termina en ".zip" si `result_kind` es "zip") y recién entonces la Tarea guarda su `ResultInfo`,
el Idioma_Detectado y los avisos del par y pasa a completada. Así nunca hay un Archivo_Resultado
parcial descargable (Req 15.2, 15.10, 16.6). Después se encola la poda de `source/`, `work/` y
`tmp/` (Req 17.2). Un error al mover el resultado es un fallo de la etapa entrega.

Retención. `run_retention_due` hace una pasada y `run_retention` la repite cada 5 segundos:

- cancela, con el lock de la Tarea, las Tareas en recibida cuya antigüedad alcanza el
  Período_de_Retención, con el motivo `sin_destino_en_retencion` (Req 14.8);
- marca como expirados los resultados de las Tareas completadas que alcanzan el
  Período_de_Retención desde `completed_at` y encola el borrado de su Espacio_de_Trabajo
  (Req 15.8, 17.3);
- elimina el registro de las Tareas cuyo Espacio_de_Trabajo se encoló para borrar hace 24 horas.
  Hasta entonces el registro, sin contenido, sigue en memoria para responder "el resultado
  expiró" o mostrar el fallo; después la Tarea no existe (`TaskUnavailable`).

`view` y `result` también marcan como expirado un resultado vencido antes de responder, así la
vista y la descarga no dependen de cuándo corrió la última pasada (Req 15.8). `result` devuelve
el `ResultInfo` de una Tarea completada y vigente; en otro estado lanza `DownloadUnavailable` y,
con el resultado expirado, `ResultExpired` (Req 15.8, 15.11, 16.6).

Los cambios de estado usan solo `state_machine.transition` (Req 14.3). Cada trabajo en segundo
plano captura los errores de su Tarea y no afecta a las demás (Req 16.11). Los métodos no son
seguros entre hilos: se llaman desde el único bucle asyncio del proceso.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import math
import os
import stat
import time
import uuid
from collections.abc import Callable, Coroutine, Mapping
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NoReturn, Protocol

from converter.converters.base import (
    ConversionContext,
    ConversionFailure,
    ConversionOutcome,
    ConversionRequest,
    Converter,
    NoticeCollector,
    UnknownConverterError,
    registered_converters,
)
from converter.converters.errors import classify_failure
from converter.detection.source_info import DetectionResult, SourceRejection
from converter.eventlog import describe_exception
from converter.languages import LanguageCatalog
from converter.messages_es import (
    CancelReason,
    FailureReason,
    RejectionCode,
    RequestError,
    render_error,
)
from converter.models import (
    TERMINAL_STATES,
    Category,
    DestinationRejection,
    Failure,
    Stage,
    TaskState,
)
from converter.naming import result_name
from converter.tasks.model import CompletedUpload, ConfirmRequest, ResultInfo, Task
from converter.tasks.progress import ProgressReporter
from converter.tasks.scheduler import SchedulerCore
from converter.tasks.state_machine import (
    CancelRequested,
    Completed,
    ConfirmAccepted,
    Failed,
    InternalError,
    InvalidTransition,
    RetentionExpired,
    SourceRejected,
    Started,
    TaskEvent,
    TimedOut,
    transition,
)
from converter.tasks.view import TaskView

if TYPE_CHECKING:
    from converter.config import Config
    from converter.eventlog import ErrorDetail, EventLog, ToolDetail
    from converter.registry.model import ConversionPair, FormatDef
    from converter.registry.registry import ConversionRegistry
    from converter.sandbox.runner import SandboxRunner
    from converter.storage.sweeper import Sweeper
    from converter.storage.workspace import Workspace

# Plazo para registrar el Tipo_Real o el Rechazo_de_Origen desde la creación de la Tarea: medio
# segundo menos que los 10 segundos de Req 2.11, para registrar el fallo dentro de ellos
DETECTION_TIMEOUT_SECONDS = 9.5

# Intentos de obtener un identificador no emitido. Con uuid4 la primera vez basta; el límite
# solo corta un generador defectuoso que repite siempre el mismo valor
MAX_ID_ATTEMPTS = 100

RETENTION_INTERVAL_SECONDS = 5.0  # período entre pasadas de la retención
# Tiempo que se conserva el registro de una Tarea, sin contenido, después de encolar el borrado
# de su Espacio_de_Trabajo
RECORD_RETENTION_SECONDS = 24 * 60 * 60

ZIP_MEDIA_TYPE = "application/zip"  # Content-Type de un resultado con `result_kind` "zip"

type FailureEvent = SourceRejected | InternalError | Failed | TimedOut
type DetectionOutcome = DetectionResult | SourceRejection


class Detector(Protocol):
    """Detector_de_Tipo: `detection.detector.TypeDetector` en el Conversor."""

    async def detect(self, workspace: Workspace, original_name: str) -> DetectionOutcome:
        """Tipo_Real del Archivo_de_Origen de `workspace`, o el Rechazo_de_Origen."""
        ...


class TaskUnavailable(LookupError):
    """No hay ninguna Tarea con ese identificador (Req 19.5).

    Es el mismo error para un identificador mal formado, desconocido o de una Tarea que ya no
    existe, y su texto no lo cita.
    """

    def __init__(self) -> None:
        super().__init__("la Tarea no existe o ya no está disponible")


class TaskRequestError(Exception):
    """Solicitud sobre una Tarea que no se atiende; la Tarea queda sin cambios.

    `code` y `params` son los de su mensaje: `messages_es.render_error(code, params)` lo arma.
    El texto de la excepción es solo el código, sin los valores que envió el usuario.
    """

    code: str
    params: Mapping[str, object]

    def __init__(self, code: StrEnum, params: Mapping[str, object] | None = None) -> None:
        super().__init__(code.value)
        self.code = code.value
        self.params = MappingProxyType(dict(params or {}))


class StateForbids(TaskRequestError):
    """La solicitud no se admite en el Estado_de_Tarea actual (Req 14.11)."""

    state: TaskState

    def __init__(self, state: TaskState) -> None:
        super().__init__(RequestError.STATE_FORBIDS, {"estado": state.value})
        self.state = state


class ConfirmationRequired(TaskRequestError):
    """El par pierde información y la confirmación no aceptó la pérdida (Req 3.3)."""

    def __init__(self) -> None:
        super().__init__(RequestError.CONFIRMATION_REQUIRED)


class TargetRejected(TaskRequestError):
    """Rechazo_de_Destino: la Tarea sigue en recibida, sin Motivo_de_Fallo (Req 16.12)."""

    rejection: DestinationRejection

    def __init__(self, rejection: DestinationRejection) -> None:
        """Valida el rechazo con su mensaje. Lanza `TypeError` si no es un
        `DestinationRejection`, `ValueError` si su código no es un `RejectionCode` e
        `InvalidMessageError` si sus parámetros no son los que declara el mensaje."""
        if not isinstance(rejection, DestinationRejection):
            raise TypeError("un Rechazo_de_Destino es un DestinationRejection")
        code = RejectionCode(rejection.code)
        render_error(code, rejection.params)
        super().__init__(code, rejection.params)
        self.rejection = rejection


class DownloadUnavailable(TaskRequestError):
    """La Tarea no está completada: no hay Archivo_Resultado que descargar (Req 15.11, 16.6)."""

    state: TaskState

    def __init__(self, state: TaskState) -> None:
        super().__init__(RequestError.DOWNLOAD_UNAVAILABLE, {"estado": state.value})
        self.state = state


class ResultExpired(TaskRequestError):
    """Venció el Período_de_Retención del Archivo_Resultado (Req 15.8)."""

    def __init__(self) -> None:
        super().__init__(RequestError.RESULT_EXPIRED)


class MissingResultError(RuntimeError):
    """El par terminó sin dejar su Archivo_Resultado como archivo regular en `work/` (Req 16.3)."""


class _ProcessingTimeout(Exception):
    """Venció el Tiempo_Máximo_de_Procesamiento de la Tarea (Req 14.7)."""


def processing_timeout_seconds(pair: ConversionPair, config: Config) -> int:
    """Tiempo_Máximo_de_Procesamiento de una Tarea con `pair`, en segundos (Req 14.7).

    Es el de transcripción si `pair` es un Par_de_Transcripción y el de la Categoría del par en
    los demás casos.
    """
    if pair.is_transcription:
        return config.timeout_transcription_seconds
    by_category = {
        Category.IMAGE: config.timeout_image_seconds,
        Category.DOCUMENT: config.timeout_document_seconds,
        Category.AUDIO: config.timeout_audio_seconds,
        Category.VIDEO: config.timeout_video_seconds,
    }
    return by_category[pair.category]


def storage_free_bytes(path: Path) -> int:
    """Bytes libres, para un proceso sin privilegios, del sistema de archivos de `path`."""
    info = os.statvfs(path)
    return info.f_bavail * info.f_frsize


def _processing_stage(pair: ConversionPair | None) -> Stage:
    """Etapa_de_Procesamiento de un fallo del par: transcripción o conversión."""
    return Stage.TRANSCRIPTION if pair is not None and pair.is_transcription else Stage.CONVERSION


def _failure_event(failure: Failure) -> FailureEvent:
    """Evento que lleva una Tarea en_proceso a fallida con `failure`."""
    if failure.reason is FailureReason.TIMED_OUT:
        return TimedOut(failure)
    if failure.reason is FailureReason.INTERNAL_ERROR:
        return InternalError(failure)
    return Failed(failure)


def _is_result_file(path: object, work_dir: Path) -> bool:
    """`path` es un archivo regular, no un enlace, dentro de `work_dir` (Req 16.3, 17.7)."""
    if not isinstance(path, Path) or not path.is_absolute():
        return False
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    # Con el último componente ya comprobado, `realpath` solo resuelve los directorios: un enlace
    # en el camino que sale de work/ deja el resultado fuera
    real_work = Path(os.path.realpath(work_dir))
    return Path(os.path.realpath(path)).is_relative_to(real_work)


def _raise_if_cancelled() -> None:
    """Relanza la cancelación de la tarea actual si la pidieron, aunque el convertidor la haya
    absorbido: quien cancela decide el estado de la Tarea."""
    current = asyncio.current_task()
    if current is not None and current.cancelling():
        raise asyncio.CancelledError


def _rejection(code: RejectionCode, params: Mapping[str, object]) -> TargetRejected:
    return TargetRejected(DestinationRejection(code=code, params=MappingProxyType(dict(params))))


def _internal_error(exc: BaseException) -> SourceRejection:
    """`error_interno` de la etapa detección con el detalle saneado de `exc` (Req 16.9)."""
    return SourceRejection(FailureReason.INTERNAL_ERROR, detail=describe_exception(exc))


def _outcome(detection: asyncio.Task[DetectionOutcome]) -> DetectionOutcome:
    """Resultado de una detección terminada; sus excepciones son `error_interno`."""
    try:
        outcome = detection.result()
    except Exception as exc:  # no depende del archivo: catálogo, sonda, inspector o E/S
        return _internal_error(exc)
    if not isinstance(outcome, DetectionResult | SourceRejection):
        return _internal_error(TypeError("el Detector_de_Tipo devolvió otro tipo de resultado"))
    return outcome


async def _stop(work: asyncio.Task[Any]) -> None:
    """Cancela `work` y espera a que termine, aunque cancelen también a quien espera.

    Cancelar el detector hace que el ejecutor detenga el subproceso del inspector (SIGTERM, hasta
    2 segundos y SIGKILL); así, cuando termina el trabajo de una Tarea, no queda nada suyo
    corriendo. Si quien espera recibe una cancelación, la propaga después.
    """
    work.cancel()  # no hace nada si ya terminó
    cancelled = False
    while not work.done():
        try:
            await asyncio.wait({work})
        except asyncio.CancelledError:
            cancelled = True
    if not work.cancelled():
        work.exception()  # marca como leída una excepción que llegó después del plazo
    if cancelled:
        raise asyncio.CancelledError


class TaskManager:
    """Gestor_de_Tareas: Tareas en memoria y sus trabajos en segundo plano."""

    __slots__ = (
        "_clock",
        "_closed",
        "_config",
        "_converters",
        "_detection_timeout",
        "_detector",
        "_event_log",
        "_forget_at",
        "_free_bytes",
        "_issued",
        "_jobs",
        "_languages",
        "_new_uuid",
        "_processing_timeout",
        "_queue_seq",
        "_registry",
        "_runner",
        "_scheduler",
        "_sweeper",
        "_tasks",
    )

    def __init__(
        self,
        *,
        config: Config,
        registry: ConversionRegistry,
        detector: Detector,
        runner: SandboxRunner,
        sweeper: Sweeper,
        event_log: EventLog,
        converters: Mapping[str, Converter] | None = None,
        languages: LanguageCatalog | None = None,
        clock: Callable[[], float] = time.monotonic,
        detection_timeout: float = DETECTION_TIMEOUT_SECONDS,
        new_uuid: Callable[[], uuid.UUID] = uuid.uuid4,
        processing_timeout: Callable[[ConversionPair, Config], float] = (
            processing_timeout_seconds
        ),
        free_bytes: Callable[[Path], int] = storage_free_bytes,
    ) -> None:
        """`config` da el Límite_de_Concurrencia, el Período_de_Retención, los
        Tiempos_Máximos_de_Procesamiento y los valores que reciben los convertidores.
        `converters` son los convertidores por identificador; si es None, los registrados en
        `converters.base` al crear el Gestor_de_Tareas. `languages` son los
        Idiomas_Soportados; si es None, ninguno. `clock` da segundos de un reloj monotónico y
        mide la retención; el Tiempo_Máximo_de_Procesamiento se mide con el reloj del bucle.
        `new_uuid` genera los Identificadores_de_Tarea y debe producir UUID versión 4 de un
        generador seguro; se inyecta solo en pruebas, igual que `processing_timeout` (segundos
        de ejecución de un par) y `free_bytes` (espacio libre del sistema de archivos de un
        Espacio_de_Trabajo). Lanza `ValueError` si `detection_timeout` no es positivo."""
        if not (math.isfinite(detection_timeout) and detection_timeout > 0):
            raise ValueError(f"detection_timeout debe ser positivo: {detection_timeout!r}")
        self._config = config
        self._registry = registry
        self._detector = detector
        self._runner = runner  # ejecutor aislado de las conversiones
        self._sweeper = sweeper  # borrados del Almacenamiento_Temporal
        self._event_log = event_log
        self._converters: Mapping[str, Converter] = MappingProxyType(
            dict(registered_converters() if converters is None else converters)
        )
        self._languages = LanguageCatalog() if languages is None else languages
        self._clock = clock
        self._detection_timeout = float(detection_timeout)
        self._new_uuid = new_uuid
        self._processing_timeout = processing_timeout
        self._free_bytes = free_bytes
        # Sin Tareas al iniciar (Req 14.13)
        self._tasks: dict[str, Task] = {}
        self._issued: set[str] = set()  # identificadores emitidos desde el inicio (Req 19.2)
        self._jobs: dict[str, asyncio.Task[None]] = {}  # trabajo en curso de cada Tarea
        # Cola FIFO única y Tareas en_proceso, con un único Límite_de_Concurrencia (Req 19.8)
        self._scheduler = SchedulerCore(limit=config.concurrency)
        self._queue_seq = itertools.count(1)  # números de ingreso a en_cola
        # Instante del reloj en que se elimina el registro de cada Tarea cuyo
        # Espacio_de_Trabajo ya se encoló para borrar
        self._forget_at: dict[str, float] = {}
        self._closed = False  # `aclose` empezó: no se inician más Tareas

    def __len__(self) -> int:
        """Cantidad de Tareas en memoria; no se ofrece fuera del proceso (Req 19.6)."""
        return len(self._tasks)

    # Operaciones

    async def create(self, upload: CompletedUpload) -> Task:
        """Crea la Tarea de una recepción terminada y lanza su detección (Req 1.1, 19.2).

        Devuelve la Tarea en recibida sin esperar la detección.
        """
        task = Task(
            id=self._issue_id(),
            workspace=upload.workspace,
            original_name=upload.original_name,
            size_bytes=upload.size_bytes,
            created_at=self._clock(),
        )
        self._tasks[task.id] = task
        job = self._start_job(task, self._detect(task))
        # `_detect` activa el evento al terminar; esto cubre un trabajo cancelado antes de
        # empezar, que nunca ejecuta su `finally`, para que ninguna confirmación espere de más
        job.add_done_callback(lambda _: task.detection_done.set())
        return task

    async def confirm(self, task_id: str, request: ConfirmRequest) -> TaskView:
        """Confirma el Formato_de_Destino de la Tarea `task_id` (Req 3.3, 3.4, 11.9, 11.10,
        14.1, 16.12, 19.11).

        Devuelve la vista de la Tarea en_cola, en_proceso si el planificador tenía lugar, o
        fallida si el `preflight` falló. Lanza `TaskUnavailable`, `StateForbids`,
        `ConfirmationRequired` o `TargetRejected` sin cambiar la Tarea.
        """
        task = self._get(task_id)
        async with task.lock:
            detection = await self._detected_while_received(task)
            pairs = self._registry.targets_for(detection.real_type)
            pair = next((p for p in pairs if p.target == request.target), None)
            if pair is None:
                raise _rejection(
                    RejectionCode.TARGET_UNAVAILABLE,
                    {
                        "destino": request.target,
                        "tipo_real": detection.real_type,
                        "destinos": [p.target for p in pairs],
                    },
                )
            if pair.requires_confirmation and not request.acknowledge_loss:
                raise ConfirmationRequired()
            language = self._transcription_language(pair, request.language)
            conversion = ConversionRequest(target=pair.target, language=language)
            try:
                rejected = self._preflight(pair, detection, conversion)
            except Exception as exc:  # error del convertidor, no del pedido (Req 16.9)
                task.pair, task.language = pair, language  # el Log_de_Eventos registra el par
                stage = Stage.TRANSCRIPTION if pair.is_transcription else Stage.CONVERSION
                failure = Failure(FailureReason.INTERNAL_ERROR, stage, MappingProxyType({}))
                self._fail(task, InternalError(failure), detail=describe_exception(exc))
                return self._view(task)
            if rejected is not None:
                raise rejected
            task.pair, task.language = pair, language
            self._apply(task, ConfirmAccepted())
            task.queued_seq = next(self._queue_seq)
            self._scheduler.enqueue(task.id)
            self._dispatch()
            return self._view(task)

    async def decline(self, task_id: str, target: str) -> TaskView:
        """Rechazo de la confirmación de pérdida del destino `target` (Req 3.7).

        La Tarea sigue en recibida, sin cambios, y la vista trae los mismos
        Formatos_de_Destino; `target` no cambia la respuesta. Lanza `TaskUnavailable` o
        `StateForbids`.
        """
        task = self._get(task_id)
        async with task.lock:
            await self._detected_while_received(task)
            return self._view(task)

    async def cancel(self, task_id: str) -> TaskView:
        """Cancela la Tarea `task_id` en recibida, en_cola o en_proceso (Req 14.6, 14.11).

        Detiene el trabajo en curso de la Tarea y espera a que termine antes de pasarla a
        cancelada. Lanza `TaskUnavailable`, o `StateForbids` en un Estado_Terminal.
        """
        task = self._get(task_id)
        async with task.lock:
            if task.state in TERMINAL_STATES:
                raise StateForbids(task.state)
            try:
                await self._stop_jobs(task)  # la detección o, en_proceso, la ejecución
            finally:
                # También si interrumpen esta solicitud: el trabajo ya se detuvo
                cancelled = self._apply(task, CancelRequested())
                if cancelled:
                    self._abandon(task)
            if not cancelled:  # el trabajo llevó la Tarea a un Estado_Terminal al detenerse
                raise StateForbids(task.state)
            return self._view(task)

    def view(self, task_id: str) -> TaskView:
        """Vista de la Tarea `task_id` (Req 3.1, 3.2, 3.5, 14.2, 14.5, 15.8, 19.6).

        Un resultado cuyo Período_de_Retención venció se marca como expirado antes de armarla.
        Lanza `TaskUnavailable` si no hay una Tarea con ese identificador.
        """
        task = self._get(task_id)
        self._expire_result_if_due(task, self._clock())
        return self._view(task)

    def result(self, task_id: str) -> ResultInfo:
        """Archivo_Resultado de la Tarea completada `task_id`, para descargarlo (Req 15.8, 15.9,
        15.11, 16.6).

        Lanza `TaskUnavailable`, `DownloadUnavailable` con el estado actual si la Tarea no está
        completada, o `ResultExpired` si venció su Período_de_Retención.
        """
        task = self._get(task_id)
        self._expire_result_if_due(task, self._clock())
        if task.state is not TaskState.COMPLETED or task.result is None:
            raise DownloadUnavailable(task.state)
        if task.result.expired:
            raise ResultExpired()
        return task.result

    async def run_retention_due(self) -> None:
        """Hace una pasada de la retención (Req 14.8, 15.8, 17.3).

        Cancela las Tareas en recibida que alcanzaron el Período_de_Retención desde su creación,
        marca como expirados los resultados que lo alcanzaron desde `completed_at` y elimina
        los registros que cumplieron 24 horas desde que se encoló el borrado de su
        Espacio_de_Trabajo. Las cancelaciones esperan el lock de su Tarea; mientras tanto, una
        confirmación en curso puede sacarla de recibida, y entonces la Tarea no se cancela.
        """
        now = self._clock()
        retention = self._config.retention_seconds
        expired: list[Task] = []
        for task in tuple(self._tasks.values()):
            if task.state is TaskState.RECEIVED and now - task.created_at >= retention:
                expired.append(task)
            else:
                self._expire_result_if_due(task, now)
        for task_id, forget_at in tuple(self._forget_at.items()):
            if now >= forget_at:
                del self._forget_at[task_id]
                self._tasks.pop(task_id, None)  # el identificador sigue emitido (Req 19.2)
        await asyncio.gather(*(self._expire_received(task) for task in expired))

    async def run_retention(self) -> NoReturn:
        """Hace una pasada de la retención cada 5 segundos hasta que se cancela."""
        while True:
            await self.run_retention_due()
            await asyncio.sleep(RETENTION_INTERVAL_SECONDS)

    async def aclose(self) -> None:
        """Interrumpe los trabajos en curso y espera a que terminen, al apagar el Conversor.

        Las Tareas en_proceso quedan como estaban y no se inician otras.
        """
        self._closed = True
        jobs = tuple(self._jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    # Tareas e identificadores

    def _get(self, task_id: str) -> Task:
        task = self._tasks.get(task_id) if isinstance(task_id, str) else None
        if task is None:
            raise TaskUnavailable()
        return task

    def _issue_id(self) -> str:
        for _ in range(MAX_ID_ATTEMPTS):
            candidate = str(self._new_uuid())
            if candidate not in self._issued:
                self._issued.add(candidate)
                return candidate
        raise RuntimeError("el generador de Identificadores_de_Tarea repite sus valores")

    def _start_job(self, task: Task, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Ejecuta `work` en segundo plano como el trabajo en curso de `task` y lo devuelve."""
        job = asyncio.create_task(work)
        self._jobs[task.id] = job

        def forget(finished: asyncio.Task[None]) -> None:
            if self._jobs.get(task.id) is finished:
                del self._jobs[task.id]

        job.add_done_callback(forget)
        return job

    def _view(self, task: Task) -> TaskView:
        targets = (
            self._registry.targets_for(task.detection.real_type)
            if task.detection is not None
            else ()
        )
        return TaskView.from_task(task, targets)

    # Validaciones de las solicitudes

    async def _detected_while_received(self, task: Task) -> DetectionResult:
        """Resultado de la detección de `task`, que debe estar en recibida antes y después.

        Si la detección sigue en curso, la espera: termina dentro de su plazo. Lanza
        `StateForbids` con el estado actual si la Tarea no está en recibida, también si la
        detección la llevó a fallida mientras se esperaba.
        """
        if task.state is not TaskState.RECEIVED:
            raise StateForbids(task.state)
        await task.detection_done.wait()
        if task.state is not TaskState.RECEIVED:
            raise StateForbids(task.state)
        if task.detection is None:  # solo si se interrumpió al apagar el Conversor
            raise RuntimeError("la detección de la Tarea se interrumpió")
        return task.detection

    def _transcription_language(self, pair: ConversionPair, requested: str | None) -> str | None:
        """Idioma_de_Transcripción de la Tarea, o None si el par no es de transcripción.

        Lanza `TargetRejected` con `idioma_no_soportado` si `requested` no es None, "auto" ni
        un Idioma_Soportado (Req 11.9, 11.10, 13.8).
        """
        if not pair.is_transcription:
            return None
        language = self._languages.resolve(requested)
        if language is None:
            raise _rejection(
                RejectionCode.UNSUPPORTED_LANGUAGE,
                {"valor": requested, "idiomas": list(self._languages.names())},
            )
        return language

    def _preflight(
        self, pair: ConversionPair, detection: DetectionResult, request: ConversionRequest
    ) -> TargetRejected | None:
        """Rechazo_de_Destino del convertidor del par, o None.

        Lanza `UnknownConverterError` si el convertidor no está y las excepciones de su
        `preflight`, y también si lo que devuelve no es un Rechazo_de_Destino válido.
        """
        converter = self._converters.get(pair.converter_id)
        if converter is None:
            raise UnknownConverterError(pair.converter_id)
        rejection = converter.preflight(detection.info, request, self._config)
        return None if rejection is None else TargetRejected(rejection)

    # Planificación

    def _dispatch(self) -> None:
        """Pasa a en_proceso, en orden de ingreso, las Tareas en_cola que tienen lugar
        (Req 14.4, 14.10); se llama después de cada cambio que agrega o libera trabajo."""
        if self._closed:
            return
        while started := self._scheduler.dispatch():
            for task_id in started:
                self._start(task_id)

    def _start(self, task_id: str) -> None:
        """Aplica `Started` a la Tarea que despachó el planificador y lanza su ejecución.

        Si no puede iniciarla, la saca del planificador para que el lugar quede libre.
        """
        task = self._tasks.get(task_id)
        if task is None or not self._apply(task, Started()):
            self._scheduler.remove(task_id)
            return
        task.started_at = self._clock()
        task.progress = ProgressReporter()  # 0 % desde el cambio a en_proceso (Req 14.5)
        started = asyncio.get_running_loop().time()  # el plazo se cuenta desde aquí (Req 14.7)
        # La detección puede seguir deteniendo su inspector: la ejecución la espera
        previous = self._jobs.get(task.id)
        self._start_job(task, self._execute(task, previous, started))

    def _release(self, task: Task) -> None:
        """Libera el lugar de una Tarea que terminó su ejecución y despacha la siguiente."""
        self._scheduler.remove(task.id)
        self._dispatch()

    async def _stop_jobs(self, task: Task) -> None:
        """Detiene el trabajo en curso de `task` y espera a que termine.

        Mientras se detiene la detección, el planificador puede iniciar la ejecución de la
        Tarea: si aparece otro trabajo, también se detiene.
        """
        job = self._jobs.get(task.id)
        while job is not None:
            await _stop(job)
            following = self._jobs.get(task.id)
            job = following if following is not job else None

    def _abandon(self, task: Task) -> None:
        """Lo que sigue a cancelar `task`: libera su lugar y borra su Espacio_de_Trabajo."""
        leftover = self._jobs.get(task.id)
        if leftover is not None:
            leftover.cancel()  # solo si se interrumpió la espera de `_stop_jobs`
        self._discard_workspace(task)
        self._release(task)

    # Ejecución y entrega

    async def _execute(
        self, task: Task, previous: asyncio.Task[None] | None, started: float
    ) -> None:
        """Trabajo de una Tarea en_proceso: ejecuta su par y entrega el resultado.

        `started` es el instante del reloj del bucle en que pasó a en_proceso. No toma el lock
        de la Tarea. Si lo cancelan, termina sin aplicar ningún evento.
        """
        if previous is not None:
            await _stop(previous)
        if task.state is not TaskState.PROCESSING:
            return  # se canceló mientras se esperaba la detección
        notices = NoticeCollector()
        cancelled = False
        try:
            try:
                outcome = await self._convert(task, notices, started)
            except Exception as exc:  # Req 16.11: el fallo afecta solo a esta Tarea
                self._fail_execution(task, exc, _processing_stage(task.pair))
            else:
                try:
                    self._deliver(task, outcome, notices)
                except Exception as exc:
                    self._fail_execution(task, exc, Stage.DELIVERY)
        except asyncio.CancelledError:
            cancelled = True  # quien canceló libera el lugar (`cancel`) o apaga (`aclose`)
            raise
        finally:
            if not cancelled:
                self._release(task)

    async def _convert(
        self, task: Task, notices: NoticeCollector, started: float
    ) -> ConversionOutcome:
        """Ejecuta el par de `task` dentro de su Tiempo_Máximo_de_Procesamiento.

        Devuelve el resultado, que está en `work/`. Lanza `_ProcessingTimeout` si venció el
        plazo, `MissingResultError` si el par no dejó su Archivo_Resultado, las excepciones del
        convertidor y `asyncio.CancelledError` si cancelaron la ejecución.
        """
        pair, detection, progress = task.pair, task.detection, task.progress
        if pair is None or detection is None or progress is None:
            raise RuntimeError("la Tarea en_proceso no tiene par, detección o progreso")
        converter = self._converters.get(pair.converter_id)
        if converter is None:
            raise UnknownConverterError(pair.converter_id)
        ctx = ConversionContext(
            task_id=task.id,
            pair=pair,
            request=ConversionRequest(target=pair.target, language=task.language),
            source=detection.info,
            workspace=task.workspace,
            config=self._config,
            runner=self._runner,
            progress=progress,
            notices=notices,
        )
        # Al vencer, cancela el par: el ejecutor detiene su subproceso con SIGTERM y SIGKILL
        scope = asyncio.timeout_at(started + self._processing_timeout(pair, self._config))
        try:
            async with scope:
                outcome = await converter.run(ctx)
        except Exception as exc:
            _raise_if_cancelled()
            if scope.expired():
                raise _ProcessingTimeout from exc
            raise
        _raise_if_cancelled()
        if scope.expired():  # el convertidor absorbió la cancelación del plazo
            raise _ProcessingTimeout
        if not isinstance(outcome, ConversionOutcome):
            raise TypeError("el convertidor devolvió otro tipo de resultado")
        if not _is_result_file(outcome.result_path, task.workspace.work_dir):
            raise MissingResultError("el par no dejó su Archivo_Resultado en work/")
        return outcome

    def _deliver(self, task: Task, outcome: ConversionOutcome, notices: NoticeCollector) -> None:
        """Mueve el resultado a `result/output` y recién entonces aplica `Completed` (Req 15.2,
        15.10, 16.6); después encola la poda del Espacio_de_Trabajo (Req 17.2)."""
        pair = task.pair
        if pair is None or task.state is not TaskState.PROCESSING:
            raise RuntimeError("solo se entrega el resultado de una Tarea en_proceso con su par")
        target = self._format(pair.target)
        download_name = result_name(task.original_name, target, outcome.result_kind)
        media_type = ZIP_MEDIA_TYPE if outcome.result_kind == "zip" else target.mime_type
        destination = task.workspace.result_file
        os.replace(outcome.result_path, destination)  # atómico: nunca hay un resultado parcial
        size = os.lstat(destination).st_size
        task.result = ResultInfo(
            path=destination,
            download_name=download_name,
            media_type=media_type,
            size_bytes=size,
            completed_at=self._clock(),
        )
        task.detected_language = outcome.detected_language
        task.notices.extend(notices.notices)
        self._apply(task, Completed())
        self._sweeper.enqueue_prune(task.workspace, task_id=task.id)

    def _format(self, name: str) -> FormatDef:
        """Formato registrado con `name`. Lanza `LookupError` si no está."""
        for fmt in self._registry.formats():
            if fmt.name == name:
                return fmt
        raise LookupError("el Formato_de_Destino del par no está registrado")

    def _fail_execution(self, task: Task, exc: Exception, stage: Stage) -> None:
        """Lleva a fallida una Tarea en_proceso según la excepción de su ejecución (Req 14.7,
        16.3, 16.9, 16.10)."""
        detail: ToolDetail | ErrorDetail | None
        if isinstance(exc, _ProcessingTimeout):
            failure = Failure(FailureReason.TIMED_OUT, stage, MappingProxyType({}))
            detail = describe_exception(TimeoutError())
        elif isinstance(exc, ConversionFailure):
            failure, detail = exc.failure, exc.detail
            if failure.reason is FailureReason.INTERNAL_ERROR and self._out_of_space(task, exc):
                failure = Failure(FailureReason.NO_SPACE, failure.stage, MappingProxyType({}))
        else:
            reason = classify_failure(exception=exc, free_bytes=self._measure_free(task))
            failure = Failure(reason, stage, MappingProxyType({}))
            detail = describe_exception(exc)
        self._fail(task, _failure_event(failure), detail=detail)

    def _out_of_space(self, task: Task, exc: Exception) -> bool:
        """El fallo trae un `ENOSPC` o quedan menos de 1 MB libres (Req 16.10)."""
        reason = classify_failure(exception=exc, free_bytes=self._measure_free(task))
        return reason is FailureReason.NO_SPACE

    def _measure_free(self, task: Task) -> int | None:
        """Espacio libre del Almacenamiento_Temporal, o None si no se puede medir."""
        try:
            return self._free_bytes(task.workspace.path)
        except OSError:
            return None

    # Retención

    def _expire_result_if_due(self, task: Task, now: float) -> None:
        """Marca como expirado el resultado de `task` si alcanzó el Período_de_Retención desde
        que se completó, y encola el borrado de su Espacio_de_Trabajo (Req 15.8, 17.3)."""
        result = task.result
        if (
            task.state is not TaskState.COMPLETED
            or result is None
            or result.expired
            or now - result.completed_at < self._config.retention_seconds
        ):
            return
        task.result = dataclasses.replace(result, expired=True)
        task.version += 1
        self._discard_workspace(task)

    async def _expire_received(self, task: Task) -> None:
        """Cancela `task` por no elegir un destino en el Período_de_Retención (Req 14.8)."""
        async with task.lock:
            if task.state is not TaskState.RECEIVED:
                return  # una solicitud la sacó de recibida mientras se esperaba el lock
            try:
                await self._stop_jobs(task)
            finally:
                if self._apply(task, RetentionExpired()):
                    self._abandon(task)

    def _discard_workspace(self, task: Task) -> None:
        """Encola el borrado del Espacio_de_Trabajo de `task` y programa el olvido de su
        registro, que sigue sin contenido durante 24 horas (Req 17.3, 17.4)."""
        self._sweeper.enqueue_remove(task.workspace, task_id=task.id)
        self._forget_at[task.id] = self._clock() + RECORD_RETENTION_SECONDS

    # Detección

    async def _call_detector(self, task: Task) -> DetectionOutcome:
        # Dentro de su propia corrutina, así toda excepción del detector queda en su resultado
        return await self._detector.detect(task.workspace, task.original_name)

    async def _detect(self, task: Task) -> None:
        detection = asyncio.create_task(self._call_detector(task))
        try:
            done, _ = await asyncio.wait({detection}, timeout=self._detection_timeout)
            if detection in done:
                self._finish_detection(task, _outcome(detection))
            else:
                # Se registra antes de detener el detector, para cumplir el plazo (Req 2.11)
                self._finish_detection(task, _internal_error(TimeoutError()))
        finally:
            task.detection_done.set()
            await _stop(detection)

    def _finish_detection(self, task: Task, outcome: DetectionOutcome) -> None:
        """Registra el resultado de la detección de `task`, si sigue en recibida."""
        if task.state is not TaskState.RECEIVED:
            return  # la Tarea cambió mientras se detectaba; el resultado ya no cuenta
        task.real_type = outcome.real_type
        if isinstance(outcome, SourceRejection):
            failure = outcome.failure
            event: FailureEvent = (
                InternalError(failure)
                if failure.reason is FailureReason.INTERNAL_ERROR
                else SourceRejected(failure)
            )
            self._fail(task, event, detail=outcome.detail)
            return
        task.detection = outcome
        task.notices.extend(outcome.notices)
        task.version += 1

    # Cambios de estado

    def _apply(self, task: Task, event: TaskEvent) -> bool:
        """Aplica `event` con la máquina de estados; False si no se admite en el estado actual.

        Un evento de fallo registra su Motivo_de_Fallo en la Tarea (Req 16.1) y uno de
        cancelación, su motivo de cancelación (Req 14.8).
        """
        new_state = transition(task.state, event)
        if isinstance(new_state, InvalidTransition):
            return False
        task.state = new_state
        if isinstance(event, SourceRejected | InternalError | Failed | TimedOut):
            task.failure = event.failure
        elif isinstance(event, CancelRequested):
            task.cancel_reason = CancelReason.BY_USER
        elif isinstance(event, RetentionExpired):
            task.cancel_reason = CancelReason.RETENTION_EXPIRED
        task.version += 1
        return True

    def _fail(
        self, task: Task, event: FailureEvent, *, detail: ToolDetail | ErrorDetail | None
    ) -> None:
        """Lleva `task` a fallida, registra `task_failed` y encola el borrado de su espacio."""
        if not self._apply(task, event):
            return
        failure = event.failure
        self._event_log.task_failed(
            task_id=task.id,
            stage=failure.stage,
            reason=failure.reason,
            real_type=task.real_type,
            pair=None if task.pair is None else (task.pair.source, task.pair.target),
            detail=detail,
        )
        self._discard_workspace(task)
