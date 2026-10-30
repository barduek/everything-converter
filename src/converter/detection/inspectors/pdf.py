"""Inspector `pdf`: abre el PDF con pypdfium2 y lee lo que necesitan sus convertidores (Req 8.7,
8.10, 16.1, 16.2).

Devuelve un `PdfInfo`, con el tamaño mostrado de cada página y si el PDF tiene permisos
restringidos, o un Rechazo_de_Origen:

- `pdf_protegido` (Req 8.7): PDFium informa `FPDF_ERR_PASSWORD` al abrir el PDF sin contraseña.
  Nunca se pide una contraseña, y por eso este es el primer rechazo que se evalúa.
- `archivo_danado` (Req 16.2): `FPDF_ERR_FORMAT` o `FPDF_ERR_FILE` al abrir, una página que no se
  puede cargar, o una página sin área visible. También un fallo al abrir sin código de error, que
  es lo que informa PDFium ante un PDF sin páginas: el archivo tiene la firma del PDF pero su
  estructura es inválida.

Cualquier otro fallo de PDFium se propaga como `PdfiumError`: el worker termina con el traceback
en stderr y el proceso principal lo clasifica con la tabla de `converters/errors.py` (por
ejemplo, `FPDF_ERR_SECURITY`, un esquema de cifrado no admitido, es `error_interno`).

**Tamaño mostrado.** `PdfPage` guarda el ancho y el alto en puntos PDF (1/72 de pulgada) de la
página tal como se muestra: el `/CropBox` (por omisión, el `/MediaBox`, y recortado a este) con
el `/Rotate` aplicado, incluidos los valores heredados del árbol de páginas. Es lo que informa
`FPDF_GetPageWidthF` y `FPDF_GetPageHeightF`, los mismos valores de los que `page.render` calcula
el tamaño en píxeles. Se conservan sin redondear para que la verificación del Límite_de_Píxeles
de PDF a PNG y el renderizado usen los mismos números (design.md, "PDF como origen").

**Permisos restringidos.** Un PDF_con_Permisos_Restringidos se abre sin contraseña y usa el
cifrado del formato PDF para restringir permisos (Req 8.10, D11). `permissions_restricted` es
verdadero si el PDF está cifrado y su campo `/P` quita alguno de los permisos de imprimir,
modificar, copiar o anotar y, desde la revisión 3 del cifrado, de rellenar formularios, extraer
para accesibilidad, ensamblar o imprimir en alta calidad. Es un dato informativo: PDFium ignora
las restricciones al extraer texto y renderizar, así que no cambia los Formatos_de_Destino ni
el resultado (Req 8.10).

Este módulo importa pypdfium2 dentro de la función del inspector (ver `base.py`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from converter.detection.inspectors.base import InspectorInput, register_inspector
from converter.detection.source_info import SourceInfo, SourceRejection, register_source_info
from converter.eventlog import Tool
from converter.messages_es import FailureReason

# Permisos del campo /P que se consideran para `permissions_restricted` (ISO 32000-1, tabla 22).
# Los bits se cuentan desde 1: 3 imprimir, 4 modificar, 5 copiar, 6 anotar, 9 rellenar
# formularios, 10 extraer para accesibilidad, 11 ensamblar y 12 imprimir en alta calidad. Los
# bits 9 a 12 existen desde la revisión 3 del cifrado estándar.
_PERMISSION_BITS_REVISION_2 = 0x03C
_PERMISSION_BITS_REVISION_3 = 0xF3C


@dataclass(frozen=True, slots=True)
class PdfPage:
    """Una página del PDF, con el tamaño con el que se muestra."""

    width_pt: float  # ancho en puntos PDF (CropBox con /Rotate aplicado)
    height_pt: float  # alto en puntos PDF

    def __post_init__(self) -> None:
        for name in ("width_pt", "height_pt"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} debe ser un número")
            if not (math.isfinite(value) and value > 0):
                raise ValueError(f"{name} debe ser un número positivo y finito")


@register_source_info("pdf")
@dataclass(frozen=True, slots=True)
class PdfInfo(SourceInfo):
    """Información de un PDF que produce el inspector `pdf`."""

    pages: tuple[PdfPage, ...]  # en orden de Número_de_Página
    permissions_restricted: bool  # PDF_con_Permisos_Restringidos (Req 8.10)

    def __post_init__(self) -> None:
        if not isinstance(self.pages, tuple) or not all(
            isinstance(page, PdfPage) for page in self.pages
        ):
            raise TypeError("pages debe ser una tupla de PdfPage")
        if not isinstance(self.permissions_restricted, bool):
            raise TypeError("permissions_restricted debe ser un booleano")


def is_restricted(revision: int, permissions: int) -> bool:
    """Si el /P `permissions` de un PDF cifrado con la `revision` dada quita algún permiso.

    `revision` es la del cifrado estándar (`FPDF_GetSecurityHandlerRevision`), -1 si el PDF no
    está cifrado, y `permissions` los permisos del usuario que abre sin contraseña
    (`FPDF_GetDocUserPermissions`).
    """
    if revision < 0:
        return False
    mask = _PERMISSION_BITS_REVISION_2 if revision == 2 else _PERMISSION_BITS_REVISION_3
    return (permissions & mask) != mask


@register_inspector("pdf", info_type=PdfInfo, tool=Tool.PYPDFIUM2)
def inspect_pdf(source: InspectorInput) -> SourceInfo | SourceRejection:
    """Inspector del Formato PDF."""
    # pypdfium2 no publica anotaciones de tipos; sus objetos quedan como Any
    import pypdfium2  # type: ignore[import-untyped]
    import pypdfium2.raw as pdfium_raw  # type: ignore[import-untyped]

    try:
        document = pypdfium2.PdfDocument(source.path)
    except pypdfium2.PdfiumError as error:
        if error.err_code == pdfium_raw.FPDF_ERR_PASSWORD:
            return SourceRejection(FailureReason.PROTECTED_PDF)
        # FPDF_ERR_SUCCESS: PDFium no abrió el documento y no informó por qué
        if error.err_code in (
            pdfium_raw.FPDF_ERR_FORMAT,
            pdfium_raw.FPDF_ERR_FILE,
            pdfium_raw.FPDF_ERR_SUCCESS,
        ):
            return SourceRejection(FailureReason.DAMAGED)
        raise
    try:
        pages: list[PdfPage] = []
        for index in range(len(document)):
            try:
                page = document[index]
            except pypdfium2.PdfiumError:
                return SourceRejection(FailureReason.DAMAGED)
            try:
                width, height = page.get_size()
            finally:
                page.close()
            # Una página sin área visible no se puede mostrar ni rasterizar
            if not (math.isfinite(width) and width > 0 and math.isfinite(height) and height > 0):
                return SourceRejection(FailureReason.DAMAGED)
            pages.append(PdfPage(width_pt=width, height_pt=height))
        if not pages:
            return SourceRejection(FailureReason.DAMAGED)
        revision = pdfium_raw.FPDF_GetSecurityHandlerRevision(document.raw)
        permissions = pdfium_raw.FPDF_GetDocUserPermissions(document.raw)
        return PdfInfo(
            pages=tuple(pages), permissions_restricted=is_restricted(revision, permissions)
        )
    finally:
        document.close()
