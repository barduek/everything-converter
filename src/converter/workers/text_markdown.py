"""Worker de HTML a MD con `markdownify` (Req 6.5, 6.6, 6.7, 6.13, 6.14).

Uso:

    python -m converter.workers.text_markdown --encoding <utf8|windows1252> -- <origen> <resultado>

Lee el HTML `<origen>`, lo decodifica con la codificación que detectó el Detector_de_Tipo y
escribe en `<resultado>` el Markdown, en UTF-8 sin BOM_UTF8. El convertidor `text.markdown`
(`converters/text_docs/to_md.py`) lo lanza a través del Ejecutor aislado. Ahí está también la
lista de lo que el MVP deja para después (escapes, cercas de código, sangría a medida).

Pasos:

1. `decode_text` devuelve el Texto_Decodificado y `parse_html` arma el árbol a partir de ese
   texto, así que un `<meta charset>` que contradiga la codificación real no tiene efecto
   (Req 2.12, 6.14).
2. `sanitize_dom` quita los Scripts y neutraliza los Recursos_Externos sin leerlos ni
   descargarlos, y cuenta los Recursos_Externos (Req 6.6, 6.7, 6.13).
3. `html_to_markdown` convierte solo el `body`, sin los elementos que el Texto_Visible oculta
   (`title`, `template`, `iframe`...). La subclase de `markdownify.MarkdownConverter` usa títulos
   ATX (`# Título`), viñetas `-` y enlaces sin autoenlace (`[texto](destino)`: la forma `<destino>`
   solo es un enlace de CommonMark con esquema). Un enlace sin destino queda como texto: es lo
   que le pasa a un enlace `javascript:` cuando `sanitize_dom` le quita el `href` (Req 6.5).

Salida estándar, una línea de JSON solo ASCII al terminar (ver `dump_event` y `load_event`):

    {"event":"done","external_resources":N}

con la cantidad de Recursos_Externos omitidos. Sin esta línea la ejecución no cuenta.

Códigos de salida: 0 si escribió el resultado; 2 si los argumentos son inválidos; 1 si una
excepción terminó el proceso, con el traceback en stderr (por ejemplo, un HTML anidado a más de
unos cientos de niveles agota la recursión de Python). El proceso principal no registra esa
salida: puede contener datos del archivo.
"""

from __future__ import annotations

import argparse
import functools
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bs4.element import Tag

from converter.converters.text_docs.dom import (
    _HIDDEN_ELEMENTS,
    Document,
    parse_html,
    sanitize_dom,
)
from converter.detection.text import TextKind, decode_text

if TYPE_CHECKING:
    from markdownify import MarkdownConverter

MODULE = "converter.workers.text_markdown"  # para `python -m`

EXIT_OK = 0
EXIT_USAGE = 2  # el código de argparse para argumentos inválidos

# Codificaciones de un Formato_de_Texto; TextKind.INVALID no tiene Texto_Decodificado
ENCODINGS: tuple[TextKind, ...] = (TextKind.UTF8, TextKind.WINDOWS_1252)

_SEPARATOR = "--"
_ASCII_BLANKS = " \t\r\n"


# --------------------------------------------------------------------------------------------
# Evento de la salida estándar


class WorkerOutputError(ValueError):
    """Una línea de la salida estándar del worker no es un evento válido."""


@dataclass(frozen=True, slots=True)
class DoneEvent:
    """El worker escribió el resultado; omitió `external_resources` Recursos_Externos."""

    external_resources: int

    def __post_init__(self) -> None:
        if self.external_resources < 0:
            raise ValueError(f"cantidad negativa de recursos: {self.external_resources}")


def dump_event(event: DoneEvent) -> str:
    """Línea de JSON solo ASCII y sin saltos de línea para `event`."""
    data = {"event": "done", "external_resources": event.external_resources}
    return json.dumps(data, separators=(",", ":"))


def load_event(line: str) -> DoneEvent:
    """Interpreta una línea de la salida del worker. Lanza `WorkerOutputError` si es inválida."""
    try:
        data: object = json.loads(line)
    except ValueError:
        raise WorkerOutputError("la línea no es JSON") from None
    if not isinstance(data, dict):
        raise WorkerOutputError("la línea no es un objeto JSON")
    return _build_event(data)


def _build_event(data: Mapping[str, object]) -> DoneEvent:
    if data.keys() != {"event", "external_resources"} or data["event"] != "done":
        raise WorkerOutputError("evento desconocido o con campos distintos de los esperados")
    count = data["external_resources"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise WorkerOutputError("external_resources: debe ser un entero de 0 o más")
    return DoneEvent(count)


# --------------------------------------------------------------------------------------------
# HTML a Markdown


@functools.cache
def markdown_converter() -> MarkdownConverter:
    """El `markdownify.MarkdownConverter` del worker: títulos ATX, viñetas `-` y enlaces sin
    autoenlace.

    Se crea la primera vez que se necesita, y `markdownify` se importa entonces y no al importar
    este módulo, así el convertidor puede importarlo para armar el comando y leer los eventos sin
    cargar la biblioteca en el proceso principal.
    """
    from markdownify import ATX, MarkdownConverter

    class HtmlToMarkdown(MarkdownConverter):
        def __init__(self) -> None:
            super().__init__(heading_style=ATX, bullets="-", autolinks=False)

        def convert_a(self, el: Tag, text: str, parent_tags: set[str]) -> str:
            """Enlace `[texto](destino)`; sin destino, el texto tal cual.

            `markdownify` devuelve el texto sin sus espacios de borde cuando el enlace no tiene
            `href`, y con ellos `foo<a> bar </a>baz` pasaría a «foobarbaz».
            """
            if not el.get("href"):
                return text
            # El stub de markdownify solo declara `convert` y `convert_soup`
            result: str = super().convert_a(el, text, parent_tags)  # type: ignore[misc]
            return result

    return HtmlToMarkdown()


def html_to_markdown(doc: Document) -> str:
    """Markdown del `body` de `doc`, que debe estar saneado con `sanitize_dom`.

    Cambia el árbol: quita los elementos que el Texto_Visible oculta. Devuelve la cadena vacía
    si el documento no tiene `body` o este no tiene contenido. El resultado no empieza ni termina
    con espacios en blanco ASCII.
    """
    body = doc.find("body")
    if not isinstance(body, Tag):
        return ""  # documento de marcos (frameset)
    for tag in body.find_all(_HIDDEN_ELEMENTS):
        if not tag.decomposed:
            tag.decompose()
    markdown: str = markdown_converter().convert_soup(body)
    return markdown.strip(_ASCII_BLANKS)


def convert_file(source: Path, output: Path, encoding: TextKind) -> DoneEvent:
    """Escribe en `output` el Markdown del HTML `source`, codificado como `encoding`.

    El resultado es UTF-8 sin BOM_UTF8 y termina con un salto de línea, salvo que esté vacío.
    """
    doc = parse_html(decode_text(source.read_bytes(), encoding))
    report = sanitize_dom(doc)
    markdown = html_to_markdown(doc)
    output.write_bytes((markdown + "\n" if markdown else "").encode("utf-8"))
    return DoneEvent(report.external_resources)


# --------------------------------------------------------------------------------------------
# Línea de comandos


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerArgs:
    """Argumentos de una invocación del worker."""

    encoding: TextKind  # codificación del Archivo_de_Origen: UTF8 o WINDOWS_1252
    source: Path  # Archivo_de_Origen
    output: Path  # Archivo_Resultado; el worker lo crea o lo reemplaza

    def __post_init__(self) -> None:
        if self.encoding not in ENCODINGS:
            raise ValueError(f"codificación sin Texto_Decodificado: {self.encoding!r}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m {MODULE}",
        description="Convierte un HTML a Markdown y lo escribe en UTF-8.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--encoding",
        required=True,
        choices=[kind.value for kind in ENCODINGS],
        help="codificación del HTML de origen",
    )
    parser.add_argument("source", type=Path, help="HTML de origen")
    parser.add_argument("output", type=Path, help="archivo MD que se escribe")
    return parser


def parse_args(args: Sequence[str]) -> WorkerArgs:
    """Interpreta los argumentos del worker. Lanza `SystemExit(2)` si son inválidos."""
    parsed = _parser().parse_args(list(args))
    return WorkerArgs(
        encoding=TextKind(parsed.encoding), source=parsed.source, output=parsed.output
    )


def format_args(args: WorkerArgs) -> list[str]:
    """Argumentos del worker para `args`; `parse_args(format_args(args)) == args`.

    Los posicionales van después de "--", así una ruta que empiece con "-" no se toma como opción.
    """
    return ["--encoding", args.encoding.value, _SEPARATOR, str(args.source), str(args.output)]


def main(argv: Sequence[str] | None = None) -> int:
    """Ejecuta el worker y devuelve su código de salida.

    Una excepción se propaga: el intérprete escribe el traceback en stderr y termina con el
    código 1.
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    event = convert_file(args.source, args.output, args.encoding)
    sys.stdout.write(dump_event(event) + "\n")
    sys.stdout.flush()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
