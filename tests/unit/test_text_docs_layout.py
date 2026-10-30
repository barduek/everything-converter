"""Pruebas de src/converter/converters/text_docs/layout.py: maquetación de TXT a PDF, cobertura de
glifos de la Fuente_Predeterminada y caracteres no representables (Req 6.1, 6.11), y de
docker/fontconfig/default-only.conf.

La lógica de corte se prueba con avances escritos a mano. La lectura de `cmap` y avances se
prueba con fuentes TrueType reales creadas con fontTools (`tests/fontbuild.py`). Las pruebas con
DejaVu se omiten si DejaVu Sans Mono no está instalado, como en el equipo de desarrollo; corren
dentro de la Imagen_de_Contenedor.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree

import pytest

from converter.converters.text_docs.layout import (
    DEFAULT_FONT_DIR,
    FONTCONFIG_FILE,
    LAYOUT_FONT_FAMILY,
    LAYOUT_FONT_FILE,
    MAX_UNREPRESENTABLE,
    TXT_PAGE,
    FontMetrics,
    PageLayout,
    expand_tabs,
    find_unrepresentable,
    layout_text,
    load_coverage,
    load_font_metrics,
    paginate,
    split_source_lines,
    wrap_line,
    wrap_lines,
)
from tests.fontbuild import build_collection, build_font

_REPO = Path(__file__).resolve().parents[2]
_CONF = _REPO / "docker" / "fontconfig" / "default-only.conf"

# Directorios donde puede estar DejaVu solo: el de la imagen y el del paquete de Debian
_DEJAVU_DIRS = (DEFAULT_FONT_DIR, Path("/usr/share/fonts/truetype/dejavu"))

# Avance de DejaVu Sans Mono (todos sus glifos) y su tamaño del em
_MONO_ADVANCE = 1233
_MONO_UPM = 2048


def _unit(_: str) -> int:
    return 1


def _with(widths: dict[str, int], default: int = 1) -> Callable[[str], int]:
    return lambda char: widths.get(char, default)


# --- split_source_lines y expand_tabs ---


@pytest.mark.req("6.1")
@pytest.mark.parametrize(
    ("text", "lines"),
    [
        ("", []),
        ("\n", [""]),
        ("uno", ["uno"]),
        ("uno\n", ["uno"]),
        ("uno\ndos\r\ntres\rcuatro", ["uno", "dos", "tres", "cuatro"]),
        ("a\n\nb\n\n", ["a", "", "b", ""]),
        ("a\r\n\r\nb", ["a", "", "b"]),
    ],
)
def test_split_source_lines(text: str, lines: list[str]) -> None:
    assert split_source_lines(text) == lines


@pytest.mark.req("6.1")
@pytest.mark.parametrize(
    ("line", "expanded"),
    [
        ("\t", " " * 8),
        ("a\tb", "a" + " " * 7 + "b"),
        ("1234567\tx", "1234567 x"),
        ("12345678\tx", "12345678" + " " * 8 + "x"),
        ("\t\tx", " " * 16 + "x"),
        ("sin tabulación", "sin tabulación"),
    ],
)
def test_tabs_go_to_the_next_multiple_of_8(line: str, expanded: str) -> None:
    assert expand_tabs(line) == expanded


# --- wrap_line ---


@pytest.mark.req("6.1")
@pytest.mark.parametrize(
    ("line", "width", "pieces"),
    [
        ("", 5, [""]),
        ("cabe", 4, ["cabe"]),
        # Corte en el último espacio que cabe; el espacio queda en la línea
        ("uno dos tres", 8, ["uno dos ", "tres"]),
        ("uno dos tres", 6, ["uno ", "dos ", "tres"]),
        # La palabra termina justo en el ancho: el espacio pasa a la línea siguiente
        ("abcd ef", 4, ["abcd", " ef"]),
        # Varios espacios: se corta después del último que cabe
        ("ab    cd", 4, ["ab  ", "  cd"]),
        # Corte dentro de la palabra solo si no cabe sola en una línea
        ("ab cdefghij", 5, ["ab ", "cdefg", "hij"]),
        ("abcdefghij", 4, ["abcd", "efgh", "ij"]),
        ("   abcdefgh", 5, ["   ", "abcde", "fgh"]),
        # Espacios al final que no caben quedan en su propia línea
        ("abcde   ", 5, ["abcde", "   "]),
    ],
)
def test_wrap_line_with_unit_advances(line: str, width: int, pieces: list[str]) -> None:
    assert wrap_line(line, width, _unit) == pieces
    assert "".join(pieces) == line


@pytest.mark.req("6.1")
def test_wrap_line_measures_with_the_advances() -> None:
    # Por cantidad de caracteres "aa MM" cabría en 5; por avances mide 7
    advance = _with({"M": 2})
    assert wrap_line("aa MM aa", 5, advance) == ["aa ", "MM ", "aa"]
    # Los caracteres de avance 0 no ocupan ancho
    assert wrap_line("a\u0301b\u0301c", 3, _with({"\u0301": 0})) == ["a\u0301b\u0301c"]


@pytest.mark.req("6.1")
def test_wrap_line_puts_a_character_wider_than_the_line_alone() -> None:
    assert wrap_line("aWb", 2, _with({"W": 3})) == ["a", "W", "b"]


@pytest.mark.req("6.1")
@pytest.mark.parametrize("width", [0, -1])
def test_wrap_line_rejects_a_width_below_1(width: int) -> None:
    with pytest.raises(ValueError, match="max_width"):
        wrap_line("a", width, _unit)


# --- wrap_lines ---


@pytest.mark.req("6.1")
def test_each_source_line_starts_a_new_line() -> None:
    text = "uno dos\r\n\ntres cuatro cinco\rseis"
    assert wrap_lines(text, 8, _unit) == ["uno dos", "", "tres ", "cuatro ", "cinco", "seis"]


@pytest.mark.req("6.1")
def test_tabs_are_expanded_per_source_line_before_measuring() -> None:
    assert wrap_lines("\tx", 8, _unit) == [" " * 8, "x"]
    # La columna vuelve a 0 en cada línea del origen, no en cada línea maquetada
    assert wrap_lines("abcdefghij\tk\nab\tc", 12, _unit) == [
        "abcdefghij  ",
        "    k",
        "ab      c",
    ]


# --- paginate y PageLayout ---


@pytest.mark.req("6.1")
def test_paginate_keeps_every_line_in_order() -> None:
    lines = [f"línea {n}" for n in range(125)]
    pages = paginate(lines, 60)
    assert [len(page) for page in pages] == [60, 60, 5]
    assert [line for page in pages for line in page] == lines
    assert paginate(lines[:60], 60) == [lines[:60]]


@pytest.mark.req("6.1")
def test_paginate_without_lines_gives_one_empty_page() -> None:
    assert paginate([], 60) == [[]]


@pytest.mark.req("6.1")
def test_paginate_rejects_pages_without_lines() -> None:
    with pytest.raises(ValueError, match="lines_per_page"):
        paginate(["a"], 0)


@pytest.mark.req("6.1")
def test_default_page_is_a4_with_2_cm_margins() -> None:
    page = TXT_PAGE
    assert (page.width_mm, page.height_mm, page.margin_mm) == (210, 297, 20)
    assert (page.font_size_pt, page.line_height_pt) == (10, 12)
    # 170 mm por 257 mm; 1 mm = 72 / 25,4 pt
    assert page.text_width_pt == pytest.approx(170 * 72 / 25.4)
    assert page.text_height_pt == pytest.approx(257 * 72 / 25.4)
    assert page.lines_per_page == 60  # 728,5 pt / 12 pt
    # 481,89 pt en unidades de un em de 2048 a 10 pt
    assert page.max_line_width(_MONO_UPM) == 98_691
    assert page.max_line_width(_MONO_UPM) // _MONO_ADVANCE == 80


@pytest.mark.req("6.1")
@pytest.mark.parametrize(
    "fields",
    [
        {"width_mm": 0},
        {"font_size_pt": 0},
        {"line_height_pt": 0},
        {"margin_mm": -1},
        {"margin_mm": 105},
        {"line_height_pt": 1000},
    ],
)
def test_page_layout_rejects_pages_without_text_area(fields: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        PageLayout(**fields)


@pytest.mark.req("6.1")
def test_layout_text_fills_80_columns_and_60_lines_per_page() -> None:
    metrics = FontMetrics(_MONO_UPM, {ord("x"): _MONO_ADVANCE, ord(" "): _MONO_ADVANCE}, 1233)
    assert layout_text("x" * 200, metrics) == [["x" * 80, "x" * 80, "x" * 40]]
    pages = layout_text("x\n" * 125, metrics)
    assert [len(page) for page in pages] == [60, 60, 5]
    assert layout_text("", metrics) == [[]]


# --- load_font_metrics y load_coverage con fuentes creadas para la prueba ---


@pytest.fixture
def font_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "fonts"
    build_font(
        directory / LAYOUT_FONT_FILE,
        " abcxyzáñ",
        family=LAYOUT_FONT_FAMILY,
        notdef_advance=1000,
        notdef_chars="\u0007",
    )
    build_font(
        directory / "DejaVuSans.ttf",
        "ab€",
        family="DejaVu Sans",
        units_per_em=1000,
        advance=500,
        advances={"€": 700},
    )
    build_font(
        directory / "variantes" / "DejaVuSans-Bold.ttf",
        "€Ω",
        family="DejaVu Sans",
        style="Bold",
        units_per_em=1000,
        advance=600,
    )
    (directory / "LICENSE").write_text("no es una fuente", encoding="utf-8")
    return directory


@pytest.mark.req("6.1")
def test_metrics_use_the_layout_font_first(font_dir: Path) -> None:
    metrics = load_font_metrics(font_dir)
    assert metrics.units_per_em == 2048
    assert metrics.advance("a") == metrics.advance(" ") == metrics.advance("ñ") == 1233


@pytest.mark.req("6.1")
def test_metrics_of_other_fonts_take_the_widest_rounded_up(font_dir: Path) -> None:
    metrics = load_font_metrics(font_dir)
    # 700 / 1000 em = 1433,6 / 2048 em; la negrita da 1228,8
    assert metrics.advance("€") == 1434
    assert metrics.advance("Ω") == 1229
    # Sin glifo en ninguna fuente: avance de .notdef de la fuente de la maquetación
    assert metrics.advance("日") == 1000
    assert metrics.advance("\u0007") == 1000


@pytest.mark.req("6.1")
def test_metrics_need_the_layout_font(font_dir: Path) -> None:
    (font_dir / LAYOUT_FONT_FILE).unlink()
    with pytest.raises(FileNotFoundError, match=LAYOUT_FONT_FILE):
        load_font_metrics(font_dir)


@pytest.mark.req("6.11")
def test_coverage_is_the_union_of_the_cmaps(font_dir: Path) -> None:
    coverage = load_coverage(font_dir)
    assert coverage == {ord(c) for c in " abcxyzáñ€Ω"}


@pytest.mark.req("6.11")
def test_coverage_excludes_characters_mapped_to_notdef(font_dir: Path) -> None:
    assert 0x07 not in load_coverage(font_dir)


@pytest.mark.req("6.11")
def test_coverage_reads_font_collections(tmp_path: Path) -> None:
    first = build_font(tmp_path / "sueltas" / "a.ttf", "ab")
    second = build_font(tmp_path / "sueltas" / "b.ttf", "€")
    directory = tmp_path / "coleccion"
    build_collection(directory / "familia.ttc", [first, second])
    assert load_coverage(directory) == {ord("a"), ord("b"), ord("€")}


@pytest.mark.req("6.11")
def test_coverage_without_fonts_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "LICENSE").write_text("no es una fuente", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        load_coverage(tmp_path)
    with pytest.raises(FileNotFoundError):
        load_coverage(tmp_path / "no-existe")


@pytest.mark.req("6.1", "6.11")
def test_font_directory_defaults_to_the_image_path() -> None:
    image_dir = Path("/opt/converter/fonts/default")
    for function in (load_coverage, load_font_metrics):
        assert inspect.signature(function).parameters["font_dir"].default == image_dir


# --- find_unrepresentable ---


_LATIN = frozenset(map(ord, "abcdefghijklmnopqrstuvwxyzáéíóúñ.,"))


@pytest.mark.req("6.11")
def test_unrepresentable_in_order_of_first_appearance() -> None:
    assert find_unrepresentable("año 日本, 日 b 🙂 本", _LATIN) == ["日", "本", "🙂"]


@pytest.mark.req("6.11")
def test_all_representable_gives_an_empty_list() -> None:
    assert find_unrepresentable("año, canción.", _LATIN) == []
    assert find_unrepresentable("", frozenset()) == []


@pytest.mark.req("6.11")
def test_spaces_tabs_and_line_breaks_are_not_checked() -> None:
    assert find_unrepresentable(" \t\r\n \r\n", frozenset()) == []
    # Otros espacios sí necesitan glifo
    assert find_unrepresentable("a\u00a0b\u3000", _LATIN) == ["\u00a0", "\u3000"]


@pytest.mark.req("6.11")
def test_at_most_10_unrepresentable_are_reported() -> None:
    chars = [chr(0x4E00 + n) for n in range(15)]
    text = " ".join(chars + chars)
    assert MAX_UNREPRESENTABLE == 10
    assert find_unrepresentable(text, _LATIN) == chars[:10]


# --- DejaVu, si está instalado ---


@pytest.fixture(scope="module")
def dejavu_dir() -> Path:
    for directory in _DEJAVU_DIRS:
        if (directory / LAYOUT_FONT_FILE).is_file():
            return directory
    pytest.skip("DejaVu Sans Mono no está instalado; se prueba en la Imagen_de_Contenedor")


@pytest.mark.req("6.1")
def test_dejavu_sans_mono_gives_80_columns(dejavu_dir: Path) -> None:
    metrics = load_font_metrics(dejavu_dir)
    assert metrics.units_per_em == _MONO_UPM
    assert {metrics.advance(c) for c in "aMñ€ 0.@"} == {_MONO_ADVANCE}
    words = " ".join(["palabra"] * 30)  # 239 caracteres
    lines = [line for page in layout_text(words, metrics) for line in page]
    assert "".join(lines) == words
    assert [len(line) for line in lines] == [80, 80, 79]


@pytest.mark.req("6.11")
def test_dejavu_covers_spanish_and_not_cjk_or_emoji(dejavu_dir: Path) -> None:
    coverage = load_coverage(dejavu_dir)
    spanish = "áéíóúüñÁÉÍÓÚÜÑ¿¡€«»—“”‘’…"
    assert find_unrepresentable(spanish + "abcXYZ0123", coverage) == []
    assert find_unrepresentable("Año 日本 🙂", coverage) == ["日", "本", "🙂"]


# --- docker/fontconfig/default-only.conf ---


@pytest.mark.req("6.11")
def test_fontconfig_sees_only_the_default_font_dir() -> None:
    root = ElementTree.parse(_CONF).getroot()
    assert root.tag == "fontconfig"
    dirs = list(root.iter("dir"))
    assert [d.text for d in dirs] == [str(DEFAULT_FONT_DIR)]
    assert all(not d.attrib for d in dirs)  # sin prefix="xdg" ni otros directorios relativos
    # Sin include no se cargan fonts.conf, conf.d ni las fuentes del sistema o del usuario
    assert list(root.iter("include")) == []
    assert FONTCONFIG_FILE.name == _CONF.name


@pytest.mark.req("6.1")
def test_fontconfig_monospace_is_the_layout_font() -> None:
    root = ElementTree.parse(_CONF).getroot()
    aliases = {
        alias.findtext("family"): [f.text for f in alias.iterfind("prefer/family")]
        for alias in root.iter("alias")
    }
    assert aliases["monospace"] == [LAYOUT_FONT_FAMILY]
