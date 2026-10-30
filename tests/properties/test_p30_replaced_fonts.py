"""Propiedad 30: las fuentes informadas como reemplazadas son las que faltan (Req 7.4).

`missing_fonts` recibe las fuentes referenciadas por un DOCX, las incrustadas y las instaladas, y
devuelve las que se informan como reemplazadas. El oráculo no usa
`converter.converters.docx.fonts`: compara cada par de nombres con la coincidencia sin distinguir
mayúsculas que define Unicode (`str.casefold`) y agrupa las grafías de cada fuente recorriendo
listas. Además del conjunto exacto, verifica lo que hace que el aviso no dependa del
orden de los conjuntos: una sola grafía por fuente faltante, la menor según los puntos de código,
y el orden sin distinguir mayúsculas.

Los nombres mezclan mayúsculas y minúsculas, letras que `casefold()` iguala y `lower()` no (ſ, ß,
ẞ, ﬁ, ς, µ, ŉ), letras cherokee, que `casefold()` pasa a mayúscula y `lower()` a minúscula, y
texto Unicode arbitrario con sus variantes de mayúsculas. Hay grafías distintas de una misma
fuente en un mismo conjunto y fuentes que están en dos o tres conjuntos a la vez.

Las mismas fuentes se escriben además en un DOCX pequeño armado a mano, en la tabla de fuentes y
en runs del documento principal, y las faltantes se calculan con lo que lee
`read_document_fonts`, como hace `find_replacements` antes de consultar fontconfig. fc-list y
fc-match no se ejecutan: las fuentes instaladas son las generadas.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from xml.sax.saxutils import quoteattr

import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from converter.converters.docx.fonts import missing_fonts, read_document_fonts

# Grafías intercambiables de una posición del nombre: todas tienen el mismo casefold(). En ſ, ß, ẞ,
# ﬁ, ς, µ, ŉ y ꭰ, lower() no da lo mismo que casefold(). En el cherokee, casefold() da la mayúscula
_LETTERS: tuple[tuple[str, ...], ...] = (
    ("a", "A"),
    ("e", "E"),
    ("r", "R"),
    ("t", "T"),
    ("é", "É"),
    ("s", "S", "ſ"),  # s larga
    ("ss", "SS", "Ss", "sS", "ß", "ẞ"),  # con "s" + "s", dos nombres de forma distinta coinciden
    ("fi", "FI", "Fi", "fI", "ﬁ"),  # ligadura fi
    ("σ", "Σ", "ς"),  # sigma final
    ("μ", "Μ", "µ"),  # signo micro
    ("ʼn", "ʼN", "ŉ"),  # n precedida de apóstrofo
    ("Ꭰ", "ꭰ"),  # cherokee
    ("ǆ", "ǅ", "Ǆ"),  # dígrafo con forma de título
    ("k", "K", "\u212a"),  # signo kelvin
)
# Solo las grafías de `_LETTERS` en las que lower() y casefold() difieren. Sin una grafía ASCII en
# la posición, la menor según los puntos de código es una de estas, así que llegan a los nombres
# informados. Ordenar con lower() da otro orden que con casefold(): "t" < "ß" < "ﬁ" con lower() y
# "fi" < "ss" < "t" con casefold().
_SPECIAL_LETTERS: tuple[tuple[str, ...], ...] = (
    ("ſ",),
    ("ß", "ẞ"),
    ("ﬁ",),
    ("ς",),
    ("µ",),
    ("ŉ",),
    ("ꭰ",),
)
# Cerca de la mitad de las posiciones lleva una letra especial
_LETTER = st.one_of(st.sampled_from(_LETTERS), st.sampled_from(_SPECIAL_LETTERS))
# Separadores entre letras; la cadena vacía aparece dos veces para que sean menos frecuentes
_SEPARATORS = ("", "", " ", "-")
# Letras, marcas y números: son válidos en XML y ni ellos ni sus variantes de mayúsculas son
# espacios, así que `read_document_fonts` lee los nombres sin recortarlos
_NAME_CHARS = st.characters(categories=("L", "M", "N"))
# Cantidad de grafías de una fuente en cada conjunto. Hypothesis reduce los ejemplos hacia 0
_REFERENCED_COUNTS = (0, 1, 1, 2, 3)
_AVAILABLE_COUNTS = (0, 0, 0, 0, 1, 2)

# Forma de un nombre: por posición, las grafías entre las que se elige
_Shape = tuple[tuple[str, ...], ...]

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


@dataclass(frozen=True)
class _FontSets:
    referenced: frozenset[str]
    embedded: frozenset[str]
    installed: frozenset[str]


@dataclass(frozen=True)
class _Document:
    fonts: _FontSets
    # Referenciadas que van en la tabla de fuentes; las demás van en runs del documento principal
    in_table: frozenset[str]


@st.composite
def _letter_shapes(draw: st.DrawFn) -> _Shape:
    """Nombres de 1 a 5 posiciones de letra, que empiezan y terminan con una letra."""
    shape = [draw(_LETTER)]
    rest = st.tuples(st.sampled_from(_SEPARATORS), _LETTER)
    for separator, letter in draw(st.lists(rest, max_size=4)):
        if separator:
            shape.append((separator,))
        shape.append(letter)
    return tuple(shape)


def _case_variants(text: str) -> _Shape:
    # Las variantes no siempre tienen el mismo casefold() (por ejemplo, "ı" e "I"): el oráculo
    # decide cuáles son la misma fuente
    variants = (text, text.upper(), text.lower(), text.casefold(), text.swapcase(), text.title())
    return (tuple(dict.fromkeys(variants)),)


def _shapes() -> st.SearchStrategy[_Shape]:
    return st.one_of(
        _letter_shapes(), st.text(_NAME_CHARS, min_size=1, max_size=10).map(_case_variants)
    )


def _spellings(shape: _Shape) -> st.SearchStrategy[str]:
    return st.tuples(*(st.sampled_from(options) for options in shape)).map("".join)


@st.composite
def _font_sets(draw: st.DrawFn) -> _FontSets:
    """De 2 a 10 fuentes, cada una con hasta 3 grafías al azar en cada conjunto.

    Las incrustadas y las instaladas tienen menos grafías y faltan más seguido que las
    referenciadas, para que haya fuentes disponibles y fuentes faltantes en el mismo ejemplo.
    """
    referenced: set[str] = set()
    embedded: set[str] = set()
    installed: set[str] = set()
    for shape in draw(st.lists(_shapes(), min_size=2, max_size=10)):
        for names, counts in (
            (referenced, _REFERENCED_COUNTS),
            (embedded, _AVAILABLE_COUNTS),
            (installed, _AVAILABLE_COUNTS),
        ):
            for _ in range(draw(st.sampled_from(counts))):
                names.add(draw(_spellings(shape)))
    return _FontSets(frozenset(referenced), frozenset(embedded), frozenset(installed))


@st.composite
def _documents(draw: st.DrawFn) -> _Document:
    fonts = draw(_font_sets())
    # Ordenadas, para que los datos generados no dependan del orden de iteración del conjunto
    names = sorted(fonts.referenced)
    in_table = draw(st.sets(st.sampled_from(names))) if names else set()
    return _Document(fonts, frozenset(in_table))


# --- Oráculo ---


def _same_font(first: str, second: str) -> bool:
    return first.casefold() == second.casefold()


def _expected_report(fonts: _FontSets) -> list[str]:
    """Fuentes que se deben informar como reemplazadas, en el orden en que se informan."""
    available = [*fonts.embedded, *fonts.installed]
    groups: list[list[str]] = []  # grafías referenciadas de cada fuente faltante
    for name in fonts.referenced:
        if any(_same_font(name, other) for other in available):
            continue
        group = next((group for group in groups if _same_font(group[0], name)), None)
        if group is None:
            groups.append([name])
        else:
            group.append(name)
    return sorted((min(group) for group in groups), key=str.casefold)


# --- DOCX armado a mano ---


def _relationships(*items: tuple[str, str, str]) -> str:
    """Parte de relaciones con (Id, tipo sin el prefijo de `_R`, destino)."""
    body = "".join(
        f'<Relationship Id="{rel_id}" Type="{_R}/{kind}" Target="{target}"/>'
        for rel_id, kind, target in items
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<Relationships xmlns="{_PKG}">{body}</Relationships>'
    )


def _wml(root: str, body: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:{root} xmlns:w="{_W}" xmlns:r="{_R}">{body}</w:{root}>'
    )


def _docx(document: _Document) -> bytes:
    """Paquete con las incrustadas y `in_table` en la tabla de fuentes y el resto en runs.

    Cada incrustada tiene un `w:embedRegular` con una relación a una parte del paquete.
    """
    fonts = document.fonts
    embedded = sorted(fonts.embedded)
    font_parts = [f"fonts/font{index}.odttf" for index in range(1, len(embedded) + 1)]
    table = [f"<w:font w:name={quoteattr(name)}/>" for name in sorted(document.in_table)]
    table += [
        f'<w:font w:name={quoteattr(name)}><w:embedRegular r:id="rId{index}"/></w:font>'
        for index, name in enumerate(embedded, start=1)
    ]
    runs = "".join(
        f"<w:p><w:r><w:rPr><w:rFonts w:ascii={quoteattr(name)}/></w:rPr>"
        "<w:t>texto</w:t></w:r></w:p>"
        for name in sorted(fonts.referenced - document.in_table)
    )
    parts: dict[str, str | bytes] = {
        "_rels/.rels": _relationships(("rId1", "officeDocument", "word/document.xml")),
        "word/document.xml": _wml("document", f"<w:body>{runs}</w:body>"),
        "word/_rels/document.xml.rels": _relationships(("rId1", "fontTable", "fontTable.xml")),
        "word/fontTable.xml": _wml("fonts", "".join(table)),
        "word/_rels/fontTable.xml.rels": _relationships(
            *((f"rId{index}", "font", part) for index, part in enumerate(font_parts, start=1))
        ),
    }
    for part in font_parts:
        parts[f"word/{part}"] = b"\x00" * 16
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _record_case(fonts: _FontSets, reported: Sequence[str]) -> None:
    available = fonts.embedded | fonts.installed
    if any(
        _same_font(name, other) and name.lower() != other.lower()
        for name in fonts.referenced
        for other in available
    ):
        event("referenciada disponible con casefold() y no con lower()")
    if any(_same_font(name, other) for name in fonts.embedded for other in fonts.installed):
        event("fuente incrustada e instalada a la vez")
    missing_spellings = [
        name for name in fonts.referenced if not any(_same_font(name, other) for other in available)
    ]
    if len(missing_spellings) > len(reported):
        event("fuente faltante con varias grafías")
    if sorted(reported, key=str.lower) != list(reported):
        event("orden con casefold() distinto del de lower()")
    event("ninguna fuente faltante" if not reported else "alguna fuente faltante")


# Feature: file-converter, Property 30: Las fuentes informadas como reemplazadas son las que faltan
@pytest.mark.req("7.4")
@given(document=_documents())
def test_reported_fonts_are_the_missing_ones(document: _Document) -> None:
    fonts = document.fonts
    expected = _expected_report(fonts)
    reported = missing_fonts(fonts.referenced, fonts.embedded, fonts.installed)
    assert list(reported) == expected

    # En el DOCX, las incrustadas también están en la tabla de fuentes y por eso son
    # referenciadas. Eso no cambia el resultado esperado: una incrustada nunca falta.
    with zipfile.ZipFile(io.BytesIO(_docx(document))) as archive:
        read = read_document_fonts(archive)
    assert read.referenced == fonts.referenced | fonts.embedded
    assert read.embedded == fonts.embedded
    from_docx = missing_fonts(read.referenced, read.embedded, fonts.installed)
    assert list(from_docx) == expected

    _record_case(fonts, reported)
