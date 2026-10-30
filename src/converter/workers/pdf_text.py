"""Worker de PDF a TXT: extrae el texto de cada página con pypdfium2 (Req 8.1, 8.2, 8.3, 8.10).

Uso:

    python -m converter.workers.pdf_text <origen> <resultado>

Abre el PDF `<origen>`, recorre sus páginas en orden de Número_de_Página y escribe en
`<resultado>` el texto de las páginas que tienen Capa_de_Texto. El convertidor `pdf.text`
(`converters/pdf_text.py`) lo lanza a través del Ejecutor aislado.

Texto de una página:

- **Extracción.** `get_text_range()` de PDFium entrega el texto en el orden de lectura que
  calcula PDFium, con `\\r\\n` entre líneas. `clean_page_text` convierte cada `\\r\\n` en `\\n` y
  elimina U+2060 (WORD JOINER) y U+FEFF: los PDF que genera el Conversor llevan U+2060 dentro de
  las palabras para que no se partan entre líneas (ver `converters/text_docs`), y ninguno de los
  dos caracteres se ve.
- **Líneas.** El worker nunca une dos líneas. PDFium reemplaza el guion que termina una línea y
  la palabra sigue en la línea siguiente por una marca (U+FFFE en el texto, `FPDFText_IsHyphen`
  en la lista de caracteres) y omite el salto de línea. El worker escribe en su lugar un guion
  ASCII y un salto de línea, así que "inter-" y "nacional" siguen en dos líneas. PDFium no
  conserva si el guion era U+002D o U+00AD (guion suave): en ambos casos se escribe U+002D.
- **Páginas.** Una Página_sin_Texto, cuyo texto solo tiene espacios, tabulaciones o saltos de
  línea una vez limpio, no aporta nada al resultado. Las demás se separan con una línea en blanco
  (`PAGE_SEPARATOR`), así que entre el texto de una página y el de la siguiente hay siempre al
  menos un salto de línea. El resultado se escribe página por página, sin juntar el texto del
  documento en memoria.

Salida estándar, una línea de JSON solo ASCII por evento (ver `dump_event` y `load_event`):

- `{"event":"page","page":N,"total":T,"text":true|false}` al terminar cada página, en orden. El
  convertidor lo usa para el Porcentaje_de_Avance (N / T) y para los números de las
  Páginas_sin_Texto.
- `{"event":"done","pages":T,"with_text":K}` al final. Sin esta línea la ejecución no cuenta.

Antes de abrir el PDF, el worker guarda un duplicado de stdout y apunta el descriptor 1 a
stderr: lo que impriman PDFium o sus bibliotecas va a stderr y no se mezcla con los eventos.

Códigos de salida: 0 si terminó; 2 si los argumentos son inválidos; 1 si pypdfium2 lanzó una
excepción (un PDF dañado o protegido, por ejemplo), con el traceback en stderr. El proceso
principal sanea esa salida antes de registrarla (`sanitize_tool_output`): puede contener datos
del archivo.

pypdfium2 se importa dentro de las funciones que lo usan, así el convertidor puede importar este
módulo para armar el comando y leer los eventos sin cargar PDFium en el proceso principal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

MODULE = "converter.workers.pdf_text"  # para `python -m`

EXIT_OK = 0
EXIT_USAGE = 2  # el código de argparse para argumentos inválidos

PAGE_SEPARATOR = "\n\n"  # entre el texto de dos páginas con texto (Req 8.1)
HYPHEN = "-"
HYPHEN_MARK = "\ufffe"  # lo que `get_text_range` entrega en lugar de un guion de fin de línea
INVISIBLE_CHARACTERS = "\u2060\ufeff"  # WORD JOINER y BOM / ZERO WIDTH NO-BREAK SPACE
BLANK_CHARACTERS = " \t\r\n"  # lo único que tiene una Página_sin_Texto

_REMOVE_INVISIBLE = str.maketrans("", "", INVISIBLE_CHARACTERS)

_SEPARATOR = "--"


# --------------------------------------------------------------------------------------------
# Eventos de la salida estándar


class WorkerOutputError(ValueError):
    """Una línea de la salida estándar del worker no es un evento válido."""


@dataclass(frozen=True, slots=True)
class PageEvent:
    """El worker terminó la página `page` (desde 1) de un documento de `total` páginas."""

    page: int
    total: int
    has_text: bool  # False: Página_sin_Texto

    def __post_init__(self) -> None:
        if not 1 <= self.page <= self.total:
            raise ValueError(f"página fuera de rango: {self.page} de {self.total}")


@dataclass(frozen=True, slots=True)
class DoneEvent:
    """El worker terminó el documento: `pages` páginas, `pages_with_text` con texto."""

    pages: int
    pages_with_text: int

    def __post_init__(self) -> None:
        if not 0 <= self.pages_with_text <= self.pages:
            raise ValueError(f"páginas con texto fuera de rango: {self.pages_with_text}")


type Event = PageEvent | DoneEvent


def dump_event(event: Event) -> str:
    """Línea de JSON solo ASCII y sin saltos de línea para `event`."""
    data: dict[str, object]
    if isinstance(event, PageEvent):
        data = {"event": "page", "page": event.page, "total": event.total, "text": event.has_text}
    else:
        data = {"event": "done", "pages": event.pages, "with_text": event.pages_with_text}
    return json.dumps(data, separators=(",", ":"))


def _integer(data: Mapping[str, object], key: str, minimum: int) -> int:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise WorkerOutputError(f"{key}: debe ser un entero de {minimum} o más")
    return value


def _build_event(data: Mapping[str, object]) -> Event:
    kind = data.get("event")
    if kind == "page" and data.keys() == {"event", "page", "total", "text"}:
        has_text = data["text"]
        if not isinstance(has_text, bool):
            raise WorkerOutputError("text: debe ser un booleano")
        return PageEvent(_integer(data, "page", 1), _integer(data, "total", 1), has_text)
    if kind == "done" and data.keys() == {"event", "pages", "with_text"}:
        return DoneEvent(_integer(data, "pages", 0), _integer(data, "with_text", 0))
    raise WorkerOutputError("evento desconocido o con campos distintos de los esperados")


def load_event(line: str) -> Event:
    """Interpreta una línea de la salida del worker. Lanza `WorkerOutputError` si es inválida."""
    try:
        data = json.loads(line)
    except ValueError:
        raise WorkerOutputError("la línea no es JSON") from None
    if not isinstance(data, dict):
        raise WorkerOutputError("la línea no es un objeto JSON")
    try:
        return _build_event(data)
    except WorkerOutputError:
        raise
    except ValueError as exc:  # un rango que rechaza el constructor del evento
        raise WorkerOutputError(str(exc)) from None


# --------------------------------------------------------------------------------------------
# Texto de una página


def clean_page_text(raw: str) -> str:
    """Texto de una página sin `\\r\\n`, U+2060 ni U+FEFF.

    Cada `\\r\\n` pasa a `\\n`, y un `\\r` suelto también, para que el resultado tenga un único
    tipo de salto de línea. No quita ni junta nada más: las líneas quedan como las entregó PDFium.
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    return text.translate(_REMOVE_INVISIBLE)


def is_blank_page(text: str) -> bool:
    """True si `text`, ya limpio, no tiene ningún carácter distinto de espacio, tabulación o
    salto de línea: es una Página_sin_Texto."""
    return not text.strip(BLANK_CHARACTERS)


def _load_pdfium() -> Any:
    """El módulo pypdfium2, importado la primera vez que se necesita."""
    # pypdfium2 no publica anotaciones de tipos; el módulo queda como Any
    import pypdfium2  # type: ignore[import-untyped]

    return pypdfium2


def page_text(textpage: Any) -> str:
    """Texto de la página de `textpage` (un `PdfTextPage` de pypdfium2) tal como lo entrega PDFium,
    con cada guion de fin de línea convertido en `-\\n`. Sin limpiar: ver `clean_page_text`."""
    text: str = textpage.get_text_range()
    if HYPHEN_MARK not in text:
        return text  # PDFium escribe U+FFFE en lugar de cada guion de fin de línea
    is_hyphen = _load_pdfium().raw.FPDFText_IsHyphen

    # Los guiones se buscan en la lista de caracteres, no en el texto, porque un U+FFFE puede
    # venir también del documento. `get_text_range` recibe posiciones de esa misma lista.
    count: int = textpage.count_chars()
    parts: list[str] = []
    start = 0
    for index in range(count):
        if is_hyphen(textpage, index):
            parts.append(textpage.get_text_range(start, index - start))
            parts.append(HYPHEN + "\n")
            start = index + 1
    parts.append(textpage.get_text_range(start, count - start))
    return "".join(parts)


def _page_text(pdf: Any, index: int) -> str:
    """Texto sin limpiar de la página `index` (desde 0) de `pdf`."""
    page = pdf[index]
    try:
        textpage = page.get_textpage()
        try:
            return page_text(textpage)
        finally:
            textpage.close()
    finally:
        page.close()


def extract_text(source: Path, output: Path, on_page: Callable[[PageEvent], None]) -> DoneEvent:
    """Escribe en `output` el texto del PDF `source` y llama a `on_page` al terminar cada página.

    Devuelve el resumen del documento. Si ninguna página tiene texto, `output` queda vacío. Lanza
    `PdfiumError` si PDFium no puede abrir el documento o cargar una página.
    """
    pdf = _load_pdfium().PdfDocument(str(source))
    try:
        total = len(pdf)
        with_text = 0
        with output.open("w", encoding="utf-8", newline="") as stream:
            for index in range(total):
                text = clean_page_text(_page_text(pdf, index))
                has_text = not is_blank_page(text)
                if has_text:
                    if with_text:
                        stream.write(PAGE_SEPARATOR)
                    stream.write(text)
                    with_text += 1
                on_page(PageEvent(index + 1, total, has_text))
    finally:
        pdf.close()
    return DoneEvent(total, with_text)


# --------------------------------------------------------------------------------------------
# Línea de comandos


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerArgs:
    """Argumentos de una invocación del worker."""

    source: Path  # Archivo_de_Origen
    output: Path  # Archivo_Resultado; el worker lo crea o lo reemplaza


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m {MODULE}",
        description="Extrae el texto de un PDF y lo escribe en UTF-8.",
        allow_abbrev=False,
    )
    parser.add_argument("source", type=Path, help="PDF de origen")
    parser.add_argument("output", type=Path, help="archivo TXT que se escribe")
    return parser


def parse_args(args: Sequence[str]) -> WorkerArgs:
    """Interpreta los argumentos del worker. Lanza `SystemExit(2)` si son inválidos."""
    parsed = _parser().parse_args(list(args))
    return WorkerArgs(source=parsed.source, output=parsed.output)


def format_args(args: WorkerArgs) -> list[str]:
    """Argumentos del worker para `args`; `parse_args(format_args(args)) == args`.

    Los posicionales van después de "--", así una ruta que empiece con "-" no se toma como opción.
    """
    return [_SEPARATOR, str(args.source), str(args.output)]


def _claim_stdout() -> TextIO:
    """Devuelve un flujo sobre el stdout original y apunta el descriptor 1 a stderr."""
    sys.stdout.flush()
    result_fd = os.dup(1)
    os.dup2(2, 1)
    return os.fdopen(result_fd, "w", encoding="ascii", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Ejecuta el worker y devuelve su código de salida.

    Una excepción de pypdfium2 se propaga: el intérprete escribe el traceback en stderr y termina
    con el código 1.
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    events = _claim_stdout()
    with events:

        def emit(event: Event) -> None:
            events.write(dump_event(event) + "\n")
            events.flush()  # el avance llega al convertidor página por página

        emit(extract_text(args.source, args.output, emit))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
