"""Fuentes TrueType mínimas, creadas con fontTools, para probar la lectura de `cmap` y avances.

Son archivos de fuente reales, que fontTools y fontconfig leen igual que DejaVu, con glifos
vacíos y la cobertura y los avances que pide cada prueba. Así las pruebas no dependen de que
DejaVu esté instalado en el equipo de desarrollo.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from fontTools.fontBuilder import FontBuilder  # type: ignore[import-untyped]
from fontTools.pens.ttGlyphPen import TTGlyphPen  # type: ignore[import-untyped]
from fontTools.ttLib import TTCollection, TTFont  # type: ignore[import-untyped]

NOTDEF = ".notdef"


def build_font(
    path: Path,
    chars: str,
    *,
    family: str = "Prueba",
    style: str = "Regular",
    units_per_em: int = 2048,
    advance: int = 1233,
    advances: Mapping[str, int] | None = None,
    notdef_advance: int | None = None,
    notdef_chars: str = "",
) -> Path:
    """Escribe en `path` una fuente con un glifo por cada carácter de `chars`.

    Cada glifo avanza `advances[c]` si está, o `advance`. `notdef_advance` cambia el avance de
    `.notdef` (por defecto `advance`) y `notdef_chars` son caracteres que el `cmap` asigna a
    `.notdef`, que no cuentan como cubiertos.
    """
    per_char = advances or {}
    cmap: dict[int, str] = {ord(c): f"uni{ord(c):04X}" for c in chars}
    cmap.update({ord(c): NOTDEF for c in notdef_chars})
    names = {ord(c): cmap[ord(c)] for c in chars}
    glyph_advance = {NOTDEF: advance if notdef_advance is None else notdef_advance}
    glyph_advance.update({names[ord(c)]: per_char.get(c, advance) for c in chars})
    glyphs = [NOTDEF, *sorted(set(names.values()))]

    ascent = units_per_em * 4 // 5
    descent = units_per_em - ascent
    builder = FontBuilder(units_per_em, isTTF=True)
    builder.setupGlyphOrder(glyphs)
    builder.setupCharacterMap(cmap)
    builder.setupGlyf({glyph: TTGlyphPen(None).glyph() for glyph in glyphs})
    builder.setupHorizontalMetrics({glyph: (glyph_advance[glyph], 0) for glyph in glyphs})
    builder.setupHorizontalHeader(ascent=ascent, descent=-descent)
    builder.setupNameTable({"familyName": family, "styleName": style})
    builder.setupOS2(
        sTypoAscender=ascent, sTypoDescender=-descent, usWinAscent=ascent, usWinDescent=descent
    )
    builder.setupPost()
    path.parent.mkdir(parents=True, exist_ok=True)
    builder.save(str(path))
    return path


def build_collection(path: Path, fonts: list[Path]) -> Path:
    """Escribe en `path` una colección TTC con las fuentes de `fonts`, en ese orden."""
    collection = TTCollection()
    collection.fonts = [TTFont(font) for font in fonts]
    path.parent.mkdir(parents=True, exist_ok=True)
    collection.save(str(path))
    return path
