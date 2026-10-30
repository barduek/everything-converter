"""Convertidor `text.plain`: MD a TXT y HTML a TXT (Req 6.4, 6.6, 6.7, 6.13, 6.14).

Lanza `python -m converter.workers.text_plain` (ver `workers/text_plain.py`) a través del
Ejecutor aislado y deja el texto en `work/output.txt`. El worker parte del Texto_Decodificado del
origen, pasa un MD a HTML con CommonMark, sanea el DOM y escribe su Texto_Visible en UTF-8 sin
BOM_UTF8, con los elementos de lista marcados con `- ` o `N. `. Este módulo decide qué hacer con
lo que informa:

- **Codificación.** Usa la que clasificó el Detector_de_Tipo (`TextInfo.encoding`), no la que
  declare el documento (Req 2.12). Un origen en Windows-1252 se escribe en UTF-8 (Req 6.14).
- **Recursos_Externos** (Req 6.6). Si el origen es HTML y el DOM referenciaba alguno, el aviso
  `recursos_externos_omitidos` informa cuántos se omitieron sin leerlos ni descargarlos. Un MD a
  TXT no lleva ese aviso: el criterio 6.6 lo pide para un MD solo cuando el destino es PDF.
- **Scripts** (Req 6.7, 6.13). El saneamiento los quita antes de extraer el texto, así que su
  código no llega al resultado y no se ejecuta. No hay aviso.
- **Fallos del worker.** Con un código de salida distinto de 0, la salida de error se sanea con
  `sanitize_tool_output` y se clasifica con `classify_failure`: la falta de espacio da
  `sin_espacio_proceso` y cualquier otro fallo, `error_interno`. El texto de un Formato_de_Texto
  no puede estar dañado, porque el Detector_de_Tipo ya lo clasificó como Texto_Válido. También es
  `error_interno` una salida del worker que no sigue su protocolo. El detalle técnico saneado va
  en `ConversionFailure.detail`.

Para sanear la salida del worker se usa el conjunto de patrones de `Tool.PILLOW`, el de un
subproceso Python: `eventlog.Tool` no tiene una entrada propia para este worker y los patrones
que importan (falta de espacio, límites de recursos y mensajes del lanzador) son comunes a todas.

No importa BeautifulSoup ni markdown-it-py: solo las carga el worker.
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
from converter.converters.errors import classify_failure
from converter.detection.source_info import SourceInfo, TextInfo
from converter.eventlog import Tool, describe_exception, sanitize_tool_output
from converter.messages_es import FailureReason, NoticeCode
from converter.models import DestinationRejection
from converter.sandbox.limits import SandboxTool, tool_limits
from converter.sandbox.runner import CompletedRun, minimal_env, python_module_command
from converter.workers import text_plain as worker

if TYPE_CHECKING:
    from converter.config import Config

CONVERTER_ID = "text.plain"
OUTPUT_NAME = "output.txt"  # en work/

TARGET_FORMAT = "TXT"
HTML_FORMAT = "HTML"  # el aviso de Recursos_Externos corresponde a este origen (Req 6.6)

# Motivos que el worker puede dar en la etapa de conversión
_WORKER_REASONS = frozenset({FailureReason.NO_SPACE})


def _text_info(source: SourceInfo) -> TextInfo:
    """`source` como información de un Formato_de_Texto. Lanza `TypeError` si es de otro tipo."""
    if not isinstance(source, TextInfo):
        raise TypeError(f"text.plain necesita la información de un texto: {type(source)}")
    return source


def worker_failure(completed: CompletedRun) -> ConversionFailure:
    """Fallo de un worker que terminó con un código distinto de 0 o por una señal."""
    detail = sanitize_tool_output(
        Tool.PILLOW, completed.stderr_lines, returncode=completed.returncode
    )
    reason = classify_failure(detail=detail)
    if reason not in _WORKER_REASONS:
        reason = FailureReason.INTERNAL_ERROR
    return ConversionFailure(reason, detail=detail)


def read_done_event(lines: list[str]) -> worker.DoneEvent:
    """Evento final del worker a partir de las líneas de su salida estándar.

    Lanza `WorkerOutputError` si no hay exactamente una línea o no es un evento válido.
    """
    if len(lines) != 1:
        raise worker.WorkerOutputError(f"se esperaba un evento y hay {len(lines)} líneas")
    return worker.load_event(lines[0])


@register_converter(CONVERTER_ID)
class PlainTextConverter:
    """MD y HTML a TXT: el Texto_Visible del documento saneado, en UTF-8 sin BOM_UTF8."""

    def preflight(
        self, source: SourceInfo, request: ConversionRequest, config: Config
    ) -> DestinationRejection | None:
        """MD a TXT y HTML a TXT no tienen Rechazos_de_Destino propios (Req 16.12)."""
        return None

    async def run(self, ctx: ConversionContext) -> ConversionOutcome:
        """Convierte el documento en un subproceso y lo deja en `work/output.txt`.

        Lanza `ConversionFailure` con el motivo que corresponda si el worker falla y `TypeError`
        si `ctx.source` no es la información de un Formato_de_Texto.
        """
        if ctx.pair.source not in worker.SOURCE_FORMATS or ctx.pair.target != TARGET_FORMAT:
            raise ValueError(f"text.plain no convierte {ctx.pair.source} a {ctx.pair.target}")
        info = _text_info(ctx.source)
        workspace = ctx.workspace
        output = (workspace.work_dir / OUTPUT_NAME).absolute()
        args = worker.WorkerArgs(
            source_format=ctx.pair.source,
            encoding=info.encoding,
            source=workspace.source_file.absolute(),
            output=output,
        )
        limits = tool_limits(
            SandboxTool.PYTHON, timeout_seconds=ctx.config.timeout_document_seconds
        )
        lines: list[str] = []

        completed = await ctx.runner.run(
            python_module_command(worker.MODULE, *worker.format_args(args)),
            cwd=workspace.tmp_dir,
            limits=limits,
            env=minimal_env(workspace.tmp_dir),
            on_stdout_line=lines.append,
        )
        if completed.returncode != 0:
            raise worker_failure(completed)
        try:
            event = read_done_event(lines)
        except worker.WorkerOutputError as exc:
            raise ConversionFailure(
                FailureReason.INTERNAL_ERROR, detail=describe_exception(exc)
            ) from exc

        if ctx.pair.source == HTML_FORMAT and event.external_resources > 0:
            ctx.notices.add(
                NoticeCode.EXTERNAL_RESOURCES_SKIPPED, {"cantidad": event.external_resources}
            )
        ctx.progress.report(1.0)
        return ConversionOutcome(result_path=output, result_kind="single")
