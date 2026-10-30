"""Generador de Nombres_Originales que usan las propiedades 57, 58 y 59.

`original_names()` genera nombres de 0 a 300 caracteres que mezclan texto Unicode arbitrario
(con caracteres de 1 a 4 bytes en UTF-8), separadores de ruta, segmentos "..", puntos, los
Caracteres_No_Permitidos del glosario, caracteres permitidos que se les parecen, espacios,
acentos y mayúsculas. Una parte de los ejemplos tiene un Nombre_Base de 196 a 204 bytes, para que
el truncado a 200 bytes de Req 15.5 caiga justo en el límite o en medio de un carácter multibyte.

Los nombres no tienen sustitutos sueltos (U+D800 a U+DFFF): no se pueden codificar en UTF-8, y
Starlette decodifica el nombre de archivo de un envío multipart como UTF-8 o Latin-1, que nunca
los producen.
"""

from __future__ import annotations

from collections.abc import Sequence

from hypothesis import strategies as st

MAX_NAME_CHARS = 300

# Carácter_No_Permitido según el glosario de requirements.md. Es una copia propia y no
# `naming.FORBIDDEN`, para que un carácter que falte o sobre en naming.py haga fallar las pruebas
FORBIDDEN_CHARS: tuple[str, ...] = (
    tuple('/\\:*?"<>|') + tuple(chr(code) for code in range(0x20)) + ("\x7f",)
)

# Caracteres según los bytes que ocupan en UTF-8; `codec="utf-8"` excluye los sustitutos
_BY_WIDTH: dict[int, st.SearchStrategy[str]] = {
    1: st.characters(max_codepoint=0x7F),
    2: st.characters(min_codepoint=0x80, max_codepoint=0x7FF),
    3: st.characters(min_codepoint=0x800, max_codepoint=0xFFFF, codec="utf-8"),
    4: st.characters(min_codepoint=0x10000),
}
# Caracteres de 1 byte sin el punto, para no mover el último punto del nombre
_ONE_BYTE_NO_DOT = st.characters(max_codepoint=0x7F, exclude_characters=".")

# Caracteres permitidos que se parecen a un Carácter_No_Permitido o al punto: controles C1,
# espacios y separadores Unicode, caracteres de ancho cero, marcas de dirección, BOM y las
# variantes de ancho completo de los no permitidos y del punto. Deben quedar sin cambios.
LOOKALIKE_CHARS: tuple[str, ...] = (
    tuple(chr(code) for code in range(0x80, 0xA0))
    + ("\u00a0", "\u2028", "\u2029", "\u200b", "\u200d", "\u202e", "\ufeff")
    + ("\u2215", "\u2024", "\uff0e")  # barra de división, punto guía, punto de ancho completo
    + tuple("／＼：＊？＂＜＞｜")
)

# Palabras con mayúsculas, acentos precompuestos y acentos como marca combinante aparte (NFD),
# que tampoco deben cambiar de forma Unicode
_WORDS: tuple[str, ...] = (
    "Vacaciones 2024",
    "INFORME",
    "Año",
    "ÁÉÍÓÚÑÜ",
    "áéíóúñü",
    "Ça",
    "Straße",
    "Cafe\u0301",
    "A\u0301rbol",
    "n\u0303andu\u0301",
    "U\u0308BER",
    "Ελληνικά",
    "Кириллица",
    "日本語",
)

# Fragmentos de rutas de Windows y Unix, segmentos "..", puntos, espacios y otros caracteres
_FRAGMENTS: tuple[str, ...] = (
    "/",
    "\\",
    "..",
    "../",
    "..\\",
    "./",
    ".",
    "...",
    "~/",
    "C:\\Users\\",
    "/etc/",
    "\\\\servidor\\",
    " ",
    "  ",
    "\u3000",
    "_",
    "-",
    "€",
    "😀",
    "👩\u200d💻",
    "\U0010ffff",
)

# Finales de nombre: extensiones en minúsculas y mayúsculas, dobles, un punto final o ninguno
_SUFFIXES: tuple[str, ...] = (
    ".png",
    ".PNG",
    ".jpg",
    ".JPEG",
    ".webp",
    ".pdf",
    ".txt",
    ".md",
    ".html",
    ".docx",
    ".mp3",
    ".srt",
    ".tar.gz",
    ".",
    "",
)
# Finales con un punto como mucho, para que el Nombre_Base sea todo lo anterior
_SINGLE_DOT_SUFFIXES: tuple[str, ...] = ("", ".", ".png", ".PNG", ".docx")

# Nombres conocidos: vacío, solo puntos, sin Nombre_Base, punto final, rutas y bytes nulos
_SPECIAL: tuple[str, ...] = (
    "",
    ".",
    "..",
    "...",
    ".png",
    "notas",
    "notas.",
    "informe.final.docx",
    "Vacaciones 2024.PNG",
    "../../etc/passwd",
    "C:\\fakepath\\foto.png",
    "con\x00nulo.txt",
    "a" * MAX_NAME_CHARS,
)

_pieces = st.one_of(
    st.text(st.characters(codec="utf-8"), max_size=10),
    st.sampled_from(_FRAGMENTS + LOOKALIKE_CHARS),
    st.sampled_from(_WORDS),
    st.sampled_from(FORBIDDEN_CHARS),
    # Todos los caracteres de cada grupo juntos, para que ninguno quede sin probar en 100 ejemplos
    st.permutations(FORBIDDEN_CHARS).map("".join),
    st.permutations(LOOKALIKE_CHARS).map("".join),
    st.sampled_from(_SUFFIXES),
)


def _cycled(chars: Sequence[str], count: int) -> str:
    """`count` caracteres que repiten `chars` en orden."""
    return "".join(chars[index % len(chars)] for index in range(count))


def _fit(body: str, suffix: str) -> str:
    """`body` recortado para que, con `suffix` al final, no pase de MAX_NAME_CHARS caracteres."""
    return body[: MAX_NAME_CHARS - len(suffix)] + suffix


@st.composite
def _free_names(draw: st.DrawFn) -> str:
    """Concatenación de fragmentos, texto arbitrario y Caracteres_No_Permitidos."""
    return _fit("".join(draw(st.lists(_pieces, max_size=30))), "")


@st.composite
def _long_names(draw: st.DrawFn) -> str:
    """Fragmentos con una tirada de hasta 300 caracteres del mismo ancho, que suele pasar los
    200 bytes, y un final de extensión."""
    width = draw(st.integers(1, 4))
    chars = draw(st.lists(_BY_WIDTH[width], min_size=1, max_size=4))
    run = _cycled(chars, draw(st.integers(0, MAX_NAME_CHARS)))
    pieces = draw(st.lists(_pieces, max_size=10))
    pieces.insert(draw(st.integers(0, len(pieces))), run)
    return _fit("".join(pieces), draw(st.sampled_from(_SUFFIXES)))


@st.composite
def _near_limit_names(draw: st.DrawFn) -> str:
    """Nombre_Base sin puntos de 196 a 204 bytes en UTF-8, seguido de un final con un punto o
    ninguno.

    El Nombre_Base empieza con caracteres de 1 byte (incluidos los Caracteres_No_Permitidos, que
    ocupan 1 byte como el "_" que los reemplaza) y termina con caracteres de 2 a 4 bytes. Cuando
    esa cola cruza el byte 200, el corte puede caer en medio de un carácter.
    """
    total = draw(st.integers(196, 204))
    width = draw(st.integers(2, 4))
    wide = draw(st.integers(0, total // width))
    head = _cycled(draw(st.lists(_ONE_BYTE_NO_DOT, min_size=1, max_size=8)), total - wide * width)
    tail = _cycled(draw(st.lists(_BY_WIDTH[width], min_size=1, max_size=4)), wide)
    return head + tail + draw(st.sampled_from(_SINGLE_DOT_SUFFIXES))


def original_names() -> st.SearchStrategy[str]:
    """Nombres_Originales de 0 a 300 caracteres, sin sustitutos sueltos."""
    return st.one_of(
        st.sampled_from(_SPECIAL),
        _free_names(),
        _long_names(),
        _near_limit_names(),
    )
