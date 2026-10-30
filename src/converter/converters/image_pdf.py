"""Convertidor `image.pdf`: PNG, JPG y WEBP a PDF (Req 5.4, 5.10, 4.11, 13.4).

Los tres pares de imagen a PDF los ejecuta el subproceso `python -m converter.workers.image_pdf`
(ver `workers/image_pdf.py`), a través del Ejecutor aislado. Ahí están la Orientación_EXIF, la
normalización del modo, la composición sobre blanco y el tamaño de página. Este módulo decide lo
que no necesita Pillow:

- `preflight` no rechaza nada: Req 5.5 fija máximos de dimensiones solo para WEBP y JPG, y PDF no
  los tiene. Comprueba que `source` sea la información de una imagen.
- `run` lanza el worker con el Límite_de_Píxeles y los límites de recursos de Python con Pillow, y
  clasifica su fallo como `image.raster`: `archivo_danado` si Pillow no puede decodificar el
  origen, `sin_espacio_proceso` si no hay espacio y `error_interno` en cualquier otro caso
  (Req 16.2, 16.9, 16.10). Si el origen es un PNG de 16 bits por canal agrega el aviso
  `bits_reducidos`: el worker guarda 8 bits por canal.

MVP (tarea 15.6): el worker usa el escritor de PDF de Pillow en lugar del escritor propio
`pdf_image_writer` del diseño, que queda para después. El PDF recodifica la imagen como JPEG,
reemplaza la transparencia por blanco y reduce a 8 bits los PNG de 16 bits, así que estos pares no
son Sin_Pérdida como indica el diseño (Req 4.11). El detalle y lo que debe registrar la tarea 15.7
están en el docstring de `workers/image_pdf.py`. Cuando exista el escritor propio, que conserva
los 16 bits, el aviso `bits_reducidos` de `run` deja de aplicar a los PNG de 16 bits y hay que
quitarlo.

Límite conocido: libjpeg admite como máximo 65500 píxeles de ancho o alto. Un origen cuyas
Dimensiones_Mostradas superan ese valor pasa el `preflight`, pero el worker se niega a guardarlo
y la Tarea termina con `error_interno`. Un Rechazo_de_Destino `dimensiones_excedidas` para PDF
sería mejor, pero Req 5.5 no lo prevé.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from converter.converters.base import (
    ConversionContext,
    ConversionOutcome,
    ConversionRequest,
    register_converter,
)
from converter.converters.images import (
    RESULT_NAME,
    SIXTEEN_BITS,
    _failure,
    _image_source,
)
from converter.detection.source_info import SourceInfo
from converter.messages_es import NoticeCode
from converter.models import DestinationRejection
from converter.sandbox.cpus import effective_cpus
from converter.sandbox.limits import SandboxTool, tool_limits
from converter.sandbox.runner import minimal_env, python_module_command
from converter.workers import image_pdf as worker

if TYPE_CHECKING:
    from converter.config import Config


@register_converter("image.pdf")
class ImagePdfConverter:
    """Convierte PNG, JPG y WEBP en un PDF de una página con Pillow en un subproceso."""

    def preflight(
        self, source: SourceInfo, request: ConversionRequest, config: Config
    ) -> DestinationRejection | None:
        """No rechaza ningún destino: PDF no tiene máximo de dimensiones en Req 5.5.

        Lanza `TypeError` si `source` no es la información de una imagen.
        """
        _image_source(source)
        return None

    async def run(self, ctx: ConversionContext) -> ConversionOutcome:
        """Ejecuta el par en el worker y deja el Archivo_Resultado en `work/`.

        Lanza `ConversionFailure` si el worker termina con error o sin escribir el resultado.
        """
        image = _image_source(ctx.source)
        output = (ctx.workspace.work_dir / RESULT_NAME).absolute()
        args = worker.WorkerArgs(
            source_format=ctx.pair.source,
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
        if image.bits_per_channel == SIXTEEN_BITS:
            ctx.notices.add(NoticeCode.BITS_REDUCED)
        ctx.progress.report(1.0)
        return ConversionOutcome(result_path=output, result_kind="single")
