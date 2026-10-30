"""Worker de TXT y MD a HTML (Req 6.2, 6.3, 6.14, 2.12).

Uso:

    python -m converter.workers.text_html -- <formato> <codificación> <origen> <resultado>

Lee el Archivo_de_Origen `<origen>`, obtiene su Texto_Decodificado según `<codificación>`
(`utf8` o `windows1252`, la que el Detector_de_Tipo clasificó) y escribe en `<resultado>` un
documento HTML5 en UTF-8 sin BOM_UTF8. `<formato>` es `TXT` o `MD`. El convertidor `text.html`
(`converters/text_docs/to_html.py`) lo lanza a través del Ejecutor aislado.

Como parte del Texto_Decodificado y no de los bytes, el resultado no depende de la codificación
que declare el propio documento (Req 2.12), y un origen en Windows-1252 se escribe en UTF-8
(Req 6.14).

Los dos documentos comparten la misma estructura:

    <!DOCTYPE html>
    <html lang="es">
    <head>
    <meta charset="utf-8">
    <title>Documento</title>
    </head>
    <body>
    …
    </body>
    </html>

- **TXT** (Req 6.2). El cuerpo es un único `<pre>` con el texto escapado por `html.escape` con
  `quote=True`: `&`, `<`, `>`, `"` y `'` pasan a referencias de carácter. `pre` conserva los
  saltos de línea y el espacio en blanco, así que cada línea del origen se ve en una línea
  distinta y la Secuencia_de_Palabras no cambia. El analizador de HTML descarta un salto de línea
  que sigue inmediatamente a `<pre>`; por eso la etiqueta siempre va seguida de uno, y un
  origen que empieza con una línea en blanco la conserva.
- **MD** (Req 6.3). El cuerpo es, sin cambios, el HTML que CommonMark define para el texto
  (`markdown_to_html`). El HTML embebido en el MD se conserva como dicta CommonMark: el
  saneamiento de Scripts y Recursos_Externos corresponde a los destinos PDF, TXT y MD, no a este
  (design.md, "Documentos de texto").

El título es fijo: ni el nombre del archivo ni el texto del origen salen de la conversión por
otro camino que el cuerpo, y `<title>` no es parte del Texto_Visible.

Códigos de salida: 0 si terminó; 2 si los argumentos son inválidos; 1 si la lectura, la
decodificación o la escritura lanzaron una excepción, con el traceback en stderr. El proceso
principal sanea esa salida antes de registrarla (`sanitize_tool_output`).

`markdown_to_html` pertenece a `text_docs.dom`, que carga BeautifulSoup y markdown-it-py. Este
módulo lo importa solo cuando el origen es MD, así el convertidor puede importar el módulo para
armar el comando sin cargar esas bibliotecas en el proceso principal.
"""

from __future__ import annotations

import argparse
import html
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from converter.detection.text import TextKind, decode_text

MODULE = "converter.workers.text_html"  # para `python -m`

EXIT_OK = 0
EXIT_USAGE = 2  # el código de argparse para argumentos inválidos

SOURCE_FORMATS: tuple[str, ...] = ("TXT", "MD")
# Codificaciones del Texto_Decodificado que pueden llegar al worker (`TextKind.INVALID` no)
ENCODINGS: tuple[TextKind, ...] = (TextKind.UTF8, TextKind.WINDOWS_1252)

DOCUMENT_LANGUAGE = "es"
DOCUMENT_TITLE = "Documento"

_SEPARATOR = "--"

_DOCUMENT = (
    "<!DOCTYPE html>\n"
    '<html lang="{lang}">\n'
    "<head>\n"
    '<meta charset="utf-8">\n'
    "<title>{title}</title>\n"
    "</head>\n"
    "<body>\n"
    "{body}"
    "</body>\n"
    "</html>\n"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerArgs:
    """Argumentos de una invocación del worker."""

    source_format: str  # Formato del Archivo_de_Origen: "TXT" o "MD"
    encoding: TextKind  # codificación del Texto_Decodificado: UTF8 o WINDOWS_1252
    source: Path  # Archivo_de_Origen
    destination: Path  # Archivo_Resultado; el worker lo crea o lo reemplaza

    def __post_init__(self) -> None:
        if self.source_format not in SOURCE_FORMATS:
            raise ValueError(f"formato de origen no admitido: {self.source_format!r}")
        if self.encoding not in ENCODINGS:
            raise ValueError(f"codificación no admitida: {self.encoding!r}")


# --------------------------------------------------------------------------------------------
# Documentos HTML


def html_document(body: str) -> str:
    """Documento HTML5 en español con `<meta charset="utf-8">` y `body` como cuerpo.

    Si `body` no está vacío y no termina en un salto de línea, se lo agrega: así `</body>`
    queda en una línea propia sin cambiar el contenido (es espacio en blanco entre etiquetas).
    """
    if body and not body.endswith("\n"):
        body += "\n"
    return _DOCUMENT.format(lang=DOCUMENT_LANGUAGE, title=DOCUMENT_TITLE, body=body)


def txt_to_html(text: str) -> str:
    """Documento HTML de `text`, el Texto_Decodificado de un TXT (Req 6.2).

    El texto va escapado en un `<pre>`, con el salto de línea que el analizador de HTML descarta
    justo después de la etiqueta de apertura, de modo que el contenido del elemento es `text`.
    """
    return html_document(f"<pre>\n{html.escape(text, quote=True)}</pre>\n")


def md_to_html(text: str) -> str:
    """Documento HTML de `text`, el Texto_Decodificado de un MD (Req 6.3).

    El cuerpo es la salida de CommonMark, sin saneamiento.
    """
    from converter.converters.text_docs.dom import markdown_to_html

    return html_document(markdown_to_html(text))


def render_document(source_format: str, text: str) -> str:
    """Documento HTML de `text` según el Formato de origen, "TXT" o "MD"."""
    if source_format == "TXT":
        return txt_to_html(text)
    if source_format == "MD":
        return md_to_html(text)
    raise ValueError(f"formato de origen no admitido: {source_format!r}")


# --------------------------------------------------------------------------------------------
# Línea de comandos


def _encoding(raw: str) -> TextKind:
    for kind in ENCODINGS:
        if kind.value == raw:
            return kind
    choices = ", ".join(kind.value for kind in ENCODINGS)
    raise argparse.ArgumentTypeError(f"codificación inválida: {raw!r} (use {choices})")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m {MODULE}",
        description="Convierte un TXT o un MD a un documento HTML en UTF-8.",
        allow_abbrev=False,
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
    )


def format_args(args: WorkerArgs) -> list[str]:
    """Argumentos del worker para `args`; `parse_args(format_args(args)) == args`.

    Todos van después de "--", así una ruta que empiece con "-" no se toma como una opción.
    """
    return [
        _SEPARATOR,
        args.source_format,
        args.encoding.value,
        str(args.source),
        str(args.destination),
    ]


# --------------------------------------------------------------------------------------------
# Conversión


def convert_file(args: WorkerArgs) -> None:
    """Convierte `args.source` y escribe el documento HTML en `args.destination`, en UTF-8.

    Lanza `UnicodeDecodeError` si el contenido no es de la codificación indicada y `OSError` si
    no se puede leer o escribir.
    """
    text = decode_text(args.source.read_bytes(), args.encoding)
    document = render_document(args.source_format, text)
    # Bytes y no `write_text`: ninguna traducción de saltos de línea, y UTF-8 sin BOM_UTF8
    args.destination.write_bytes(document.encode("utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    """Ejecuta el worker y devuelve su código de salida.

    Una excepción de la conversión se propaga: el intérprete escribe el traceback en stderr y
    termina con el código 1.
    """
    convert_file(parse_args(sys.argv[1:] if argv is None else argv))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
