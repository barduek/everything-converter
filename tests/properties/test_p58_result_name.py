"""Propiedad 58: el nombre del Archivo_Resultado cumple las reglas de normalización (Req 15.2 a
15.6, 15.10, 15.12).

El oráculo divide el Nombre_Original y normaliza el Nombre_Base con su propia copia del glosario
y del Requisito 15, sin usar `split_name`, `truncate_utf8` ni `normalize_base`, para que un error
de naming.py haga fallar la prueba en lugar de pasar al valor esperado.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from converter.naming import ResultKind, normalize_base, result_name
from tests.strategies.names import FORBIDDEN_CHARS, original_names

_FORBIDDEN = frozenset(FORBIDDEN_CHARS)
_MAX_BASE_BYTES = 200
_EMPTY_BASE = "resultado"
_ZIP = ".zip"


@dataclass(frozen=True)
class _Target:
    """Lo que `result_name` usa del Formato_de_Destino."""

    name: str
    primary_extension: str


# Extensiones_Principales del Mapa_de_Conversión_Inicial, copiadas del glosario
_TARGETS: tuple[_Target, ...] = tuple(
    _Target(name, extension)
    for name, extension in (
        ("PNG", ".png"),
        ("JPG", ".jpg"),
        ("WEBP", ".webp"),
        ("PDF", ".pdf"),
        ("TXT", ".txt"),
        ("MD", ".md"),
        ("HTML", ".html"),
        ("DOCX", ".docx"),
        ("MP3", ".mp3"),
        ("WAV", ".wav"),
        ("OGG", ".ogg"),
        ("MP4", ".mp4"),
        ("WEBM", ".webm"),
        ("SRT", ".srt"),
    )
)
_PNG = _TARGETS[0]


@st.composite
def _destinations(draw: st.DrawFn) -> tuple[_Target, ResultKind]:
    # El ZIP solo se entrega en PDF a PNG con más de una página (Req 15.10)
    if draw(st.booleans()):
        return _PNG, "zip"
    return draw(st.sampled_from(_TARGETS)), "single"


def _base(original: str) -> str:
    # Glosario: lo anterior al último punto o, si no hay punto, el Nombre_Original completo
    dot = original.rfind(".")
    return original if dot == -1 else original[:dot]


def _replaced(base: str) -> str:
    # Req 15.3: cada Carácter_No_Permitido pasa a "_" y los demás quedan iguales
    return "".join("_" if ch in _FORBIDDEN else ch for ch in base)


def _longest_prefix(text: str, max_bytes: int) -> str:
    # Req 15.5: suma caracteres completos mientras el total no pase de `max_bytes`
    size = 0
    for index, ch in enumerate(text):
        size += len(ch.encode("utf-8"))
        if size > max_bytes:
            return text[:index]
    return text


def _expected_base(base: str) -> str:
    # Orden de Req 15.12: reemplazo, truncado y, si quedó vacío, "resultado" (Req 15.4)
    return _longest_prefix(_replaced(base), _MAX_BASE_BYTES) or _EMPTY_BASE


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


# Feature: file-converter, Property 58: El nombre del Archivo_Resultado cumple las reglas de normalización  # noqa: E501
@pytest.mark.req("15.2", "15.3", "15.4", "15.5", "15.6", "15.10", "15.12")
@given(original=original_names(), destination=_destinations())
def test_result_name_follows_normalization_rules(
    original: str, destination: tuple[_Target, ResultKind]
) -> None:
    target, kind = destination
    extension = _ZIP if kind == "zip" else target.primary_extension
    base = _base(original)
    name = result_name(original, target, kind)

    # Req 15.2 y 15.10: el Nombre_Base normalizado seguido de la Extensión_Principal o de ".zip"
    assert name == normalize_base(base) + extension
    assert name.endswith(extension), name
    stem = name.removesuffix(extension)

    # Req 15.6: sin Caracteres_No_Permitidos, con una parte anterior a la extensión no vacía y
    # de 200 bytes o menos en UTF-8
    assert _FORBIDDEN.isdisjoint(name), name
    assert stem
    assert _utf8_size(stem) <= _MAX_BASE_BYTES, _utf8_size(stem)

    # Req 15.3, 15.4, 15.5 y 15.12: coincide con la referencia
    assert stem == _expected_base(base)

    # Req 15.3: salvo la sustitución por "resultado", el nombre es un prefijo del Nombre_Base en
    # el que solo cambian los Caracteres_No_Permitidos; espacios, acentos y mayúsculas siguen
    # iguales, sin cambios de mayúsculas ni de forma Unicode
    if base:
        assert len(stem) <= len(base)
        for new, old in zip(stem, base[: len(stem)], strict=True):
            assert new == ("_" if old in _FORBIDDEN else old), (new, old)

    if kind == "zip":
        event("ZIP")
    if not base:
        event("Nombre_Base vacío")
    if not _FORBIDDEN.isdisjoint(base):
        event("con Caracteres_No_Permitidos")
    if _utf8_size(base) > _MAX_BASE_BYTES:
        # Cuando el truncado deja menos de 200 bytes, el corte cayó en medio de un carácter
        cut_inside = _utf8_size(stem) < _MAX_BASE_BYTES
        event("truncado en medio de un carácter" if cut_inside else "truncado justo en 200 bytes")
    elif _utf8_size(base) >= _MAX_BASE_BYTES - 4:
        event("de 196 a 200 bytes, sin truncar")
