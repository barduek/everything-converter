"""Convertidor `docx.libreoffice`: DOCX a PDF con LibreOffice (Req 7.1, 7.4, 16.2, 16.3).

MVP (tarea 18.8): no sanea el DOCX. Convierte el archivo original con LibreOffice y un perfil
propio que desactiva las macros y deja la actualización de vínculos en "nunca". El PDF muestra
los comentarios y las marcas de revisión como los muestre LibreOffice, sin los avisos de
comentarios, cambios ni macros: eso queda para el saneamiento de la tarea 18.4.

Pasos de `run`:

1. Comprobar que el paquete tiene parte principal. Si no se puede leer, la Tarea falla con
   `archivo_danado` (Req 16.2). Los Límites_de_Descompresión ya se verificaron en la detección.
2. `fonts.find_replacements` informa las fuentes que no están incrustadas ni instaladas, con el
   reemplazo de `fc-match` (Req 7.4). El aviso es `fuentes_reemplazadas`.
3. Copiar el perfil a `tmp/lo-profile` y ejecutar `soffice --headless` con la exportación PDF
   del diseño (notas desactivadas, fuentes estándar incrustadas).
4. Abrir el PDF con pypdfium2. Si no existe o no abre, la Tarea falla (Req 16.3).
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from converter.converters.base import (
    ConversionContext,
    ConversionFailure,
    ConversionOutcome,
    ConversionRequest,
    register_converter,
)
from converter.converters.docx.fonts import (
    FontAnalysisError,
    FontReplacement,
    FontToolError,
    find_replacements,
)
from converter.converters.errors import classify_failure
from converter.detection.source_info import SourceInfo
from converter.eventlog import Tool, describe_exception, sanitize_tool_output
from converter.messages_es import FailureReason, NoticeCode
from converter.models import DestinationRejection
from converter.sandbox.limits import SandboxTool, tool_limits
from converter.sandbox.runner import minimal_env

if TYPE_CHECKING:
    from converter.config import Config

CONVERTER_ID = "docx.libreoffice"
SOFFICE = "soffice"
# Perfil de la Imagen_de_Contenedor. Las pruebas pasan otro directorio al constructor.
DEFAULT_PROFILE = Path("/opt/converter/libreoffice/profile")
INPUT_NAME = "document.docx"
OUTPUT_NAME = "output.pdf"
# Notas fuera del PDF, fuentes estándar incrustadas (design.md, "DOCX a PDF")
PDF_FILTER = (
    "pdf:writer_pdf_Export:"
    '{"ExportNotes":{"type":"boolean","value":"false"},'
    '"ExportNotesInMargin":{"type":"boolean","value":"false"},'
    '"EmbedStandardFonts":{"type":"boolean","value":"true"}}'
)
_CONVERSION_REASONS = frozenset(
    {FailureReason.DAMAGED, FailureReason.NO_SPACE, FailureReason.INTERNAL_ERROR}
)


def soffice_command(profile: Path, source: Path, outdir: Path) -> list[str]:
    """Comando `soffice --headless` que convierte `source` a PDF en `outdir`."""
    return [
        SOFFICE,
        "--headless",
        "--norestore",
        "--nolockcheck",
        f"-env:UserInstallation={profile.resolve().as_uri()}",
        "--convert-to",
        PDF_FILTER,
        "--outdir",
        str(outdir.resolve()),
        str(source.resolve()),
    ]


def _damaged(exc: BaseException) -> ConversionFailure:
    return ConversionFailure(FailureReason.DAMAGED, detail=describe_exception(exc))


def _replacements(source: Path) -> tuple[FontReplacement, ...]:
    """Fuentes faltantes del DOCX. Un paquete ilegible es `archivo_danado`."""
    try:
        with zipfile.ZipFile(source) as archive:
            return find_replacements(archive)
    except (zipfile.BadZipFile, FontAnalysisError) as exc:
        raise _damaged(exc) from exc
    except FontToolError as exc:
        raise ConversionFailure(
            FailureReason.INTERNAL_ERROR, detail=describe_exception(exc)
        ) from exc


def _pdf_opens(path: Path) -> bool:
    """Si pypdfium2 abre `path` y el PDF tiene al menos una página."""
    # pypdfium2 no publica anotaciones de tipos
    import pypdfium2  # type: ignore[import-untyped]

    try:
        document = pypdfium2.PdfDocument(path)
    except pypdfium2.PdfiumError:
        return False
    try:
        return len(document) >= 1
    finally:
        document.close()


@register_converter(CONVERTER_ID)
class DocxLibreOfficeConverter:
    """Convierte un DOCX a PDF con LibreOffice y el perfil de macros desactivadas."""

    def __init__(self, profile_dir: Path = DEFAULT_PROFILE) -> None:
        self._profile = profile_dir

    def preflight(
        self, source: SourceInfo, request: ConversionRequest, config: Config
    ) -> DestinationRejection | None:
        """DOCX a PDF no tiene Rechazos_de_Destino propios (Req 16.12)."""
        return None

    async def run(self, ctx: ConversionContext) -> ConversionOutcome:
        """Convierte el DOCX original y deja el PDF en `work/output.pdf`."""
        workspace = ctx.workspace
        source_copy = workspace.work_dir / INPUT_NAME
        produced = workspace.work_dir / "document.pdf"
        output = (workspace.work_dir / OUTPUT_NAME).absolute()
        shutil.copyfile(workspace.source_file, source_copy)
        replacements = _replacements(source_copy)
        profile = workspace.tmp_dir / "lo-profile"
        if not self._profile.is_dir():
            raise ConversionFailure(
                FailureReason.INTERNAL_ERROR,
                detail=describe_exception(FileNotFoundError(self._profile)),
            )
        shutil.copytree(self._profile, profile)
        completed = await ctx.runner.run(
            soffice_command(profile, source_copy, workspace.work_dir),
            cwd=workspace.tmp_dir,
            limits=tool_limits(
                SandboxTool.LIBREOFFICE, timeout_seconds=ctx.config.timeout_document_seconds
            ),
            env=minimal_env(workspace.tmp_dir),
        )
        if completed.returncode != 0 or not produced.is_file():
            detail = sanitize_tool_output(
                Tool.LIBREOFFICE, completed.stderr_lines, returncode=completed.returncode
            )
            reason = classify_failure(detail=detail)
            if reason not in _CONVERSION_REASONS:
                reason = FailureReason.INTERNAL_ERROR
            raise ConversionFailure(reason, detail=detail)
        produced.replace(output)
        if not _pdf_opens(output):
            raise ConversionFailure(FailureReason.DAMAGED)
        if replacements:
            ctx.notices.add(
                NoticeCode.FONTS_REPLACED,
                {"reemplazos": [(item.name, item.replacement) for item in replacements]},
            )
        ctx.progress.report(1.0)
        return ConversionOutcome(result_path=output, result_kind="single")
