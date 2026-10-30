"""Convertidor TXT y MD a HTML, `text.html` (Req 6.2, 6.3, 6.14, 2.12).

Lanza `python -m converter.workers.text_html` (ver `workers/text_html.py`) a través del Ejecutor
aislado y escribe el documento en `work/output.html`. El worker parte del Texto_Decodificado del
origen, no de sus bytes, y escribe UTF-8 sin BOM_UTF8:

- **TXT a HTML** (Req 6.2): un documento HTML5 con `lang="es"` y `<meta charset="utf-8">` cuyo
  cuerpo es un `<pre>` con el texto escapado con `html.escape(quote=True)`.
- **MD a HTML** (Req 6.3): un documento HTML5 con la misma estructura y, como cuerpo, el HTML
  que CommonMark define para el texto (markdown-it-py con el preset `commonmark`).

Los dos pares comparten este convertidor y se distinguen por el Formato_de_Origen del par
(`ctx.pair.source`). El convertidor se registra con el identificador `text.html`; el catálogo
registra los pares en `catalog/pairs.toml`.

- **Codificación** (Req 2.12, 6.14). `ctx.source` es el `TextInfo` del inspector `text`: la
  codificación que clasificó el Detector_de_Tipo (UTF-8 o Windows-1252) es lo único que el worker
  necesita para decodificar. La etiqueta `meta charset` de un MD con HTML embebido no interviene.
- **Rechazos_de_Destino.** TXT y MD a HTML no tienen ninguno propio (Req 16.12).
- **Avisos.** No hay: el HTML embebido en un MD se conserva como dicta CommonMark, y los Scripts
  y Recursos_Externos solo se omiten cuando el destino es PDF, TXT o MD (Req 6.6, 6.7, 6.13).
- **Fallos del worker.** Con un código de salida distinto de 0, la salida de error se sanea con
  `sanitize_tool_output` y se clasifica con `classify_failure`: la falta de espacio da
  `sin_espacio_proceso` y todo lo demás `error_interno` (Req 16.9, 16.10). Un origen que el
  Detector_de_Tipo clasificó como Texto_Válido siempre se decodifica, así que un fallo de lectura
  no indica un archivo dañado. El detalle técnico saneado va en `ConversionFailure.detail`.

No hay un valor de `eventlog.Tool` para un worker de Python puro. El detalle usa `Tool.PILLOW`
porque la única evidencia que importa aquí, `no_space` y `enospc`, es común a todas las
herramientas; el nombre solo aparece en el campo `tool` del Log_de_Eventos.

El módulo no importa BeautifulSoup ni markdown-it-py: solo el worker los carga, y solo con un MD.
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
from converter.eventlog import Tool, sanitize_tool_output
from converter.messages_es import FailureReason
from converter.models import DestinationRejection, Stage
from converter.sandbox.limits import SandboxTool, tool_limits
from converter.sandbox.runner import CompletedRun, minimal_env, python_module_command
from converter.workers import text_html as worker

if TYPE_CHECKING:
    from converter.config import Config

CONVERTER_ID = "text.html"
OUTPUT_NAME = "output.html"  # en work/

# Etiqueta del detalle técnico del worker en el Log_de_Eventos (ver el docstring del módulo)
_LOG_TOOL = Tool.PILLOW

# Motivos que el worker puede dar en la etapa de conversión
_WORKER_REASONS = frozenset({FailureReason.NO_SPACE, FailureReason.INTERNAL_ERROR})


def _text_source(source: SourceInfo) -> TextInfo:
    """`source` como información de un Formato_de_Texto. Lanza `TypeError` si es de otro Formato."""
    if not isinstance(source, TextInfo):
        raise TypeError(f"text.html necesita la información de un texto: {type(source)}")
    return source


def worker_failure(completed: CompletedRun) -> ConversionFailure:
    """Fallo de un worker que terminó con error o sin escribir su resultado."""
    detail = sanitize_tool_output(
        _LOG_TOOL, completed.stderr_lines, returncode=completed.returncode
    )
    reason = classify_failure(detail=detail)
    if reason not in _WORKER_REASONS:
        reason = FailureReason.INTERNAL_ERROR
    return ConversionFailure(reason, stage=Stage.CONVERSION, detail=detail)


@register_converter(CONVERTER_ID)
class TextHtmlConverter:
    """TXT y MD a HTML: documento HTML5 en UTF-8 a partir del Texto_Decodificado."""

    def preflight(
        self, source: SourceInfo, request: ConversionRequest, config: Config
    ) -> DestinationRejection | None:
        """TXT y MD a HTML no tienen Rechazos_de_Destino propios (Req 16.12)."""
        return None

    async def run(self, ctx: ConversionContext) -> ConversionOutcome:
        """Ejecuta el par en el worker y deja el documento HTML en `work/output.html`.

        Lanza `TypeError` si `ctx.source` no es la información de un Formato_de_Texto y
        `ValueError` si el Formato_de_Origen del par no es TXT ni MD. Lanza `ConversionFailure`
        si el worker termina con error o sin escribir el resultado.
        """
        text = _text_source(ctx.source)
        if ctx.pair.source not in worker.SOURCE_FORMATS:
            raise ValueError(f"text.html no convierte el Formato {ctx.pair.source!r}")
        workspace = ctx.workspace
        output = (workspace.work_dir / OUTPUT_NAME).absolute()
        args = worker.WorkerArgs(
            source_format=ctx.pair.source,
            encoding=text.encoding,
            source=workspace.source_file.absolute(),
            destination=output,
        )
        limits = tool_limits(
            SandboxTool.PYTHON, timeout_seconds=ctx.config.timeout_document_seconds
        )
        completed = await ctx.runner.run(
            python_module_command(worker.MODULE, *worker.format_args(args)),
            cwd=workspace.tmp_dir,
            limits=limits,
            env=minimal_env(workspace.tmp_dir),
        )
        if completed.returncode != 0 or not output.is_file():
            raise worker_failure(completed)
        ctx.progress.report(1.0)
        return ConversionOutcome(result_path=output, result_kind="single")
