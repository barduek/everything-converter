"""Lanzador de subprocesos con límites de recursos (Req 13.7, 14.6).

Uso:

    python -m converter.sandbox.launch --as-mb N --cpu-s N --fsize-mb N [--nofile N] -- <argv>

Fija `RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_FSIZE` y `RLIMIT_NOFILE` con `resource.setrlimit` y
reemplaza el proceso por `argv` con `os.execvp`. El Ejecutor aislado lo usa en lugar de
`preexec_fn`, que no es seguro con hilos.

- `--as-mb` y `--fsize-mb` se expresan en MB (1 MB = 1 000 000 bytes, como en las
  Variables_de_Configuración), `--cpu-s` en segundos de CPU y `--nofile` en descriptores de
  archivo. Si se omite `--nofile` vale 256, el predeterminado de `Limits.open_files`.
- Cada límite se fija como blando y como duro, para que el programa no pueda volver a subirlo.
  Si el límite duro heredado es menor que el pedido, se usa el heredado: el lanzador nunca
  amplía un límite.
- Con el límite blando de `RLIMIT_CPU` igual al duro, Linux envía SIGKILL al agotarlo. El
  tiempo de CPU del propio lanzador (unos milisegundos) cuenta dentro de ese límite.
- `exec` conserva el PID, la sesión y el grupo de procesos, así que las señales de cancelación
  del Ejecutor aislado llegan al programa real y a sus hijos (Req 14.6).
- El intérprete de Python ignora SIGPIPE y SIGXFSZ al iniciar, y `exec` hereda las señales
  ignoradas. El lanzador las devuelve a su acción predeterminada antes de `exec`, como hace
  `subprocess` con `restore_signals=True`. Sin eso, un programa que supera `RLIMIT_FSIZE`
  recibiría EFBIG en cada escritura en lugar de terminar por SIGXFSZ.
- Falla cerrado: si un límite no puede fijarse, el programa no se ejecuta. La única excepción es
  `RLIMIT_AS` en macOS, que solo se usa en desarrollo: macOS rechaza con EINVAL un valor menor
  que el espacio de direcciones que el proceso ya reservó, que en arm64 supera los 2 GB desde
  el inicio. Ahí el lanzador omite ese límite. La Imagen_de_Contenedor corre en Linux, donde
  se fijan los cuatro.

Códigos de salida propios, los mismos que usan `env` y `timeout` de coreutils: 125 si los
argumentos son inválidos o un límite no puede fijarse, 126 si el programa existe pero no puede
ejecutarse y 127 si no se encuentra. En los demás casos el código de salida es el del programa.
Los mensajes del lanzador van a stderr con el prefijo `converter.sandbox.launch:`.
"""

from __future__ import annotations

import os
import resource
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NoReturn

MODULE = "converter.sandbox.launch"  # para `python -m`
BYTES_PER_MB = 1_000_000  # igual que `converter.config.BYTES_PER_MB`
DEFAULT_OPEN_FILES = 256  # `Limits.open_files` del diseño

EXIT_LAUNCH_ERROR = 125
EXIT_CANNOT_EXECUTE = 126
EXIT_NOT_FOUND = 127

SEPARATOR = "--"
_AS_MB = "--as-mb"
_CPU_S = "--cpu-s"
_FSIZE_MB = "--fsize-mb"
_NOFILE = "--nofile"
_REQUIRED = (_AS_MB, _CPU_S, _FSIZE_MB)
_OPTIONS = (*_REQUIRED, _NOFILE)

# Señales que Python ignora al iniciar y que `exec` heredaría ignoradas
_RESTORED_SIGNALS = (signal.SIGPIPE, signal.SIGXFSZ)


class UsageError(ValueError):
    """Los argumentos del lanzador no tienen la forma documentada."""


@dataclass(frozen=True, slots=True, kw_only=True)
class LaunchSpec:
    """Límites y programa de una invocación del lanzador."""

    address_space_mb: int  # RLIMIT_AS
    cpu_seconds: int  # RLIMIT_CPU
    file_size_mb: int  # RLIMIT_FSIZE
    open_files: int = DEFAULT_OPEN_FILES  # RLIMIT_NOFILE
    argv: tuple[str, ...]  # programa y sus argumentos; argv[0] se busca en el PATH


def _positive_int(option: str, raw: str) -> int:
    # Solo dígitos ASCII: `int()` aceptaría además signos, espacios, "_" y dígitos no ASCII
    if not (raw.isascii() and raw.isdigit()):
        raise UsageError(f"{option} debe ser un entero positivo: {raw!r}")
    value = int(raw)
    if value < 1:
        raise UsageError(f"{option} debe ser un entero positivo: {raw!r}")
    return value


def parse_args(args: Sequence[str]) -> LaunchSpec:
    """Interpreta `--as-mb N --cpu-s N --fsize-mb N [--nofile N] -- <argv>`.

    Las opciones van antes del primer "--", cada una una sola vez y en cualquier orden. Todo lo
    que sigue al primer "--" es el programa con sus argumentos, sin interpretar.
    """
    args = list(args)
    if SEPARATOR not in args:
        raise UsageError(f"falta {SEPARATOR!r} antes del programa")
    split = args.index(SEPARATOR)
    options, argv = args[:split], tuple(args[split + 1 :])
    if not argv:
        raise UsageError(f"falta el programa después de {SEPARATOR!r}")

    values: dict[str, int] = {}
    for i in range(0, len(options), 2):
        option = options[i]
        if option not in _OPTIONS:
            raise UsageError(f"opción desconocida: {option!r}")
        if option in values:
            raise UsageError(f"opción repetida: {option}")
        if i + 1 == len(options):
            raise UsageError(f"falta el valor de {option}")
        values[option] = _positive_int(option, options[i + 1])

    missing = [option for option in _REQUIRED if option not in values]
    if missing:
        raise UsageError(f"faltan opciones: {', '.join(missing)}")
    return LaunchSpec(
        address_space_mb=values[_AS_MB],
        cpu_seconds=values[_CPU_S],
        file_size_mb=values[_FSIZE_MB],
        open_files=values.get(_NOFILE, DEFAULT_OPEN_FILES),
        argv=argv,
    )


def format_args(spec: LaunchSpec) -> list[str]:
    """Argumentos del lanzador para `spec`; `parse_args(format_args(spec)) == spec`."""
    return [
        _AS_MB,
        str(spec.address_space_mb),
        _CPU_S,
        str(spec.cpu_seconds),
        _FSIZE_MB,
        str(spec.file_size_mb),
        _NOFILE,
        str(spec.open_files),
        SEPARATOR,
        *spec.argv,
    ]


def _set_limit(resource_id: int, value: int) -> None:
    _, hard = resource.getrlimit(resource_id)
    # En Linux RLIM_INFINITY vale -1 en Python: `min` lo tomaría como el menor
    if hard != resource.RLIM_INFINITY:
        value = min(value, hard)
    resource.setrlimit(resource_id, (value, value))


def apply_limits(spec: LaunchSpec) -> None:
    """Fija los cuatro límites del proceso actual como blandos y duros.

    Lanza `ValueError`, `OverflowError` u `OSError` si alguno no puede fijarse.
    """
    try:
        _set_limit(resource.RLIMIT_AS, spec.address_space_mb * BYTES_PER_MB)
    except ValueError:
        # Ver el docstring del módulo: macOS no admite valores realistas de RLIMIT_AS
        if sys.platform != "darwin":
            raise
    _set_limit(resource.RLIMIT_CPU, spec.cpu_seconds)
    _set_limit(resource.RLIMIT_FSIZE, spec.file_size_mb * BYTES_PER_MB)
    _set_limit(resource.RLIMIT_NOFILE, spec.open_files)


def _restore_signals() -> None:
    for signum in _RESTORED_SIGNALS:
        signal.signal(signum, signal.SIG_DFL)


def _fail(code: int, message: str) -> NoReturn:
    sys.stderr.write(f"{MODULE}: {message}\n")
    sys.stderr.flush()
    sys.exit(code)


def main(args: Sequence[str] | None = None) -> NoReturn:
    """Fija los límites y reemplaza el proceso por el programa; solo vuelve si falla."""
    try:
        spec = parse_args(sys.argv[1:] if args is None else args)
    except UsageError as exc:
        _fail(EXIT_LAUNCH_ERROR, f"argumentos inválidos: {exc}")
    try:
        apply_limits(spec)
    except (ValueError, OverflowError, OSError) as exc:
        _fail(EXIT_LAUNCH_ERROR, f"no se pudieron fijar los límites: {exc}")
    _restore_signals()
    program = spec.argv[0]
    try:
        os.execvp(program, spec.argv)
    except FileNotFoundError:
        _fail(EXIT_NOT_FOUND, f"no se encontró el programa {program!r}")
    except OSError as exc:
        _fail(EXIT_CANNOT_EXECUTE, f"no se pudo ejecutar {program!r}: {exc.strerror}")


if __name__ == "__main__":
    main()
