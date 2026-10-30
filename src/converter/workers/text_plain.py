"""Worker de MD y HTML a TXT: el Texto_Visible del documento, en UTF-8 (Req 6.4, 6.6, 6.7, 6.13,
6.14).

Uso:

    python -m converter.workers.text_plain -- <formato> <codificación> <origen> <resultado>

Convierte el documento `<origen>`, de Formato `<formato>` (`MD` o `HTML`), a TXT y lo escribe en
`<resultado>`. `<codificación>` es la que el Detector_de_Tipo clasificó (`utf8` o `windows1252`,
ver `TextKind`): con ella el worker obtiene el Texto_Decodificado y no usa la que declare el
documento, como `<meta charset>` en HTML (Req 2.12). El convertidor `text.plain`
(`converters/text_docs/to_txt.py`) lo lanza a través del Ejecutor aislado.

Pasos, en este orden (design.md, "Documentos de texto"):

1. Texto_Decodificado del origen (`decode_text`): en UTF-8 sin la BOM_UTF8 inicial, o en
   Windows-1252.
2. Un MD pasa antes a HTML con `markdown_to_html`, el HTML que define CommonMark. El HTML embebido
   en el MD se conserva, así que el DOM de un MD se sanea igual que el de un HTML.
3. `parse_html` arma el DOM y `sanitize_dom` lo sanea: quita los Scripts sin ejecutarlos
   (Req 6.7, 6.13) y neutraliza los Recursos_Externos sin leerlos ni descargarlos (Req 6.6).
4. `visible_text(list_markers=True)` entrega el Texto_Visible del DOM saneado, con cada título,
   párrafo, elemento de lista, bloque de código y salto de línea forzado en una línea nueva, y con
   los elementos de lista marcados con `- ` o `N. ` (Req 6.4).
5. El resultado se escribe en UTF-8 sin BOM_UTF8 y con `\\n` como único salto de línea (Req 6.14),
   también cuando el origen es Texto_Windows_1252. Si el documento no tiene Texto_Visible, el
   resultado es un archivo vacío.

Salida estándar: una única línea de JSON solo ASCII al terminar, `{"event":"done",
"external_resources":N}` (ver `dump_event` y `load_event`), con la cantidad de Recursos_Externos
que `sanitize_dom` neutralizó. Sin esa línea la ejecución no cuenta. El convertidor decide si la
informa: el aviso corresponde a un origen HTML (Req 6.6).

Códigos de salida: 0 si escribió el resultado; 2 si los argumentos son inválidos; 1 si la
conversión o el sistema de archivos lanzaron una excepción, con el traceback en stderr. El proceso
principal sanea esa salida antes de registrarla (`sanitize_tool_output`): puede contener datos del
archivo.

`converters.text_docs.dom` (BeautifulSoup, html5lib y markdown-it-py) se importa dentro de
`to_plain_text`, así el convertidor puede importar este módulo para armar el comando y leer el
evento sin cargar esas bibliotecas.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from converter.detection.text import TextKind, decode_text

MODULE = "converter.workers.text_plain"  # para `python -m`

EXIT_OK = 0
EXIT_USAGE = 2  # el código de argparse para argumentos inválidos

SOURCE_FORMATS = ("MD", "HTML")  # Formatos_de_Origen que convierte el worker
ENCODINGS = (TextKind.UTF8.value, TextKind.WINDOWS_1252.value)  # codificaciones de un texto válido

_SEPARATOR = "--"


# --------------------------------------------------------------------------------------------
# Evento de la salida estándar


class WorkerOutputError(ValueError):
    """La salida estándar del worker no es el evento final esperado."""


@dataclass(frozen=True, slots=True)
class DoneEvent:
    """El worker escribió el resultado."""

    external_resources: int  # Recursos_Externos que `sanitize_dom` neutralizó (Req 6.6)

    def __post_init__(self) -> None:
        if isinstance(self.external_resources, bool) or self.external_resources < 0:
            raise ValueError(f"cantidad de Recursos_Externos inválida: {self.external_resources!r}")


def dump_event(event: DoneEvent) -> str:
    """Línea de JSON solo ASCII y sin saltos de línea para `event`."""
    data = {"event": "done", "external_resources": event.external_resources}
    return json.dumps(data, separators=(",", ":"))


def _build_event(data: Mapping[str, object]) -> DoneEvent:
    if data.keys() != {"event", "external_resources"} or data["event"] != "done":
        raise WorkerOutputError("evento desconocido o con campos distintos de los esperados")
    count = data["external_resources"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise WorkerOutputError("external_resources: debe ser un entero de 0 o más")
    return DoneEvent(count)


def load_event(line: str) -> DoneEvent:
    """Interpreta la línea final del worker. Lanza `WorkerOutputError` si es inválida."""
    try:
        data = json.loads(line)
    except ValueError:
        raise WorkerOutputError("la línea no es JSON") from None
    if not isinstance(data, dict):
        raise WorkerOutputError("la línea no es un objeto JSON")
    return _build_event(data)


# --------------------------------------------------------------------------------------------
# Conversión


def to_plain_text(text: str, source_format: str) -> tuple[str, int]:
    """TXT del Texto_Decodificado `text` de un documento `source_format` (`MD` o `HTML`).

    Devuelve el Texto_Visible del DOM saneado, con marcadores de lista, y la cantidad de
    Recursos_Externos que se neutralizaron. Lanza `ValueError` si el Formato no es `MD` ni `HTML`.
    """
    if source_format not in SOURCE_FORMATS:
        raise ValueError(f"Formato de origen no admitido: {source_format!r}")
    from converter.converters.text_docs.dom import (
        markdown_to_html,
        parse_html,
        sanitize_dom,
        visible_text,
    )

    doc = parse_html(markdown_to_html(text) if source_format == "MD" else text)
    report = sanitize_dom(doc)
    return visible_text(doc, list_markers=True), report.external_resources


def convert_file(args: WorkerArgs) -> DoneEvent:
    """Convierte `args.source` y escribe el resultado en `args.output`.

    Lanza `OSError` si no puede leer el origen o escribir el resultado.
    """
    text = decode_text(args.source.read_bytes(), args.encoding)
    plain, external_resources = to_plain_text(text, args.source_format)
    args.output.write_bytes(plain.encode("utf-8"))  # sin BOM_UTF8
    return DoneEvent(external_resources)


# --------------------------------------------------------------------------------------------
# Línea de comandos


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerArgs:
    """Argumentos de una invocación del worker."""

    source_format: str  # Formato del Archivo_de_Origen: "MD" o "HTML"
    encoding: TextKind  # codificación del origen: UTF8 o WINDOWS_1252
    source: Path  # Archivo_de_Origen
    output: Path  # Archivo_Resultado; el worker lo crea o lo reemplaza

    def __post_init__(self) -> None:
        if self.source_format not in SOURCE_FORMATS:
            raise ValueError(f"Formato de origen no admitido: {self.source_format!r}")
        if self.encoding.value not in ENCODINGS:
            raise ValueError(f"codificación no admitida: {self.encoding!r}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m {MODULE}",
        description="Convierte un documento MD o HTML a TXT, en UTF-8.",
        allow_abbrev=False,
    )
    parser.add_argument("source_format", choices=SOURCE_FORMATS, help="Formato de origen")
    parser.add_argument("encoding", choices=ENCODINGS, help="codificación del origen")
    parser.add_argument("source", type=Path, help="Archivo_de_Origen")
    parser.add_argument("output", type=Path, help="archivo TXT que se escribe")
    return parser


def parse_args(args: Sequence[str]) -> WorkerArgs:
    """Interpreta los argumentos del worker. Lanza `SystemExit(2)` si son inválidos."""
    parsed = _parser().parse_args(list(args))
    return WorkerArgs(
        source_format=parsed.source_format,
        encoding=TextKind(parsed.encoding),
        source=parsed.source,
        output=parsed.output,
    )


def format_args(args: WorkerArgs) -> list[str]:
    """Argumentos del worker para `args`; `parse_args(format_args(args)) == args`.

    Los posicionales van después de "--", así una ruta que empiece con "-" no se toma como opción.
    """
    return [
        _SEPARATOR,
        args.source_format,
        args.encoding.value,
        str(args.source),
        str(args.output),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Ejecuta el worker y devuelve su código de salida.

    Una excepción de la conversión se propaga: el intérprete escribe el traceback en stderr y
    termina con el código 1.
    """
    event = convert_file(parse_args(sys.argv[1:] if argv is None else argv))
    sys.stdout.write(dump_event(event) + "\n")
    sys.stdout.flush()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
