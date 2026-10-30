"""Prueba de humo: el paquete `converter` y sus subpaquetes se importan."""

import importlib

import pytest

SUBPACKAGES = [
    "api",
    "registry",
    "catalog",
    "detection",
    "tasks",
    "storage",
    "sandbox",
    "converters",
    "workers",
    "srt",
]


def test_converter_package_imports() -> None:
    converter = importlib.import_module("converter")
    # Es un paquete (tiene __path__), no un módulo suelto
    assert hasattr(converter, "__path__")


@pytest.mark.parametrize("subpackage", SUBPACKAGES)
def test_subpackage_imports(subpackage: str) -> None:
    importlib.import_module(f"converter.{subpackage}")
