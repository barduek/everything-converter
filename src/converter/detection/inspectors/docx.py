"""Inspector `docx`: Límites_de_Descompresión, daño del paquete y contenido que se omite al
convertir (Req 7.7, 16.1, 16.2).

Un DOCX es un paquete ZIP (OPC). El inspector lo analiza en dos pasos y devuelve un `DocxInfo`, con
lo que el convertidor informa al usuario, o un Rechazo_de_Origen.

**Rechazos**, en el orden de `INSPECTION_REJECTIONS`:

1. `docx_excede_limites_descompresion` (Req 7.7): el paquete supera los Límites_de_Descompresión
   (`zipcheck.check_zip_package`): la suma de los Tamaños_Descomprimidos, la cantidad de
   Entradas_ZIP o la Razón_de_Compresión de una entrada.
2. `archivo_danado` (Req 16.2): el directorio central o una entrada del paquete son ilegibles, o
   no se puede leer una parte que el inspector necesita (ver más abajo).

Los Límites_de_Descompresión van antes que "archivo dañado" porque Req 16.2 excluye de ese motivo
a los DOCX que los superan. `classify_zip_verdict` ya les da esa prioridad, también cuando el
mismo paquete tiene una entrada superada y otra dañada. Solo después de que el paquete pasa esa
comprobación se lee con `zipfile`, que a partir de ahí puede confiar en los tamaños: ninguna parte
produce más bytes de los que declara, y en total son como mucho 512 000 000.

DOCX_Cifrado (`docx_cifrado`, Req 7.2) no se decide aquí: lo detecta la sonda de variante
`docx_encrypted` del Detector_de_Tipo, porque un documento cifrado es un archivo OLE y no un ZIP.

**Datos de `DocxInfo`**, que sirven para los avisos de Req 7.5 y 7.6:

- `has_macros`: el paquete tiene un proyecto de VBA, es decir, una parte `vbaProject.bin` o una
  relación de tipo `vbaProject`. Los controles ActiveX no cuentan como macros.
- `has_comments`: alguna parte de comentarios (relación `comments` de la parte principal) tiene al
  menos un `w:comment`. Una parte de comentarios vacía, que Word deja al borrar el último, no
  cuenta: no hay nada que omitir.
- `has_tracked_changes`: la parte principal, un encabezado, un pie de página, las notas al pie o
  las notas al final tienen cambios pendientes del control de cambios: inserciones, eliminaciones,
  movimientos o cambios de propiedades (`w:ins`, `w:del`, `w:moveFrom`, `w:moveTo`,
  `w:rPrChange`...). Que el control de cambios esté activado (`w:trackRevisions`) no es un cambio.
- `external_relationships`: cantidad de relaciones con `TargetMode="External"` de todo el paquete
  que no son hipervínculos (imágenes vinculadas, plantillas, objetos OLE...). Son los
  Recursos_Externos del DOCX (glosario): los enlaces de navegación no lo son. Se cuenta cada
  relación, no cada destino distinto.

**Lectura del XML.** La parte principal es el destino de la relación `officeDocument` de
`_rels/.rels` o, si el paquete no la declara, `word/document.xml`. Las demás partes se ubican por
las relaciones de la parte principal, como define OPC, y los nombres de parte se comparan sin
distinguir mayúsculas. Se admiten los espacios de nombres de OOXML de transición y estricto. Los
destinos con una parte que no existe no se siguen, salvo el de la parte principal: si falta, el
paquete está dañado.

Las partes se leen con lxml en modo SAX (un objeto `target` que recibe solo los inicios de
elemento), sin construir el árbol, así que la memoria no depende del tamaño de la parte. El
analizador no resuelve entidades, no accede a la red, no carga el DTD ni lo valida y conserva los
límites de libxml2 (`huge_tree=False`): un archivo con una entidad externa no lee nada y uno con
expansión de entidades excesiva está dañado. Una parte que se lee y no es XML bien formado deja el
paquete dañado. Solo se abren las partes de relaciones, la principal, las de encabezados, pies de
página, notas y comentarios; el resto (estilos, numeración, imágenes) no se lee, y su daño lo
detecta la conversión (Req 16.2, "desde en_proceso").

Este módulo importa lxml dentro de las funciones, porque el proceso principal y cada worker
importan todos los inspectores solo para conocer sus nombres y sus tipos de `SourceInfo` (ver
`base.py`).
"""

from __future__ import annotations

import posixpath
import zipfile
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from converter.converters.errors import classify_zip_verdict
from converter.detection.inspectors.base import InspectorInput, register_inspector
from converter.detection.source_info import SourceInfo, SourceRejection, register_source_info
from converter.detection.zipcheck import check_zip_package
from converter.messages_es import FailureReason

PACKAGE_RELATIONSHIPS = "_rels/.rels"  # relaciones del paquete, en minúsculas
DEFAULT_MAIN_PART = "word/document.xml"  # parte principal si el paquete no declara otra

_READ_BLOCK = 64 * 1024  # bytes que se entregan por vez al analizador de lxml
_RELATIONSHIPS_SUFFIX = ".rels"
_RELATIONSHIPS_DIRECTORY = "_rels"
_VBA_PROJECT_PART = "vbaproject.bin"  # nombre de la parte de macros, en minúsculas

_W_NAMESPACES = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",  # OOXML estricto
)
_RELATIONSHIP_TAG = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
# Prefijos de los tipos de relación de Office, de transición y estricto
_OFFICE_DOCUMENT_PREFIXES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/",
    "http://purl.oclc.org/ooxml/officeDocument/relationships/",
)
_VBA_PROJECT_TYPE = "http://schemas.microsoft.com/office/2006/relationships/vbaProject"

# Tipos de relación, sin el prefijo de `_OFFICE_DOCUMENT_PREFIXES`
_MAIN_KIND = "officeDocument"
_HYPERLINK_KIND = "hyperlink"
_COMMENTS_KIND = "comments"
# Partes, relacionadas con la principal, que pueden tener cambios pendientes
_CHANGES_KINDS = frozenset({"header", "footer", "footnotes", "endnotes"})

# Elementos del control de cambios: inserciones, eliminaciones y movimientos de contenido, de filas
# y de celdas, y cambios de propiedades (de ejecuciones, párrafos, secciones, tablas y numeración)
_TRACKED_CHANGE_NAMES = (
    "ins",
    "del",
    "moveFrom",
    "moveTo",
    "rPrChange",
    "pPrChange",
    "sectPrChange",
    "tblPrChange",
    "tblPrExChange",
    "tblGridChange",
    "trPrChange",
    "tcPrChange",
    "numberingChange",
    "cellIns",
    "cellDel",
    "cellMerge",
)


def _w_tags(*names: str) -> frozenset[str]:
    """Nombres de elemento en notación de Clark (`{espacio}nombre`) en ambos espacios de nombres."""
    return frozenset(f"{{{namespace}}}{name}" for namespace in _W_NAMESPACES for name in names)


_TRACKED_CHANGE_TAGS = _w_tags(*_TRACKED_CHANGE_NAMES)
_COMMENT_TAGS = _w_tags("comment")

# Opciones del analizador de lxml: sin entidades, sin red y sin DTD
_PARSER_OPTIONS: Mapping[str, object] = {
    "resolve_entities": False,
    "no_network": True,
    "huge_tree": False,
    "load_dtd": False,
    "dtd_validation": False,
}


@register_source_info("docx")
@dataclass(frozen=True, slots=True)
class DocxInfo(SourceInfo):
    """Información de un DOCX que produce el inspector `docx`."""

    has_macros: bool  # proyecto de VBA (Req 7.5)
    has_comments: bool  # al menos un comentario (Req 7.6)
    has_tracked_changes: bool  # cambios pendientes del control de cambios (Req 7.6)
    external_relationships: int  # relaciones externas que no son hipervínculos (Req 7.5)

    def __post_init__(self) -> None:
        for name in ("has_macros", "has_comments", "has_tracked_changes"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} debe ser un booleano")
        count = self.external_relationships
        if isinstance(count, bool) or not isinstance(count, int):
            raise TypeError("external_relationships debe ser un entero")
        if count < 0:
            raise ValueError("external_relationships no puede ser negativo")


class _DamagedPackage(Exception):
    """Una parte que el inspector necesita falta o no es XML bien formado.

    Sin mensaje: el contenido del paquete no debe llegar a ningún registro.
    """


# Errores de `zipfile` y de zlib ante un paquete que el lector propio aceptó pero `zipfile` no. El
# nombre de una entrada con el indicador UTF-8 y bytes inválidos lanza `UnicodeDecodeError` al
# abrir el directorio. Un error de lectura del sistema (`OSError`) no es del archivo y se propaga:
# el proceso principal lo informa como `error_interno`
_DAMAGED_ERRORS = (
    _DamagedPackage,
    zipfile.BadZipFile,
    zlib.error,
    EOFError,
    NotImplementedError,
    UnicodeDecodeError,
)


@register_inspector("docx", info_type=DocxInfo)
def inspect_docx(source: InspectorInput) -> DocxInfo | SourceRejection:
    """Inspecciona el DOCX de `source.path`."""
    with source.path.open("rb") as stream:
        reason = classify_zip_verdict(check_zip_package(stream))
        if reason is not None:
            return SourceRejection(reason)
        try:
            with zipfile.ZipFile(stream) as archive:
                return _read_info(_Package(archive))
        except _DAMAGED_ERRORS:
            return SourceRejection(FailureReason.DAMAGED)


# --------------------------------------------------------------------------------------------
# Objetivos del analizador SAX de lxml: reciben cada inicio de elemento y no guardan el árbol


class _Target(Protocol):
    """Lo que lxml pide a un objeto `target`: los inicios de elemento y el cierre."""

    def start(self, tag: str, attrib: Mapping[str, str]) -> None: ...

    def close(self) -> None: ...


class _PresenceTarget:
    """Registra si alguno de los elementos `tags` aparece en las partes que se le entregan."""

    def __init__(self, tags: frozenset[str]) -> None:
        self._tags = tags
        self.found = False

    def start(self, tag: str, attrib: Mapping[str, str]) -> None:
        if tag in self._tags:
            self.found = True

    def close(self) -> None:
        """Lo exige lxml; no hay resultado que devolver."""


class _RelationshipsTarget:
    """Lee una parte de relaciones.

    Cuenta las relaciones externas que no son hipervínculos y detecta el proyecto de VBA. Recuerda
    el destino de la primera relación `officeDocument` y, si `collect` es verdadero, las partes
    del paquete a las que apuntan las relaciones de encabezados, pies de página, notas y
    comentarios. `source` es la parte a la que pertenecen las relaciones, con "" para el paquete.
    """

    def __init__(
        self, source: str, parts: Mapping[str, zipfile.ZipInfo], *, collect: bool = False
    ) -> None:
        self._source = source
        self._parts = parts
        self._collect = collect
        self.external = 0  # relaciones externas que no son hipervínculos
        self.vba = False  # hay una relación de tipo `vbaProject`
        self.main: str | None = None  # nombre, en minúsculas, de la parte principal declarada
        self.changes_parts: dict[str, zipfile.ZipInfo] = {}  # partes con posibles cambios
        self.comments_parts: dict[str, zipfile.ZipInfo] = {}

    def start(self, tag: str, attrib: Mapping[str, str]) -> None:
        if tag != _RELATIONSHIP_TAG:
            return
        rel_type = attrib.get("Type", "")
        kind = _relationship_kind(rel_type)
        self.vba = self.vba or rel_type == _VBA_PROJECT_TYPE
        if attrib.get("TargetMode") == "External":
            if kind != _HYPERLINK_KIND:
                self.external += 1
            return
        if kind == _MAIN_KIND and self.main is None:
            self.main = self._target(attrib)
        if not self._collect:
            return
        if kind in _CHANGES_KINDS:
            self._add(self.changes_parts, attrib)
        elif kind == _COMMENTS_KIND:
            self._add(self.comments_parts, attrib)

    def close(self) -> None:
        """Lo exige lxml; no hay resultado que devolver."""

    def _target(self, attrib: Mapping[str, str]) -> str:
        return _resolve_target(self._source, attrib.get("Target", "")).lower()

    def _add(self, parts: dict[str, zipfile.ZipInfo], attrib: Mapping[str, str]) -> None:
        name = self._target(attrib)
        part = self._parts.get(name)
        if part is not None:
            parts[name] = part


def _relationship_kind(rel_type: str) -> str | None:
    """Tipo de relación de Office sin su prefijo; None si es de otro vocabulario."""
    for prefix in _OFFICE_DOCUMENT_PREFIXES:
        if rel_type.startswith(prefix):
            return rel_type[len(prefix) :]
    return None


def _resolve_target(source: str, target: str) -> str:
    """Nombre de parte de `target`, destino de una relación de la parte `source`."""
    # Un destino absoluto parte de la raíz del paquete; uno relativo, de la carpeta de `source`
    if target.startswith("/"):
        return posixpath.normpath(target[1:])
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), target))


def _is_relationships_part(name: str) -> bool:
    """Si `name` es una parte de relaciones: `<carpeta>/_rels/<parte>.rels`."""
    directory, base = posixpath.split(name)
    return (
        base.endswith(_RELATIONSHIPS_SUFFIX)
        and posixpath.basename(directory) == _RELATIONSHIPS_DIRECTORY
    )


def _relationships_source(name: str) -> str:
    """Parte a la que pertenecen las relaciones de `name`; "" si son las del paquete."""
    directory, base = posixpath.split(name)
    owner = base[: -len(_RELATIONSHIPS_SUFFIX)]
    return posixpath.join(posixpath.dirname(directory), owner) if owner else ""


# --------------------------------------------------------------------------------------------
# Lectura del paquete


class _Package:
    """Paquete OPC abierto con `zipfile`; sus partes se buscan sin distinguir mayúsculas."""

    def __init__(self, archive: zipfile.ZipFile) -> None:
        self._archive = archive
        # Con nombres repetidos vale la última entrada, la que abre `zipfile` por nombre
        self.parts: Mapping[str, zipfile.ZipInfo] = {
            info.filename.lower(): info for info in archive.infolist() if not info.is_dir()
        }

    def feed(self, part: zipfile.ZipInfo, target: _Target) -> None:
        """Entrega `part` completa a `target`. Lanza `_DamagedPackage` si no es XML bien formado."""
        # lxml no publica anotaciones de tipos; `etree` queda como Any
        from lxml import etree  # type: ignore[import-untyped]

        parser = etree.XMLParser(target=target, **_PARSER_OPTIONS)
        try:
            with self._archive.open(part) as stream:
                while chunk := stream.read(_READ_BLOCK):
                    parser.feed(chunk)
            parser.close()
        except etree.XMLSyntaxError:
            # Sin encadenar: el mensaje de lxml cita el contenido de la parte
            raise _DamagedPackage from None


def _read_info(package: _Package) -> DocxInfo:
    """Lee las relaciones y las partes del DOCX. Lanza `_DamagedPackage` si falta la principal."""
    parts = package.parts
    macros = any(posixpath.basename(name) == _VBA_PROJECT_PART for name in parts)
    external = 0

    # La parte principal se conoce por las relaciones del paquete, así que se leen primero
    declared: str | None = None
    package_relationships = parts.get(PACKAGE_RELATIONSHIPS)
    if package_relationships is not None:
        target = _RelationshipsTarget("", parts)
        package.feed(package_relationships, target)
        external += target.external
        macros = macros or target.vba
        declared = target.main
    main_name = declared if declared is not None else DEFAULT_MAIN_PART
    main = parts.get(main_name)
    if main is None:
        raise _DamagedPackage

    changes_parts = {main_name: main}
    comments_parts: dict[str, zipfile.ZipInfo] = {}
    for name, part in parts.items():
        if name == PACKAGE_RELATIONSHIPS or not _is_relationships_part(name):
            continue
        source = _relationships_source(name)
        target = _RelationshipsTarget(source, parts, collect=source == main_name)
        package.feed(part, target)
        external += target.external
        macros = macros or target.vba
        for related_name, related in target.changes_parts.items():
            changes_parts.setdefault(related_name, related)
        comments_parts.update(target.comments_parts)

    changes = _PresenceTarget(_TRACKED_CHANGE_TAGS)
    for part in changes_parts.values():
        package.feed(part, changes)
    comments = _PresenceTarget(_COMMENT_TAGS)
    for part in comments_parts.values():
        package.feed(part, comments)

    return DocxInfo(
        has_macros=macros,
        has_comments=comments.found,
        has_tracked_changes=changes.found,
        external_relationships=external,
    )
