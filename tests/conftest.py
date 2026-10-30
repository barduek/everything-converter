"""Configuración común de pytest: perfiles de Hypothesis y marcas del proyecto."""

from __future__ import annotations

import os
import re
import shutil

import pytest
from hypothesis import settings

# Perfiles de Hypothesis. `ci` es el predeterminado; `nightly` hereda de `ci` y sube los ejemplos.
settings.register_profile("ci", max_examples=100, deadline=None)
settings.register_profile("nightly", parent=settings.get_profile("ci"), max_examples=1000)
# Un valor vacío o ausente usa `ci`; un nombre no registrado hace fallar la carga
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE") or "ci")

# Identificador de criterio de aceptación: "Requisito.Criterio", por ejemplo "20.3"
_REQ_ID = re.compile(r"[1-9][0-9]*\.[1-9][0-9]*")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        'req(*ids): criterios de requirements.md que cubre la prueba, por ejemplo req("20.3")',
    )
    config.addinivalue_line(
        "markers",
        "integration: prueba de integración que usa herramientas o la imagen del Conversor",
    )
    config.addinivalue_line(
        "markers",
        "requires_tool(*names): omite la prueba si alguna herramienta no está en el PATH, "
        'por ejemplo requires_tool("ffmpeg")',
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Las marcas mal escritas cortan la recolección para no perder trazabilidad en silencio
    errors: list[str] = []
    for item in items:
        for marker in item.iter_markers(name="req"):
            if not marker.args:
                errors.append(f"{item.nodeid}: req() necesita al menos un criterio")
            for req_id in marker.args:
                if not isinstance(req_id, str) or _REQ_ID.fullmatch(req_id) is None:
                    errors.append(f"{item.nodeid}: criterio inválido en req(): {req_id!r}")
        for marker in item.iter_markers(name="requires_tool"):
            if not marker.args:
                errors.append(f"{item.nodeid}: requires_tool() necesita al menos un nombre")
            for name in marker.args:
                if not isinstance(name, str) or not name:
                    errors.append(
                        f"{item.nodeid}: herramienta inválida en requires_tool(): {name!r}"
                    )
    if errors:
        raise pytest.UsageError("Marcas inválidas:\n" + "\n".join(errors))


def pytest_runtest_setup(item: pytest.Item) -> None:
    # Fuera del contenedor faltan FFmpeg, LibreOffice, whisper-cli o exiftool: se omite la prueba
    missing = [
        name
        for marker in item.iter_markers(name="requires_tool")
        for name in marker.args
        if shutil.which(name) is None
    ]
    if missing:
        pytest.skip(f"herramienta no disponible en el PATH: {', '.join(missing)}")
