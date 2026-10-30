"""Pruebas de src/converter/languages.py: lectura de languages.es.json y resolución del
Idioma_de_Transcripción (Req 11.3, 11.9, 11.10, 13.8)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from converter.languages import (
    AUTO_LANGUAGE,
    DEFAULT_LANGUAGE,
    Language,
    LanguageCatalog,
    LanguageCatalogError,
)

LANGUAGES = [
    {"code": "en", "name": "inglés"},
    {"code": "es", "name": "español"},
    {"code": "haw", "name": "hawaiano"},
]


def _write(directory: Path, document: object) -> Path:
    path = directory / "languages.es.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.req("11.3")
def test_a_missing_file_gives_an_empty_catalog(tmp_path: Path) -> None:
    catalog = LanguageCatalog.load(tmp_path / "no-existe.json")

    assert len(catalog) == 0
    assert catalog.languages == ()
    assert "es" not in catalog


@pytest.mark.req("11.3")
def test_the_file_gives_codes_and_spanish_names_in_order(tmp_path: Path) -> None:
    catalog = LanguageCatalog.load(_write(tmp_path, LANGUAGES))

    assert catalog.codes() == ("en", "es", "haw")
    assert catalog.names() == ("inglés", "español", "hawaiano")
    assert catalog.languages[0] == Language("en", "inglés")
    assert catalog.name("haw") == "hawaiano"
    assert catalog.name("pt") is None
    assert "en" in catalog and "pt" not in catalog


@pytest.mark.req("11.9", "11.10", "13.8")
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, DEFAULT_LANGUAGE),  # sin elección: español
        (AUTO_LANGUAGE, AUTO_LANGUAGE),  # Detección_Automática_de_Idioma
        ("en", "en"),
        ("es", "es"),
        ("pt", None),  # no es Idioma_Soportado
        ("ES", None),  # la comparación es exacta
        (" es", None),
        ("AUTO", None),
        ("", None),
    ],
)
def test_resolve_gives_the_transcription_language(
    tmp_path: Path, value: str | None, expected: str | None
) -> None:
    catalog = LanguageCatalog.load(_write(tmp_path, LANGUAGES))

    assert catalog.resolve(value) == expected


@pytest.mark.req("11.10")
def test_an_empty_catalog_still_defaults_to_spanish_and_auto() -> None:
    catalog = LanguageCatalog()

    assert catalog.resolve(None) == "es"
    assert catalog.resolve("auto") == "auto"
    assert catalog.resolve("es") is None


@pytest.mark.parametrize(
    "content",
    [
        "no es JSON",
        json.dumps({"es": "español"}),  # un objeto, no una lista
        json.dumps([{"code": "es"}]),  # falta el nombre
        json.dumps([{"code": "es", "name": "español", "extra": 1}]),
        json.dumps([{"code": "es", "name": "español"}, {"code": "es", "name": "otro"}]),
        json.dumps([{"code": "es", "name": "español"}, {"code": "auto", "name": "auto"}]),
        json.dumps([{"code": "es", "name": "español"}, {"code": "EN", "name": "inglés"}]),
        json.dumps([{"code": "es", "name": "español"}, {"code": "en-us", "name": "inglés"}]),
        json.dumps([{"code": "es", "name": ""}]),
        json.dumps([{"code": "es", "name": "espa\u0000ñol"}]),
        json.dumps([{"code": "es", "name": "x" * 65}]),
        json.dumps([{"code": "es", "name": 3}]),
        json.dumps([{"code": "en", "name": "inglés"}]),  # sin español
    ],
    ids=[
        "no_json",
        "objeto",
        "sin_nombre",
        "clave_extra",
        "codigo_repetido",
        "codigo_auto",
        "mayusculas",
        "region",
        "nombre_vacio",
        "nombre_no_imprimible",
        "nombre_largo",
        "nombre_no_texto",
        "sin_espanol",
    ],
)
def test_an_invalid_file_is_rejected(tmp_path: Path, content: str) -> None:
    path = tmp_path / "languages.es.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(LanguageCatalogError):
        LanguageCatalog.load(path)


def test_a_file_that_is_not_utf8_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "languages.es.json"
    path.write_bytes('[{"code": "es", "name": "español"}]'.encode("cp1252"))

    with pytest.raises(LanguageCatalogError):
        LanguageCatalog.load(path)
