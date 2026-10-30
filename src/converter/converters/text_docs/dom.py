"""DOM saneado, Texto_Visible y HTML de CommonMark para los documentos de texto (Req 2.12, 6.4,
6.6, 6.7, 6.13).

Piezas compartidas por los convertidores `text.html`, `text.plain`, `text.markdown` y `text.pdf`:

- `parse_html` arma el árbol con BeautifulSoup y html5lib a partir del Texto_Decodificado. Como
  recibe texto y no bytes, la codificación que declare el documento (por ejemplo
  `<meta charset>`) no cambia el resultado (Req 2.12). Todos los atributos quedan como texto,
  también `class` y `rel`.
- `sanitize_dom` quita los Scripts (Req 6.7, 6.13) y neutraliza los Recursos_Externos (Req 6.6),
  y devuelve cuántos encontró en un `SanitizeReport`.
- `visible_text` devuelve el Texto_Visible, con un salto de línea en cada límite de bloque y en
  cada `br`. Con `list_markers=True` agrega los marcadores de lista de MD y HTML a TXT (Req 6.4).
- `markdown_to_html` devuelve el HTML que CommonMark define para un documento MD.
- `insert_word_joiners` agrega U+2060 (WORD JOINER) donde el PDF podría cortar una palabra.

Recursos_Externos. Se cuenta uno por cada:

- elemento de la lista del diseño que referencia al menos una URL externa en sus atributos de
  recurso: `img` (`src`, `srcset`), `link` con `rel` stylesheet, icon o preload (`href`),
  `iframe` y `frame` (`src`), `object` (`data`), `embed` (`src`), `audio` (`src`), `video`
  (`src`, `poster`), `source` (`src`, `srcset`), `track` (`src`), `input` de tipo image (`src`)
  e `image` y `use` de SVG (`href`, `xlink:href`);
- regla `@import` y función `url()` con una URL externa en un elemento `style` o en un atributo
  `style`.

Una URL es externa salvo que sea `data:`, `about:`, `javascript:` (un Script) o una referencia
dentro del mismo documento (vacía o que empieza con `#`). Los enlaces de navegación (`a`, `area`
y los `link` con otros `rel`) no se cuentan. Neutralizar quita las URL externas: los atributos
de recurso se borran, `srcset` conserva solo sus candidatos `data:`, las reglas `@import` se
eliminan y cada `url()` se reemplaza por `none`. El elemento que se queda sin ninguna fuente se
quita del árbol, salvo `object`, `audio` y `video`, que se conservan con su contenido
alternativo porque ese texto es parte del Texto_Visible. Así una imagen omitida no deja su texto
alternativo en el PDF ni en el MD.
"""

from __future__ import annotations

import re
import unicodedata
import warnings
from dataclasses import dataclass
from enum import Enum

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
from bs4.element import NavigableString, PageElement, PreformattedString, Tag
from markdown_it import MarkdownIt

# Árbol de un documento HTML, también el del HTML que CommonMark produce para un MD
Document = BeautifulSoup

WORD_JOINER = "\u2060"

_HTML_NS = "http://www.w3.org/1999/xhtml"
_SVG_NS = "http://www.w3.org/2000/svg"

# Espacio en blanco de HTML y de CSS: espacio, tabulación, salto de línea, avance de página y
# retorno de carro
_ASCII_WHITESPACE = " \t\n\f\r"
_ASCII_WHITESPACE_RUN = re.compile(r"[ \t\n\f\r]+")


@dataclass(frozen=True)
class SanitizeReport:
    """Lo que `sanitize_dom` encontró y neutralizó en un documento."""

    # Recursos_Externos omitidos, contados como explica el docstring del módulo (Req 6.6)
    external_resources: int
    # Scripts quitados: elementos script, atributos on* y URL javascript: en atributos o en CSS
    scripts: int


# --- parse_html ---


def parse_html(text: str) -> Document:
    """Arma el árbol de `text`, el Texto_Decodificado de un HTML, con html5lib.

    Como `text` ya está decodificado, html5lib no busca la codificación en el documento y una
    etiqueta `meta charset` que contradice la codificación real no tiene efecto (Req 2.12).
    """
    with warnings.catch_warnings():
        # Un documento cuyo texto parece una URL o una ruta de archivo sigue siendo un documento
        warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
        return BeautifulSoup(text, "html5lib", multi_valued_attributes=None)


# --- sanitize_dom ---


class _UrlKind(Enum):
    INLINE = "inline"  # data:, about:, vacía o solo fragmento: no es un Recurso_Externo
    SCRIPT = "script"  # esquema javascript:
    EXTERNAL = "external"  # URL remota o ruta de archivo relativa, absoluta o de red


# Atributos cuyo valor es una URL; con esquema javascript: son un Script y se quitan
_URL_ATTRIBUTES = frozenset(
    {
        "action",
        "background",
        "cite",
        "classid",
        "codebase",
        "data",
        "dynsrc",
        "formaction",
        "href",
        "icon",
        "longdesc",
        "lowsrc",
        "manifest",
        "poster",
        "profile",
        "src",
        "usemap",
        "xlink:href",
    }
)

# Atributos de recurso de cada elemento de la lista de Recursos_Externos del diseño
_HTML_RESOURCES: dict[str, tuple[str, ...]] = {
    "img": ("src", "srcset"),
    "link": ("href",),  # solo con rel stylesheet, icon o preload
    "iframe": ("src",),
    "frame": ("src",),
    "object": ("data",),
    "embed": ("src",),
    "audio": ("src",),
    "video": ("src", "poster"),
    "source": ("src", "srcset"),
    "track": ("src",),
    "input": ("src",),  # solo con type=image
}
_SVG_RESOURCES: dict[str, tuple[str, ...]] = {
    "image": ("href", "xlink:href"),
    "use": ("href", "xlink:href"),
}
_RESOURCE_LINK_TYPES = frozenset({"stylesheet", "icon", "preload"})

# Elementos que conservan contenido alternativo (parte del Texto_Visible) sin su recurso
_KEPT_WITHOUT_SOURCE = frozenset({"object", "audio", "video"})

# Bytes que el análisis de URL quita al inicio y al final (controles C0 y espacio) y en cualquier
# posición (tabulación y saltos de línea), como hace un navegador
_URL_EDGE = "".join(map(chr, range(0x21)))
_URL_TAB_OR_NEWLINE = re.compile(r"[\t\n\r]")
_URL_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*):")
_INLINE_SCHEMES = frozenset({"data", "about"})


def sanitize_dom(doc: Document) -> SanitizeReport:
    """Quita los Scripts de `doc` y neutraliza sus Recursos_Externos, sobre el mismo árbol.

    Scripts (Req 6.7, 6.13): elimina los elementos `script` (también los de SVG) con su código,
    los atributos cuyo nombre empieza con "on" y los atributos de URL con esquema javascript:.
    Un enlace a un Script queda como texto, sin `href`. Recursos_Externos (Req 6.6): ver el
    docstring del módulo. No lee ni descarga nada.
    """
    scripts = 0
    for tag in _tags(doc):
        # Un script dentro de otro ya quitado no se cuenta de nuevo
        if tag.name == "script" and not tag.decomposed:
            tag.decompose()
            scripts += 1
    external = 0
    without_source: list[Tag] = []
    for tag in _tags(doc):
        scripts += _remove_script_attributes(tag)
        css_external, css_scripts = _neutralize_inline_css(tag)
        external += css_external
        scripts += css_scripts
        if _neutralize_resources(tag):
            external += 1
            if tag.name not in _KEPT_WITHOUT_SOURCE and not _has_source(tag):
                without_source.append(tag)
    for tag in without_source:
        if not tag.decomposed:
            tag.decompose()
    return SanitizeReport(external_resources=external, scripts=scripts)


def _tags(doc: Tag) -> list[Tag]:
    """Elementos de `doc` en orden de documento, en una lista para poder cambiar el árbol."""
    return [node for node in doc.descendants if isinstance(node, Tag)]


def _text(value: object) -> str:
    """Valor de un atributo como texto (con `parse_html` ya lo es; un árbol armado de otra forma
    puede traer listas en `class` o `rel`)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(map(str, value))
    return ""


def _url_kind(value: str) -> _UrlKind:
    url = _URL_TAB_OR_NEWLINE.sub("", value.strip(_URL_EDGE))
    if not url or url.startswith("#"):
        return _UrlKind.INLINE
    match = _URL_SCHEME.match(url)
    scheme = match.group(1).lower() if match else ""
    if scheme == "javascript":
        return _UrlKind.SCRIPT
    if scheme in _INLINE_SCHEMES:
        return _UrlKind.INLINE
    return _UrlKind.EXTERNAL


def _remove_script_attributes(tag: Tag) -> int:
    removed = 0
    for name in list(tag.attrs):
        lowered = name.lower()
        is_handler = lowered.startswith("on")
        if is_handler or (
            lowered in _URL_ATTRIBUTES and _url_kind(_text(tag.attrs[name])) is _UrlKind.SCRIPT
        ):
            del tag.attrs[name]
            removed += 1
    return removed


def _resource_attributes(tag: Tag) -> tuple[str, ...]:
    if tag.namespace == _SVG_NS:
        return _SVG_RESOURCES.get(tag.name, ())
    if tag.namespace not in (None, _HTML_NS):
        return ()
    if tag.name == "input" and _text(tag.attrs.get("type")).strip(_ASCII_WHITESPACE).lower() != (
        "image"
    ):
        return ()
    if tag.name == "link":
        types = _text(tag.attrs.get("rel")).lower().split()
        if _RESOURCE_LINK_TYPES.isdisjoint(types):
            return ()
    return _HTML_RESOURCES.get(tag.name, ())


def _neutralize_resources(tag: Tag) -> bool:
    """Quita las URL externas de los atributos de recurso de `tag` y dice si había alguna."""
    found = False
    for name in _resource_attributes(tag):
        if name not in tag.attrs:
            continue
        value = _text(tag.attrs[name])
        if name == "srcset":
            candidates = _srcset_candidates(value)
            kept = [(url, desc) for url, desc in candidates if _url_kind(url) is _UrlKind.INLINE]
            if len(kept) == len(candidates):
                continue
            found = found or any(_url_kind(url) is _UrlKind.EXTERNAL for url, _ in candidates)
            if kept:
                tag.attrs[name] = ", ".join(f"{url} {desc}".rstrip() for url, desc in kept)
            else:
                del tag.attrs[name]
        elif _url_kind(value) is _UrlKind.EXTERNAL:
            found = True
            del tag.attrs[name]
    return found


def _has_source(tag: Tag) -> bool:
    return any(
        _text(tag.attrs.get(name)).strip(_ASCII_WHITESPACE) for name in _resource_attributes(tag)
    )


def _srcset_candidates(value: str) -> list[tuple[str, str]]:
    """(URL, descriptores) de cada candidato de un `srcset`, separados como indica HTML: la URL
    termina en el primer espacio, y los descriptores, en la primera coma fuera de paréntesis.
    """
    candidates: list[tuple[str, str]] = []
    position, end = 0, len(value)
    while True:
        while position < end and (value[position] in _ASCII_WHITESPACE or value[position] == ","):
            position += 1
        if position >= end:
            return candidates
        start = position
        while position < end and value[position] not in _ASCII_WHITESPACE:
            position += 1
        url = value[start:position]
        descriptors = ""
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            start = position
            depth = 0
            while position < end and (value[position] != "," or depth > 0):
                if value[position] == "(":
                    depth += 1
                elif value[position] == ")" and depth > 0:
                    depth -= 1
                position += 1
            descriptors = value[start:position].strip(_ASCII_WHITESPACE)
        candidates.append((url, descriptors))


# CSS: cadenas, comentarios, reglas @import y @namespace y funciones url(). Se buscan en una sola
# pasada para que un url() dentro de una cadena o de un comentario no cuente.
_CSS_SPACE = r"[ \t\n\r\f]"
_CSS_STRING = r'"(?:[^"\\\n\r\f]|\\[\s\S])*"?' + "|" + r"'(?:[^'\\\n\r\f]|\\[\s\S])*'?"
_CSS_UNQUOTED_URL = r"(?:[^ \t\n\r\f\"'()\\]|\\[\s\S])*"


def _css_url_function(group: str) -> str:
    return (
        rf"(?<![\w-])url\({_CSS_SPACE}*(?P<{group}>{_CSS_STRING}|{_CSS_UNQUOTED_URL})"
        rf"{_CSS_SPACE}*\)"
    )


_CSS_TOKENS = re.compile(
    r"(?P<comment>/\*[\s\S]*?(?:\*/|\Z))"
    rf"|(?P<string>{_CSS_STRING})"
    rf"|(?P<rule>(?<![\w-])@(?P<keyword>import|namespace)(?![\w-]){_CSS_SPACE}*"
    rf"(?:(?P<rule_string>{_CSS_STRING})|{_css_url_function('rule_url')})?[^;{{}}]*;?)"
    rf"|{_css_url_function('url')}",
    re.IGNORECASE,
)
_CSS_ESCAPE = re.compile(r"\\(?:([0-9A-Fa-f]{1,6})[ \t\n\r\f]?|(\r\n|[\n\r\f])|([\s\S]))")


def _neutralize_inline_css(tag: Tag) -> tuple[int, int]:
    """Neutraliza las URL del atributo `style` y, si `tag` es `style`, de su hoja de estilo.

    Devuelve los Recursos_Externos y los Scripts (url() con javascript:) quitados.
    """
    external = scripts = 0
    if "style" in tag.attrs:
        css, css_external, css_scripts = _neutralize_css(_text(tag.attrs["style"]))
        if css_external or css_scripts:
            tag.attrs["style"] = css
            external += css_external
            scripts += css_scripts
    if tag.name == "style":
        for child in list(tag.contents):
            if not isinstance(child, NavigableString) or isinstance(child, PreformattedString):
                continue
            css, css_external, css_scripts = _neutralize_css(str(child))
            if css_external or css_scripts:
                child.replace_with(type(child)(css))
                external += css_external
                scripts += css_scripts
    return external, scripts


def _neutralize_css(css: str) -> tuple[str, int, int]:
    """Quita de `css` las reglas @import y reemplaza por `none` las funciones url() que no son
    `data:`, `about:` ni fragmento. Devuelve el CSS nuevo y cuántas eran externas y Scripts.
    """
    external = scripts = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal external, scripts
        if match.group("rule") is not None:
            target = match.group("rule_string")
            if target is None:
                target = match.group("rule_url")
            if match.group("keyword").lower() == "namespace" or target is None:
                # @namespace solo nombra un espacio de nombres; no se descarga
                return match.group(0)
            replacement = ""
        elif match.group("url") is not None:
            target = match.group("url")
            replacement = "none"
        else:
            return match.group(0)  # comentario o cadena
        kind = _url_kind(_css_value(target))
        if kind is _UrlKind.INLINE:
            return match.group(0)
        if kind is _UrlKind.EXTERNAL:
            external += 1
        else:
            scripts += 1
        return replacement

    return _CSS_TOKENS.sub(replace, css), external, scripts


def _css_value(token: str) -> str:
    """Valor de una cadena o URL sin comillas de CSS, con los escapes resueltos."""
    if token[:1] in ('"', "'"):
        quote = token[0]
        token = token[1:-1] if len(token) > 1 and token.endswith(quote) else token[1:]

    def unescape(match: re.Match[str]) -> str:
        if match.group(1) is not None:
            code = int(match.group(1), 16)
            valid = 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF
            return chr(code) if valid else "\ufffd"
        if match.group(2) is not None:
            return ""  # salto de línea escapado: continúa la cadena
        return match.group(3)

    return _CSS_ESCAPE.sub(unescape, token)


# --- visible_text ---

# Elementos cuyo contenido no se presenta como texto: Scripts, hojas de estilo, plantillas,
# elementos que un navegador no muestra y el texto crudo de iframe, noembed y noframes
_HIDDEN_ELEMENTS = frozenset(
    {
        "datalist",
        "head",
        "iframe",
        "noembed",
        "noframes",
        "rp",
        "script",
        "style",
        "template",
        "title",
    }
)

# Elementos de bloque: su inicio y su fin son límites de línea (títulos, párrafos, elementos de
# lista, celdas de tabla, bloques de código y el resto de los bloques de HTML)
_BLOCK_ELEMENTS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "caption",
        "center",
        "dd",
        "details",
        "dialog",
        "dir",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hgroup",
        "hr",
        "legend",
        "li",
        "listing",
        "main",
        "menu",
        "nav",
        "ol",
        "optgroup",
        "option",
        "p",
        "plaintext",
        "pre",
        "search",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "textarea",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
        "xmp",
    }
)

# Elementos que conservan los espacios y los saltos de línea de su texto
_PRESERVED_ELEMENTS = frozenset({"listing", "plaintext", "pre", "textarea", "xmp"})

_ORDERED_LISTS = frozenset({"ol"})
_UNORDERED_LISTS = frozenset({"dir", "menu", "ul"})
_UNORDERED_MARKER = "- "

# Entero de HTML: espacios iniciales, signo opcional y dígitos; lo que sigue se ignora
_HTML_INTEGER = re.compile(r"[ \t\n\f\r]*([+-]?)([0-9]+)")
_MAX_LIST_NUMBER_DIGITS = 9


def visible_text(doc: Document, *, list_markers: bool = False) -> str:
    """Texto_Visible de `doc`: el texto de `body` sin etiquetas, comentarios, Scripts, hojas de
    estilo ni plantillas, con las referencias de caracteres ya reemplazadas por el parser.

    - Cada límite de un elemento de bloque (títulos, párrafos, elementos de lista, celdas de
      tabla, bloques de código y demás bloques) inicia una línea nueva; varios límites seguidos
      dan un solo salto. Cada `br` es un salto de línea forzado, también en una línea vacía.
    - Fuera de `pre` (y `textarea`, `listing`, `xmp`, `plaintext`), cada secuencia de espacios
      en blanco de HTML es un espacio, y no hay espacios al inicio ni al final de una línea.
      Dentro, el texto se conserva tal cual.
    - Con `list_markers=True`, cada elemento de lista empieza con "- " o "N. " (desde `start`
      del `ol` o `value` del `li`), y su contenido y sus listas anidadas se sangran con el
      ancho del marcador. Es el formato de MD y HTML a TXT (Req 6.4).

    Las líneas se separan con "\\n". El texto no termina con un salto de línea salvo que lo
    fuerce un `br` o un bloque preformateado.
    """
    body = doc.find("body")
    if not isinstance(body, Tag):
        return ""  # documento de marcos (frameset): no tiene cuerpo
    writer = _TextWriter()
    lists: list[_ListState] = []
    indents: list[str] = []
    preserved = 0
    stack: list[tuple[PageElement, bool]] = [(body, False)]
    # Recorrido con pila propia: un documento muy anidado no agota la recursión de Python
    while stack:
        node, leaving = stack.pop()
        if isinstance(node, Tag):
            name = node.name
            if leaving:
                if list_markers and name == "li":
                    writer.end_item(indents.pop())
                if list_markers and (name in _ORDERED_LISTS or name in _UNORDERED_LISTS):
                    lists.pop()
                if name in _PRESERVED_ELEMENTS:
                    preserved -= 1
                if name in _BLOCK_ELEMENTS:
                    writer.block_boundary()
                continue
            if name in _HIDDEN_ELEMENTS:
                continue
            if name == "br":
                writer.line_break()
                continue
            if name in _BLOCK_ELEMENTS:
                writer.block_boundary()
            if name in _PRESERVED_ELEMENTS:
                preserved += 1
            if list_markers and name in _ORDERED_LISTS:
                start = _html_integer(node.attrs.get("start"))
                lists.append(_ListState(ordered=True, next_number=1 if start is None else start))
            if list_markers and name in _UNORDERED_LISTS:
                lists.append(_ListState(ordered=False, next_number=1))
            if list_markers and name == "li":
                indents.append(writer.begin_item(_item_marker(node, lists)))
            stack.append((node, True))
            stack.extend((child, False) for child in reversed(node.contents))
        elif isinstance(node, NavigableString) and not isinstance(node, PreformattedString):
            # Los comentarios, CDATA, declaraciones y DOCTYPE son PreformattedString
            if preserved:
                writer.preformatted(str(node))
            else:
                writer.collapsible(str(node))
    return writer.result()


@dataclass
class _ListState:
    ordered: bool
    next_number: int


def _html_integer(value: object) -> int | None:
    """Entero de un atributo según las reglas de HTML, o None si no tiene uno razonable."""
    match = _HTML_INTEGER.match(_text(value))
    if match is None:
        return None
    sign, digits = match.groups()
    if len(digits.lstrip("0")) > _MAX_LIST_NUMBER_DIGITS:
        return None
    return int(sign + digits)


def _item_marker(item: Tag, lists: list[_ListState]) -> str:
    if not lists or not lists[-1].ordered:
        return _UNORDERED_MARKER
    state = lists[-1]
    value = _html_integer(item.attrs.get("value"))
    number = state.next_number if value is None else value
    state.next_number = number + 1
    return f"{number}. "


class _TextWriter:
    """Arma el texto línea por línea, con el espacio en blanco colapsado como en HTML.

    El salto de un límite de bloque se escribe recién cuando aparece contenido después, así
    que varios límites seguidos dan un solo salto y el texto no termina con uno.
    """

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._line_open = False  # la línea actual ya tiene contenido
        self._break_pending = False  # un límite de bloque cerró la línea; falta escribir "\n"
        self._space = False  # hay un espacio colapsado pendiente entre dos palabras
        self._indent = ""  # sangría de cada línea nueva dentro de un elemento de lista
        self._marker: str | None = None  # marcador de lista, con su sangría, de la próxima línea

    def collapsible(self, text: str) -> None:
        for index, word in enumerate(_ASCII_WHITESPACE_RUN.split(text)):
            if index > 0 and self._line_open:
                self._space = True
            if word:
                self._write(word)

    def preformatted(self, text: str) -> None:
        for index, piece in enumerate(text.split("\n")):
            if index > 0:
                self._newline()
            if piece:
                self._write(piece)

    def block_boundary(self) -> None:
        if self._line_open:
            self._line_open = False
            self._break_pending = True
        self._space = False

    def line_break(self) -> None:
        self._flush_marker()
        self._newline()

    def begin_item(self, marker: str) -> str:
        """Empieza un elemento de lista con `marker`; devuelve la sangría para `end_item`."""
        self.block_boundary()
        if self._marker is not None:
            # El elemento anterior empezó sin texto, con otra lista: su marcador va solo
            self._flush_marker()
            self.block_boundary()
        outer = self._indent
        self._marker = outer + marker
        self._indent = outer + " " * len(marker)
        return outer

    def end_item(self, outer_indent: str) -> None:
        self._flush_marker()  # un elemento vacío también ocupa su línea
        self.block_boundary()
        self._indent = outer_indent

    def result(self) -> str:
        return "".join(self._parts)

    def _write(self, text: str) -> None:
        if self._line_open:
            if self._space:
                self._parts.append(" ")
        else:
            self._start_line()
            if self._marker is not None:
                self._parts.append(self._marker)
                self._marker = None
            else:
                self._parts.append(self._indent)
            self._line_open = True
        self._space = False
        self._parts.append(text)

    def _flush_marker(self) -> None:
        if self._marker is not None and not self._line_open:
            self._start_line()
            self._parts.append(self._marker.rstrip(" "))
            self._marker = None
            self._line_open = True

    def _newline(self) -> None:
        self._start_line()
        self._parts.append("\n")
        self._line_open = False
        self._space = False

    def _start_line(self) -> None:
        if self._break_pending:
            self._parts.append("\n")
            self._break_pending = False


# --- markdown_to_html ---


class _CommonMark(MarkdownIt):
    """markdown-it-py con el preset "commonmark" y sin filtro de URL.

    El filtro predeterminado deja como texto los enlaces `javascript:`, `vbscript:`, `file:` y
    algunos `data:`, lo que se aparta del HTML que define CommonMark y dejaría el código de un
    Script como texto visible. Sin el filtro, esos enlaces salen como `a` o `img`, y
    `sanitize_dom` los neutraliza cuando el destino es PDF, TXT o MD.
    """

    def __init__(self) -> None:
        super().__init__("commonmark")

    def validateLink(self, url: str) -> bool:
        return True


def markdown_to_html(text: str) -> str:
    """HTML que CommonMark define para `text`, el Texto_Decodificado de un MD, sin plugins.

    El HTML embebido en el MD se conserva, como indica CommonMark. markdown-it-py corta el
    anidamiento a 20 niveles para acotar el trabajo con documentos patológicos.
    """
    # El renderizador de markdown-it-py no declara el tipo de lo que devuelve
    html: str = _CommonMark().render(text)
    return html


# --- insert_word_joiners ---

# Separadores de palabras de la Secuencia_de_Palabras
_WORD_SEPARATORS = frozenset(" \t\r\n")
# U+2060 y U+FEFF ya prohíben el corte
_JOINERS = frozenset({WORD_JOINER, "\ufeff"})
# Caracteres antes de los que UAX #14 permite cortar aunque sigan a una letra (clases BB y B2),
# por ejemplo el acento agudo U+00B4, que es un Carácter_Latino
_BREAK_BEFORE = frozenset("\u00b4\u02c8\u02cc\u02df\u1ffd\u2014\u2e3a\u2e3b")
_ZERO_WIDTH_JOINER = "\u200d"
_ZERO_WIDTH_NON_JOINER = "\u200c"
# Letras y dígitos seguidos: entre ellos no va U+2060, así que se copian sin analizar
_PLAIN_RUN = re.compile(r"[^\W_\u02c8\u02cc]{3,}")


class _Kind(Enum):
    SEPARATOR = "separator"
    JOINER = "joiner"
    EXTEND = "extend"  # continúa el grafema anterior: marcas combinantes, ZWJ, modificadores
    PUNCTUATION = "punctuation"  # categorías Unicode P (puntuación) y S (símbolos)
    OTHER = "other"


def _char_kind(char: str) -> _Kind:
    if char in _WORD_SEPARATORS:
        return _Kind.SEPARATOR
    if char in _JOINERS:
        return _Kind.JOINER
    code = ord(char)
    category = unicodedata.category(char)
    if (
        category[0] == "M"
        or char in (_ZERO_WIDTH_JOINER, _ZERO_WIDTH_NON_JOINER)
        or 0x1F3FB <= code <= 0x1F3FF  # modificadores de tono de piel de los emoji
        or 0xE0020 <= code <= 0xE007F  # etiquetas de las banderas de subdivisiones
    ):
        return _Kind.EXTEND
    if category[0] in "PS":
        return _Kind.PUNCTUATION
    return _Kind.OTHER


class _JoinerState:
    """Recorre un texto por grafemas aproximados y dice dónde va un U+2060."""

    def __init__(self) -> None:
        self._base: _Kind | None = None  # tipo del primer carácter del grafema actual
        self._after_zwj = False
        self._lone_regional = False  # el grafema es un indicador regional sin su pareja

    def feed(self, char: str) -> bool:
        """Avanza a `char` y dice si va un U+2060 justo antes de él."""
        kind = _char_kind(char)
        regional = 0x1F1E6 <= ord(char) <= 0x1F1FF
        pairs_regional = regional and self._lone_regional
        insert = False
        if kind is _Kind.EXTEND or self._after_zwj or pairs_regional:
            if self._base is None:
                self._base = _Kind.OTHER  # continúa un grafema que empezó antes del texto
        else:
            base = self._base
            insert = (
                base is not None
                and base not in (_Kind.SEPARATOR, _Kind.JOINER)
                and kind not in (_Kind.SEPARATOR, _Kind.JOINER)
                and (base is _Kind.PUNCTUATION or char in _BREAK_BEFORE)
            )
            self._base = kind
        self._after_zwj = char == _ZERO_WIDTH_JOINER
        self._lone_regional = regional and not pairs_regional
        return insert


def insert_word_joiners(text: str, following: str = "") -> str:
    """Inserta U+2060 (WORD JOINER) donde el renderizado del PDF podría partir una palabra.

    Va un U+2060 después de cada carácter de puntuación o símbolo (categorías Unicode P y S)
    que está dentro de una palabra, es decir, seguido por un carácter que no es separador
    (espacio, tabulación, retorno de carro ni salto de línea), y antes de los caracteres que
    permiten cortar delante de ellos (`_BREAK_BEFORE`, como U+00B4) cuando siguen a un
    carácter de la misma palabra. Nunca se inserta dentro de un grafema (marcas combinantes,
    secuencias de emoji con ZWJ, banderas) ni junto a un U+2060 o U+FEFF, así que aplicarla dos
    veces da el mismo texto, y quitar los U+2060 agregados devuelve `text`.

    `following` es el texto que sigue a `text` en el mismo párrafo, por ejemplo el run
    siguiente de un DOCX o el nodo de texto siguiente del DOM: solo se mira su primer carácter,
    para decidir si va un U+2060 al final de `text`. El límite con el texto anterior le
    corresponde a la llamada sobre ese texto.
    """
    state = _JoinerState()
    parts: list[str] = []
    position = 0
    for run in _PLAIN_RUN.finditer(text):
        _feed_chars(state, text[position : run.start() + 2], parts)
        # Desde el segundo carácter de la serie el grafema es una letra o un dígito: lo que
        # sigue no lleva U+2060 y deja el mismo estado
        parts.append(text[run.start() + 2 : run.end()])
        position = run.end()
    _feed_chars(state, text[position:], parts)
    if text and following and state.feed(following[0]):
        parts.append(WORD_JOINER)
    return "".join(parts)


def _feed_chars(state: _JoinerState, chars: str, parts: list[str]) -> None:
    for char in chars:
        if state.feed(char):
            parts.append(WORD_JOINER)
        parts.append(char)
