"""Propiedad 34: los nombres de página del ZIP se ordenan como las páginas (Req 8.8).

El Nombre_Base normalizado de entrada sale de aplicar `split_name` y `normalize_base` a un
Nombre_Original, como hace PDF a PNG; la Propiedad 58 ya prueba esas funciones. El oráculo arma el
nombre esperado con su propia copia de Req 8.8 y no usa `page_entry_name`.

Con hasta 1000 páginas se prueban todas; con más, una muestra que incluye la primera, la última y
las vecinas de cada potencia de 10, donde cambia la cantidad de dígitos del Número_de_Página.
"""

from __future__ import annotations

from itertools import pairwise

import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from converter.naming import normalize_base, page_entry_name, split_name
from tests.strategies.names import original_names

_PAGE_EXTENSION = ".png"
_FULL_RANGE_MAX = 1000
_MAX_TOTAL = 10**12

# Totales vecinos de cada potencia de 10, de 9 a 1 000 000 001 páginas
_DIGIT_BOUNDARIES: tuple[int, ...] = tuple(
    10**exponent + delta for exponent in range(1, 10) for delta in (-1, 0, 1)
)


def _normalized_base(original: str) -> str:
    base, _ = split_name(original)
    return normalize_base(base)


def _totals() -> st.SearchStrategy[int]:
    """Totales de páginas T ≥ 2, con más peso en los que cambian la cantidad de dígitos."""
    return st.one_of(
        st.integers(2, _FULL_RANGE_MAX),
        st.sampled_from(_DIGIT_BOUNDARIES),
        st.integers(2, _MAX_TOTAL),
    )


@st.composite
def _documents(draw: st.DrawFn) -> tuple[int, list[int]]:
    """Un total de páginas y Números_de_Página distintos de 1 a ese total, en orden creciente."""
    total = draw(_totals())
    if total <= _FULL_RANGE_MAX:
        return total, list(range(1, total + 1))
    edges = {1, 2, total - 1, total}
    edges |= {
        10**exponent + delta for exponent in range(1, len(str(total)) + 1) for delta in (-1, 0, 1)
    }
    sampled = draw(st.lists(st.integers(1, total), max_size=50))
    return total, sorted(page for page in edges | set(sampled) if 1 <= page <= total)


def _expected_name(base: str, page: int, total: int) -> str:
    # Req 8.8: Nombre_Base normalizado, "_", Número_de_Página con ceros a la izquierda hasta los
    # dígitos del total y ".png"
    digits = str(page)
    return f"{base}_{'0' * (len(str(total)) - len(digits))}{digits}{_PAGE_EXTENSION}"


# Feature: file-converter, Property 34: Los nombres de página del ZIP se ordenan como las páginas
@pytest.mark.req("8.8")
@given(base=original_names().map(_normalized_base), document=_documents())
def test_page_entry_names_sort_like_pages(base: str, document: tuple[int, list[int]]) -> None:
    total, pages = document
    names = [page_entry_name(base, page, total) for page in pages]

    for page, name in zip(pages, names, strict=True):
        assert name == _expected_name(base, page, total), (page, total, name)

    # Los Números_de_Página están en orden creciente: si cada nombre es menor que el siguiente,
    # el orden lexicográfico de los nombres es el de las páginas y no hay nombres repetidos. Con
    # el mismo Nombre_Base, comparar por punto de código o por bytes UTF-8 da el mismo orden.
    for (page, name), (next_page, next_name) in pairwise(zip(pages, names, strict=True)):
        assert name < next_name, (page, name, next_page, next_name)

    event(f"dígitos del total: {len(str(total))}")
    if total <= _FULL_RANGE_MAX:
        event("todas las páginas")
    if total in _DIGIT_BOUNDARIES:
        event("total vecino de una potencia de 10")
