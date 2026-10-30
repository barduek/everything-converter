"""Convertidor HTML a MD, `text.markdown` (Req 6.5, 6.6, 6.7, 6.13, 6.14).

Lanza `python -m converter.workers.text_markdown` (ver `workers/text_markdown.py`) a través del
Ejecutor aislado y escribe el Markdown en `work/output.md`. El worker decodifica el HTML con la
codificación que detectó el Detector_de_Tipo (`TextInfo.encoding`), sanea el DOM con
`sanitize_dom`, convierte el `body` con `markdownify` y escribe UTF-8 sin BOM_UTF8. Este módulo
decide qué hacer con lo que informa:

- **Recursos_Externos omitidos** (Req 6.6): el worker no lee ni descarga ningún recurso y
  cuenta los que neutralizó. Si hay alguno, la Tarea se completa con el aviso
  `recursos_externos_omitidos` y esa cantidad.
- **Scripts** (Req 6.7, 6.13): `sanitize_dom` quita los elementos `script`, los atributos `on*`
  y los destinos `javascript:`. Un enlace a un Script queda como texto, sin destino. No se
  informan en un aviso: no hay una plantilla para ellos.
- **Fallos del worker.** Con un código de salida distinto de 0, sin resultado o con una salida
  que no sigue su protocolo, la Tarea falla con `error_interno`. El Gestor_de_Tareas lo cambia
  por `sin_espacio_proceso` si la falla vino de la falta de espacio (Req 16.10). La salida de
  error del worker no se registra.

HTML a MD no tiene Rechazos_de_Destino propios (Req 16.12). Como `text.markdown` solo convierte
HTML, el convertidor necesita la información de un Formato_de_Texto (`TextInfo`), que es la que
entrega el inspector `text`.

MVP: lo que queda para después
------------------------------

El MVP usa `markdownify` con títulos ATX, viñetas `-` y enlaces sin autoenlace, más el saneamiento
del DOM, el aviso de Recursos_Externos y la salida en UTF-8. El diseño ("HTML a MD") pide una
subclase de `markdownify.MarkdownConverter` con más reglas, que todavía no están. Por eso este
convertidor **no garantiza** que el Markdown conserve la Estructura_de_Documento (Req 6.5) ni la
ida y vuelta MD→HTML→MD→HTML (Req 6.10); las pruebas de propiedad de las tareas 17.12 y 17.13
fijarán las garantías reales. Falta:

- **Escapes con barra invertida.** `markdownify` solo escapa `*` y `_`. No se escapan los demás
  signos de puntuación ASCII que pueden abrir o cerrar construcciones en línea (`\\`, `` ` ``,
  `[`, `]`, `<`, `>`, `&`, `~`, `|`...) ni los marcadores de bloque al inicio de línea (`#`, `-`,
  `+`, `>`, `N.` y `N)`), así que un texto como «# no es un título» se lee como marcado.
- **Cercas de código a medida.** Un bloque `pre` siempre se cerca con tres acentos graves. Si su
  contenido tiene una línea con tres o más, la cerca se cierra antes de tiempo. El diseño pide una
  cerca más larga que la secuencia más larga de acentos graves del contenido (mínimo 3).
- **Sangría según el ancho del marcador.** La hace `markdownify` al sangrar el contenido de cada
  elemento de lista con el ancho de su viñeta o número. No se comprobó en todas las combinaciones,
  como bloques de código o citas dentro de un elemento de lista.
- **`br` como barra invertida y salto de línea.** `markdownify` escribe dos espacios y un salto.
  Una barra invertida al final de un párrafo (`<p>texto<br></p>`) no es un salto de línea en
  CommonMark, así que el cambio necesita una regla propia que quite el último `br`.
- **Elementos sin equivalente en CommonMark.** `markdownify` escribe tablas con barras verticales
  y `~~tachado~~`, que CommonMark muestra como texto.
- **Anidamiento.** `markdownify` recorre el árbol con recursión: un HTML anidado a más de unos
  cientos de niveles agota la recursión de Python y la Tarea falla con `error_interno`.

Importar este módulo no carga `markdownify`: el worker la importa recién al convertir, así que el
proceso principal no paga por ella.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from converter.converters.base import (
    ConversionContext,
    ConversionFailure,
    ConversionOutcome,
    ConversionRequest,
    register_converter,
)
from converter.detection.source_info import SourceInfo, TextInfo
from converter.eventlog import describe_exception
from converter.messages_es import FailureReason, NoticeCode
from converter.models import DestinationRejection
from converter.sandbox.limits import SandboxTool, tool_limits
from converter.sandbox.runner import minimal_env, python_module_command
from converter.workers import text_markdown as worker
from converter.workers.text_markdown import DoneEvent, WorkerOutputError

if TYPE_CHECKING:
    from converter.config import Config

CONVERTER_ID = "text.markdown"
OUTPUT_NAME = "output.md"  # en work/


class EventCollector:
    """Interpreta la salida estándar del worker, línea por línea.

    `feed` es el callback de líneas del Ejecutor aislado y nunca lanza excepciones: un error del
    protocolo queda guardado y `result` lo lanza.
    """

    __slots__ = ("_done", "_error")

    def __init__(self) -> None:
        self._done: DoneEvent | None = None
        self._error: WorkerOutputError | None = None

    def feed(self, line: str) -> None:
        """Procesa una línea de la salida del worker; el primer error invalida el resto."""
        if self._error is not None:
            return
        try:
            event = worker.load_event(line)
        except WorkerOutputError as exc:
            self._error = exc
            return
        if self._done is not None:
            self._error = WorkerOutputError("hay dos eventos finales")
            return
        self._done = event

    def result(self) -> DoneEvent:
        """Evento final. Lanza `WorkerOutputError` si la salida no siguió el protocolo o terminó
        sin el evento final."""
        if self._error is not None:
            raise self._error
        if self._done is None:
            raise WorkerOutputError("falta el evento final")
        return self._done


def _text_info(source: SourceInfo) -> TextInfo:
    """`source` como información de un Formato_de_Texto. Lanza `TypeError` si es de otro Formato."""
    if not isinstance(source, TextInfo):
        raise TypeError(f"text.markdown necesita la información de un texto: {type(source)}")
    return source


@register_converter(CONVERTER_ID)
class HtmlToMarkdownConverter:
    """HTML a MD: Markdown en UTF-8 del `body`, sin Scripts ni Recursos_Externos."""

    def preflight(
        self, source: SourceInfo, request: ConversionRequest, config: Config
    ) -> DestinationRejection | None:
        """HTML a MD no tiene Rechazos_de_Destino propios (Req 16.12)."""
        return None

    async def run(self, ctx: ConversionContext) -> ConversionOutcome:
        """Convierte el HTML en un subproceso y deja el Markdown en `work/output.md`.

        Agrega el aviso `recursos_externos_omitidos` si el HTML referencia Recursos_Externos.
        Lanza `TypeError` si `ctx.source` no es la información de un texto y `ConversionFailure`
        con `error_interno` si el worker falla.
        """
        info = _text_info(ctx.source)
        workspace = ctx.workspace
        output = (workspace.work_dir / OUTPUT_NAME).absolute()
        args = worker.WorkerArgs(
            encoding=info.encoding, source=workspace.source_file.absolute(), output=output
        )
        limits = tool_limits(
            SandboxTool.PYTHON, timeout_seconds=ctx.config.timeout_document_seconds
        )
        collector = EventCollector()

        completed = await ctx.runner.run(
            python_module_command(worker.MODULE, *worker.format_args(args)),
            cwd=workspace.tmp_dir,
            limits=limits,
            env=minimal_env(workspace.tmp_dir),
            on_stdout_line=collector.feed,
        )
        if completed.returncode != 0 or not output.is_file():
            raise ConversionFailure(FailureReason.INTERNAL_ERROR)
        try:
            done = collector.result()
        except WorkerOutputError as exc:
            raise ConversionFailure(
                FailureReason.INTERNAL_ERROR, detail=describe_exception(exc)
            ) from exc

        if done.external_resources:
            ctx.notices.add(
                NoticeCode.EXTERNAL_RESOURCES_SKIPPED, {"cantidad": done.external_resources}
            )
        ctx.progress.report(1.0)
        return ConversionOutcome(result_path=output, result_kind="single")
