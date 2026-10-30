"""Convertidor TXT, MD y HTML a PDF, `text.pdf` (Req 6.1, 6.6, 6.7, 6.8, 6.11, 6.12, 6.13, 13.7).

Lanza `python -m converter.workers.text_pdf` (ver `workers/text_pdf.py`) a través del Ejecutor
aislado y escribe el PDF en `work/output.pdf`. El worker parte del Texto_Decodificado del origen,
no de sus bytes:

- **TXT a PDF** (Req 6.1, 6.8): la maquetación de `layout.py` (A4, márgenes de 2 cm, DejaVu Sans
  Mono de 10 pt, líneas cortadas en el último espacio que cabe) escrita con `white-space: pre`.
- **MD a PDF y HTML a PDF** (Req 6.12): el DOM saneado con U+2060 dentro de las palabras y la hoja
  base (A4, DejaVu, `hyphens: manual`, `font-variant-ligatures: none`). En HTML, las hojas del
  documento tienen prioridad sobre la base.

Los tres pares comparten este convertidor y se distinguen por el Formato_de_Origen del par
(`ctx.pair.source`). Se registra con el identificador `text.pdf`; el catálogo registra los pares
en `catalog/pairs.toml`. Este módulo decide qué hacer con lo que informa el worker:

- **Caracteres no representables** (Req 6.11). Si un TXT o un MD tiene caracteres que la
  Fuente_Predeterminada no puede representar, la Tarea falla con `caracteres_no_representables`
  y no hay Archivo_Resultado. El mensaje cita como máximo 10 caracteres distintos, en orden de
  aparición. Un HTML no se comprueba.
- **Recursos_Externos omitidos** (Req 6.6). El worker no lee ni descarga ningún recurso: el
  saneamiento del DOM neutraliza los que cuenta y el `url_fetcher` de WeasyPrint rechaza todo
  esquema que no sea `data:`, incluido `file:` (Req 13.7). Si el origen es HTML o MD y el DOM
  referenciaba alguno, el aviso `recursos_externos_omitidos` informa cuántos. Un TXT no los tiene.
- **Scripts** (Req 6.7, 6.13). El saneamiento los quita antes de escribir el PDF, así que su
  código no llega al resultado y no se ejecuta. No hay aviso.
- **Codificación** (Req 2.12). Se usa la que clasificó el Detector_de_Tipo (`TextInfo.encoding`),
  no la que declare el documento.
- **Fallos del worker.** Con un código de salida distinto de 0, la salida de error se sanea con
  `sanitize_tool_output` y se clasifica con `classify_failure`: la falta de espacio da
  `sin_espacio_proceso` y todo lo demás `error_interno` (Req 16.9, 16.10). También es
  `error_interno` una salida del worker que no sigue su protocolo y un PDF ausente. Un origen que
  el Detector_de_Tipo clasificó como Texto_Válido siempre se decodifica, así que un fallo de
  lectura no indica un archivo dañado. El detalle técnico saneado va en `ConversionFailure.detail`.

TXT, MD y HTML a PDF no tienen Rechazos_de_Destino propios (Req 16.12).

El worker corre con `FONTCONFIG_FILE` en `default-only.conf` (`layout.FONTCONFIG_FILE`), que solo
ve el directorio de la Fuente_Predeterminada (`layout.DEFAULT_FONT_DIR`). Ambas rutas son
parámetros del constructor, con los valores de la Imagen_de_Contenedor, para poder probar con
otras fuentes fuera del contenedor.

Para sanear la salida del worker se usa el conjunto de patrones de `Tool.PILLOW`, el de un
subproceso Python: `eventlog.Tool` no tiene una entrada propia para WeasyPrint y los patrones que
importan (falta de espacio, límites de recursos y mensajes del lanzador) son comunes a todas.

El módulo no importa BeautifulSoup, markdown-it-py ni WeasyPrint: solo las carga el worker.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from converter.converters.base import (
    ConversionContext,
    ConversionFailure,
    ConversionOutcome,
    ConversionRequest,
    register_converter,
)
from converter.converters.errors import classify_failure
from converter.converters.text_docs.layout import DEFAULT_FONT_DIR, FONTCONFIG_FILE
from converter.detection.source_info import SourceInfo, TextInfo
from converter.eventlog import Tool, describe_exception, sanitize_tool_output
from converter.messages_es import FailureReason, NoticeCode
from converter.models import DestinationRejection
from converter.sandbox.limits import SandboxTool, tool_limits
from converter.sandbox.runner import CompletedRun, minimal_env, python_module_command
from converter.workers import text_pdf as worker

if TYPE_CHECKING:
    from converter.config import Config

CONVERTER_ID = "text.pdf"
OUTPUT_NAME = "output.pdf"  # en work/

TARGET_FORMAT = "PDF"
# El aviso de Recursos_Externos corresponde a estos orígenes (Req 6.6)
MARKUP_FORMATS = ("MD", "HTML")

# Motivos que el worker puede dar en la etapa de conversión
_WORKER_REASONS = frozenset({FailureReason.NO_SPACE, FailureReason.INTERNAL_ERROR})


def _text_info(source: SourceInfo) -> TextInfo:
    """`source` como información de un Formato_de_Texto. Lanza `TypeError` si es de otro tipo."""
    if not isinstance(source, TextInfo):
        raise TypeError(f"text.pdf necesita la información de un texto: {type(source)}")
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


def read_final_event(lines: list[str]) -> worker.Event:
    """Evento final del worker a partir de las líneas de su salida estándar.

    Lanza `WorkerOutputError` si no hay exactamente una línea o no es un evento válido.
    """
    if len(lines) != 1:
        raise worker.WorkerOutputError(f"se esperaba un evento y hay {len(lines)} líneas")
    return worker.load_event(lines[0])


@register_converter(CONVERTER_ID)
class TextPdfConverter:
    """TXT, MD y HTML a PDF con WeasyPrint, a partir del Texto_Decodificado."""

    def __init__(
        self, *, font_dir: Path = DEFAULT_FONT_DIR, fontconfig_file: Path = FONTCONFIG_FILE
    ) -> None:
        self._font_dir = font_dir  # fuentes de la maquetación y de la cobertura de glifos
        self._fontconfig_file = fontconfig_file  # FONTCONFIG_FILE del worker

    def preflight(
        self, source: SourceInfo, request: ConversionRequest, config: Config
    ) -> DestinationRejection | None:
        """TXT, MD y HTML a PDF no tienen Rechazos_de_Destino propios (Req 16.12)."""
        return None

    async def run(self, ctx: ConversionContext) -> ConversionOutcome:
        """Convierte el documento en un subproceso y deja el PDF en `work/output.pdf`.

        Lanza `ConversionFailure` con `caracteres_no_representables` si un TXT o un MD tiene
        caracteres que la Fuente_Predeterminada no puede representar y con el motivo que
        corresponda si el worker falla. Lanza `TypeError` si `ctx.source` no es la información
        de un Formato_de_Texto y `ValueError` si el par no es TXT, MD o HTML a PDF.
        """
        if ctx.pair.source not in worker.SOURCE_FORMATS or ctx.pair.target != TARGET_FORMAT:
            raise ValueError(f"text.pdf no convierte {ctx.pair.source} a {ctx.pair.target}")
        info = _text_info(ctx.source)
        workspace = ctx.workspace
        output = (workspace.work_dir / OUTPUT_NAME).absolute()
        args = worker.WorkerArgs(
            source_format=ctx.pair.source,
            encoding=info.encoding,
            source=workspace.source_file.absolute(),
            destination=output,
            font_dir=self._font_dir,
        )
        limits = tool_limits(
            SandboxTool.WEASYPRINT, timeout_seconds=ctx.config.timeout_document_seconds
        )
        lines: list[str] = []

        completed = await ctx.runner.run(
            python_module_command(worker.MODULE, *worker.format_args(args)),
            cwd=workspace.tmp_dir,
            limits=limits,
            env=minimal_env(workspace.tmp_dir, fontconfig_file=self._fontconfig_file),
            on_stdout_line=lines.append,
        )
        if completed.returncode != 0:
            raise worker_failure(completed)
        try:
            event = read_final_event(lines)
        except worker.WorkerOutputError as exc:
            raise ConversionFailure(
                FailureReason.INTERNAL_ERROR, detail=describe_exception(exc)
            ) from exc

        if isinstance(event, worker.UnrepresentableEvent):
            # El worker no escribe el PDF: no hay Archivo_Resultado (Req 6.11)
            raise ConversionFailure(
                FailureReason.UNREPRESENTABLE_CHARACTERS,
                params={"caracteres": list(event.characters)},
            )
        if not output.is_file():
            raise ConversionFailure(FailureReason.INTERNAL_ERROR)

        if ctx.pair.source in MARKUP_FORMATS and event.external_resources > 0:
            ctx.notices.add(
                NoticeCode.EXTERNAL_RESOURCES_SKIPPED, {"cantidad": event.external_resources}
            )
        ctx.progress.report(1.0)
        return ConversionOutcome(result_path=output, result_kind="single")
