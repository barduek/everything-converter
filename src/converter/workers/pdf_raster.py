"""Worker de PDF a PNG: rasteriza cada página con pypdfium2 (Req 8.4, 8.5, 8.6, 8.8, 8.9, 8.11).

Uso:

    python -m converter.workers.pdf_raster --dpi N -- <origen> <resultado> <base>

Abre el PDF `<origen>`, rasteriza sus páginas en orden de Número_de_Página a `--dpi` píxeles por
pulgada y escribe en `<resultado>` el Archivo_Resultado. `<base>` es el Nombre_Base ya normalizado
con el que se nombran las imágenes del ZIP. El convertidor `pdf.raster`
(`converters/pdf_raster.py`) lo lanza a través del Ejecutor aislado.

Resultado:

- **Una página** (Req 8.9): `<resultado>` es la imagen PNG de la página, sin ZIP.
- **Varias páginas** (Req 8.5): `<resultado>` es un ZIP con una imagen PNG por página, sin
  carpetas. Cada entrada se llama `page_entry_name(<base>, N, T)` (Req 8.8), usa
  `ZIP_DEFLATED` y lleva la fecha fija 1980-01-01 00:00:00, así que el mismo PDF produce el mismo
  ZIP en el mismo Equipo_de_Ejecución (Req 13.4). Cada página se guarda primero en un archivo
  junto a `<resultado>` y de ahí pasa al ZIP, de modo que en memoria nunca hay más de una página.

**Tamaño.** `raster_size(page, dpi)` calcula el ancho y el alto en píxeles de una página a partir
de su tamaño mostrado en puntos PDF (CropBox con `/Rotate` aplicado, ver el inspector `pdf`), con
la misma regla de redondeo que `PdfPage.render` de pypdfium2: `ceil(puntos * dpi / 72)`. El
convertidor la usa para comprobar el Límite_de_Píxeles de todas las páginas antes de lanzar este
worker (Req 8.11), y el worker la usa para dibujar: después de renderizar cada página comprueba
que la imagen mide lo que dijo `raster_size`, así lo que se verificó es lo que se rasteriza. Si
una versión de pypdfium2 cambiara el redondeo, el worker falla en lugar de entregar imágenes de
otro tamaño.

**Renderizado.** `page.render(scale=dpi/72, may_draw_forms=True)` respeta la rotación de la
página y la rellena de blanco. Para que `may_draw_forms` dibuje los campos de formulario, el
documento inicia su entorno de formularios (`init_forms`) antes de leer las páginas: sin él,
PDFium omite los campos. PDFium se compila sin V8 ni XFA, así que ningún formulario ejecuta
JavaScript. Las páginas sin contenido, como las Páginas_sin_Texto, también producen su imagen
(Req 8.4). PDFium abre un PDF_con_Permisos_Restringidos sin contraseña e ignora las
restricciones, así que el resultado es el mismo que el del PDF sin cifrar (Req 8.10). Los PNG se
guardan con opciones fijas de Pillow y sin metadatos.

Salida estándar, una línea de JSON solo ASCII por evento (ver `dump_event` y `load_event`):

- `{"event":"page","page":N,"total":T}` al terminar cada página, en orden. El convertidor lo usa
  para el Porcentaje_de_Avance (N / T).
- `{"event":"done","pages":T}` al final. Sin esta línea la ejecución no cuenta.

Antes de abrir el PDF, el worker guarda un duplicado de stdout y apunta el descriptor 1 a
stderr: lo que impriman PDFium o sus bibliotecas va a stderr y no se mezcla con los eventos.

Códigos de salida: 0 si terminó; 2 si los argumentos son inválidos; 1 si pypdfium2 o el sistema de
archivos lanzaron una excepción (un PDF dañado o un disco lleno, por ejemplo), con el traceback en
stderr. El proceso principal sanea esa salida antes de registrarla (`sanitize_tool_output`):
puede contener datos del archivo.

pypdfium2 se importa dentro de las funciones que lo usan, así el convertidor puede importar este
módulo para armar el comando, leer los eventos y calcular `raster_size` sin cargar PDFium en el
proceso principal.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO

from converter.naming import page_entry_name

MODULE = "converter.workers.pdf_raster"  # para `python -m`

EXIT_OK = 0
EXIT_USAGE = 2  # el código de argparse para argumentos inválidos

POINTS_PER_INCH = 72  # un punto PDF mide 1/72 de pulgada (Req 8.4)

PNG_COMPRESS_LEVEL = 6
ZIP_COMPRESS_LEVEL = 6
ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)  # fecha mínima de ZIP: el archivo es reproducible
ZIP_FILE_MODE = 0o644  # permisos de cada entrada, fijos para que el ZIP no dependa del sistema
ZIP_CREATE_SYSTEM = 3  # Unix: la interpretación de `external_attr`
PAGE_TEMP_SUFFIX = ".page.png"  # junto a `<resultado>`, la página que se está empaquetando

_DPI = "--dpi"
_SEPARATOR = "--"


# --------------------------------------------------------------------------------------------
# Tamaño rasterizado


class PageSize(Protocol):
    """Tamaño mostrado de una página en puntos PDF; `PdfPage` del inspector `pdf` lo cumple."""

    @property
    def width_pt(self) -> float: ...

    @property
    def height_pt(self) -> float: ...


@dataclass(frozen=True, slots=True)
class PagePoints:
    """Tamaño mostrado de una página que leyó el worker de PDFium."""

    width_pt: float
    height_pt: float


def raster_scale(dpi: int) -> float:
    """Factor de escala de `page.render` para rasterizar a `dpi` píxeles por pulgada."""
    return dpi / POINTS_PER_INCH


def raster_size(page: PageSize, dpi: int) -> tuple[int, int]:
    """Ancho y alto en píxeles de `page` rasterizada a `dpi` píxeles por pulgada (Req 8.4).

    Es el redondeo de `PdfPage.render`: cada dimensión es `ceil(puntos * escala)`, con la escala
    de `raster_scale`. Verificar el Límite_de_Píxeles (Req 8.11) y renderizar usan esta función.
    """
    scale = raster_scale(dpi)
    return math.ceil(page.width_pt * scale), math.ceil(page.height_pt * scale)


# --------------------------------------------------------------------------------------------
# Eventos de la salida estándar


class WorkerOutputError(ValueError):
    """Una línea de la salida estándar del worker no es un evento válido."""


@dataclass(frozen=True, slots=True)
class PageEvent:
    """El worker terminó la página `page` (desde 1) de un documento de `total` páginas."""

    page: int
    total: int

    def __post_init__(self) -> None:
        if not 1 <= self.page <= self.total:
            raise ValueError(f"página fuera de rango: {self.page} de {self.total}")


@dataclass(frozen=True, slots=True)
class DoneEvent:
    """El worker terminó el documento: rasterizó `pages` páginas."""

    pages: int

    def __post_init__(self) -> None:
        if self.pages < 0:
            raise ValueError(f"cantidad de páginas negativa: {self.pages}")


type Event = PageEvent | DoneEvent


def dump_event(event: Event) -> str:
    """Línea de JSON solo ASCII y sin saltos de línea para `event`."""
    data: dict[str, object]
    if isinstance(event, PageEvent):
        data = {"event": "page", "page": event.page, "total": event.total}
    else:
        data = {"event": "done", "pages": event.pages}
    return json.dumps(data, separators=(",", ":"))


def _integer(data: Mapping[str, object], key: str, minimum: int) -> int:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise WorkerOutputError(f"{key}: debe ser un entero de {minimum} o más")
    return value


def _build_event(data: Mapping[str, object]) -> Event:
    kind = data.get("event")
    if kind == "page" and data.keys() == {"event", "page", "total"}:
        return PageEvent(_integer(data, "page", 1), _integer(data, "total", 1))
    if kind == "done" and data.keys() == {"event", "pages"}:
        return DoneEvent(_integer(data, "pages", 0))
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
# Rasterizado


def _load_pdfium() -> Any:
    """El módulo pypdfium2, importado la primera vez que se necesita."""
    # pypdfium2 no publica anotaciones de tipos; el módulo queda como Any
    import pypdfium2  # type: ignore[import-untyped]

    return pypdfium2


def _render_page(page: Any, dpi: int, destination: Path) -> None:
    """Rasteriza `page` (un `PdfPage` de pypdfium2) a `dpi` y la guarda como PNG en `destination`.

    Lanza `RuntimeError` si la imagen no mide lo que calcula `raster_size`.
    """
    expected = raster_size(PagePoints(*page.get_size()), dpi)
    bitmap = page.render(scale=raster_scale(dpi), may_draw_forms=True)
    try:
        if (bitmap.width, bitmap.height) != expected:
            raise RuntimeError(
                "el tamaño rasterizado no coincide con raster_size: "
                f"{bitmap.width}x{bitmap.height} y {expected[0]}x{expected[1]}"
            )
        image = bitmap.to_pil()
        try:
            # Sin pnginfo, metadatos ni marca de tiempo: las opciones son fijas (Req 13.4)
            image.save(destination, format="PNG", compress_level=PNG_COMPRESS_LEVEL)
        finally:
            image.close()
    finally:
        bitmap.close()


def _add_to_archive(archive: zipfile.ZipFile, name: str, source: Path) -> None:
    """Agrega `source` a `archive` como la entrada `name`, con fecha y permisos fijos."""
    info = zipfile.ZipInfo(name, date_time=ZIP_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = ZIP_CREATE_SYSTEM
    info.external_attr = ZIP_FILE_MODE << 16
    info.file_size = source.stat().st_size  # para que zipfile sepa si necesita ZIP64
    with source.open("rb") as stream, archive.open(info, "w") as entry:
        shutil.copyfileobj(stream, entry)


def render_pages(
    source: Path,
    output: Path,
    dpi: int,
    entry_base: str,
    on_page: Callable[[PageEvent], None],
) -> DoneEvent:
    """Rasteriza el PDF `source` a `dpi` y escribe el resultado en `output`.

    Con una página, `output` es su PNG; con varias, un ZIP con una entrada por página llamada
    `page_entry_name(entry_base, N, T)`. Llama a `on_page` al terminar cada página y devuelve el
    resumen del documento. Lanza `PdfiumError` si PDFium no puede abrir el documento, lo que
    incluye un PDF sin páginas, o no puede cargar una página.
    """
    pdf = _load_pdfium().PdfDocument(str(source))
    try:
        pdf.init_forms()  # antes de leer las páginas: así `may_draw_forms` dibuja los campos
        total = len(pdf)  # PDFium no abre un PDF sin páginas, así que hay al menos una
        page_file = output.with_name(output.name + PAGE_TEMP_SUFFIX)
        archive = (
            zipfile.ZipFile(
                output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=ZIP_COMPRESS_LEVEL
            )
            if total > 1
            else None
        )
        try:
            for index in range(total):
                page = pdf[index]
                try:
                    if archive is None:
                        _render_page(page, dpi, output)
                    else:
                        _render_page(page, dpi, page_file)
                        _add_to_archive(
                            archive, page_entry_name(entry_base, index + 1, total), page_file
                        )
                        page_file.unlink()
                finally:
                    page.close()
                on_page(PageEvent(index + 1, total))
        finally:
            if archive is not None:
                archive.close()
                page_file.unlink(missing_ok=True)
    finally:
        pdf.close()
    return DoneEvent(total)


# --------------------------------------------------------------------------------------------
# Línea de comandos


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerArgs:
    """Argumentos de una invocación del worker."""

    source: Path  # Archivo_de_Origen
    output: Path  # Archivo_Resultado; el worker lo crea o lo reemplaza
    entry_base: str  # Nombre_Base normalizado de las entradas del ZIP
    dpi: int  # Resolución_de_Rasterizado


def _positive_int(raw: str) -> int:
    # Solo dígitos ASCII: `int()` aceptaría además signos, espacios, "_" y dígitos no ASCII
    if not (raw.isascii() and raw.isdigit()) or int(raw) < 1:
        raise argparse.ArgumentTypeError(f"debe ser un entero positivo: {raw!r}")
    return int(raw)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m {MODULE}",
        description="Rasteriza las páginas de un PDF a PNG: un PNG o un ZIP de PNG.",
        allow_abbrev=False,
    )
    parser.add_argument(
        _DPI, dest="dpi", type=_positive_int, required=True, help="Resolución_de_Rasterizado"
    )
    parser.add_argument("source", type=Path, help="PDF de origen")
    parser.add_argument("output", type=Path, help="PNG o ZIP que se escribe")
    parser.add_argument("entry_base", help="Nombre_Base normalizado de las imágenes del ZIP")
    return parser


def parse_args(args: Sequence[str]) -> WorkerArgs:
    """Interpreta los argumentos del worker. Lanza `SystemExit(2)` si son inválidos."""
    parsed = _parser().parse_args(list(args))
    return WorkerArgs(
        source=parsed.source,
        output=parsed.output,
        entry_base=parsed.entry_base,
        dpi=parsed.dpi,
    )


def format_args(args: WorkerArgs) -> list[str]:
    """Argumentos del worker para `args`; `parse_args(format_args(args)) == args`.

    Los posicionales van después de "--", así uno que empiece con "-" no se toma como opción.
    """
    return [
        _DPI,
        str(args.dpi),
        _SEPARATOR,
        str(args.source),
        str(args.output),
        args.entry_base,
    ]


def _claim_stdout() -> TextIO:
    """Devuelve un flujo sobre el stdout original y apunta el descriptor 1 a stderr."""
    sys.stdout.flush()
    result_fd = os.dup(1)
    os.dup2(2, 1)
    return os.fdopen(result_fd, "w", encoding="ascii", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Ejecuta el worker y devuelve su código de salida.

    Una excepción de pypdfium2 o del sistema de archivos se propaga: el intérprete escribe el
    traceback en stderr y termina con el código 1.
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    events = _claim_stdout()
    with events:

        def emit(event: Event) -> None:
            events.write(dump_event(event) + "\n")
            events.flush()  # el avance llega al convertidor página por página

        emit(render_pages(args.source, args.output, args.dpi, args.entry_base, emit))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
