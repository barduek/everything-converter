"""Worker de TXT, MD y HTML a PDF con WeasyPrint (Req 6.1, 6.6, 6.7, 6.8, 6.11, 6.12, 6.13, 13.7).

Uso:

    python -m converter.workers.text_pdf [--font-dir=DIR] -- \\
        <formato> <codificación> <origen> <resultado>

Lee el Archivo_de_Origen `<origen>`, obtiene su Texto_Decodificado según `<codificación>` (`utf8`
o `windows1252`, la que el Detector_de_Tipo clasificó) y escribe en `<resultado>` un PDF.
`<formato>` es `TXT`, `MD` o `HTML`. El convertidor `text.pdf` (`converters/text_docs/to_pdf.py`)
lo lanza a través del Ejecutor aislado con `FONTCONFIG_FILE` en `default-only.conf`, de modo que
WeasyPrint solo ve la Fuente_Predeterminada (DejaVu). `--font-dir` es el directorio de esas
fuentes para la maquetación y la cobertura de glifos; su valor predeterminado es el de la
Imagen_de_Contenedor (`/opt/converter/fonts/default`).

Pasos, en este orden (design.md, "Documentos de texto"):

- **TXT** (Req 6.1, 6.8, 6.11). Los caracteres que la Fuente_Predeterminada no representa
  (`find_unrepresentable`) detienen la conversión antes de abrir WeasyPrint. Si no hay ninguno, la
  maquetación de `layout_text` corta las líneas en el ancho de la página, las reparte en páginas
  A4 de 60 líneas y WeasyPrint las escribe con `white-space: pre`: un `<pre>` por página, así que
  los únicos cortes de línea del PDF son los que calcula `layout.py`.
- **MD y HTML** (Req 6.6, 6.7, 6.12, 6.13). Un MD pasa antes a HTML con `markdown_to_html`.
  `parse_html` arma el DOM y `sanitize_dom` lo sanea: quita los Scripts (Req 6.7, 6.13) y
  neutraliza los Recursos_Externos sin leerlos ni descargarlos (Req 6.6). Con un MD, el
  Texto_Visible del DOM saneado se comprueba contra la cobertura de la Fuente_Predeterminada
  (Req 6.11); un HTML no se comprueba. Después `insert_word_joiners` agrega U+2060 dentro de las
  palabras, texto por texto y teniendo en cuenta los límites entre elementos de una misma línea,
  para que el PDF no parta una palabra en `-`, `/` y otros signos (Req 6.12).
- **Hojas de estilo.** La hoja base (`markup_stylesheet`: A4, márgenes de 2 cm, DejaVu,
  `hyphens: manual` y `font-variant-ligatures: none`) se entrega a WeasyPrint como hoja de
  usuario. Por el orden de la cascada (agente de usuario, usuario, autor), las hojas y los
  atributos de estilo del propio documento HTML tienen prioridad sobre ella.

Sin red (Req 6.6, 13.7). WeasyPrint recibe un `url_fetcher` que solo responde URL `data:` y
rechaza todo otro esquema, incluido `file:` (`make_url_fetcher`). El documento no tiene URL
base. Además de los Recursos_Externos que `sanitize_dom` ya neutralizó, esto cubre los que el
saneamiento no cuenta, como `@font-face`, `link rel=attachment` o una URL `file:` que un `base`
haga resolver. Que un recurso no se cargue no detiene la conversión.

Salida estándar: una única línea de JSON solo ASCII al terminar, con el evento final (ver
`dump_event` y `load_event`):

- `{"event":"done","external_resources":N}`: escribió el PDF. `N` es la cantidad de
  Recursos_Externos que `sanitize_dom` neutralizó, 0 en un TXT. El convertidor la informa en un
  aviso con un origen HTML o MD (Req 6.6).
- `{"event":"unrepresentable","characters":[...]}`: un TXT o un MD tiene caracteres que la
  Fuente_Predeterminada no puede representar (Req 6.11). Son como máximo 10, en orden de
  aparición. El worker termina con el código 0 y no escribe el PDF.

Sin esa línea la ejecución no cuenta. Antes de empezar, el worker guarda un duplicado de stdout y
apunta el descriptor 1 a stderr: lo que impriman WeasyPrint, Pango o fontconfig va a stderr y no
se mezcla con el evento.

Códigos de salida: 0 si terminó; 2 si los argumentos son inválidos; 1 si la lectura, la
decodificación, las fuentes o WeasyPrint lanzaron una excepción, con el traceback en stderr. El
proceso principal sanea esa salida antes de registrarla (`sanitize_tool_output`).

`converters.text_docs.dom` (BeautifulSoup, html5lib y markdown-it-py) y WeasyPrint se importan
dentro de las funciones que los usan, así el convertidor puede importar este módulo para armar el
comando y leer el evento sin cargar esas bibliotecas en el proceso principal.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import html
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO
from urllib.parse import unquote_to_bytes

from converter.converters.text_docs.layout import (
    DEFAULT_FONT_DIR,
    LAYOUT_FONT_FAMILY,
    MAX_UNREPRESENTABLE,
    NOT_CHECKED,
    TXT_PAGE,
    PageLayout,
    find_unrepresentable,
    layout_text,
    load_coverage,
    load_font_metrics,
)
from converter.detection.text import TextKind, decode_text

if TYPE_CHECKING:
    from converter.converters.text_docs.dom import Document

MODULE = "converter.workers.text_pdf"  # para `python -m`

EXIT_OK = 0
EXIT_USAGE = 2  # el código de argparse para argumentos inválidos

SOURCE_FORMATS: tuple[str, ...] = ("TXT", "MD", "HTML")
# Formatos cuyo Texto_Visible se comprueba contra la Fuente_Predeterminada (Req 6.11)
CHECKED_FORMATS: tuple[str, ...] = ("TXT", "MD")
# Codificaciones del Texto_Decodificado que pueden llegar al worker (`TextKind.INVALID` no)
ENCODINGS: tuple[TextKind, ...] = (TextKind.UTF8, TextKind.WINDOWS_1252)

TEXT_FONT_FAMILY = "DejaVu Sans"  # texto de MD y HTML; el código usa LAYOUT_FONT_FAMILY

_SEPARATOR = "--"
_NEWLINE = "\n"


# --------------------------------------------------------------------------------------------
# Eventos de la salida estándar


class WorkerOutputError(ValueError):
    """La salida estándar del worker no es el evento final esperado."""


@dataclass(frozen=True, slots=True)
class DoneEvent:
    """El worker escribió el PDF."""

    external_resources: int  # Recursos_Externos que `sanitize_dom` neutralizó (Req 6.6)

    def __post_init__(self) -> None:
        if isinstance(self.external_resources, bool) or self.external_resources < 0:
            raise ValueError(f"cantidad de Recursos_Externos inválida: {self.external_resources!r}")


@dataclass(frozen=True, slots=True)
class UnrepresentableEvent:
    """El texto tiene caracteres que la Fuente_Predeterminada no puede representar (Req 6.11)."""

    characters: tuple[str, ...]  # de 1 a 10 caracteres distintos, en orden de aparición

    def __post_init__(self) -> None:
        if not 1 <= len(self.characters) <= MAX_UNREPRESENTABLE:
            raise ValueError(f"cantidad de caracteres inválida: {len(self.characters)}")
        if any(len(char) != 1 or char in NOT_CHECKED for char in self.characters):
            raise ValueError("cada elemento debe ser un carácter que no sea espacio ni salto")
        if len(set(self.characters)) != len(self.characters):
            raise ValueError("los caracteres deben ser distintos")


type Event = DoneEvent | UnrepresentableEvent


def dump_event(event: Event) -> str:
    """Línea de JSON solo ASCII y sin saltos de línea para `event`."""
    data: dict[str, object]
    if isinstance(event, DoneEvent):
        data = {"event": "done", "external_resources": event.external_resources}
    else:
        data = {"event": "unrepresentable", "characters": list(event.characters)}
    return json.dumps(data, separators=(",", ":"))  # ensure_ascii: solo ASCII


def _build_event(data: Mapping[str, object]) -> Event:
    kind = data.get("event")
    if kind == "done" and data.keys() == {"event", "external_resources"}:
        count = data["external_resources"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise WorkerOutputError("external_resources: debe ser un entero de 0 o más")
        return DoneEvent(count)
    if kind == "unrepresentable" and data.keys() == {"event", "characters"}:
        characters = data["characters"]
        if not isinstance(characters, list) or not all(isinstance(c, str) for c in characters):
            raise WorkerOutputError("characters: debe ser una lista de textos")
        try:
            return UnrepresentableEvent(tuple(characters))
        except ValueError as exc:
            raise WorkerOutputError(f"characters: {exc}") from None
    raise WorkerOutputError("evento desconocido o con campos distintos de los esperados")


def load_event(line: str) -> Event:
    """Interpreta la línea final del worker. Lanza `WorkerOutputError` si es inválida."""
    try:
        data = json.loads(line)
    except ValueError:
        raise WorkerOutputError("la línea no es JSON") from None
    if not isinstance(data, dict):
        raise WorkerOutputError("la línea no es un objeto JSON")
    return _build_event(data)


# --------------------------------------------------------------------------------------------
# Sin red: solo URL data:


class UrlRefusedError(ValueError):
    """El `url_fetcher` no responde la URL: no es `data:` o es una URL `data:` inválida."""


@dataclass(frozen=True, slots=True)
class DataUrl:
    """Contenido de una URL `data:`."""

    media_type: str  # "type/subtype"; text/plain si la URL no lo declara o no es válido
    charset: str | None
    body: bytes

    @property
    def content_type(self) -> str:
        """Valor de un encabezado Content-Type."""
        if self.charset is None:
            return self.media_type
        return f"{self.media_type}; charset={self.charset}"


_DATA_PREFIX = "data:"
_DEFAULT_MEDIA_TYPE = "text/plain"
_BASE64_FLAG = "base64"
# Tipo MIME y valor de charset: token de RFC 7231 sin comillas ni separadores
_MEDIA_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*")
_CHARSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
_BASE64_WHITESPACE = re.compile(rb"[ \t\n\r\f]+")


def parse_data_url(url: str) -> DataUrl:
    """Contenido de `url` si es una URL `data:` (RFC 2397); lanza `UrlRefusedError` si no.

    El esquema `data` se reconoce sin distinguir mayúsculas y debe estar al inicio, sin espacios
    ni caracteres de control delante. Todo otro esquema, o una URL sin esquema, se rechaza; no se
    delega nada en `urllib`, así que ningún esquema distinto de `data:` puede llegar a leerse. Un
    fragmento (`#...`) no es parte del contenido. La parte base64 admite espacios y relleno
    faltante, como los navegadores.
    """
    if not isinstance(url, str) or url[: len(_DATA_PREFIX)].lower() != _DATA_PREFIX:
        raise UrlRefusedError("solo se admiten URL data:")
    meta, comma, payload = url[len(_DATA_PREFIX) :].partition(",")
    if not comma:
        raise UrlRefusedError("URL data: sin coma")
    payload = payload.partition("#")[0]

    parameters = [part.strip() for part in meta.split(";")]
    is_base64 = len(parameters) > 1 and parameters[-1].lower() == _BASE64_FLAG
    if is_base64:
        parameters.pop()
    media_type = parameters[0].lower()
    if not _MEDIA_TYPE.fullmatch(media_type):
        media_type = _DEFAULT_MEDIA_TYPE
    charset: str | None = None
    for parameter in parameters[1:]:
        name, _, value = parameter.partition("=")
        if name.strip().lower() == "charset" and _CHARSET.fullmatch(value.strip()):
            charset = value.strip().lower()

    body = unquote_to_bytes(payload)
    if is_base64:
        body = _BASE64_WHITESPACE.sub(b"", body)
        body += b"=" * (-len(body) % 4)
        try:
            body = base64.b64decode(body, validate=True)
        except binascii.Error:
            raise UrlRefusedError("URL data: con base64 inválido") from None
    return DataUrl(media_type=media_type, charset=charset, body=body)


def make_url_fetcher() -> Any:
    """`url_fetcher` de WeasyPrint que solo responde URL `data:` (Req 6.6, 13.7).

    Es una subclase de `weasyprint.urls.URLFetcher` (WeasyPrint 70) cuyo `fetch` no pasa nunca
    por los manejadores de `urllib`: lee el contenido con `parse_data_url` y rechaza con
    `UrlRefusedError` todo lo demás, incluidas las URL `file:`, `http:`, `https:` y `ftp:`.
    WeasyPrint registra el rechazo como una advertencia y sigue sin el recurso. Como defensa
    adicional, el fetcher base se crea con `allowed_protocols=("data",)` y sin redirecciones.
    """
    from weasyprint.urls import URLFetcher, URLFetcherResponse  # type: ignore[import-untyped]

    class DataOnlyFetcher(URLFetcher):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__(allowed_protocols=("data",), allow_redirects=False)

        def fetch(self, url: str, headers: Mapping[str, str] | None = None) -> Any:
            data = parse_data_url(url)
            return URLFetcherResponse(url, data.body, {"Content-Type": data.content_type})

    return DataOnlyFetcher()


# --------------------------------------------------------------------------------------------
# Hojas de estilo


def txt_stylesheet(page: PageLayout = TXT_PAGE) -> str:
    """Hoja de estilo de TXT a PDF: la página y la fuente de `page`, y `white-space: pre`.

    Cada página de `layout_text` es un `<pre>` sin márgenes, con la fuente y el interlineado de
    `page`, y cada `<pre>` después del primero empieza una página nueva.
    """
    return (
        f"@page {{ size: {page.width_mm}mm {page.height_mm}mm; margin: {page.margin_mm}mm }}\n"
        "html { hyphens: manual; font-variant-ligatures: none }\n"
        "body { margin: 0 }\n"
        f'pre {{ margin: 0; white-space: pre; font-family: "{LAYOUT_FONT_FAMILY}", monospace; '
        f"font-size: {page.font_size_pt}pt; line-height: {page.line_height_pt}pt }}\n"
        "pre + pre { break-before: page }\n"
    )


def markup_stylesheet() -> str:
    """Hoja base de MD y HTML a PDF: A4, márgenes de 2 cm, DejaVu Sans para el texto y DejaVu
    Sans Mono para el código, `white-space: pre-wrap` en `pre`, `hyphens: manual`,
    `font-variant-ligatures: none` y alineación a la izquierda.

    `overflow-wrap: break-word` hace que una palabra que no cabe sola en una línea se corte
    dentro de ella, como en TXT a PDF, en lugar de salirse de la página; las demás palabras no
    se cortan, porque el DOM lleva U+2060 en los lugares donde Pango permitiría hacerlo.
    """
    return (
        "@page { size: A4; margin: 2cm }\n"
        f'html {{ font-family: "{TEXT_FONT_FAMILY}", sans-serif; font-size: 11pt; '
        "line-height: 1.4; text-align: left; hyphens: manual; "
        "font-variant-ligatures: none; overflow-wrap: break-word }\n"
        "body { margin: 0 }\n"
        f'pre, code, kbd, samp, tt {{ font-family: "{LAYOUT_FONT_FAMILY}", monospace }}\n'
        "pre { white-space: pre-wrap }\n"
        "img { max-width: 100% }\n"
    )


# --------------------------------------------------------------------------------------------
# Documentos


@dataclass(frozen=True, slots=True)
class PreparedDocument:
    """Lo que se entrega a WeasyPrint."""

    html: str  # documento HTML
    stylesheet: str  # hoja base, de usuario: las hojas del documento tienen prioridad
    external_resources: int  # Recursos_Externos que `sanitize_dom` neutralizó


class UnrepresentableTextError(Exception):
    """El Texto_Visible tiene caracteres que la Fuente_Predeterminada no puede representar."""

    def __init__(self, characters: Sequence[str]) -> None:
        super().__init__(f"{len(characters)} caracteres no representables")
        self.characters = tuple(characters)


def _pre_element(lines: Sequence[str]) -> str:
    """`<pre>` de una página: sus líneas escapadas, separadas por saltos de línea."""
    return f"<pre>\n{html.escape(_NEWLINE.join(lines), quote=True)}</pre>\n"


def txt_document(
    text: str, font_dir: Path = DEFAULT_FONT_DIR, page: PageLayout = TXT_PAGE
) -> PreparedDocument:
    """Documento HTML de `text`, el Texto_Decodificado de un TXT (Req 6.1, 6.11).

    Lanza `UnrepresentableTextError` si `text` tiene caracteres que las fuentes de `font_dir` no
    representan, y `FileNotFoundError` si `font_dir` no tiene las fuentes de la maquetación.
    """
    missing = find_unrepresentable(text, load_coverage(font_dir))
    if missing:
        raise UnrepresentableTextError(missing)
    pages = layout_text(text, load_font_metrics(font_dir), page)
    # Un salto de línea justo después de `<pre>` lo descarta el analizador de HTML; por eso la
    # etiqueta siempre va seguida de uno, y una página que empieza con una línea vacía la conserva
    body = "".join(_pre_element(lines) for lines in pages)
    document = (
        '<!DOCTYPE html>\n<html lang="es">\n<head>\n<meta charset="utf-8">\n'
        f"<title>Documento</title>\n</head>\n<body>\n{body}</body>\n</html>\n"
    )
    return PreparedDocument(document, txt_stylesheet(page), external_resources=0)


# Elementos de texto en línea de HTML: entre dos textos separados solo por ellos no hay un límite
# de línea. Todo otro elemento (bloques, `br`, `wbr`, `img`, celdas...) es un límite.
_INLINE_ELEMENTS = frozenset(
    {
        "a",
        "abbr",
        "acronym",
        "b",
        "bdi",
        "bdo",
        "big",
        "cite",
        "code",
        "data",
        "del",
        "dfn",
        "em",
        "font",
        "i",
        "ins",
        "kbd",
        "mark",
        "nobr",
        "q",
        "s",
        "samp",
        "small",
        "span",
        "strike",
        "strong",
        "sub",
        "sup",
        "time",
        "tt",
        "u",
        "var",
    }
)
# Elementos cuyo texto no se presenta: no recibe U+2060 y es un límite de línea
_NOT_PRESENTED = frozenset({"noembed", "noframes", "script", "style", "template", "title"})


def add_word_joiners(doc: Document) -> int:
    """Inserta U+2060 en los textos del `body` de `doc`, sobre el mismo árbol (Req 6.12).

    Cada texto se procesa con `insert_word_joiners`. Cuando el texto siguiente continúa la misma
    línea, es decir, solo hay entre ambos elementos en línea (`b`, `em`, `span`, `a`...), se le
    pasa como `following` para que `foo-<b>bar</b>` también quede sin corte. No se tocan los
    comentarios, el contenido de `style`, `script`, `template` y `title` ni el `head`. Devuelve
    la cantidad de textos que cambiaron.
    """
    from bs4.element import NavigableString, PreformattedString, Tag

    from converter.converters.text_docs.dom import insert_word_joiners

    body = doc.find("body")
    if not isinstance(body, Tag):
        return 0
    texts: list[NavigableString] = []
    continues: list[bool] = []  # continues[i]: texts[i + 1] sigue la misma línea que texts[i]
    boundary = False
    # Recorrido con pila propia: un documento muy anidado no agota la recursión de Python
    stack: list[tuple[Any, bool]] = [(body, False)]
    while stack:
        node, leaving = stack.pop()
        if isinstance(node, Tag):
            if leaving:
                boundary = boundary or node.name not in _INLINE_ELEMENTS
                continue
            if node.name in _NOT_PRESENTED:
                boundary = True
                continue
            boundary = boundary or node.name not in _INLINE_ELEMENTS
            stack.append((node, True))
            stack.extend((child, False) for child in reversed(node.contents))
        elif isinstance(node, NavigableString) and not isinstance(node, PreformattedString):
            if texts:
                continues.append(not boundary)
            texts.append(node)
            boundary = False
    continues.append(False)

    changed = 0
    originals = [str(node) for node in texts]
    for index, node in enumerate(texts):
        following = originals[index + 1] if continues[index] else ""
        joined = insert_word_joiners(originals[index], following)
        if joined != originals[index]:
            node.replace_with(type(node)(joined))
            changed += 1
    return changed


def markup_document(
    text: str, source_format: str, font_dir: Path = DEFAULT_FONT_DIR
) -> PreparedDocument:
    """Documento HTML saneado de `text`, el Texto_Decodificado de un MD o un HTML (Req 6.6, 6.7,
    6.11, 6.12, 6.13).

    Con un MD, lanza `UnrepresentableTextError` si su Texto_Visible tiene caracteres que las
    fuentes de `font_dir` no representan y `FileNotFoundError` si `font_dir` no tiene fuentes.
    Un HTML no se comprueba (Req 6.11) y no lee `font_dir`.
    """
    if source_format not in ("MD", "HTML"):
        raise ValueError(f"Formato de origen no admitido: {source_format!r}")
    from converter.converters.text_docs.dom import (
        markdown_to_html,
        parse_html,
        sanitize_dom,
        visible_text,
    )

    doc = parse_html(markdown_to_html(text) if source_format == "MD" else text)
    report = sanitize_dom(doc)
    if source_format in CHECKED_FORMATS:
        missing = find_unrepresentable(visible_text(doc), load_coverage(font_dir))
        if missing:
            raise UnrepresentableTextError(missing)
    add_word_joiners(doc)
    return PreparedDocument(str(doc), markup_stylesheet(), report.external_resources)


def prepare_document(
    text: str, source_format: str, font_dir: Path = DEFAULT_FONT_DIR
) -> PreparedDocument:
    """Documento HTML y hoja de estilo de `text` según el Formato de origen: TXT, MD o HTML."""
    if source_format == "TXT":
        return txt_document(text, font_dir)
    if source_format in ("MD", "HTML"):
        return markup_document(text, source_format, font_dir)
    raise ValueError(f"Formato de origen no admitido: {source_format!r}")


def render_pdf(document: PreparedDocument, destination: Path) -> None:
    """Escribe en `destination` el PDF de `document` con WeasyPrint.

    El documento no tiene URL base y el `url_fetcher` solo responde URL `data:`. La hoja base va
    como hoja de usuario: las hojas del documento tienen prioridad.
    """
    from weasyprint import CSS, HTML  # type: ignore[import-untyped]
    from weasyprint.text.fonts import FontConfiguration  # type: ignore[import-untyped]

    fetcher = make_url_fetcher()
    font_config = FontConfiguration()
    page = HTML(string=document.html, url_fetcher=fetcher)
    sheet = CSS(string=document.stylesheet, url_fetcher=fetcher, font_config=font_config)
    page.write_pdf(str(destination), stylesheets=[sheet], font_config=font_config)


# --------------------------------------------------------------------------------------------
# Línea de comandos


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerArgs:
    """Argumentos de una invocación del worker."""

    source_format: str  # Formato del Archivo_de_Origen: "TXT", "MD" o "HTML"
    encoding: TextKind  # codificación del Texto_Decodificado: UTF8 o WINDOWS_1252
    source: Path  # Archivo_de_Origen
    destination: Path  # Archivo_Resultado; el worker lo crea o lo reemplaza
    font_dir: Path = DEFAULT_FONT_DIR  # fuentes de la maquetación y de la cobertura de glifos

    def __post_init__(self) -> None:
        if self.source_format not in SOURCE_FORMATS:
            raise ValueError(f"formato de origen no admitido: {self.source_format!r}")
        if self.encoding not in ENCODINGS:
            raise ValueError(f"codificación no admitida: {self.encoding!r}")


def _encoding(raw: str) -> TextKind:
    for kind in ENCODINGS:
        if kind.value == raw:
            return kind
    choices = ", ".join(kind.value for kind in ENCODINGS)
    raise argparse.ArgumentTypeError(f"codificación inválida: {raw!r} (use {choices})")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m {MODULE}",
        description="Convierte un TXT, un MD o un HTML a PDF con WeasyPrint.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--font-dir",
        type=Path,
        default=DEFAULT_FONT_DIR,
        help="directorio de la Fuente_Predeterminada (predeterminado: %(default)s)",
    )
    parser.add_argument("source_format", choices=SOURCE_FORMATS, help="Formato de origen")
    parser.add_argument(
        "encoding",
        type=_encoding,
        metavar="{" + ",".join(kind.value for kind in ENCODINGS) + "}",
        help="codificación del Texto_Decodificado",
    )
    parser.add_argument("source", type=Path, help="Archivo_de_Origen")
    parser.add_argument("destination", type=Path, help="Archivo_Resultado")
    return parser


def parse_args(args: Sequence[str]) -> WorkerArgs:
    """Interpreta los argumentos del worker. Lanza `SystemExit(2)` si son inválidos."""
    parsed = _parser().parse_args(list(args))
    return WorkerArgs(
        source_format=parsed.source_format,
        encoding=parsed.encoding,
        source=parsed.source,
        destination=parsed.destination,
        font_dir=parsed.font_dir,
    )


def format_args(args: WorkerArgs) -> list[str]:
    """Argumentos del worker para `args`; `parse_args(format_args(args)) == args`.

    El directorio de fuentes va en la forma `--font-dir=RUTA` y los posicionales después de "--",
    así una ruta que empiece con "-" no se toma como una opción.
    """
    return [
        f"--font-dir={args.font_dir}",
        _SEPARATOR,
        args.source_format,
        args.encoding.value,
        str(args.source),
        str(args.destination),
    ]


# --------------------------------------------------------------------------------------------
# Conversión


def convert_file(args: WorkerArgs) -> Event:
    """Convierte `args.source` y escribe el PDF en `args.destination`.

    Devuelve `DoneEvent` si escribió el PDF y `UnrepresentableEvent`, sin escribir nada, si un TXT
    o un MD tiene caracteres que la Fuente_Predeterminada no representa. Lanza
    `UnicodeDecodeError` si el contenido no es de la codificación indicada, `FileNotFoundError` si
    `args.font_dir` no tiene las fuentes y `OSError` si no se puede leer o escribir.
    """
    text = decode_text(args.source.read_bytes(), args.encoding)
    try:
        document = prepare_document(text, args.source_format, args.font_dir)
    except UnrepresentableTextError as exc:
        return UnrepresentableEvent(exc.characters)
    render_pdf(document, args.destination)
    return DoneEvent(document.external_resources)


def _claim_stdout() -> TextIO:
    """Devuelve un flujo sobre el stdout original y apunta el descriptor 1 a stderr."""
    sys.stdout.flush()
    result_fd = os.dup(1)
    os.dup2(2, 1)
    return os.fdopen(result_fd, "w", encoding="ascii", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Ejecuta el worker y devuelve su código de salida.

    Una excepción de la conversión se propaga: el intérprete escribe el traceback en stderr y
    termina con el código 1.
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    events = _claim_stdout()
    with events:
        event = convert_file(args)
        events.write(dump_event(event) + "\n")
        events.flush()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
