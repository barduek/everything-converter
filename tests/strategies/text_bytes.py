"""Contenidos de texto y predicado de referencia del glosario, que usan las propiedades 2, 3 y 18.

Predicado de referencia, escrito a partir de las definiciones de requirements.md y sin usar
`converter.detection.text`, para que un error del módulo haga fallar las pruebas en lugar de
pasar al valor esperado:

- `is_valid_utf8_text`, `is_windows1252_text` e `is_valid_text`: Texto_UTF8_Válido,
  Texto_Windows_1252 y Texto_Válido.
- `decoded_text`: el Texto_Decodificado de un Texto_Válido. Windows-1252 se decodifica con una
  tabla propia de los caracteres de 0x80 a 0x9F, no con el códec cp1252 de Python.

Generadores:

- `valid_utf8_text()`: Texto_UTF8_Válido con caracteres de 1 a 4 bytes, controles, U+FEFF dentro
  del texto y, según el parámetro, con BOM_UTF8 inicial o sin ella.
- `windows1252_text()`: Texto_Windows_1252 con texto Latin-1, caracteres propios de
  Windows-1252, los bytes sin carácter asignado, UTF-8 cortado o mal formado y controles hasta
  justo debajo del 1 %.
- `arbitrary_bytes()`: cualquier secuencia de bytes, con Texto_UTF8_Válido, Texto_Windows_1252,
  contenidos con 0x00, contenidos alrededor del límite del 1 % de controles, textos con bytes
  cambiados y bytes al azar.
"""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction

from hypothesis import strategies as st

# Glosario, BOM_UTF8: EF BB BF al inicio de un contenido de texto
BOM_UTF8 = b"\xef\xbb\xbf"

# Glosario, Texto_Windows_1252: bytes de control que deben ser menos del 1 % del total, de 0x01 a
# 0x1F y 0x7F salvo tabulación, salto de línea, avance de página y retorno de carro
COUNTED_CONTROLS: frozenset[int] = frozenset({*range(0x01, 0x20), 0x7F} - {0x09, 0x0A, 0x0C, 0x0D})
_CONTROL_LIMIT = Fraction(1, 100)

# Bytes sin carácter asignado en Windows-1252; se interpretan como el carácter de igual valor
WINDOWS_1252_UNDEFINED: frozenset[int] = frozenset({0x81, 0x8D, 0x8F, 0x90, 0x9D})

# Caracteres de Windows-1252 de 0x80 a 0x9F (tabla de Microsoft). Los demás bytes, como en
# ISO-8859-1, y los sin asignar dan el carácter Unicode de igual valor.
_WINDOWS_1252_80_9F: dict[int, str] = {
    0x80: "\u20ac",  # €
    0x82: "\u201a",  # ‚
    0x83: "\u0192",  # ƒ
    0x84: "\u201e",  # „
    0x85: "\u2026",  # …
    0x86: "\u2020",  # †
    0x87: "\u2021",  # ‡
    0x88: "\u02c6",  # ˆ
    0x89: "\u2030",  # ‰
    0x8A: "\u0160",  # Š
    0x8B: "\u2039",  # ‹
    0x8C: "\u0152",  # Œ
    0x8E: "\u017d",  # Ž
    0x91: "\u2018",  # ‘
    0x92: "\u2019",  # ’
    0x93: "\u201c",  # “
    0x94: "\u201d",  # ”
    0x95: "\u2022",  # •
    0x96: "\u2013",  # –
    0x97: "\u2014",  # —
    0x98: "\u02dc",  # ˜
    0x99: "\u2122",  # ™
    0x9A: "\u0161",  # š
    0x9B: "\u203a",  # ›
    0x9C: "\u0153",  # œ
    0x9E: "\u017e",  # ž
    0x9F: "\u0178",  # Ÿ
}


# --- Predicado de referencia ---


def is_valid_utf8_text(data: bytes) -> bool:
    """Texto_UTF8_Válido: se decodifica completo como UTF-8 sin secuencias de bytes inválidas,
    con o sin BOM_UTF8 inicial, y no contiene el carácter nulo U+0000."""
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    return "\x00" not in text


def is_windows1252_text(data: bytes) -> bool:
    """Texto_Windows_1252: no es Texto_UTF8_Válido, no contiene el byte 0x00 y los
    `COUNTED_CONTROLS` representan menos del 1 % del total de bytes."""
    if is_valid_utf8_text(data) or 0x00 in data:
        return False
    # El contenido vacío es Texto_UTF8_Válido, así que aquí tiene al menos un byte
    controls = sum(1 for byte in data if byte in COUNTED_CONTROLS)
    return Fraction(controls, len(data)) < _CONTROL_LIMIT


def is_valid_text(data: bytes) -> bool:
    """Texto_Válido: Texto_UTF8_Válido o Texto_Windows_1252."""
    return is_valid_utf8_text(data) or is_windows1252_text(data)


def decoded_text(data: bytes) -> str:
    """Texto_Decodificado de `data`: UTF-8 sin la BOM_UTF8 inicial si es Texto_UTF8_Válido, o
    Windows-1252 si es Texto_Windows_1252.

    Lanza `ValueError` si `data` no es Texto_Válido.
    """
    if is_valid_utf8_text(data):
        text = data.decode("utf-8")
        # La BOM_UTF8 se decodifica como U+FEFF; solo se quita la del inicio
        return text[1:] if data.startswith(BOM_UTF8) else text
    if is_windows1252_text(data):
        return "".join(_WINDOWS_1252_80_9F.get(byte, chr(byte)) for byte in data)
    raise ValueError("el contenido no es Texto_Válido")


# --- Piezas comunes ---

# Controles de Req 6.15 (U+0000 a U+001F y U+007F a U+009F) salvo tabulación, retorno de carro y
# salto de línea, que se quitan con `controls=False`
_REQ_6_15_CONTROLS: tuple[int, ...] = tuple(
    code for code in (*range(0x00, 0x20), *range(0x7F, 0xA0)) if code not in (0x09, 0x0A, 0x0D)
)
_DROP_CONTROL_CHARS: dict[int, None] = dict.fromkeys(_REQ_6_15_CONTROLS)
# En Windows-1252 esos caracteres vienen de los bytes 0x00 a 0x1F, 0x7F y los sin asignar
_DROP_CONTROL_BYTES: bytes = bytes(
    code for code in _REQ_6_15_CONTROLS if code < 0x80 or code in WINDOWS_1252_UNDEFINED
)

_COUNTED_CONTROL_BYTES: tuple[int, ...] = tuple(sorted(COUNTED_CONTROLS))
# Bytes que no son 0x00 ni controles contados: todo Texto_Windows_1252 se arma con ellos
_TEXT_BYTES: tuple[int, ...] = tuple(b for b in range(0x01, 0x100) if b not in COUNTED_CONTROLS)
# Bytes que nunca aparecen en UTF-8 bien formado: agregar uno vuelve inválido cualquier contenido
_NEVER_IN_UTF8: tuple[int, ...] = (0xC0, 0xC1, *range(0xF5, 0x100))

_MAX_RUN = 300  # tiradas largas, para que los contenidos pasen de 100 bytes


def _run(piece: str, count: int) -> str:
    return piece * count


def _byte_run(byte: int, count: int) -> bytes:
    return bytes([byte]) * count


def _cycled(seed: Sequence[int], count: int) -> bytes:
    """`count` bytes que repiten `seed` en orden."""
    return bytes(seed[index % len(seed)] for index in range(count))


# --- Texto_UTF8_Válido ---

# Caracteres sin U+0000 según los bytes que ocupan en UTF-8, sin sustitutos sueltos
_UTF8_CHARS = st.one_of(
    st.characters(min_codepoint=0x01, max_codepoint=0x7F),
    st.characters(min_codepoint=0x80, max_codepoint=0x7FF),
    st.characters(min_codepoint=0x800, max_codepoint=0xFFFF, codec="utf-8"),
    st.characters(min_codepoint=0x10000, codec="utf-8"),
)

_UTF8_WORDS: tuple[str, ...] = (
    "hola mundo",
    "año",
    "canción",
    "pingüino",
    "¿Qué? ¡Sí!",
    "ÁÉÍÓÚÑÜ",
    "\u201ccomillas\u201d \u2013 \u20ac",
    "Ελληνικά",
    "кириллица",
    "日本語",
    "🙂",
    "👩\u200d💻",
    "\ufeff",  # U+FEFF dentro del texto: no es la BOM_UTF8 inicial y queda en el texto
    "\ufffd",
    "\uffff",
    "\U0010ffff",
    "\u0081\u008d\u008f\u0090\u009d",  # controles C1: en UTF-8 son C2 81, C2 8D, ...
    "\t\n\x0c\r",
    "\x01\x1b\x7f",  # controles: la regla del 1 % es solo de Texto_Windows_1252
)

_UTF8_PIECES = st.one_of(
    st.text(_UTF8_CHARS, max_size=20),
    st.sampled_from(_UTF8_WORDS),
    st.builds(_run, st.one_of(_UTF8_CHARS, st.sampled_from(_UTF8_WORDS)), st.integers(1, _MAX_RUN)),
)


@st.composite
def _utf8_texts(draw: st.DrawFn, bom: bool | None, controls: bool) -> bytes:
    text = "".join(draw(st.lists(_UTF8_PIECES, max_size=20)))
    if not controls:
        text = text.translate(_DROP_CONTROL_CHARS)
    with_bom = draw(st.booleans()) if bom is None else bom
    if with_bom:
        # Un U+FEFF al comienzo de `text` deja dos BOM_UTF8 seguidas; solo la primera es BOM
        return BOM_UTF8 + text.encode("utf-8")
    # Sin BOM_UTF8: el contenido no puede empezar con EF BB BF
    return text.lstrip("\ufeff").encode("utf-8")


def valid_utf8_text(*, bom: bool | None = None, controls: bool = True) -> st.SearchStrategy[bytes]:
    """Texto_UTF8_Válido.

    `bom=True` antepone siempre la BOM_UTF8, `bom=False` nunca la pone y `bom=None` la pone en
    la mitad de los ejemplos. Con `controls=False` el Texto_Decodificado no tiene caracteres de
    control (U+0000 a U+001F y U+007F a U+009F) salvo tabulación, retorno de carro y salto de
    línea.
    """
    return _utf8_texts(bom, controls)


# --- Texto_Windows_1252 ---

_WINDOWS_WORDS: tuple[bytes, ...] = tuple(
    word.encode("cp1252")
    for word in (
        "hola mundo",
        "año",
        "canción",
        "pingüino",
        "¿Qué? ¡Sí!",
        "ÁÉÍÓÚÑÜ",
        "\u201ccomillas\u201d \u2013 \u20ac",
        "Œuvre \u2026 \u2122 Š Ž Ÿ",
        "ÿû",  # FF FB, como el comienzo de una cabecera MP3
        "©®°±µ¶·",
    )
)

# Bytes que parecen UTF-8 pero no son UTF-8 bien formado, la BOM_UTF8 y los bytes sin carácter
# asignado en Windows-1252. Ninguno es 0x00 ni un control contado.
_UTF8_LOOKALIKES: tuple[bytes, ...] = (
    BOM_UTF8,
    b"\xc3",  # comienzo de una secuencia de 2 bytes sin continuación
    b"\xe2\x82",  # secuencia de 3 bytes cortada
    b"\xf0\x9f\x99",  # secuencia de 4 bytes cortada
    b"\x80",  # continuación suelta
    b"\xbf",
    b"\xc0\x80",  # forma sobrelarga de U+0000
    b"\xc1\xbf",  # forma sobrelarga de U+007F
    b"\xe0\x80\xaf",  # forma sobrelarga de "/"
    b"\xed\xa0\x80",  # sustituto U+D800 codificado
    b"\xf4\x90\x80\x80",  # mayor que U+10FFFF
    b"\xf5\x80\x80\x80",
    b"\xfe\xff",  # BOM de UTF-16
    bytes(sorted(WINDOWS_1252_UNDEFINED)),
)

_WINDOWS_PIECES = st.one_of(
    st.lists(st.sampled_from(_TEXT_BYTES), max_size=20).map(bytes),
    st.sampled_from(_WINDOWS_WORDS),
    st.sampled_from(_UTF8_LOOKALIKES),
    # UTF-8 bien formado sin ASCII, que junto a un byte inválido se lee como Windows-1252
    st.text(st.characters(min_codepoint=0x80, codec="utf-8"), min_size=1, max_size=4).map(
        str.encode
    ),
    st.builds(_byte_run, st.sampled_from(_TEXT_BYTES), st.integers(1, _MAX_RUN)),
)


@st.composite
def _with_counted_controls(draw: st.DrawFn, data: bytes) -> bytes:
    """`data`, sin controles contados, con k controles contados insertados, siempre menos del 1 %.

    Con k controles el total es len(data) + k, y 100·k < len(data) + k equivale a
    k ≤ (len(data) - 1) // 99. La mitad de las veces k es ese máximo, justo debajo del límite.
    """
    most = max(0, (len(data) - 1) // 99)
    count = draw(st.one_of(st.just(most), st.integers(0, most)))
    result = bytearray(data)
    for _ in range(count):
        result.insert(
            draw(st.integers(0, len(result))), draw(st.sampled_from(_COUNTED_CONTROL_BYTES))
        )
    return bytes(result)


def _decodes_as_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


@st.composite
def _windows_texts(draw: st.DrawFn, controls: bool) -> bytes:
    pieces = draw(st.lists(_WINDOWS_PIECES, max_size=20))
    if draw(st.integers(0, 3)) == 0:
        # Con la BOM_UTF8 al inicio el contenido no es UTF-8 válido por otros bytes, y la BOM
        # queda en el Texto_Decodificado como "ï»¿"
        pieces.insert(0, BOM_UTF8)
    data = b"".join(pieces)
    if not controls:
        data = data.translate(None, _DROP_CONTROL_BYTES)
    if _decodes_as_utf8(data):
        # Sin 0x00, UTF-8 válido sería Texto_UTF8_Válido: un byte que nunca está en UTF-8 lo
        # impide. En Windows-1252 esos bytes son letras (À, Á, õ a ÿ), no controles.
        at = draw(st.integers(0, len(data)))
        data = data[:at] + bytes([draw(st.sampled_from(_NEVER_IN_UTF8))]) + data[at:]
    if controls:
        # Insertar bytes ASCII no corrige una secuencia UTF-8 inválida
        data = draw(_with_counted_controls(data))
    assert is_windows1252_text(data), data
    return data


def windows1252_text(*, controls: bool = True) -> st.SearchStrategy[bytes]:
    """Texto_Windows_1252.

    Con `controls=True` puede tener controles contados hasta justo debajo del 1 %. Con
    `controls=False` el Texto_Decodificado no tiene caracteres de control (U+0000 a U+001F y
    U+007F a U+009F) salvo tabulación, retorno de carro y salto de línea; por eso tampoco tiene
    los bytes sin carácter asignado, que se leen como controles C1.
    """
    return _windows_texts(controls)


# --- Cualquier secuencia de bytes ---


@st.composite
def _near_control_limit(draw: st.DrawFn) -> bytes:
    """Contenido sin 0x00 que no es UTF-8 válido, con k controles contados en 100·k - 1, 100·k o
    100·k + 1 bytes. Solo el último es Texto_Windows_1252: en los otros los controles son el 1 %
    o más."""
    count = draw(st.integers(1, 3))
    total = 100 * count + draw(st.sampled_from((-1, 0, 1)))
    seed = draw(st.lists(st.sampled_from(_TEXT_BYTES), min_size=1, max_size=10))
    data = bytearray(_cycled(seed, total - count - 1))
    data.insert(draw(st.integers(0, len(data))), draw(st.sampled_from(_NEVER_IN_UTF8)))
    for _ in range(count):
        data.insert(draw(st.integers(0, len(data))), draw(st.sampled_from(_COUNTED_CONTROL_BYTES)))
    return bytes(data)


# Bytes que cambian la clasificación: nulo, controles, avance de página, continuaciones,
# comienzos de secuencia, los de la BOM_UTF8 y uno que nunca está en UTF-8
_SPECIAL_BYTES: tuple[int, ...] = (0x00, 0x01, 0x0C, 0x1F, 0x7F, 0x80, 0x81, 0xBB, 0xBF, 0xC3)
_SPECIAL_BYTES += (0xED, 0xEF, 0xF0, 0xFF)


@st.composite
def _mutated(draw: st.DrawFn) -> bytes:
    """Un Texto_UTF8_Válido o Texto_Windows_1252 con 1 a 4 bytes reemplazados o insertados."""
    data = bytearray(draw(st.one_of(valid_utf8_text(), windows1252_text())))
    for _ in range(draw(st.integers(1, 4))):
        byte = draw(st.one_of(st.sampled_from(_SPECIAL_BYTES), st.integers(0, 0xFF)))
        if data and draw(st.booleans()):
            data[draw(st.integers(0, len(data) - 1))] = byte
        else:
            data.insert(draw(st.integers(0, len(data))), byte)
    return bytes(data)


def arbitrary_bytes() -> st.SearchStrategy[bytes]:
    """Cualquier secuencia de bytes, con Texto_UTF8_Válido, Texto_Windows_1252 y contenidos que no
    son Texto_Válido en proporciones parecidas."""
    return st.one_of(
        valid_utf8_text(),
        windows1252_text(),
        _near_control_limit(),
        _mutated(),
        st.binary(max_size=_MAX_RUN),
    )
