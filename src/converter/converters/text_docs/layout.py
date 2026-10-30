"""Maquetación de TXT a PDF y cobertura de glifos de la Fuente_Predeterminada (Req 6.1, 6.11).

Maquetación (Req 6.1). El convertidor `text.pdf` escribe las páginas que devuelve `layout_text`
con WeasyPrint y `white-space: pre`, de modo que los únicos cortes de línea del PDF son los que
se calculan aquí:

- `split_source_lines` separa el texto en sus líneas del origen (saltos CRLF, CR y LF).
- `expand_tabs` lleva cada tabulación a la siguiente columna múltiplo de 8.
- `wrap_line` corta una línea más ancha que el área de texto en la última posición entre
  palabras que cabe en el ancho, medido con los avances de la fuente. Solo corta dentro de una
  palabra si la palabra no cabe sola en una línea. Unir las líneas devuelve la línea original.
- `paginate` reparte las líneas en páginas, en orden.
- `PageLayout` fija la página: A4, márgenes de 2 cm, DejaVu Sans Mono de 10 pt e interlineado
  de 12 pt. La hoja de estilo de `text.pdf` debe usar esos mismos valores.
- `FontMetrics` tiene los avances de DejaVu Sans Mono leídos con fontTools. Un carácter que esa
  fuente no tiene se mide con el mayor avance de las demás fuentes de la Fuente_Predeterminada
  que lo tienen, que son las que fontconfig usa como reemplazo, así que la línea maquetada nunca
  es más ancha que la medida.

Cobertura (Req 6.11). WeasyPrint corre con `FONTCONFIG_FILE` en `default-only.conf`, que solo ve
el directorio de la Fuente_Predeterminada, así que los caracteres representables son la unión de
los `cmap` de las fuentes de ese directorio (`load_coverage`). `find_unrepresentable` devuelve los
que faltan, como máximo 10, en el orden en que aparecen.

El directorio de fuentes es un parámetro, con valor predeterminado `/opt/converter/fonts/default`
(la ruta en la Imagen_de_Contenedor), para poder probar con otras fuentes fuera del contenedor.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from fontTools.ttLib import TTCollection, TTFont  # type: ignore[import-untyped]

# Directorio de la Fuente_Predeterminada y configuración de fontconfig en la Imagen_de_Contenedor
DEFAULT_FONT_DIR = Path("/opt/converter/fonts/default")
FONTCONFIG_FILE = Path("/opt/converter/fontconfig/default-only.conf")

# Fuente de la maquetación de TXT a PDF: DejaVu Sans Mono regular
LAYOUT_FONT_FAMILY = "DejaVu Sans Mono"
LAYOUT_FONT_FILE = "DejaVuSansMono.ttf"

# Archivos que se leen como fuentes; fontconfig recorre el directorio también en subdirectorios
FONT_SUFFIXES = frozenset({".ttf", ".otf", ".ttc", ".otc"})
_COLLECTION_SUFFIXES = frozenset({".ttc", ".otc"})

TAB_SIZE = 8  # las tabulaciones avanzan hasta la siguiente columna múltiplo de 8

MAX_UNREPRESENTABLE = 10  # caracteres no representables que se informan como máximo
# Espacio, tabulación, salto de línea y retorno de carro no se buscan en la cobertura
NOT_CHECKED = frozenset(" \t\n\r")

_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_SPACE = " "  # separador de palabras dentro de una línea, ya sin tabulaciones
_PT_PER_MM = Fraction(720, 254)  # 1 pulgada = 25,4 mm = 72 pt


@dataclass(frozen=True)
class PageLayout:
    """Página de TXT a PDF. Las medidas son enteras para calcular sin errores de redondeo.

    Con los valores predeterminados el área de texto mide 170 mm por 257 mm, con 60 líneas de
    12 pt por página y 80 caracteres de DejaVu Sans Mono de 10 pt por línea.
    """

    width_mm: int = 210  # A4
    height_mm: int = 297
    margin_mm: int = 20  # 2 cm en los cuatro lados
    font_size_pt: int = 10
    line_height_pt: int = 12

    def __post_init__(self) -> None:
        if min(self.width_mm, self.height_mm, self.font_size_pt, self.line_height_pt) < 1:
            raise ValueError(f"las medidas de la página deben ser positivas: {self}")
        if self.margin_mm < 0 or 2 * self.margin_mm >= min(self.width_mm, self.height_mm):
            raise ValueError(f"los márgenes no dejan área de texto: {self}")
        if self.lines_per_page < 1:
            raise ValueError(f"no cabe ninguna línea en la página: {self}")

    @property
    def text_width_pt(self) -> Fraction:
        return (self.width_mm - 2 * self.margin_mm) * _PT_PER_MM

    @property
    def text_height_pt(self) -> Fraction:
        return (self.height_mm - 2 * self.margin_mm) * _PT_PER_MM

    @property
    def lines_per_page(self) -> int:
        return math.floor(self.text_height_pt / self.line_height_pt)

    def max_line_width(self, units_per_em: int) -> int:
        """Ancho del área de texto en unidades del em de la fuente, redondeado hacia abajo.

        Una línea cuya suma de avances no supera este valor cabe en el área de texto.
        """
        return math.floor(self.text_width_pt * units_per_em / self.font_size_pt)


TXT_PAGE = PageLayout()


@dataclass(frozen=True)
class FontMetrics:
    """Avances horizontales para medir líneas, en unidades del em de la fuente de la maquetación.

    `advances` va de punto de código a avance. Un carácter sin glifo en ninguna fuente se mide
    con `missing_advance`, el avance de `.notdef`; ese texto no llega a maquetarse, porque la
    conversión falla antes con los caracteres no representables.
    """

    units_per_em: int
    advances: Mapping[int, int]
    missing_advance: int

    def advance(self, char: str) -> int:
        return self.advances.get(ord(char), self.missing_advance)


def split_source_lines(text: str) -> list[str]:
    """Separa `text` en sus líneas del origen, sin los saltos de línea.

    Los saltos de línea son CRLF, CR y LF. Un salto al final del texto cierra la última línea y
    no abre otra: "a\\nb\\n" tiene dos líneas, "\\n" tiene una línea vacía y "" no tiene líneas.
    """
    lines = _LINE_BREAK.split(text)
    if lines[-1] == "":
        lines.pop()
    return lines


def expand_tabs(line: str) -> str:
    """Reemplaza cada tabulación por los espacios hasta la siguiente columna múltiplo de 8.

    Las columnas cuentan caracteres desde el inicio de la línea del origen, empezando en 0.
    """
    return line.expandtabs(TAB_SIZE)


def wrap_line(line: str, max_width: int, advance: Callable[[str], int]) -> list[str]:
    """Corta `line` (sin saltos de línea ni tabulaciones) en líneas de `max_width` o menos.

    El ancho de un texto es la suma de `advance` de sus caracteres. Si la línea no cabe, se corta
    en la última posición que cabe y que no está dentro de una palabra, es decir, junto a un
    espacio; los espacios quedan en la línea donde caben. Si la palabra que empieza la línea no
    cabe sola, se corta dentro de ella después del último carácter que cabe. Unir las líneas
    devuelve `line` sin perder caracteres. Una línea vacía da una sola línea vacía.

    Un carácter más ancho que `max_width` queda solo en su línea, para avanzar siempre.
    """
    if max_width < 1:
        raise ValueError(f"max_width debe ser 1 o mayor: {max_width}")
    pieces: list[str] = []
    start = 0
    length = len(line)
    while True:
        # `end` es el final del prefijo más largo desde `start` que cabe (al menos un carácter)
        end = start
        width = 0
        while end < length:
            width += advance(line[end])
            if width > max_width and end > start:
                break
            end += 1
        if end == length:
            pieces.append(line[start:])
            return pieces
        # Último corte que cabe y no separa dos caracteres de la misma palabra
        cut = end
        while cut > start and line[cut - 1] != _SPACE and line[cut] != _SPACE:
            cut -= 1
        if cut == start:
            cut = end  # la palabra no cabe sola en una línea: se corta dentro de ella
        pieces.append(line[start:cut])
        start = cut


def wrap_lines(text: str, max_width: int, advance: Callable[[str], int]) -> list[str]:
    """Maqueta `text` en líneas de `max_width` o menos, medidas con `advance`.

    Cada línea del origen, con las tabulaciones expandidas, empieza una línea nueva y continúa
    en las siguientes si no cabe (`wrap_line`).
    """
    return [
        piece
        for line in split_source_lines(text)
        for piece in wrap_line(expand_tabs(line), max_width, advance)
    ]


def paginate(lines: Sequence[str], lines_per_page: int) -> list[list[str]]:
    """Reparte `lines` en páginas de `lines_per_page` líneas, en orden; la última puede tener
    menos. Sin líneas devuelve una página vacía, porque el PDF tiene al menos una página.
    """
    if lines_per_page < 1:
        raise ValueError(f"lines_per_page debe ser 1 o mayor: {lines_per_page}")
    if not lines:
        return [[]]
    return [list(lines[i : i + lines_per_page]) for i in range(0, len(lines), lines_per_page)]


def layout_text(text: str, metrics: FontMetrics, page: PageLayout = TXT_PAGE) -> list[list[str]]:
    """Páginas de TXT a PDF: `text` maquetado en el ancho de `page` y repartido en páginas."""
    max_width = page.max_line_width(metrics.units_per_em)
    return paginate(wrap_lines(text, max_width, metrics.advance), page.lines_per_page)


def load_font_metrics(
    font_dir: Path = DEFAULT_FONT_DIR, layout_font: str = LAYOUT_FONT_FILE
) -> FontMetrics:
    """Lee con fontTools los avances de `layout_font` y de las demás fuentes de `font_dir`.

    Los caracteres de `layout_font` usan su avance. Los demás caracteres de la cobertura usan el
    mayor avance entre las otras fuentes que los tienen, convertido a unidades del em de
    `layout_font` y redondeado hacia arriba. Lanza `FileNotFoundError` si `layout_font` no está
    en `font_dir`.
    """
    files = _font_files(font_dir)
    layout_path = next((path for path in files if path.name == layout_font), None)
    if layout_path is None:
        raise FileNotFoundError(f"no se encontró la fuente {layout_font} en {font_dir}")
    with TTFont(layout_path) as font:
        units_per_em = int(font["head"].unitsPerEm)
        hmtx = font["hmtx"]
        advances = {code: int(hmtx[glyph][0]) for code, glyph in _cmap(font).items()}
        missing_advance = int(hmtx[font.getGlyphOrder()[0]][0])
    fallback: dict[int, int] = {}
    for path in files:
        if path == layout_path:
            continue
        for face in _faces(path):
            face_units = int(face["head"].unitsPerEm)
            face_hmtx = face["hmtx"]
            for code, glyph in _cmap(face).items():
                if code in advances:
                    continue
                # División entera redondeada hacia arriba, para no medir de menos
                scaled = -(-int(face_hmtx[glyph][0]) * units_per_em // face_units)
                fallback[code] = max(scaled, fallback.get(code, 0))
    advances.update(fallback)
    return FontMetrics(units_per_em, advances, missing_advance)


def load_coverage(font_dir: Path = DEFAULT_FONT_DIR) -> frozenset[int]:
    """Puntos de código que la Fuente_Predeterminada representa: la unión de los `cmap` de las
    fuentes de `font_dir`, sin los que apuntan a `.notdef`.

    Lanza `FileNotFoundError` si `font_dir` no tiene fuentes, para no tomar una instalación
    incompleta como texto no representable.
    """
    coverage: set[int] = set()
    for path in _font_files(font_dir):
        for face in _faces(path):
            coverage.update(_cmap(face))
    return frozenset(coverage)


def find_unrepresentable(text: str, coverage: AbstractSet[int]) -> list[str]:
    """Caracteres distintos de `text` cuyo punto de código no está en `coverage`, en el orden de
    su primera aparición y como máximo `MAX_UNREPRESENTABLE`.

    No se cuentan los espacios, las tabulaciones ni los saltos de línea (`NOT_CHECKED`).
    """
    missing = {char for char in set(text) - NOT_CHECKED if ord(char) not in coverage}
    if not missing:
        return []
    # Se recorre el texto solo hasta encontrar los que se informan
    wanted = min(len(missing), MAX_UNREPRESENTABLE)
    found: dict[str, None] = {}  # conserva el orden de inserción
    for char in text:
        if char in missing and char not in found:
            found[char] = None
            if len(found) == wanted:
                break
    return list(found)


def _font_files(font_dir: Path) -> list[Path]:
    files = sorted(
        path
        for path in font_dir.rglob("*")
        if path.suffix.lower() in FONT_SUFFIXES and path.is_file()
    )
    if not files:
        raise FileNotFoundError(f"no hay archivos de fuente en {font_dir}")
    return files


def _faces(path: Path) -> Iterator[Any]:
    """Fuentes de un archivo: una sola, o todas las de una colección TTC u OTC."""
    if path.suffix.lower() in _COLLECTION_SUFFIXES:
        with TTCollection(path) as collection:
            yield from collection.fonts
    else:
        with TTFont(path) as font:
            yield font


def _cmap(font: Any) -> dict[int, str]:
    """`cmap` Unicode preferido de la fuente, sin los caracteres que apuntan al glifo 0."""
    notdef = font.getGlyphOrder()[0]
    best = font.getBestCmap() or {}
    return {int(code): str(glyph) for code, glyph in best.items() if glyph != notdef}
