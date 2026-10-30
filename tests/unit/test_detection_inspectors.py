"""Pruebas del registro y el descubrimiento de inspectores en src/converter/detection/inspectors/
(Req 2.11, 4.4)."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from converter.detection import inspectors
from converter.detection.inspectors import (
    InspectorInput,
    UnknownInspectorError,
    get_inspector,
    register_inspector,
    registered_inspectors,
)
from converter.detection.source_info import SourceInfo, SourceRejection
from converter.eventlog import Tool
from converter.messages_es import FailureReason
from converter.sandbox.limits import SandboxTool
from tests.inspector_fakes import FakeInfo, isolate_registries


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate_registries(monkeypatch)


def _damaged(source: InspectorInput) -> SourceInfo | SourceRejection:
    return SourceRejection(FailureReason.DAMAGED)


@pytest.mark.req("4.4")
def test_register_inspector_adds_an_inspector_by_name() -> None:
    before = registered_inspectors()
    decorated = register_inspector(
        "sample", info_type=FakeInfo, tool=Tool.FFPROBE, sandbox_tool=SandboxTool.FFMPEG
    )(_damaged)

    assert decorated is _damaged
    assert registered_inspectors() == (*before, "sample")
    spec = get_inspector("sample")
    assert (spec.name, spec.function, spec.info_type) == ("sample", _damaged, FakeInfo)
    assert (spec.tool, spec.sandbox_tool) == (Tool.FFPROBE, SandboxTool.FFMPEG)
    # Sin herramienta declarada, y con los límites de Python
    register_inspector("plain", info_type=FakeInfo)(_damaged)
    assert (get_inspector("plain").tool, get_inspector("plain").sandbox_tool) == (
        None,
        SandboxTool.PYTHON,
    )


def test_register_inspector_rejects_invalid_registrations() -> None:
    register_inspector("sample", info_type=FakeInfo)(_damaged)
    with pytest.raises(ValueError, match="ya hay un inspector"):
        register_inspector("sample", info_type=FakeInfo)(_damaged)
    for name in ["", "Image", "1image", "image-info", " image", "-x"]:
        with pytest.raises(ValueError, match="nombre de inspector inválido"):
            register_inspector(name, info_type=FakeInfo)
    with pytest.raises(TypeError, match="subclase de SourceInfo"):
        register_inspector("plain", info_type=dict)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        register_inspector("tool", info_type=FakeInfo, tool="no_existe")  # type: ignore[arg-type]
    assert registered_inspectors()[-1] == "sample"


@pytest.mark.req("16.9")
def test_unknown_inspector_raises() -> None:
    with pytest.raises(UnknownInspectorError, match="no_existe") as error:
        get_inspector("no_existe")
    assert error.value.name == "no_existe"
    assert isinstance(error.value, LookupError)


@pytest.fixture
def extra_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # Un directorio más en el paquete: el descubrimiento importa también sus módulos
    monkeypatch.setattr(inspectors, "__path__", [*inspectors.__path__, str(tmp_path)])
    yield tmp_path
    for name in list(sys.modules):
        if name.startswith(f"{inspectors.__name__}.discovered"):
            del sys.modules[name]
    importlib.invalidate_caches()


@pytest.mark.req("4.4")
def test_discovery_imports_every_module_but_base_and_private_ones(extra_modules: Path) -> None:
    register = (
        "from converter.detection.inspectors import register_inspector\n"
        "from tests.inspector_fakes import FakeInfo, fake_inspector\n"
        "register_inspector({name!r}, info_type=FakeInfo)(fake_inspector)\n"
    )
    (extra_modules / "discovered_b.py").write_text(register.format(name="discovered_b"))
    (extra_modules / "discovered_a.py").write_text(register.format(name="discovered_a"))
    (extra_modules / "_discovered_private.py").write_text("raise AssertionError('privado')\n")
    importlib.invalidate_caches()

    inspectors._discover()

    # En orden por nombre de módulo; `base` ya estaba importado y no se vuelve a registrar
    assert registered_inspectors()[-2:] == ("discovered_a", "discovered_b")


@pytest.mark.req("2.11")
def test_inspector_modules_import_no_heavy_libraries() -> None:
    # El proceso principal y cada worker importan todos los inspectores: sus bibliotecas se
    # importan dentro de la función del inspector
    code = (
        "import json, sys\n"
        "import converter.workers.inspect, converter.detection.inspect_runner\n"
        "heavy = ('PIL', 'pypdfium2', 'lxml', 'olefile', 'weasyprint')\n"
        "print(json.dumps([name for name in heavy if name in sys.modules]))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60, check=True
    )
    assert json.loads(result.stdout) == []
