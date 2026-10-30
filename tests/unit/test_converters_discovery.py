"""Pruebas de src/converter/converters/__init__.py: descubrimiento de las familias de
convertidores con `pkgutil.walk_packages` (Req 4.3).

Las familias nuevas se arman como paquetes temporales con nombre único, así cada prueba importa
módulos frescos y no deja convertidores ni funciones de arranque registrados para las demás.
"""

from __future__ import annotations

import importlib
import io
import json
import subprocess
import sys
import textwrap
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import ModuleType

import pytest

import converter.converters as converters_package
from converter.converters import base, import_submodules, load_converters
from converter.converters.base import registered_converters, startup_hooks
from converter.eventlog import EventLog
from converter.registry.registry import ConversionRegistry, RegistrationError, RegistryErrorCode

# Módulos del paquete que ya existen; las familias que se agreguen se suman a estos
EXISTING_MODULES = frozenset(
    f"converter.converters.{name}"
    for name in (
        "docx",
        "docx.fonts",
        "errors",
        "media_rules",
        "orientation",
        "text_docs",
        "text_docs.dom",
        "text_docs.layout",
    )
)

_FAMILY = """\
from converter.converters.base import ConversionOutcome, register_converter


@register_converter("{converter_id}")
class Converter:
    def preflight(self, source, request, config):
        return None

    async def run(self, ctx):
        return ConversionOutcome(ctx.workspace.source_file, "single")
"""

_HOOK = """\
from converter.converters.base import on_startup


@on_startup
def prepare(ctx):
    return None
"""

_FAILS = 'raise RuntimeError("este módulo no debe importarse")\n'

type MakePackage = Callable[[Mapping[str, str]], ModuleType]


@pytest.fixture
def make_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[MakePackage]:
    """Crea un paquete importable con los archivos dados (ruta relativa → contenido)."""
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(base, "_CONVERTERS", dict(base._CONVERTERS))
    monkeypatch.setattr(base, "_STARTUP_HOOKS", list(base._STARTUP_HOOKS))
    created: list[str] = []

    def make(files: Mapping[str, str]) -> ModuleType:
        name = f"fake_families_{uuid.uuid4().hex}"
        for relative, text in {"__init__.py": "", **files}.items():
            path = tmp_path / name / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        importlib.invalidate_caches()
        created.append(name)
        return importlib.import_module(name)

    yield make
    for module in list(sys.modules):
        if any(module == name or module.startswith(f"{name}.") for name in created):
            del sys.modules[module]


@pytest.mark.req("4.3")
def test_load_converters_imports_every_module_of_the_package() -> None:
    converters = load_converters()
    names = import_submodules(converters_package)

    assert set(names) >= EXISTING_MODULES
    assert "converter.converters.base" not in names
    assert all(name in sys.modules for name in names)
    assert dict(converters) == dict(registered_converters())
    # Una segunda carga no vuelve a registrar nada
    assert dict(load_converters()) == dict(converters)


@pytest.mark.req("4.3")
def test_a_new_family_module_registers_without_core_changes(make_package: MakePackage) -> None:
    package = make_package(
        {
            "copy.py": _FAMILY.format(converter_id="fake.copy") + "\n" + _HOOK,
            "nested/__init__.py": "",
            "nested/upper.py": _FAMILY.format(converter_id="fake.upper"),
            "nested/_helpers.py": _FAILS,
            "_private.py": _FAILS,
            "base.py": _FAILS,
        }
    )

    names = import_submodules(package)

    prefix = package.__name__
    assert names == (f"{prefix}.copy", f"{prefix}.nested", f"{prefix}.nested.upper")
    assert list(registered_converters())[-2:] == ["fake.copy", "fake.upper"]
    assert startup_hooks()[-1] is sys.modules[f"{prefix}.copy"].prepare
    # El Registro_de_Conversiones acepta pares del convertidor nuevo y rechaza los de uno que
    # no está registrado
    registry = ConversionRegistry(registered_converters(), event_log=EventLog(io.StringIO()))
    for name, hex_signature in (("AAA", "414141"), ("BBB", "424242"), ("CCC", "434343")):
        registry.register_format(
            {
                "name": name,
                "category": "documento",
                "extensions": [f".{name.lower()}"],
                "primary_extension": f".{name.lower()}",
                "mime_type": "application/octet-stream",
                "signature": {"magic": [{"offset": 0, "hex": hex_signature}]},
            }
        )
    pair = {"source": "AAA", "target": "BBB", "loss_class": "Sin_Pérdida"}
    assert registry.register_pair({**pair, "converter": "fake.upper"}).converter_id == "fake.upper"
    with pytest.raises(RegistrationError) as error:
        registry.register_pair({**pair, "target": "CCC", "converter": "fake.missing"})
    assert error.value.code is RegistryErrorCode.UNKNOWN_CONVERTER


def test_a_module_that_cannot_be_imported_stops_the_discovery(make_package: MakePackage) -> None:
    package = make_package({"broken.py": "import modulo_que_no_existe\n"})
    with pytest.raises(ModuleNotFoundError):
        import_submodules(package)

    package = make_package({"broken/__init__.py": _FAILS})
    with pytest.raises(RuntimeError, match="no debe importarse"):
        import_submodules(package)


def test_importing_one_module_does_not_load_the_families() -> None:
    # En un intérprete nuevo, sin módulos ya importados por otras pruebas
    script = textwrap.dedent(
        """
        import json, sys
        import converter.converters.orientation
        before = sorted(m for m in sys.modules if m.startswith("converter.converters."))
        from converter.converters import load_converters
        converters = load_converters()
        after = sorted(m for m in sys.modules if m.startswith("converter.converters."))
        print(json.dumps({"before": before, "after": after, "converters": sorted(converters)}))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)

    assert result["before"] == ["converter.converters.base", "converter.converters.orientation"]
    assert set(result["after"]) >= EXISTING_MODULES
