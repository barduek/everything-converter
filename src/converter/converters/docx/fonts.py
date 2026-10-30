"""Fuentes de un DOCX que no están disponibles y su reemplazo (Req 7.4).

Es el paso "Fuentes" de DOCX a PDF en el diseño y se aplica al paquete ya saneado, que no tiene
comentarios ni cambios pendientes:

- `read_document_fonts` reúne las fuentes que usa el paquete y las que trae incrustadas:
  - los `w:font` de la tabla de fuentes (`fontTable.xml`);
  - los `w:rFonts` de los estilos y de los runs: documento principal, encabezados, pies de
    página, notas al pie, notas al final y numeración;
  - las fuentes del tema (`theme1.xml`) a las que apuntan los atributos `w:asciiTheme`,
    `w:hAnsiTheme`, `w:eastAsiaTheme` y `w:cstheme` de esos `w:rFonts`. Como indica ECMA-376,
    el atributo de tema reemplaza al nombre explícito de la misma posición. Si el tema no
    define esa fuente (por ejemplo, `<a:ea typeface=""/>`), se usa el nombre explícito.
  Una fuente está incrustada si su `w:font` tiene `w:embedRegular`, `w:embedBold`,
  `w:embedItalic` o `w:embedBoldItalic` con una relación interna que apunta a una parte del
  paquete.
- `missing_fonts` es el cálculo puro: las fuentes referenciadas que no están incrustadas ni
  instaladas, comparadas sin distinguir mayúsculas.
- `installed_families` lista las familias instaladas con `fc-list`, `match_font` obtiene con
  `fc-match` la familia que fontconfig usa en lugar de una fuente (por ejemplo, Carlito para
  Calibri) y `find_replacements` combina los pasos.

Las partes se ubican por sus relaciones, como define OPC: la parte principal desde
`_rels/.rels` (o `word/document.xml` si el paquete no la declara) y las demás desde las
relaciones de la parte principal. Los nombres de parte se comparan sin distinguir mayúsculas.
Se admiten los espacios de nombres de OOXML de transición y estricto.

El XML se lee con `lxml.etree.iterparse` sin resolver entidades, sin red, sin cargar DTD y con
los límites de tamaño de nodo y de profundidad de libxml2 (`huge_tree=False`). Cada elemento se
descarta después de examinarlo, así que la memoria no crece con el tamaño de la parte.

`fc-list` y `fc-match` deben correr con el mismo entorno de fontconfig que LibreOffice (sin el
`FONTCONFIG_FILE` de la Fuente_Predeterminada), para ver las mismas fuentes. Estas funciones
bloquean hasta que termina cada herramienta.
"""

from __future__ import annotations

import posixpath
import subprocess
import zipfile
import zlib
from collections.abc import Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import IO, Any

# lxml no publica anotaciones de tipos; `etree` queda como Any
from lxml import etree  # type: ignore[import-untyped]

FC_LIST = "fc-list"
FC_MATCH = "fc-match"
FC_TIMEOUT_SECONDS = 30  # por invocación; fontconfig responde en milisegundos con su caché
# Una familia por línea, con todos los nombres de cada fuente (también los alternativos)
FC_LIST_FORMAT = "%{[]family{%{family}\\n}}"
# Primer nombre de familia de la fuente elegida
FC_MATCH_FORMAT = "%{family[0]}"

# Ninguna familia real tiene nombres tan largos (Word admite 31 caracteres). Los nombres más
# largos se ignoran, para no pasar a fc-match argumentos enormes ni informarlos al usuario.
MAX_FONT_NAME_LENGTH = 255
# Cantidad máxima de fuentes faltantes que se consultan con fc-match, una invocación por fuente.
# Las que exceden el tope se informan sin reemplazo, para que un documento con miles de fuentes
# no consuma el Tiempo_Máximo_de_Procesamiento.
MAX_MATCHED_FONTS = 100

DEFAULT_MAIN_PART = "word/document.xml"

_ZIP_ENCRYPTED_FLAG = 0x1  # bit 0 de los indicadores generales de una entrada ZIP

_W_NAMESPACES = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",  # OOXML estricto
)
_A_NAMESPACES = (
    "http://schemas.openxmlformats.org/drawingml/2006/main",
    "http://purl.oclc.org/ooxml/drawingml/main",
)
_R_NAMESPACES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
)
_RELATIONSHIP_TAG = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"

# Tipos de relación, sin el prefijo de `_R_NAMESPACES`
_MAIN_KIND = "officeDocument"
_FONT_TABLE_KIND = "fontTable"
_THEME_KIND = "theme"
_CONTENT_KINDS = frozenset({"styles", "header", "footer", "footnotes", "endnotes", "numbering"})

_EMBED_ELEMENTS = frozenset({"embedRegular", "embedBold", "embedItalic", "embedBoldItalic"})

# Posiciones de `w:rFonts`: atributo con el nombre y atributo con la referencia al tema
_RFONTS_SLOTS = (
    ("ascii", "asciiTheme"),
    ("hAnsi", "hAnsiTheme"),
    ("eastAsia", "eastAsiaTheme"),
    ("cs", "cstheme"),
)
# Valor de la referencia al tema -> (fuente del esquema, elemento con el nombre)
_THEME_REFERENCES = {
    "majorAscii": ("majorFont", "latin"),
    "majorHAnsi": ("majorFont", "latin"),
    "majorEastAsia": ("majorFont", "ea"),
    "majorBidi": ("majorFont", "cs"),
    "minorAscii": ("minorFont", "latin"),
    "minorHAnsi": ("minorFont", "latin"),
    "minorEastAsia": ("minorFont", "ea"),
    "minorBidi": ("minorFont", "cs"),
}

# Caracteres que delimitan la familia en un patrón de fontconfig, más la barra que los escapa
_FC_PATTERN_SPECIAL = frozenset("\\-,:")

_PARSER_OPTIONS: Mapping[str, object] = {
    "resolve_entities": False,
    "no_network": True,
    "huge_tree": False,
    "load_dtd": False,
    "dtd_validation": False,
}


class FontAnalysisError(ValueError):
    """El paquete no tiene parte principal o una de sus partes no se puede leer."""


class FontToolError(RuntimeError):
    """`fc-list` o `fc-match` no se encontró, no terminó a tiempo o terminó con error."""


@dataclass(frozen=True, slots=True)
class DocumentFonts:
    """Fuentes que usa un paquete DOCX y las que trae incrustadas."""

    referenced: frozenset[str]  # incluye las incrustadas
    embedded: frozenset[str]


@dataclass(frozen=True, slots=True)
class FontReplacement:
    """Fuente del documento que no está disponible y la familia que fontconfig usa en su lugar."""

    name: str  # p. ej. "Calibri"
    # p. ej. "Carlito"; None si la fuente excede MAX_MATCHED_FONTS o fc-match no informa ninguna
    replacement: str | None


# --- Cálculo puro ---


def missing_fonts(
    referenced: AbstractSet[str], embedded: AbstractSet[str], installed: AbstractSet[str]
) -> tuple[str, ...]:
    """Fuentes de `referenced` que no están en `embedded` ni en `installed`.

    Los nombres se comparan sin distinguir mayúsculas (`str.casefold`). Si `referenced` tiene
    varias grafías de una misma fuente faltante, se devuelve una sola: la menor según el orden
    de los puntos de código, para que el resultado no dependa del orden de los conjuntos. El
    resultado se ordena sin distinguir mayúsculas.
    """
    available = {name.casefold() for name in embedded} | {name.casefold() for name in installed}
    missing: dict[str, str] = {}
    for name in referenced:
        key = name.casefold()
        if key in available:
            continue
        current = missing.get(key)
        if current is None or name < current:
            missing[key] = name
    return tuple(name for _, name in sorted(missing.items()))


# --- Lectura del paquete ---


def read_document_fonts(archive: zipfile.ZipFile) -> DocumentFonts:
    """Fuentes referenciadas e incrustadas del paquete DOCX abierto en `archive`.

    Lanza `FontAnalysisError` si no existe la parte principal o si una parte que se lee no es
    XML bien formado, está cifrada o no se puede descomprimir.
    """
    package = _Package(archive)
    try:
        return _read_document_fonts(package)
    # zipfile lanza NotImplementedError con un método de compresión que no admite
    except (
        etree.XMLSyntaxError,
        zipfile.BadZipFile,
        zlib.error,
        EOFError,
        NotImplementedError,
    ) as exc:
        raise FontAnalysisError(f"no se pudo leer una parte del paquete: {exc}") from exc


@dataclass(frozen=True, slots=True)
class _Relationship:
    kind: str | None  # tipo sin el prefijo de `_R_NAMESPACES`; None si es de otro vocabulario
    part: zipfile.ZipInfo | None  # None si es externa o si su destino no está en el paquete


class _Package:
    """Partes de un paquete OPC, buscadas sin distinguir mayúsculas como pide OPC."""

    def __init__(self, archive: zipfile.ZipFile) -> None:
        self._archive = archive
        self._parts = {
            info.filename.lower(): info for info in archive.infolist() if not info.is_dir()
        }

    def find(self, name: str) -> zipfile.ZipInfo | None:
        return self._parts.get(name.lower())

    def elements(self, part: zipfile.ZipInfo) -> Iterator[Any]:
        # zipfile lanza RuntimeError con una entrada cifrada; un DOCX no las usa
        if part.flag_bits & _ZIP_ENCRYPTED_FLAG:
            raise FontAnalysisError(f"la parte {part.filename!r} está cifrada")
        with self._archive.open(part) as stream:
            yield from _iter_elements(stream)


def _iter_elements(stream: IO[bytes]) -> Iterator[Any]:
    """Elementos de `stream` al cerrarse, cada uno con sus atributos y los de sus ancestros.

    Al volver del `yield`, el elemento se vacía y se quitan sus hermanos anteriores: quien lo
    recibe puede leer sus atributos y los de su padre, pero no los de sus hijos.
    """
    for _event, element in etree.iterparse(stream, events=("end",), **_PARSER_OPTIONS):
        yield element
        element.clear()
        parent = element.getparent()
        if parent is not None:
            while element.getprevious() is not None:
                del parent[0]


def _read_document_fonts(package: _Package) -> DocumentFonts:
    declared = [rel for rel in _relationships(package, "").values() if rel.kind == _MAIN_KIND]
    main = declared[0].part if declared else package.find(DEFAULT_MAIN_PART)
    if main is None:
        raise FontAnalysisError("el paquete no tiene la parte principal del documento")
    relationships = _relationships(package, main.filename)

    theme_part = _first_part(relationships, _THEME_KIND)
    theme = _theme_fonts(package, theme_part) if theme_part is not None else {}

    referenced: set[str] = set()
    embedded: set[str] = set()
    font_table_part = _first_part(relationships, _FONT_TABLE_KIND)
    if font_table_part is not None:
        names, embedded = _font_table(package, font_table_part)
        referenced |= names

    content_parts = [main]
    content_parts += [
        rel.part
        for rel in relationships.values()
        if rel.kind in _CONTENT_KINDS and rel.part is not None
    ]
    seen: set[str] = set()
    for part in content_parts:
        if part.filename not in seen:
            seen.add(part.filename)
            referenced |= _run_fonts(package, part, theme)
    return DocumentFonts(referenced=frozenset(referenced), embedded=frozenset(embedded))


def _relationships(package: _Package, source: str) -> dict[str, _Relationship]:
    """Relaciones de la parte `source` por su Id; `source` vacío es el paquete."""
    rels_name = posixpath.join(
        posixpath.dirname(source), "_rels", posixpath.basename(source) + ".rels"
    )
    rels_part = package.find(rels_name)
    if rels_part is None:
        return {}
    relationships: dict[str, _Relationship] = {}
    for element in package.elements(rels_part):
        if element.tag != _RELATIONSHIP_TAG:
            continue
        rel_id = _attr(element, "Id")
        target = _attr(element, "Target")
        if not rel_id or target is None or rel_id in relationships:
            continue
        external = _attr(element, "TargetMode") == "External"
        part = None if external else package.find(_resolve_target(source, target))
        relationships[rel_id] = _Relationship(
            kind=_relationship_kind(_attr(element, "Type") or ""), part=part
        )
    return relationships


def _resolve_target(source: str, target: str) -> str:
    # Un destino absoluto parte de la raíz del paquete; uno relativo, de la carpeta de `source`
    if target.startswith("/"):
        return posixpath.normpath(target[1:])
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), target))


def _relationship_kind(rel_type: str) -> str | None:
    for namespace in _R_NAMESPACES:
        prefix = namespace + "/"
        if rel_type.startswith(prefix):
            return rel_type[len(prefix) :]
    return None


def _first_part(relationships: Mapping[str, _Relationship], kind: str) -> zipfile.ZipInfo | None:
    return next(
        (rel.part for rel in relationships.values() if rel.kind == kind and rel.part is not None),
        None,
    )


def _theme_fonts(package: _Package, part: zipfile.ZipInfo) -> dict[str, str]:
    """Fuente del tema para cada valor de referencia (`minorHAnsi`, `majorEastAsia`...)."""
    typefaces: dict[tuple[str, str], str] = {}
    for element in package.elements(part):
        namespace, local = _split(element.tag)
        if namespace not in _A_NAMESPACES or local not in ("latin", "ea", "cs"):
            continue
        # `a:latin` también aparece en propiedades de texto; solo cuenta el esquema de fuentes
        parent = element.getparent()
        if parent is None:
            continue
        parent_namespace, parent_local = _split(parent.tag)
        if parent_namespace not in _A_NAMESPACES or parent_local not in ("majorFont", "minorFont"):
            continue
        name = _clean(_attr(element, "typeface"))
        if name is not None:
            typefaces.setdefault((parent_local, local), name)
    return {
        reference: typefaces[key]
        for reference, key in _THEME_REFERENCES.items()
        if key in typefaces
    }


def _font_table(package: _Package, part: zipfile.ZipInfo) -> tuple[set[str], set[str]]:
    """Nombres de los `w:font` de la tabla de fuentes y los de las fuentes incrustadas."""
    relationships = _relationships(package, part.filename)
    names: set[str] = set()
    embedded: set[str] = set()
    for element in package.elements(part):
        namespace, local = _split(element.tag)
        if namespace not in _W_NAMESPACES:
            continue
        if local == "font":
            name = _clean(_attr(element, f"{{{namespace}}}name"))
            if name is not None:
                names.add(name)
        elif local in _EMBED_ELEMENTS:
            parent = element.getparent()
            if parent is None or parent.tag != f"{{{namespace}}}font":
                continue
            name = _clean(_attr(parent, f"{{{namespace}}}name"))
            rel_id = _relationship_id(element)
            rel = relationships.get(rel_id) if rel_id is not None else None
            if name is not None and rel is not None and rel.part is not None:
                embedded.add(name)
    return names, embedded


def _run_fonts(package: _Package, part: zipfile.ZipInfo, theme: Mapping[str, str]) -> set[str]:
    """Fuentes de los `w:rFonts` de una parte, con las referencias al tema resueltas."""
    fonts: set[str] = set()
    for element in package.elements(part):
        namespace, local = _split(element.tag)
        if local != "rFonts" or namespace not in _W_NAMESPACES:
            continue
        for font_attr, theme_attr in _RFONTS_SLOTS:
            reference = _attr(element, f"{{{namespace}}}{theme_attr}")
            name = theme.get(reference) if reference is not None else None
            if name is None:
                name = _clean(_attr(element, f"{{{namespace}}}{font_attr}"))
            if name is not None:
                fonts.add(name)
    return fonts


def _split(tag: object) -> tuple[str, str]:
    """(espacio de nombres, nombre local) de una etiqueta `{ns}local`."""
    text = str(tag)
    if text.startswith("{"):
        namespace, _, local = text[1:].partition("}")
        return namespace, local
    return "", text


def _attr(element: Any, name: str) -> str | None:
    value = element.get(name)
    return value if isinstance(value, str) else None


def _relationship_id(element: Any) -> str | None:
    for namespace in _R_NAMESPACES:
        value = _attr(element, f"{{{namespace}}}id")
        if value:
            return value
    return None


def _clean(name: str | None) -> str | None:
    """Nombre sin espacios en los extremos; None si queda vacío o supera el tope de largo."""
    if name is None:
        return None
    name = name.strip()
    if not name or len(name) > MAX_FONT_NAME_LENGTH:
        return None
    return name


# --- fontconfig ---


def parse_installed_families(output: str) -> frozenset[str]:
    """Familias de la salida de `fc-list` con `FC_LIST_FORMAT`, una por línea."""
    return frozenset(line.strip() for line in output.split("\n") if line.strip())


def fc_match_pattern(name: str) -> str:
    """Patrón de fontconfig que pide la familia `name` tal cual.

    Escapa con barra invertida los caracteres que cortan la familia en un patrón (`-` inicia
    el tamaño, `:` las propiedades y `,` otra familia) y la propia barra. Como `-` queda
    escapado, el patrón nunca empieza con `-` y fc-match no lo toma como opción.
    """
    return "".join(f"\\{char}" if char in _FC_PATTERN_SPECIAL else char for char in name)


def installed_families(
    *, env: Mapping[str, str] | None = None, timeout: float = FC_TIMEOUT_SECONDS
) -> frozenset[str]:
    """Familias instaladas según `fc-list`, con todos sus nombres. Lanza `FontToolError`."""
    return parse_installed_families(
        _run_fontconfig([FC_LIST, "--format", FC_LIST_FORMAT], env=env, timeout=timeout)
    )


def match_font(
    name: str, *, env: Mapping[str, str] | None = None, timeout: float = FC_TIMEOUT_SECONDS
) -> str | None:
    """Familia que `fc-match` elige para `name`; None si no informa ninguna.

    Lanza `FontToolError`.
    """
    output = _run_fontconfig(
        [FC_MATCH, "--format", FC_MATCH_FORMAT, fc_match_pattern(name)], env=env, timeout=timeout
    )
    return output.strip() or None


def find_replacements(
    archive: zipfile.ZipFile,
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = FC_TIMEOUT_SECONDS,
) -> tuple[FontReplacement, ...]:
    """Fuentes del DOCX que no están incrustadas ni instaladas, con su reemplazo.

    Ejecuta `fc-list` una vez y `fc-match` una vez por fuente faltante, hasta
    `MAX_MATCHED_FONTS`. El orden es el de `missing_fonts`. Lanza `FontAnalysisError` si el
    paquete no se puede leer y `FontToolError` si falla una herramienta de fontconfig.
    """
    fonts = read_document_fonts(archive)
    installed = installed_families(env=env, timeout=timeout)
    missing = missing_fonts(fonts.referenced, fonts.embedded, installed)
    return tuple(
        FontReplacement(
            name=name,
            replacement=(
                match_font(name, env=env, timeout=timeout) if index < MAX_MATCHED_FONTS else None
            ),
        )
        for index, name in enumerate(missing)
    )


def _run_fontconfig(argv: Sequence[str], *, env: Mapping[str, str] | None, timeout: float) -> str:
    program = argv[0]
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=None if env is None else dict(env),
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FontToolError(f"no se encontró {program}") from exc
    except subprocess.TimeoutExpired as exc:
        raise FontToolError(f"{program} no terminó en {timeout} s") from exc
    except OSError as exc:
        raise FontToolError(f"no se pudo ejecutar {program}: {exc.strerror}") from exc
    if completed.returncode != 0:
        raise FontToolError(f"{program} terminó con el código {completed.returncode}")
    return completed.stdout
