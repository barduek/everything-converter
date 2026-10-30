"""Generadores de especificaciones del Registro_de_Conversiones, que usan las propiedades 1 y 8.

Una especificación es un `dict` con las claves de una tabla de `catalog/formats.toml` (Formato) o
de `catalog/pairs.toml` (Par_de_Conversión): lo que reciben `ConversionRegistry.register_format` y
`ConversionRegistry.register_pair`. Cada extracción devuelve objetos nuevos, que el llamador puede
modificar sin afectar a otras extracciones.

- `registry_specs()` genera una secuencia de entradas (`SpecEntry`) para registrar en orden. Cada
  entrada se genera conociendo las anteriores, así puede chocar con ellas (mismo nombre en otras
  mayúsculas, una extensión ya usada, la misma firma escrita de otra forma, un par repetido) o
  formar pares entre los Formatos anteriores.
- `format_specs()` y `pair_specs()` generan una sola especificación a partir de un `SpecContext`.
- `SpecContext` reúne las especificaciones que el llamador ya registró. Por ejemplo, la propiedad 1
  puede crearlo con las especificaciones de los Formatos iniciales, y la propiedad 8 le agrega cada
  entrada que el registro aceptó.

Con `valid_only=False` (predeterminado) se mezclan especificaciones válidas e inválidas: más o
menos la mitad de los Formatos y de los pares llevan uno o dos atributos inválidos
(ausentes, de otro tipo, vacíos, con mayúsculas distintas, fuera de rango o con la forma
equivocada), y los demás pueden chocar con lo que ya tiene el contexto. Los generadores no deciden
si el resultado es válido; eso lo decide el oráculo de cada prueba.

Con `valid_only=True` cada especificación es válida para un registro que contiene exactamente las
entradas del contexto y las anteriores de la secuencia, todas aceptadas: nombre y extensiones que
no usa ningún Formato anterior (sin distinguir mayúsculas), firma distinta de las anteriores y
pares nuevos entre Formatos anteriores, con los convertidores de `converters`. Dos firmas por
bytes distintas pueden igual coincidir en el mismo contenido: si la prueba necesita firmas que no
se solapan, las filtra ella.

`signatures` elige qué Formatos se generan: con firma por bytes (`"magic"`), con firma de sonda
(`"probe"`) o sin firma (`"none"`, Formato_de_Texto). Las firmas por bytes tienen de 1 a 3 partes
distintas, cada una de 1 a 8 bytes al principio del archivo o justo antes del final de los
primeros 64 KiB, con el texto hexadecimal en mayúsculas o en minúsculas y sin espacios.
"""

from __future__ import annotations

import copy
import operator
import string
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from hypothesis import strategies as st

type SpecKind = Literal["format", "pair"]
type SignatureKind = Literal["magic", "probe", "none"]
type Spec = dict[str, object]

# Convertidores que la prueba registra en `ConversionRegistry(converter_ids)`
CONVERTER_IDS: Final[tuple[str, ...]] = (
    "image.raster",
    "image.pdf",
    "text.docs",
    "audio.ffmpeg",
    "transcription.whisper",
)
# Valores admitidos, copiados del glosario de requirements.md
CATEGORIES: Final[tuple[str, ...]] = ("imagen", "documento", "audio", "video")
LOSS_CLASSES: Final[tuple[str, ...]] = ("Sin_Pérdida", "Con_Pérdida", "Limitada", "Aproximada")
MAX_DESCRIPTION: Final = 500  # caracteres de una Descripción_de_Pérdida (Req 4.7)
SIGNATURE_WINDOW: Final = 64 * 1024  # bytes iniciales donde se evalúa una firma por bytes
SIGNATURE_KINDS: Final[tuple[SignatureKind, ...]] = ("none", "magic", "probe")
_SPEC_KINDS: Final[tuple[SpecKind, ...]] = ("format", "pair")


@dataclass(frozen=True, slots=True)
class SpecEntry:
    """Una especificación y el método que la registra: `register_format` o `register_pair`."""

    kind: SpecKind
    spec: Spec


@dataclass(frozen=True, slots=True)
class SpecContext:
    """Especificaciones ya registradas, en orden de registro.

    Guarda las especificaciones tal cual, sin copiarlas: no hay que modificarlas después de
    agregarlas.
    """

    formats: tuple[Mapping[str, object], ...] = ()
    pairs: tuple[Mapping[str, object], ...] = ()

    def with_entry(self, entry: SpecEntry) -> SpecContext:
        """Un contexto nuevo con `entry` agregada al final."""
        if entry.kind == "format":
            return SpecContext((*self.formats, entry.spec), self.pairs)
        return SpecContext(self.formats, (*self.pairs, entry.spec))

    def format_names(self) -> tuple[str, ...]:
        """Nombres de los Formatos, sin repetir."""
        return _unique(name for spec in self.formats if isinstance(name := spec.get("name"), str))

    def extensions(self) -> tuple[str, ...]:
        """Extensiones de los Formatos tal como se declararon, sin repetir."""
        return _unique(
            extension
            for spec in self.formats
            if isinstance(extensions := spec.get("extensions"), list)
            for extension in extensions
            if isinstance(extension, str)
        )

    def signatures(self) -> tuple[Mapping[str, object], ...]:
        """Firmas de los Formatos que tienen una."""
        return tuple(
            signature
            for spec in self.formats
            if isinstance(signature := spec.get("signature"), Mapping)
        )

    def pair_keys(self) -> tuple[tuple[str, str], ...]:
        """Origen y destino de cada par, sin repetir."""
        return _unique(
            (source, target)
            for spec in self.pairs
            if isinstance(source := spec.get("source"), str)
            and isinstance(target := spec.get("target"), str)
        )

    def free_pair_keys(self) -> tuple[tuple[str, str], ...]:
        """Combinaciones de origen y destino entre los Formatos que todavía no son un par."""
        taken = set(self.pair_keys())
        names = self.format_names()
        return tuple((s, t) for s in names for t in names if (s, t) not in taken)


EMPTY_CONTEXT: Final = SpecContext()


def _unique[T](items: Iterable[T]) -> tuple[T, ...]:
    # Conserva el orden: un orden que dependa del hash haría irreproducibles los ejemplos
    return tuple(dict.fromkeys(items))


def _recased(text: str) -> st.SearchStrategy[str]:
    """`text` con otras mayúsculas y minúsculas, sin cambiar su forma en minúsculas."""
    variants = (text.swapcase(), text.upper(), text.lower(), text)
    return st.sampled_from(_unique(v for v in variants if v.lower() == text.lower()))


# --------------------------------------------------------------------------------------------
# Atributos válidos de un Formato

_ASCII_ALNUM: Final = string.ascii_letters + string.digits
_NAME_CHARS: Final = _ASCII_ALNUM + "._+-"
_MAX_NAME: Final = 32
_POOL_NAMES: Final = (
    "PNG",
    "JPG",
    "WEBP",
    "PDF",
    "TXT",
    "MD",
    "HTML",
    "DOCX",
    "MP3",
    "WAV",
    "OGG",
    "MP4",
    "WEBM",
    "SRT",
)


def _names_of_length(min_rest: int, max_rest: int) -> st.SearchStrategy[str]:
    """Letra o dígito ASCII seguido de `min_rest` a `max_rest` caracteres de nombre."""
    return st.builds(
        operator.add,
        st.sampled_from(_ASCII_ALNUM),
        st.text(_NAME_CHARS, min_size=min_rest, max_size=max_rest),
    )


@st.composite
def _names(draw: st.DrawFn, context: SpecContext, valid_only: bool) -> str:
    taken = {name.lower() for name in context.format_names()}
    pool = [name for name in _POOL_NAMES if not valid_only or name.lower() not in taken]
    options = ["pool"] if pool else []
    options += ["fresh", "fresh", "longest"]
    if not valid_only and taken:
        options.append("context")  # el nombre de un Formato anterior, en otras mayúsculas
    choice = draw(st.sampled_from(options))
    if choice == "context":
        return draw(st.sampled_from(context.format_names()).flatmap(_recased))
    strategy = {
        "pool": st.sampled_from(pool),
        "fresh": _names_of_length(0, _MAX_NAME - 1),
        "longest": _names_of_length(_MAX_NAME - 1, _MAX_NAME - 1),
    }[choice]
    if valid_only:
        strategy = strategy.filter(lambda name: name.lower() not in taken)
    return draw(strategy)


_POOL_EXTENSIONS: Final = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".pdf",
    ".txt",
    ".md",
    ".html",
    ".htm",
    ".docx",
    ".mp3",
    ".wav",
    ".ogg",
    ".mp4",
    ".webm",
    ".srt",
)
# Caracteres de extensión: ninguno es un punto ni un Carácter_No_Permitido. Los no ASCII prueban
# que la comparación sin mayúsculas usa la forma en minúsculas ("Ü" y "ü"; "ß" no cambia).
_EXTENSION_CHARS: Final = _ASCII_ALNUM + "-_+~!#éÜß"


def _fresh_extensions() -> st.SearchStrategy[str]:
    return st.text(_EXTENSION_CHARS, min_size=1, max_size=5).map(lambda rest: "." + rest)


def _extensions(context: SpecContext, valid_only: bool) -> st.SearchStrategy[str]:
    taken = {extension.lower() for extension in context.extensions()}
    pool = [ext for ext in _POOL_EXTENSIONS if not valid_only or ext.lower() not in taken]
    branches: list[st.SearchStrategy[str]] = []
    if pool:
        branches.append(st.sampled_from(pool).flatmap(_recased))
    branches += [_fresh_extensions()] * 3
    if not valid_only and context.extensions():
        branches.append(st.sampled_from(context.extensions()).flatmap(_recased))
    strategy = st.one_of(branches)
    if valid_only:
        strategy = strategy.filter(lambda extension: extension.lower() not in taken)
    return strategy


_POOL_MIME_TYPES: Final = (
    "image/png",
    "image/jpeg",
    "text/plain; charset=utf-8",
    "text/markdown; charset=utf-8",
    "application/octet-stream",
    "audio/mpeg",
    "video/mp4",
)


def _mime_types() -> st.SearchStrategy[str]:
    # ASCII imprimible, con espacios solo en el medio
    fresh = st.from_regex(r"[!-~]([ -~]{0,30}[!-~])?", fullmatch=True)
    return st.sampled_from(_POOL_MIME_TYPES) | fresh


_POOL_PROBES: Final = ("pdf_header", "ooxml_wordprocessing", "mpeg_audio", "isobmff_mp4")
_PROBE_CHARS: Final = string.ascii_lowercase + string.digits + "_"
_POOL_INSPECTORS: Final = ("image", "text", "pdf", "docx", "audio", "video")


def _fresh_probes() -> st.SearchStrategy[str]:
    return st.text(_PROBE_CHARS, min_size=1, max_size=12)


@st.composite
def _magic_part(draw: st.DrawFn) -> tuple[int, bytes]:
    data = draw(st.binary(min_size=1, max_size=8))
    if draw(st.booleans()):
        # Termina en el último byte de los primeros 64 KiB o un poco antes
        return SIGNATURE_WINDOW - len(data) - draw(st.integers(0, 3)), data
    return draw(st.integers(0, 64)), data


def _magic_parts() -> st.SearchStrategy[list[tuple[int, bytes]]]:
    # Partes distintas: una parte repetida haría ambigua la identidad de la firma
    return st.lists(_magic_part(), min_size=1, max_size=3, unique=True)


def _render_magic(parts: Iterable[tuple[int, bytes]], upper: bool) -> Spec:
    return {
        "magic": [
            {"offset": offset, "hex": data.hex().upper() if upper else data.hex()}
            for offset, data in parts
        ]
    }


def _parsed_magic(signature: Mapping[str, object]) -> list[tuple[int, bytes]] | None:
    """Partes (desplazamiento, bytes) de una firma por bytes bien formada, o None."""
    parts = signature.get("magic")
    if not isinstance(parts, list):
        return None
    parsed: list[tuple[int, bytes]] = []
    for part in parts:
        if not isinstance(part, Mapping):
            return None
        offset, hex_text = part.get("offset"), part.get("hex")
        if not isinstance(offset, int) or not isinstance(hex_text, str):
            return None
        try:
            parsed.append((offset, bytes.fromhex(hex_text)))
        except ValueError:
            return None
    return parsed


def _signature_key(signature: Mapping[str, object]) -> tuple[str, object] | None:
    """Identidad de una firma bien formada, o None si no tiene esa forma.

    Dos firmas por bytes son la misma si tienen los mismos pares (desplazamiento, bytes) en
    cualquier orden; dos firmas de sonda, si nombran la misma sonda.
    """
    probe = signature.get("probe")
    if isinstance(probe, str):
        return ("probe", probe)
    parts = _parsed_magic(signature)
    return None if parts is None else ("magic", tuple(sorted(parts)))


@st.composite
def _same_signature(draw: st.DrawFn, signature: Mapping[str, object]) -> Spec:
    """La misma firma escrita de otra forma: partes en otro orden y hex en otras mayúsculas."""
    parts = signature.get("magic")
    if not isinstance(parts, list) or not all(isinstance(part, Mapping) for part in parts):
        return copy.deepcopy(dict(signature))
    rewritten: list[object] = []
    for part in draw(st.permutations(parts)):
        hex_text = part.get("hex")
        recased = draw(_recased(hex_text)) if isinstance(hex_text, str) else hex_text
        rewritten.append({**copy.deepcopy(dict(part)), "hex": recased})
    return {"magic": rewritten}


@st.composite
def _similar_signature(draw: st.DrawFn, signature: Mapping[str, object]) -> Spec:
    """Una firma parecida pero distinta: una parte más o menos, un desplazamiento movido u otra
    sonda cuyo nombre difiere en mayúsculas o en un sufijo."""
    probe = signature.get("probe")
    if isinstance(probe, str):
        return {"probe": draw(st.sampled_from((probe.upper(), probe + "_v2")))}
    parts = _parsed_magic(signature)
    if not parts:
        return copy.deepcopy(dict(signature))
    change = draw(st.sampled_from(("add", "drop", "shift")))
    if change == "drop" and len(parts) > 1:
        parts.pop(draw(st.integers(0, len(parts) - 1)))
    elif change == "shift":
        index = draw(st.integers(0, len(parts) - 1))
        offset, data = parts[index]
        moved = offset + 1 if offset + 1 + len(data) <= SIGNATURE_WINDOW else offset - 1
        parts[index] = (moved, data)
    else:
        parts.append(draw(_magic_part()))
    shuffled = draw(st.permutations(_unique(parts)))
    return _render_magic(shuffled, draw(st.booleans()))


def _matches_kinds(signature: Mapping[str, object], kinds: Collection[SignatureKind]) -> bool:
    return ("magic" in signature and "magic" in kinds) or (
        "probe" in signature and "probe" in kinds
    )


@st.composite
def _signatures(
    draw: st.DrawFn,
    context: SpecContext,
    valid_only: bool,
    kinds: Collection[SignatureKind],
) -> Spec | None:
    declared = [s for s in context.signatures() if _matches_kinds(s, kinds)]
    options: list[str] = [kind for kind in SIGNATURE_KINDS if kind in kinds]
    if not valid_only and declared:
        options += ["same", "similar"]
    choice = draw(st.sampled_from(options))
    if choice == "none":
        return None
    if choice == "same":
        return draw(st.sampled_from(declared).flatmap(_same_signature))
    if choice == "similar":
        return draw(st.sampled_from(declared).flatmap(_similar_signature))
    taken: set[tuple[str, object] | None] = set()
    if valid_only:
        taken = {_signature_key(signature) for signature in context.signatures()}
    if choice == "probe":
        pool = [probe for probe in _POOL_PROBES if ("probe", probe) not in taken]
        probes = (st.sampled_from(pool) | _fresh_probes()) if pool else _fresh_probes()
        probe = draw(probes.filter(lambda name: ("probe", name) not in taken))
        return {"probe": probe}
    parts = draw(_magic_parts().filter(lambda ps: ("magic", tuple(sorted(ps))) not in taken))
    return _render_magic(parts, draw(st.booleans()))


def _extra_keys() -> st.SearchStrategy[dict[str, object]]:
    # Claves que el registro no conoce y debe ignorar
    values: st.SearchStrategy[object] = st.text(max_size=5) | st.integers()
    return st.dictionaries(st.sampled_from(("notas", "version", "alias")), values, max_size=1)


# --------------------------------------------------------------------------------------------
# Atributos inválidos

type _Mutation = Callable[[st.DrawFn, Spec], None]

_DELETE: Final = object()  # quita la clave de la especificación


def _replace(key: str, values: Sequence[object]) -> _Mutation:
    """Reemplaza el valor de `key` por uno de `values` o, con `_DELETE`, quita la clave."""

    def mutate(draw: st.DrawFn, spec: Spec) -> None:
        value = draw(st.sampled_from(values))
        if value is _DELETE:
            spec.pop(key, None)
        else:
            spec[key] = copy.deepcopy(value)

    return mutate


_BAD_NAMES: Final = (
    _DELETE,
    None,
    "",
    7,
    ["PNG"],
    "_PNG",
    ".png",
    "-x",
    "+x",
    "PN G",
    "PNG/1",
    "Ñandú",
    "a\x00",
    "PNG\n",
)
_BAD_CATEGORIES: Final = (_DELETE, None, "", "Imagen", "IMAGEN", "texto", "image", 3, ["imagen"])
_BAD_EXTENSION_LISTS: Final[tuple[object, ...]] = (_DELETE, None, [], ".png", {"ext": ".png"}, 5)
_BAD_EXTENSION_ITEMS: Final = (
    "png",
    ".",
    "",
    "..",
    ".tar.gz",
    ".a/b",
    ".a\\b",
    ".a:b",
    ".a*",
    ".a?",
    '.a"',
    ".a<",
    ".a>",
    ".a|",
    ".a\x00",
    ".a\x1f",
    ".a\x7f",
    5,
    None,
)
_BAD_PRIMARY_EXTENSIONS: Final = (_DELETE, None, "", ".", "png", ".a/b", 5)
_BAD_MIME_TYPES: Final = (
    _DELETE,
    None,
    "",
    " image/png",
    "image/png ",
    "imagen/ñ",
    "a\tb",
    "a\nb",
    "\x7f",
    5,
)
_ONE_PART: Final = [{"offset": 0, "hex": "89"}]
_BAD_SIGNATURES: Final[tuple[object, ...]] = (
    "89504E47",
    ["89504E47"],
    {},
    {"magic": _ONE_PART, "probe": "pdf_header"},
    {"magic": _ONE_PART, "notas": 1},
    {"magic": []},
    {"magic": "89"},
    {"magic": None},
    {"magic": {"offset": 0, "hex": "89"}},
    {"probe": ""},
    {"probe": 3},
    {"probe": None},
    {"probe": ["pdf_header"]},
)
_BAD_MAGIC_PARTS: Final = (
    {"offset": -1, "hex": "89"},
    {"offset": True, "hex": "89"},
    {"offset": 1.5, "hex": "89"},
    {"offset": "0", "hex": "89"},
    {"offset": None, "hex": "89"},
    {"offset": 0, "hex": ""},
    {"offset": 0, "hex": "ABC"},
    {"offset": 0, "hex": "zz"},
    {"offset": 0, "hex": "0x89"},
    {"offset": 0, "hex": 137},
    {"offset": 0, "hex": None},
    {"offset": 0},
    {"hex": "89"},
    {"offset": 0, "hex": "89", "notas": 1},
    [0, "89"],
    "8950",
)
_BAD_INSPECTORS: Final = ("", 5, False, ["image"])
_BAD_VARIANT_PROBES: Final = ("docx_encrypted", {"a": 1}, [""], [3], [None], ["a", "a"])


def _long_name(draw: st.DrawFn, spec: Spec) -> None:
    spec["name"] = draw(_names_of_length(_MAX_NAME, _MAX_NAME))  # 33 caracteres


def _bad_extension_item(draw: st.DrawFn, spec: Spec) -> None:
    extensions = spec.get("extensions")
    if isinstance(extensions, list) and extensions:
        index = draw(st.integers(0, len(extensions) - 1))
        extensions[index] = draw(st.sampled_from(_BAD_EXTENSION_ITEMS))


def _repeated_extension(draw: st.DrawFn, spec: Spec) -> None:
    # La misma extensión dos veces en el Formato, quizá con otras mayúsculas
    extensions = spec.get("extensions")
    if isinstance(extensions, list) and extensions:
        repeated = draw(st.sampled_from(extensions))
        if isinstance(repeated, str):
            extensions.insert(draw(st.integers(0, len(extensions))), draw(_recased(repeated)))


def _unlisted_primary_extension(draw: st.DrawFn, spec: Spec) -> None:
    spec["primary_extension"] = draw(_fresh_extensions())


def _bad_magic_part(draw: st.DrawFn, spec: Spec) -> None:
    # Partes válidas y una inválida en cualquier lugar: el registro revisa todas
    rendered = _render_magic(draw(_magic_parts()), draw(st.booleans()))["magic"]
    assert isinstance(rendered, list)
    bad = copy.deepcopy(draw(st.sampled_from(_BAD_MAGIC_PARTS)))
    rendered.insert(draw(st.integers(0, len(rendered))), bad)
    spec["signature"] = {"magic": rendered}


def _magic_past_window(draw: st.DrawFn, spec: Spec) -> None:
    # Una parte que termina de 1 a 3 bytes después de los primeros 64 KiB
    data = draw(st.binary(min_size=1, max_size=8))
    offset = SIGNATURE_WINDOW - len(data) + draw(st.integers(1, 3))
    spec["signature"] = _render_magic([(offset, data)], draw(st.booleans()))


_FORMAT_MUTATIONS: Final[tuple[_Mutation, ...]] = (
    _replace("name", _BAD_NAMES),
    _long_name,
    _replace("category", _BAD_CATEGORIES),
    _replace("extensions", _BAD_EXTENSION_LISTS),
    _bad_extension_item,
    _repeated_extension,
    _replace("primary_extension", _BAD_PRIMARY_EXTENSIONS),
    _unlisted_primary_extension,
    _replace("mime_type", _BAD_MIME_TYPES),
    _replace("signature", _BAD_SIGNATURES),
    _bad_magic_part,
    _magic_past_window,
    _replace("inspector", _BAD_INSPECTORS),
    _replace("variant_probes", _BAD_VARIANT_PROBES),
)

_BAD_ENDPOINTS: Final = (_DELETE, None, "", 5, ["PNG"])
_BAD_LOSS_CLASSES: Final = (
    _DELETE,
    None,
    "",
    1,
    "sin_pérdida",
    "SIN_PÉRDIDA",
    "Sin_Perdida",
    "Con Pérdida",
    "Exacta",
)
_BLANKS: Final = ("", " ", "   ", "\t\n", "\u00a0\u3000", "\u2028")
_BAD_DESCRIPTIONS: Final = (*_BLANKS, None, 5, ["Pérdida"], " " * (MAX_DESCRIPTION + 1))
_BAD_CONVERTERS: Final = (_DELETE, None, "", 5, "image.desconocido", "IMAGE.RASTER")


def _recased_endpoint(key: str) -> _Mutation:
    # Los nombres de Formato de un par se comparan tal cual: otras mayúsculas no son el Formato
    def mutate(draw: st.DrawFn, spec: Spec) -> None:
        value = spec.get(key)
        if isinstance(value, str):
            spec[key] = draw(_recased(value))

    return mutate


def _loss_class_list(draw: st.DrawFn, spec: Spec) -> None:
    # Una lista no es una Clasificación_de_Pérdida, aunque tenga un solo valor (Req 4.12)
    first = spec.get("loss_class", LOSS_CLASSES[0])
    other = draw(st.sampled_from(LOSS_CLASSES))
    spec["loss_class"] = draw(st.sampled_from(([first], [first, other])))


def _texts_of_length(length: int) -> st.SearchStrategy[str]:
    # Termina en "á", que ocupa 2 bytes en UTF-8: el límite cuenta caracteres, no bytes
    head = st.text("abcdefghijklmnñopqrstuvwxyz áéíóú", max_size=10)
    return head.map(lambda text: (text + "á" * length)[:length])


def _long_description(draw: st.DrawFn, spec: Spec) -> None:
    spec["loss_description"] = draw(_texts_of_length(MAX_DESCRIPTION + 1))


def _any_of(*mutations: _Mutation) -> _Mutation:
    """Aplica una de `mutations`."""

    def mutate(draw: st.DrawFn, spec: Spec) -> None:
        draw(st.sampled_from(mutations))(draw, spec)

    return mutate


# Un grupo por regla de la tabla "Validación del registro", para que las últimas reglas se
# prueben tan seguido como las primeras
_PAIR_MUTATIONS: Final[tuple[_Mutation, ...]] = (
    _any_of(
        _replace("source", _BAD_ENDPOINTS),
        _recased_endpoint("source"),
        _replace("target", _BAD_ENDPOINTS),
        _recased_endpoint("target"),
    ),
    _any_of(_replace("loss_class", _BAD_LOSS_CLASSES), _loss_class_list),
    _replace("loss_description", _BAD_DESCRIPTIONS),
    _long_description,
    _replace("converter", _BAD_CONVERTERS),
)


# --------------------------------------------------------------------------------------------
# Especificaciones


@st.composite
def _format_spec(
    draw: st.DrawFn,
    context: SpecContext,
    valid_only: bool,
    kinds: Collection[SignatureKind],
) -> Spec:
    extensions = draw(
        st.lists(_extensions(context, valid_only), min_size=1, max_size=3, unique_by=str.lower)
    )
    spec: Spec = {
        "name": draw(_names(context, valid_only)),
        "category": draw(st.sampled_from(CATEGORIES)),
        "extensions": extensions,
        # La Extensión_Principal puede tener otras mayúsculas que la de la lista
        "primary_extension": draw(st.sampled_from(extensions).flatmap(_recased)),
        "mime_type": draw(_mime_types()),
    }
    signature = draw(_signatures(context, valid_only, kinds))
    if signature is not None:
        spec["signature"] = signature
    inspector = draw(st.none() | st.sampled_from(_POOL_INSPECTORS))
    if inspector is not None:
        spec["inspector"] = inspector
    probe_names = st.sampled_from(("docx_encrypted", *_POOL_PROBES))
    probes = draw(st.none() | st.lists(probe_names, max_size=2, unique=True))
    if probes is not None:
        spec["variant_probes"] = probes
    spec.update(draw(_extra_keys()))
    if not valid_only and draw(st.booleans()):
        for mutate in draw(st.lists(st.sampled_from(_FORMAT_MUTATIONS), min_size=1, max_size=2)):
            mutate(draw, spec)
    return spec


@st.composite
def _endpoints(draw: st.DrawFn, names: Sequence[str]) -> str:
    """Un Formato anterior casi siempre; a veces con otras mayúsculas o uno que no existe."""
    options = ["registered"] * 5 + ["recased", "unknown"] if names else ["unknown"]
    choice = draw(st.sampled_from(options))
    if choice == "unknown":
        return draw(st.sampled_from(_POOL_NAMES) | _names_of_length(0, 8))
    name = draw(st.sampled_from(names))
    return draw(_recased(name)) if choice == "recased" else name


def _descriptions(loss_class: str) -> st.SearchStrategy[str | None]:
    """Descripción_de_Pérdida válida para `loss_class`; None deja la clave ausente."""
    text = st.text(st.characters(codec="utf-8"), min_size=1, max_size=60).filter(
        lambda value: not value.isspace()
    )
    valid: list[st.SearchStrategy[str | None]] = [text, _texts_of_length(MAX_DESCRIPTION)]
    if loss_class == LOSS_CLASSES[0]:
        # Sin_Pérdida admite una descripción ausente, vacía o en blanco
        valid = [st.none(), st.sampled_from(_BLANKS), *valid]
    return st.one_of(valid)


@st.composite
def _pair_spec(
    draw: st.DrawFn,
    context: SpecContext,
    converters: Sequence[str],
    valid_only: bool,
) -> Spec:
    names = context.format_names()
    existing = context.pair_keys()
    if valid_only:
        source, target = draw(st.sampled_from(context.free_pair_keys()))
    elif existing and draw(st.sampled_from(("new",) * 5 + ("existing",))) != "new":
        source, target = draw(st.sampled_from(existing))  # un par ya registrado
    else:
        source, target = draw(_endpoints(names)), draw(_endpoints(names))
    loss_class = draw(st.sampled_from(LOSS_CLASSES))
    spec: Spec = {"source": source, "target": target, "loss_class": loss_class}
    description = draw(_descriptions(loss_class))
    if description is not None:
        spec["loss_description"] = description
    spec["converter"] = draw(st.sampled_from(converters))
    spec.update(draw(_extra_keys()))
    if not valid_only and draw(st.booleans()):
        for mutate in draw(st.lists(st.sampled_from(_PAIR_MUTATIONS), min_size=1, max_size=2)):
            mutate(draw, spec)
    return spec


def _check_kinds(signatures: Collection[SignatureKind]) -> tuple[SignatureKind, ...]:
    kinds = tuple(kind for kind in SIGNATURE_KINDS if kind in signatures)
    if not kinds or len(kinds) != len(set(signatures)):
        raise ValueError(f"signatures debe ser un subconjunto no vacío de {SIGNATURE_KINDS}")
    return kinds


def format_specs(
    context: SpecContext = EMPTY_CONTEXT,
    *,
    valid_only: bool = False,
    signatures: Collection[SignatureKind] = SIGNATURE_KINDS,
) -> st.SearchStrategy[Spec]:
    """Especificación de un Formato para registrar después de las de `context`."""
    return _format_spec(context, valid_only, _check_kinds(signatures))


def pair_specs(
    context: SpecContext = EMPTY_CONTEXT,
    *,
    converters: Sequence[str] = CONVERTER_IDS,
    valid_only: bool = False,
) -> st.SearchStrategy[Spec]:
    """Especificación de un Par_de_Conversión para registrar después de las de `context`.

    Con `valid_only=True`, `context` debe tener al menos una combinación libre de origen y
    destino (`free_pair_keys`) y `converters` no puede estar vacío.
    """
    if not converters:
        raise ValueError("converters no puede estar vacío")
    if valid_only and not context.free_pair_keys():
        raise ValueError("el contexto no tiene Formatos entre los que falte un par")
    return _pair_spec(context, tuple(converters), valid_only)


@st.composite
def _registry_specs(
    draw: st.DrawFn,
    context: SpecContext,
    converters: tuple[str, ...],
    valid_only: bool,
    kinds: tuple[SignatureKind, ...],
    min_size: int,
    max_size: int,
) -> list[SpecEntry]:
    entries: list[SpecEntry] = []
    for _ in range(draw(st.integers(min_size, max_size))):
        can_pair = bool(converters) and (not valid_only or bool(context.free_pair_keys()))
        kind: SpecKind = draw(st.sampled_from(_SPEC_KINDS)) if can_pair else "format"
        if kind == "format":
            spec = draw(_format_spec(context, valid_only, kinds))
        else:
            spec = draw(_pair_spec(context, converters, valid_only))
        entry = SpecEntry(kind, spec)
        entries.append(entry)
        context = context.with_entry(entry)
    return entries


def registry_specs(
    *,
    context: SpecContext = EMPTY_CONTEXT,
    converters: Sequence[str] = CONVERTER_IDS,
    valid_only: bool = False,
    signatures: Collection[SignatureKind] = SIGNATURE_KINDS,
    min_size: int = 0,
    max_size: int = 12,
) -> st.SearchStrategy[list[SpecEntry]]:
    """Secuencia de `min_size` a `max_size` especificaciones de Formatos y de pares, en el orden
    en que se registran después de las de `context`.

    Cada entrada supone que las anteriores de la secuencia quedaron registradas. Con
    `valid_only=False`, si una de ellas se rechaza, algunos conflictos o pares que la toman como
    referencia dejan de serlo, lo que no cambia la validez de la prueba. Con `converters` vacío
    no se generan pares.
    """
    if not 0 <= min_size <= max_size:
        raise ValueError(f"se requiere 0 <= min_size <= max_size: {min_size}, {max_size}")
    return _registry_specs(
        context, tuple(converters), valid_only, _check_kinds(signatures), min_size, max_size
    )
