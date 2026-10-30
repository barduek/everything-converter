"""Detector_de_Tipo: Tipo_Real, información del origen o Rechazo_de_Origen (Req 2, 3.6, 4.4, 16.1).

Sigue el diagrama "Detector_de_Tipo" de design.md:

1. Firma: el Tipo_Real es el Formato con la Firma_de_Formato presente más específica
   (`match_signature`), con cualquier Extensión_del_Nombre o sin ella (Req 2.1, 2.8).
2. Variante: si ninguna firma coincide, la primera sonda de variante que coincide
   (`match_variant`) da como Tipo_Real el Formato que la declara, con el rechazo de
   `VARIANT_REJECTIONS`: un DOCX_Cifrado es DOCX con `docx_cifrado` (Req 2.10).
3. Texto: si tampoco, `classify_text` recorre el archivo completo en bloques de 1 MiB. Si no es
   Texto_Válido, el rechazo es `formato_no_reconocido` (Req 2.5). Si lo es, el Tipo_Real es el
   Formato_de_Texto que tiene la Extensión_del_Nombre, sin distinguir mayúsculas, o TXT si
   ninguno la tiene o el nombre no tiene extensión (Req 2.2, 2.3). La BOM_UTF8 inicial no cambia
   la clasificación, así que tampoco el Tipo_Real (Req 2.4).
4. Formato_de_Origen: si el Tipo_Real no es Formato_de_Origen de ningún Par_de_Conversión, el
   rechazo es `tipo_sin_destinos` con la lista completa de Formatos_de_Origen (Req 2.7, 3.6).
5. Límite_de_Tamaño: si el archivo supera el de la Categoría del Tipo_Real, el rechazo es
   `excede_limite_categoria` con la Categoría y el límite en MB (Req 2.9).
6. Inspector: el rechazo de la variante, si lo hubo. Si no, la información del origen: un
   Formato_de_Texto obtiene `TextInfo` en el proceso principal, con la clasificación del paso 3,
   para que sus convertidores trabajen sobre el Texto_Decodificado (Req 2.12); un Formato con
   firma e inspector `text` también, clasificando su contenido (si no es Texto_Válido, el
   rechazo es `archivo_danado`); un Formato sin inspector obtiene `NoInfo`; los demás llaman a
   la función de inspección inyectada, que en el Conversor ejecuta el inspector en un subproceso
   (`detection/inspect_runner.py`).

Así los rechazos siguen el orden del diseño: formato no reconocido, tipo sin destinos,
Límite_de_Tamaño de la Categoría y los del inspector, que cada inspector evalúa en el orden de
`INSPECTION_REJECTIONS`. Cada uno es un `SourceRejection` con la etapa detección y, salvo el
formato no reconocido, el Tipo_Real (Req 16.1, 16.7). Sin rechazo, si la Extensión_del_Nombre
existe y no está entre las extensiones del Tipo_Real, el resultado lleva el aviso
`extension_no_coincide` con la extensión tal como la escribió el usuario (Req 2.6).

El detector solo consulta el Registro_de_Conversiones: un Formato, un par o una sonda nuevos se
detectan sin cambiarlo (Req 4.4). Los pasos 1 a 5 y el inspector `text` leen el archivo con E/S
bloqueante: `identify` los ejecuta sobre cualquier flujo binario posicionable y `detect` los
corre en un hilo (`anyio.to_thread`) antes de llamar a la función de inspección.

Los errores que no dependen del archivo lanzan una excepción, que el Gestor_de_Tareas convierte
en `error_interno` (Req 16.9): `DetectionSetupError` si el registro no tiene TXT como
Formato_de_Texto, si el Mapa_de_Conversión no tiene Formatos_de_Origen (el mensaje de
`tipo_sin_destinos` necesita al menos uno) o si una sonda de variante no tiene rechazo;
`UnknownProbeError` si el catálogo cita una sonda no registrada; `ValueError` o `TypeError` si
la función de inspección devuelve un motivo que no es de un inspector u otro tipo de resultado.
"""

from __future__ import annotations

import io
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, BinaryIO

from anyio import to_thread

from converter.config import BYTES_PER_MB
from converter.detection.probes import ProbeInput
from converter.detection.signatures import match_signature, match_variant
from converter.detection.source_info import (
    INSPECTOR_REASONS,
    DetectionResult,
    NoInfo,
    SourceInfo,
    SourceRejection,
    TextInfo,
)
from converter.detection.text import BOM_UTF8, TextKind, classify_text, read_chunks
from converter.messages_es import FailureReason, NoticeCode, render_notice
from converter.models import Category, Notice
from converter.naming import split_name

if TYPE_CHECKING:
    from converter.config import Config
    from converter.registry.model import FormatDef
    from converter.registry.registry import ConversionRegistry
    from converter.storage.workspace import Workspace

FALLBACK_TEXT_FORMAT = "TXT"  # Tipo_Real de un Texto_Válido sin Formato_de_Texto (Req 2.3)
TEXT_INSPECTOR = "text"  # inspector que corre en el proceso principal

# Rechazo_de_Origen de cada sonda de variante (Req 2.10, 7.2)
VARIANT_REJECTIONS: Mapping[str, FailureReason] = MappingProxyType(
    {"docx_encrypted": FailureReason.ENCRYPTED_DOCX}
)


class DetectionSetupError(RuntimeError):
    """El Registro_de_Conversiones no permite terminar la detección; no depende del archivo."""


@dataclass(frozen=True, slots=True)
class InspectionRequest:
    """Lo que recibe la función de inspección."""

    inspector: str  # inspector que declara el Formato, p. ej. "image"
    real_type: str  # Tipo_Real, p. ej. "MP4": el inspector `media` elige el demuxer con él
    workspace: Workspace  # Archivo_de_Origen en source/original; tmp/ para el subproceso


# Función de inspección que recibe el detector. Devuelve la información del origen o un rechazo
# de `INSPECTOR_REASONS`; el detector le agrega el Tipo_Real al rechazo.
type Inspect = Callable[[InspectionRequest], Awaitable[SourceInfo | SourceRejection]]


@dataclass(frozen=True, slots=True)
class NeedsInspection:
    """Pasos 1 a 5 sin rechazo; falta el inspector del Formato, que corre fuera del proceso."""

    format: FormatDef  # Tipo_Real
    inspector: str  # inspector que declara el Formato
    notices: tuple[Notice, ...]  # avisos para el resultado, p. ej. extension_no_coincide


def size_limit_mb(config: Config, category: Category) -> int:
    """Límite_de_Tamaño de la Categoría en MB, de 1 000 000 bytes (Req 2.9)."""
    limits = {
        Category.IMAGE: config.max_image_mb,
        Category.DOCUMENT: config.max_document_mb,
        Category.AUDIO: config.max_audio_mb,
        Category.VIDEO: config.max_video_mb,
    }
    return limits[Category(category)]


def extension_notice(fmt: FormatDef, extension: str | None) -> Notice | None:
    """Aviso `extension_no_coincide` si la Extensión_del_Nombre existe y no es del Formato.

    La comparación no distingue mayúsculas; el aviso cita la extensión como llegó (Req 2.6).
    """
    if extension is None or extension.lower() in fmt.extensions:
        return None
    notice = Notice(
        code=NoticeCode.EXTENSION_MISMATCH,
        params=MappingProxyType({"extension": extension, "tipo_real": fmt.name}),
    )
    render_notice(notice)  # un parámetro inválido falla aquí y no al mostrar la Tarea
    return notice


def _classify(stream: BinaryIO) -> TextKind:
    stream.seek(0)
    return classify_text(read_chunks(stream))


def _fallback_text_format(formats: Iterable[FormatDef]) -> FormatDef:
    for fmt in formats:
        if fmt.name == FALLBACK_TEXT_FORMAT and fmt.signature is None:
            return fmt
    raise DetectionSetupError("el Registro_de_Conversiones no tiene el Formato_de_Texto TXT")


class TypeDetector:
    """Detector_de_Tipo sobre un Registro_de_Conversiones ya cargado."""

    __slots__ = ("_config", "_inspect", "_registry", "_variant_rejections")

    def __init__(
        self,
        registry: ConversionRegistry,
        config: Config,
        inspect: Inspect,
        *,
        variant_rejections: Mapping[str, FailureReason] = VARIANT_REJECTIONS,
    ) -> None:
        self._registry = registry
        self._config = config
        self._inspect = inspect
        self._variant_rejections = MappingProxyType(dict(variant_rejections))

    async def detect(
        self, workspace: Workspace, original_name: str
    ) -> DetectionResult | SourceRejection:
        """Detecta el Tipo_Real del Archivo_de_Origen de `workspace`.

        `original_name` es el Nombre_Original, del que solo se usa la Extensión_del_Nombre.
        """
        outcome = await to_thread.run_sync(
            self._identify_file, workspace.source_file, original_name
        )
        if not isinstance(outcome, NeedsInspection):
            return outcome
        request = InspectionRequest(
            inspector=outcome.inspector, real_type=outcome.format.name, workspace=workspace
        )
        inspected = await self._inspect(request)
        if isinstance(inspected, SourceRejection):
            if inspected.reason not in INSPECTOR_REASONS:
                raise ValueError(f"un inspector no produce el rechazo {inspected.reason.value}")
            return replace(inspected, real_type=outcome.format.name)
        if not isinstance(inspected, SourceInfo):
            raise TypeError("la función de inspección devuelve un SourceInfo o un SourceRejection")
        return DetectionResult(outcome.format.name, inspected, outcome.notices)

    def _identify_file(
        self, path: Path, original_name: str
    ) -> DetectionResult | SourceRejection | NeedsInspection:
        with path.open("rb") as stream:
            return self.identify(stream, original_name)

    def identify(
        self, stream: BinaryIO, original_name: str
    ) -> DetectionResult | SourceRejection | NeedsInspection:
        """Pasos 1 a 6 del detector, salvo los inspectores que corren fuera del proceso.

        `stream` es el Archivo_de_Origen completo: binario, posicionable y de solo lectura. Su
        posición final no está definida.
        """
        _, extension = split_name(original_name)
        size = stream.seek(0, io.SEEK_END)
        source = ProbeInput.from_stream(stream, extension)
        formats = self._registry.formats()

        # 1 a 3: Tipo_Real
        text_kind: TextKind | None = None  # clasificación, si se hizo
        variant_reason: FailureReason | None = None
        fmt = match_signature(formats, source)
        if fmt is None:
            variant = match_variant(formats, source)
            if variant is not None:
                fmt = variant.format
                variant_reason = self._variant_reason(variant.probe)
            else:
                text_kind = _classify(stream)
                if text_kind is TextKind.INVALID:
                    return SourceRejection(FailureReason.UNRECOGNIZED_FORMAT)
                fmt = self._registry.text_format_for_extension(extension)
                if fmt is None:
                    fmt = _fallback_text_format(formats)

        # 4: Formato_de_Origen
        sources = self._registry.source_formats()
        if not sources:
            raise DetectionSetupError("el Mapa_de_Conversión no tiene Formatos_de_Origen")
        if fmt.name not in sources:
            return SourceRejection(
                FailureReason.NO_TARGETS,
                {"tipo_real": fmt.name, "formatos_origen": list(sources)},
                real_type=fmt.name,
            )

        # 5: Límite_de_Tamaño de la Categoría
        limit_mb = size_limit_mb(self._config, fmt.category)
        if size > limit_mb * BYTES_PER_MB:
            return SourceRejection(
                FailureReason.CATEGORY_SIZE_LIMIT,
                {"categoria": fmt.category.value, "limite_mb": limit_mb},
                real_type=fmt.name,
            )

        # 6: rechazo de la variante o inspector
        if variant_reason is not None:
            return SourceRejection(variant_reason, real_type=fmt.name)
        notice = extension_notice(fmt, extension)
        notices = () if notice is None else (notice,)
        if fmt.signature is None or fmt.inspector == TEXT_INSPECTOR:
            if text_kind is None:
                text_kind = _classify(stream)
                if text_kind is TextKind.INVALID:
                    return SourceRejection(FailureReason.DAMAGED, real_type=fmt.name)
            had_bom = text_kind is TextKind.UTF8 and source.head.startswith(BOM_UTF8)
            info = TextInfo(encoding=text_kind, had_bom=had_bom)
            return DetectionResult(fmt.name, info, notices)
        if fmt.inspector is None:
            return DetectionResult(fmt.name, NoInfo(), notices)
        return NeedsInspection(format=fmt, inspector=fmt.inspector, notices=notices)

    def _variant_reason(self, probe: str) -> FailureReason:
        try:
            return self._variant_rejections[probe]
        except KeyError:
            raise DetectionSetupError(f"la sonda de variante {probe!r} no tiene rechazo") from None
