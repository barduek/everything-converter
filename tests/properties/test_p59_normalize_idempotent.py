"""Propiedad 59: la Normalización_de_Nombre es idempotente (Req 15.7).

Un Nombre_Base es cualquier texto sin sustitutos sueltos, incluidos puntos, porque es lo anterior
al último punto del Nombre_Original. La prueba compara `normalize_base` consigo misma: que el
resultado sea además el correcto lo prueba la Propiedad 58 contra su propia referencia.

Además de los Nombres_Originales completos y de su parte anterior al último punto, se prueban
bases puntuales: "resultado", que es el valor que sustituye a la base vacía, y bases de 200 a 202
bytes en UTF-8, con el corte de los 200 bytes justo en el límite, en medio de un carácter
multibyte o sobre un Carácter_No_Permitido.

**Validates: Requirements 15.7**
"""

from __future__ import annotations

import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from converter.naming import normalize_base, split_name
from tests.strategies.names import FORBIDDEN_CHARS, original_names

_FORBIDDEN = frozenset(FORBIDDEN_CHARS)
_MAX_BASE_BYTES = 200
_MAX_BASE_CHARS = 300

# Bases que ya cumplen las reglas o que quedan en el borde del truncado
_SPECIAL_BASES: tuple[str, ...] = (
    "",
    "resultado",
    "RESULTADO",
    "_",
    "_" * _MAX_BASE_BYTES,
    "_" * (_MAX_BASE_BYTES + 1),
    "/" * (_MAX_BASE_BYTES + 1),
    "a" * (_MAX_BASE_BYTES - 1) + "/",
    "a" * (_MAX_BASE_BYTES - 1) + "/" + "b",
    "é" * (_MAX_BASE_BYTES // 2),
    "a" * (_MAX_BASE_BYTES - 1) + "é",
    "a" * (_MAX_BASE_BYTES - 2) + "€",
    "a" * (_MAX_BASE_BYTES - 2) + "😀",
    "Cafe\u0301" * 50,
    "\x00",
    "..",
)


def _base_of(original: str) -> str:
    base, _ = split_name(original)
    return base


def _bases() -> st.SearchStrategy[str]:
    """Nombres_Base de 0 a 300 caracteres, sin sustitutos sueltos."""
    return st.one_of(
        st.sampled_from(_SPECIAL_BASES),
        original_names(),
        original_names().map(_base_of),
        st.text(st.characters(codec="utf-8"), max_size=_MAX_BASE_CHARS),
    )


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


# Feature: file-converter, Property 59: La Normalización_de_Nombre es idempotente
@pytest.mark.req("15.7")
@given(base=_bases())
def test_normalize_base_is_idempotent(base: str) -> None:
    once = normalize_base(base)
    twice = normalize_base(once)

    assert twice == once, (base, once, twice)

    if not base:
        event("Nombre_Base vacío")
    if not _FORBIDDEN.isdisjoint(base):
        event("con Caracteres_No_Permitidos")
    if _utf8_size(base) > _MAX_BASE_BYTES:
        event("truncado")
    if once == base:
        event("sin cambios en la primera aplicación")
