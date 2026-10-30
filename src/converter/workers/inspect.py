"""Worker de inspección: ejecuta un inspector en su propio proceso (Req 2.11, 16.2, 16.9).

Uso:

    python -m converter.workers.inspect [--real-type TIPO] [--max-image-pixels N] <nombre> <ruta>

Ejecuta el inspector registrado como `<nombre>` en `detection/inspectors/` sobre el archivo
`<ruta>` y escribe en stdout su resultado en JSON, con la forma de `dump_inspector_output`:
`{"info": …}` o `{"rejection": {"reason": …, "params": …}}`. `--real-type` es el Tipo_Real que
determinó el Detector_de_Tipo y `--max-image-pixels`, el Límite_de_Píxeles; si se omite, vale el
predeterminado de `CONVERTER_MAX_IMAGE_PIXELS`. El proceso principal lo lanza a través del
Ejecutor aislado (`detection/inspect_runner.py`).

- **Solo el resultado va a stdout.** Antes de ejecutar el inspector, el worker guarda un
  duplicado de stdout y apunta el descriptor 1 a stderr. Lo que impriman el inspector o sus
  bibliotecas, incluido código en C, va a stderr y no puede mezclarse con el resultado.
- **Líneas acotadas.** El resultado es JSON solo ASCII y sin saltos de línea. Si mide más de
  `OUTPUT_LINE_CHARS` caracteres, se escribe partido en líneas de ese largo, porque el Ejecutor
  aislado corta las líneas más largas que `runner.MAX_LINE_BYTES`. Quien lo lee concatena las
  líneas sin separador; un resultado corto ocupa una sola línea.
- **Códigos de salida.** 0 si escribió el resultado; 2 si los argumentos son inválidos o el
  inspector no está registrado, con un mensaje en stderr y sin resultado; 1 si el inspector
  lanzó una excepción, con el traceback en stderr. El proceso principal sanea esa salida antes
  de registrarla: puede contener datos del archivo.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from converter.config import VARIABLES
from converter.detection.inspectors import (
    InspectorInput,
    InspectorSpec,
    UnknownInspectorError,
    get_inspector,
)
from converter.detection.source_info import SourceInfo, SourceRejection, dump_inspector_output

MODULE = "converter.workers.inspect"  # para `python -m`
OUTPUT_LINE_CHARS = 32_768  # menor que `runner.MAX_LINE_BYTES`; el resultado es solo ASCII

EXIT_OK = 0
EXIT_USAGE = 2  # el código de argparse para argumentos inválidos

_REAL_TYPE = "--real-type"
_MAX_IMAGE_PIXELS = "--max-image-pixels"
_SEPARATOR = "--"

# Predeterminado de la Variable_de_Configuración del Límite_de_Píxeles
DEFAULT_MAX_IMAGE_PIXELS = {spec.name: spec.default for spec in VARIABLES}[
    "CONVERTER_MAX_IMAGE_PIXELS"
]


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerArgs:
    """Argumentos de una invocación del worker."""

    name: str  # nombre del inspector registrado
    path: Path  # Archivo_de_Origen
    real_type: str | None = None  # Tipo_Real
    max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS  # Límite_de_Píxeles


def _positive_int(raw: str) -> int:
    # Solo dígitos ASCII: `int()` aceptaría además signos, espacios, "_" y dígitos no ASCII
    if not (raw.isascii() and raw.isdigit()) or int(raw) < 1:
        raise argparse.ArgumentTypeError(f"debe ser un entero positivo: {raw!r}")
    return int(raw)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m {MODULE}",
        description="Ejecuta un inspector del Detector_de_Tipo y escribe su resultado en JSON.",
        allow_abbrev=False,
    )
    parser.add_argument(_REAL_TYPE, dest="real_type", default=None, help="Tipo_Real del archivo")
    parser.add_argument(
        _MAX_IMAGE_PIXELS,
        dest="max_image_pixels",
        type=_positive_int,
        default=DEFAULT_MAX_IMAGE_PIXELS,
        help="Límite_de_Píxeles",
    )
    parser.add_argument("name", help="nombre del inspector registrado")
    parser.add_argument("path", type=Path, help="archivo que se inspecciona")
    return parser


def parse_args(args: Sequence[str]) -> WorkerArgs:
    """Interpreta los argumentos del worker. Lanza `SystemExit(2)` si son inválidos."""
    parsed = _parser().parse_args(list(args))
    return WorkerArgs(
        name=parsed.name,
        path=parsed.path,
        real_type=parsed.real_type,
        max_image_pixels=parsed.max_image_pixels,
    )


def format_args(args: WorkerArgs) -> list[str]:
    """Argumentos del worker para `args`; `parse_args(format_args(args)) == args`.

    Los argumentos posicionales van después de "--", así una ruta o un nombre que empiecen con
    "-" no se toman como opciones.
    """
    options: list[str] = []
    if args.real_type is not None:
        options += [_REAL_TYPE, args.real_type]
    options += [_MAX_IMAGE_PIXELS, str(args.max_image_pixels)]
    return [*options, _SEPARATOR, args.name, str(args.path)]


def run_inspector(spec: InspectorSpec, args: WorkerArgs) -> SourceInfo | SourceRejection:
    """Ejecuta el inspector `spec` sobre el archivo de `args`."""
    source = InspectorInput(
        path=args.path, real_type=args.real_type, max_image_pixels=args.max_image_pixels
    )
    return spec.function(source)


def write_output(text: str, stream: TextIO) -> None:
    """Escribe `text` en líneas de hasta `OUTPUT_LINE_CHARS` caracteres y vacía `stream`."""
    for start in range(0, len(text), OUTPUT_LINE_CHARS):
        stream.write(text[start : start + OUTPUT_LINE_CHARS] + "\n")
    stream.flush()


def _claim_stdout() -> TextIO:
    """Devuelve un flujo sobre el stdout original y apunta el descriptor 1 a stderr."""
    sys.stdout.flush()
    result_fd = os.dup(1)
    os.dup2(2, 1)
    return os.fdopen(result_fd, "w", encoding="ascii", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Ejecuta el worker y devuelve su código de salida.

    Una excepción del inspector se propaga: el intérprete escribe el traceback en stderr y
    termina con el código 1.
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        spec = get_inspector(args.name)
    except UnknownInspectorError as exc:
        sys.stderr.write(f"{MODULE}: {exc}\n")
        return EXIT_USAGE
    result_stream = _claim_stdout()
    with result_stream:
        text = dump_inspector_output(run_inspector(spec, args))
        write_output(text, result_stream)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
