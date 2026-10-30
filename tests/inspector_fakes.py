"""Inspectores de prueba para el registro de inspectores, el worker y `InspectRunner`.

`register_fakes()` registra el inspector `fake` y sus tipos de `SourceInfo`. Lo llaman las
pruebas, con los registros aislados, y el worker de prueba que escribe `worker_wrapper`, que
ejecuta `converter.workers.inspect` en un subproceso real con estos inspectores registrados.

El inspector `fake` lee del Archivo_de_Origen una orden en ASCII y se comporta según ella, así
una misma inspección cubre cada camino del worker y del ejecutor. Este módulo no importa pytest:
también lo importa el subproceso.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from converter.detection import source_info
from converter.detection.inspectors import InspectorInput, register_inspector
from converter.detection.inspectors import base as inspectors_base
from converter.detection.source_info import SourceInfo, SourceRejection, register_source_info
from converter.eventlog import Tool
from converter.messages_es import FailureReason

if TYPE_CHECKING:
    import pytest

FAKE = "fake"  # inspector de prueba
REPO_ROOT = Path(__file__).resolve().parent.parent

# Órdenes que entiende el inspector `fake`
INFO = "info"  # FakeInfo con lo que recibió el inspector
NOISY = "noisy"  # imprime en stdout desde Python y desde el descriptor 1 antes de responder
BIG = "big"  # FakeInfo de más de 64 KiB en JSON
DAMAGED = "damaged"  # rechazo archivo_danado
WRONG_TYPE = "wrong_type"  # una información de otro tipo
RAISE_DAMAGED = "raise_damaged"  # excepción con el texto de un archivo de imagen dañado
RAISE = "raise"  # excepción sin un patrón conocido
HANG = "hang"  # no termina por su cuenta
BIG_VALUES = 20_000  # valores de la orden BIG


@dataclass(frozen=True, slots=True)
class FakeInfo(SourceInfo):
    """Lo que recibió el inspector `fake`."""

    command: str
    path: str
    real_type: str | None
    max_image_pixels: int
    values: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class OtherInfo(SourceInfo):
    """Un tipo de SourceInfo que el inspector `fake` no declara."""


def fake_inspector(source: InspectorInput) -> SourceInfo | SourceRejection:
    command = source.path.read_text(encoding="ascii").strip()
    info = FakeInfo(
        command=command,
        path=str(source.path),
        real_type=source.real_type,
        max_image_pixels=source.max_image_pixels,
    )
    if command == INFO:
        return info
    if command == NOISY:
        print("ruido de Python en stdout", flush=True)
        os.write(1, b"ruido de C en el descriptor 1\n")
        return info
    if command == BIG:
        # 8 caracteres por valor: el JSON mide unos 160 KB, más que una línea del ejecutor
        return replace(info, values=big_values())
    if command == DAMAGED:
        return SourceRejection(FailureReason.DAMAGED)
    if command == WRONG_TYPE:
        return OtherInfo()
    if command == RAISE_DAMAGED:
        # El mensaje de UnidentifiedImageError de Pillow; el traceback lo cita en stderr
        raise OSError(f"cannot identify image file {str(source.path)!r}")
    if command == RAISE:
        raise RuntimeError("fallo del inspector de prueba")
    if command == HANG:
        time.sleep(120)
    raise ValueError(f"orden desconocida: {command!r}")


def big_values() -> tuple[int, ...]:
    """Valores de la orden BIG."""
    return tuple(range(1_000_000, 1_000_000 + BIG_VALUES))


def isolate_registries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Copia los registros de inspectores y de SourceInfo; `monkeypatch` restaura los originales."""
    monkeypatch.setattr(inspectors_base, "_REGISTRY", dict(inspectors_base._REGISTRY))
    monkeypatch.setattr(source_info, "_BY_NAME", dict(source_info._BY_NAME))
    monkeypatch.setattr(source_info, "_NAME_OF", dict(source_info._NAME_OF))


def register_fakes() -> None:
    """Registra el inspector `fake` y sus tipos de SourceInfo."""
    register_source_info("fake")(FakeInfo)
    register_source_info("fake_other")(OtherInfo)
    register_inspector(FAKE, info_type=FakeInfo, tool=Tool.PILLOW)(fake_inspector)


# Worker de prueba: registra los inspectores de este módulo y ejecuta el worker real con los
# argumentos que recibiría `python -I -m converter.workers.inspect`
_WORKER = """\
import sys
sys.path.insert(0, {root!r})
from tests.inspector_fakes import register_fakes
register_fakes()
from converter.workers.inspect import MODULE, main
if sys.argv[1:4] != ["-I", "-m", MODULE]:
    sys.exit("argumentos inesperados: " + repr(sys.argv[1:4]))
sys.exit(main(sys.argv[4:]))
"""


def worker_wrapper(directory: Path) -> Path:
    """Escribe en `directory` un ejecutable que reemplaza al intérprete del worker.

    `InspectRunner(python=...)` lo ejecuta como `<ejecutable> -I -m converter.workers.inspect
    <argumentos>`; el ejecutable corre el worker real con el inspector `fake` registrado.
    """
    script = directory / "fake_inspect_worker.py"
    script.write_text(_WORKER.format(root=str(REPO_ROOT)), encoding="utf-8")
    wrapper = directory / "fake-python"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper
