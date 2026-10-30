"""Convertidor `image.raster`: PNG, JPG y WEBP entre sí (Req 5.1, 5.2, 5.3, 5.5, 5.7, 5.8, 5.10,
13.4).

Los seis pares de imagen a imagen del Mapa_de_Conversión_Inicial los ejecuta el subproceso
`python -m converter.workers.image` (ver `workers/image.py`), a través del Ejecutor aislado. Ahí
están la Orientación_EXIF, la normalización del modo, la composición sobre blanco para JPG y las
opciones de guardado de cada destino. Este módulo decide lo que no necesita Pillow:

- `preflight` rechaza WEBP si el ancho o el alto de las Dimensiones_Mostradas superan 16383 píxeles
  y JPG si superan 65535 (Req 5.5). Es un Rechazo_de_Destino `dimensiones_excedidas`: la Tarea
  sigue en recibida. Usa las Dimensiones_Mostradas que calculó el inspector `image`, así que un
  JPG de 3000 × 17000 con Orientación_EXIF 6 se informa como 17000 × 3000 (Req 5.10).
- `run` lanza el worker con el Límite_de_Píxeles y los límites de recursos de Python con Pillow, y
  clasifica su fallo: `archivo_danado` si Pillow no puede decodificar el origen,
  `sin_espacio_proceso` si no hay espacio y `error_interno` en cualquier otro caso (Req 16.2,
  16.9, 16.10). Si el origen es un PNG de 16 bits por canal y el destino es JPG o WEBP, agrega el
  aviso `bits_reducidos` (Req 5.3, 5.8).

El módulo solo necesita del `SourceInfo` del inspector `image` las Dimensiones_Mostradas
(`displayed_size`) y los bits por canal (`bits_per_channel`), por eso `ImageSource` las declara
como protocolo y no importa el inspector.

Límite conocido: libjpeg admite como máximo 65500 píxeles de ancho o alto, y el criterio 5.5 fija
65535 para JPG. Un origen de 65501 a 65535 píxeles pasa el `preflight` pero el worker falla al
guardarlo y la Tarea termina con `error_interno`.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from converter.converters.base import (
    ConversionContext,
    ConversionFailure,
    ConversionOutcome,
    ConversionRequest,
    register_converter,
)
from converter.converters.errors import classify_failure
from converter.detection.source_info import SourceInfo
from converter.eventlog import Tool, sanitize_tool_output
from converter.messages_es import FailureReason, NoticeCode, RejectionCode
from converter.models import DestinationRejection, Stage
from converter.sandbox.cpus import effective_cpus
from converter.sandbox.limits import SandboxTool, tool_limits
from converter.sandbox.runner import CompletedRun, minimal_env, python_module_command
from converter.workers import image as worker

if TYPE_CHECKING:
    from converter.config import Config

# Máximo de ancho y de alto de las Dimensiones_Mostradas de cada Formato_de_Destino (Req 5.5)
MAX_DIMENSIONS: Mapping[str, int] = MappingProxyType({"WEBP": 16383, "JPG": 65535})

# Formatos_de_Destino que guardan 8 bits por canal: un PNG de 16 bits se reduce (Req 5.3, 5.8)
EIGHT_BIT_TARGETS = frozenset({"JPG", "WEBP"})
SIXTEEN_BITS = 16

RESULT_NAME = "converted"  # Archivo_Resultado dentro de work/

# Motivos con los que puede terminar el worker. `excede_limite_pixeles` es de la detección: si el
# worker lo detecta, el inspector y el worker no coinciden, y eso es un error interno
_REPORTED_REASONS = frozenset(
    {FailureReason.DAMAGED, FailureReason.NO_SPACE, FailureReason.INTERNAL_ERROR}
)


@runtime_checkable
class ImageSource(Protocol):
    """Lo que el convertidor usa del `ImageInfo` del inspector `image` (design.md, "Información
    del origen")."""

    @property
    def bits_per_channel(self) -> int:
        """Bits por canal del origen: 1, 2, 4, 8 o 16 (`IHDR` en PNG; 8 en JPG y WEBP)."""
        ...

    @property
    def displayed_size(self) -> tuple[int, int]:
        """Dimensiones_Mostradas: ancho y alto con la Orientación_EXIF aplicada."""
        ...


def _image_source(source: SourceInfo) -> ImageSource:
    """`source` como información de una imagen. Lanza `TypeError` si es de otro Formato."""
    if not isinstance(source, ImageSource):
        raise TypeError(f"image.raster necesita la información de una imagen: {type(source)}")
    return source


def reduces_bit_depth(source: ImageSource, target: str) -> bool:
    """El origen es de 16 bits por canal y el destino guarda 8 (Req 5.3, 5.8)."""
    return source.bits_per_channel == SIXTEEN_BITS and target in EIGHT_BIT_TARGETS


def _failure(completed: CompletedRun) -> ConversionFailure:
    """Fallo de un worker que terminó con error o sin escribir su resultado."""
    detail = sanitize_tool_output(
        Tool.PILLOW, completed.stderr_lines, returncode=completed.returncode
    )
    reason = classify_failure(detail=detail)
    if reason not in _REPORTED_REASONS:
        reason = FailureReason.INTERNAL_ERROR
    return ConversionFailure(reason, stage=Stage.CONVERSION, detail=detail)


@register_converter("image.raster")
class RasterImageConverter:
    """Convierte PNG, JPG y WEBP entre sí con Pillow en un subproceso."""

    def preflight(
        self, source: SourceInfo, request: ConversionRequest, config: Config
    ) -> DestinationRejection | None:
        """Rechaza WEBP o JPG si las Dimensiones_Mostradas superan el máximo del Formato (Req 5.5).

        Lanza `TypeError` si `source` no es la información de una imagen.
        """
        image = _image_source(source)
        maximum = MAX_DIMENSIONS.get(request.target)
        width, height = image.displayed_size
        if maximum is None or max(width, height) <= maximum:
            return None
        return DestinationRejection(
            RejectionCode.DIMENSIONS_EXCEEDED,
            {"ancho": width, "alto": height, "destino": request.target, "maximo": maximum},
        )

    async def run(self, ctx: ConversionContext) -> ConversionOutcome:
        """Ejecuta el par en el worker y deja el Archivo_Resultado en `work/`.

        Lanza `ConversionFailure` si el worker termina con error o sin escribir el resultado.
        """
        image = _image_source(ctx.source)
        output = (ctx.workspace.work_dir / RESULT_NAME).absolute()
        args = worker.WorkerArgs(
            source_format=ctx.pair.source,
            target_format=ctx.pair.target,
            source=ctx.workspace.source_file.absolute(),
            destination=output,
            max_image_pixels=ctx.config.max_image_pixels,
        )
        limits = tool_limits(
            SandboxTool.PYTHON,
            timeout_seconds=ctx.config.timeout_image_seconds,
            cpus=effective_cpus(),
        )
        tmp_dir = ctx.workspace.tmp_dir
        completed = await ctx.runner.run(
            python_module_command(worker.MODULE, *worker.format_args(args)),
            cwd=tmp_dir,
            limits=limits,
            env=minimal_env(tmp_dir),
        )
        if completed.returncode != 0 or not output.is_file():
            raise _failure(completed)
        if reduces_bit_depth(image, ctx.pair.target):
            ctx.notices.add(NoticeCode.BITS_REDUCED)
        ctx.progress.report(1.0)
        return ConversionOutcome(result_path=output, result_kind="single")
