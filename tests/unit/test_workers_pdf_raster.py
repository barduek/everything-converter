"""Pruebas de src/converter/workers/pdf_raster.py: `raster_size`, eventos, argumentos y
rasterizado de PDF reales con pypdfium2 (Req 8.4, 8.5, 8.6, 8.8, 8.9, 8.10, 8.11).

El recorrido completo a través del Ejecutor aislado y del convertidor `pdf.raster` está en
`test_converters_pdf_raster.py`.
"""

from __future__ import annotations

import io
import subprocess
import sys
import zipfile
from pathlib import Path

# pypdfium2 no publica anotaciones de tipos; sus módulos quedan como Any
import pypdfium2  # type: ignore[import-untyped]
import pytest
from PIL import Image

from converter.detection.inspectors.pdf import PdfPage
from converter.naming import page_entry_name
from converter.workers.pdf_raster import (
    EXIT_USAGE,
    MODULE,
    DoneEvent,
    PageEvent,
    PagePoints,
    WorkerArgs,
    WorkerOutputError,
    dump_event,
    format_args,
    load_event,
    parse_args,
    raster_scale,
    raster_size,
    render_pages,
)
from tests.pdf_samples import Encryption, PageSpec
from tests.pdf_samples import build_pdf as build_blank_pdf
from tests.pdf_text_support import build_pdf as build_text_pdf

DPI = 150

# --- raster_size ---


@pytest.mark.req("8.4")
def test_raster_scale_converts_dots_per_inch_to_a_scale_factor() -> None:
    assert raster_scale(72) == 1.0
    assert raster_scale(144) == 2.0
    assert raster_scale(150) == 150 / 72


@pytest.mark.req("8.4")
@pytest.mark.parametrize(
    ("width_pt", "height_pt", "dpi", "expected"),
    [
        (612, 792, 72, (612, 792)),  # a 72 ppp un punto es un píxel
        (612, 792, 144, (1224, 1584)),
        (595.0, 842.0, 72, (595, 842)),
        (400, 400, 150, (834, 834)),  # 833,33… sube al entero siguiente
        (0.5, 0.5, 72, (1, 1)),  # nunca menos de un píxel en una página con área
        (200.25, 300.75, 72, (201, 301)),
    ],
)
def test_raster_size_is_the_displayed_size_in_inches_times_the_resolution(
    width_pt: float, height_pt: float, dpi: int, expected: tuple[int, int]
) -> None:
    assert raster_size(PagePoints(width_pt, height_pt), dpi) == expected


@pytest.mark.req("8.4")
def test_raster_size_accepts_the_page_of_the_pdf_inspector() -> None:
    # La verificación del Límite_de_Píxeles usa el PdfPage del inspector, sin redondear
    assert raster_size(PdfPage(width_pt=612, height_pt=792), 72) == (612, 792)


_SIZE_CASES = [
    PageSpec(),  # Carta
    PageSpec(width=595.276, height=841.89),  # A4 con decimales
    PageSpec(width=200, height=300, rotate=90),
    PageSpec(width=200, height=300, rotate=270),
    PageSpec(width=200, height=300, rotate=180),
    PageSpec(width=612, height=792, crop_box=(10.5, 20.25, 400.125, 500.5)),
    PageSpec(width=612, height=792, crop_box=(0, 0, 300, 100), rotate=90),
    PageSpec(width=33.3, height=77.7),
    PageSpec(width=1, height=1),
]


@pytest.mark.req("8.4", "8.11")
@pytest.mark.parametrize("dpi", [36, 72, 96, 150, 300, 600])
@pytest.mark.parametrize("spec", _SIZE_CASES)
def test_raster_size_is_the_size_pdfium_renders(spec: PageSpec, dpi: int) -> None:
    # La verificación y el renderizado deben coincidir con el tamaño real, con CropBox y /Rotate
    pdf = pypdfium2.PdfDocument(build_blank_pdf([spec]))
    try:
        page = pdf[0]
        try:
            width, height = page.get_size()
            bitmap = page.render(scale=raster_scale(dpi), may_draw_forms=True)
            try:
                assert (bitmap.width, bitmap.height) == raster_size(PagePoints(width, height), dpi)
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        pdf.close()


@pytest.mark.req("8.4")
@pytest.mark.parametrize("spec", _SIZE_CASES)
def test_raster_size_is_within_one_pixel_of_the_size_in_inches_times_the_resolution(
    spec: PageSpec,
) -> None:
    pdf = pypdfium2.PdfDocument(build_blank_pdf([spec]))
    try:
        width, height = pdf[0].get_size()
    finally:
        pdf.close()

    got_width, got_height = raster_size(PagePoints(width, height), DPI)

    assert abs(got_width - width / 72 * DPI) <= 1
    assert abs(got_height - height / 72 * DPI) <= 1


# --- Eventos de la salida estándar ---


@pytest.mark.parametrize(
    "event",
    [PageEvent(page=1, total=1), PageEvent(page=3, total=12), DoneEvent(pages=0), DoneEvent(12)],
)
def test_events_round_trip_as_single_ascii_lines(event: PageEvent | DoneEvent) -> None:
    line = dump_event(event)

    assert "\n" not in line
    assert line.isascii()
    assert load_event(line) == event


def test_events_have_a_fixed_json_form() -> None:
    assert dump_event(PageEvent(2, 5)) == '{"event":"page","page":2,"total":5}'
    assert dump_event(DoneEvent(5)) == '{"event":"done","pages":5}'


@pytest.mark.parametrize(
    "line",
    [
        "",
        "no es JSON",
        "[]",
        "3",
        '{"event":"otro"}',
        '{"event":"page","page":1}',  # falta total
        '{"event":"page","page":1,"total":1,"extra":1}',
        '{"event":"page","page":0,"total":1}',
        '{"event":"page","page":2,"total":1}',
        '{"event":"page","page":true,"total":1}',
        '{"event":"page","page":1.0,"total":1}',
        '{"event":"done","pages":-1}',
        '{"event":"done","pages":"2"}',
        '{"event":"done"}',
    ],
)
def test_load_event_rejects_invalid_lines(line: str) -> None:
    with pytest.raises(WorkerOutputError):
        load_event(line)


# --- Argumentos ---


@pytest.mark.parametrize(
    "args",
    [
        WorkerArgs(
            source=Path("/ws/source/original"),
            output=Path("/ws/work/output"),
            entry_base="informe",
            dpi=150,
        ),
        WorkerArgs(source=Path("-raro"), output=Path("--otro"), entry_base="-base", dpi=36),
    ],
)
def test_format_args_round_trips(args: WorkerArgs) -> None:
    formatted = format_args(args)

    # Los posicionales van después de "--": uno que empieza con "-" no es una opción
    assert "--" in formatted
    assert parse_args(formatted) == args


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--dpi", "150", "--", "origen.pdf", "salida"],  # falta la base
        ["--", "a", "b", "c"],  # falta --dpi
        ["--dpi", "0", "--", "a", "b", "c"],
        ["--dpi", "-5", "--", "a", "b", "c"],
        ["--dpi", "1_5", "--", "a", "b", "c"],
        ["--dpi", "ciento", "--", "a", "b", "c"],
        ["--dpi", "150", "--", "a", "b", "c", "d"],
        ["--otra", "x", "--dpi", "150", "--", "a", "b", "c"],
    ],
)
def test_parse_args_rejects_invalid_arguments(args: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        parse_args(args)
    assert error.value.code == EXIT_USAGE


# --- Rasterizado de PDF reales ---


def _render(
    tmp_path: Path, data: bytes, *, dpi: int = DPI, base: str = "informe"
) -> tuple[Path, DoneEvent, list[PageEvent]]:
    """Ruta del resultado, resumen y eventos de página de `render_pages` sobre el PDF `data`."""
    source = tmp_path / "origen.pdf"
    output = tmp_path / "resultado"
    source.write_bytes(data)
    pages: list[PageEvent] = []
    done = render_pages(source, output, dpi, base, pages.append)
    return output, done, pages


def _png(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data), formats=["PNG"])
    image.load()
    return image


def _direct_render(data: bytes, index: int, dpi: int = DPI) -> Image.Image:
    """La página `index` (desde 0) rasterizada directamente con pypdfium2."""
    pdf = pypdfium2.PdfDocument(data)
    try:
        page = pdf[index]
        try:
            image: Image.Image = page.render(scale=raster_scale(dpi), may_draw_forms=True).to_pil()
            return image
        finally:
            page.close()
    finally:
        pdf.close()


@pytest.mark.req("8.9", "8.4")
def test_a_single_page_gives_one_png_and_no_zip(tmp_path: Path) -> None:
    pdf = build_text_pdf([["Una sola página"]])

    output, done, pages = _render(tmp_path, pdf)

    assert not zipfile.is_zipfile(output)
    image = _png(output.read_bytes())
    assert image.format == "PNG"
    assert image.mode == "RGB"
    assert image.size == raster_size(PagePoints(400, 400), DPI)
    assert image.tobytes() == _direct_render(pdf, 0).tobytes()
    assert done == DoneEvent(pages=1)
    assert pages == [PageEvent(1, 1)]
    # Ni el archivo temporal de página queda junto al resultado
    assert [path.name for path in sorted(tmp_path.iterdir())] == ["origen.pdf", "resultado"]


@pytest.mark.req("8.5", "8.8")
def test_several_pages_give_a_zip_with_one_png_per_page_named_by_page_number(
    tmp_path: Path,
) -> None:
    pdf = build_text_pdf([[f"Página {n}"] for n in range(1, 13)])

    output, done, pages = _render(tmp_path, pdf, base="informe")

    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == [page_entry_name("informe", n, 12) for n in range(1, 13)]
        assert archive.namelist()[0] == "informe_01.png"
        assert archive.namelist()[-1] == "informe_12.png"
        assert archive.testzip() is None
        for entry in archive.infolist():
            assert _png(archive.read(entry)).format == "PNG"
    assert done == DoneEvent(pages=12)
    assert pages == [PageEvent(n, 12) for n in range(1, 13)]
    assert [path.name for path in sorted(tmp_path.iterdir())] == ["origen.pdf", "resultado"]


@pytest.mark.req("8.5")
def test_the_zip_is_deflated_flat_dated_1980_and_reproducible(tmp_path: Path) -> None:
    pdf = build_text_pdf([["Uno"], ["Dos"], ["Tres"]])
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    first, _, _ = _render(tmp_path / "a", pdf)
    second, _, _ = _render(tmp_path / "b", pdf)

    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert len(archive.infolist()) == 3
        for entry in archive.infolist():
            assert entry.compress_type == zipfile.ZIP_DEFLATED
            assert entry.date_time == (1980, 1, 1, 0, 0, 0)
            assert not entry.is_dir()
            assert "/" not in entry.filename
            assert entry.external_attr >> 16 == 0o644


@pytest.mark.req("8.5")
def test_zip_entry_names_keep_a_non_ascii_base(tmp_path: Path) -> None:
    output, _, _ = _render(tmp_path, build_text_pdf([["a"], ["b"]]), base="Año nuevo ñandú")

    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["Año nuevo ñandú_1.png", "Año nuevo ñandú_2.png"]


@pytest.mark.req("8.6")
def test_image_n_is_the_raster_of_page_n(tmp_path: Path) -> None:
    pdf = build_text_pdf([["Uno", "uno"], ["Dos"], ["Tres", "tres", "tres"], ["Cuatro"]])

    output, _, _ = _render(tmp_path, pdf)

    with zipfile.ZipFile(output) as archive:
        images = [_png(archive.read(name)).tobytes() for name in archive.namelist()]
    assert images == [_direct_render(pdf, index).tobytes() for index in range(4)]
    assert len(set(images)) == 4  # cada página es distinta de las demás


@pytest.mark.req("8.4")
def test_pages_without_text_also_give_an_image(tmp_path: Path) -> None:
    pdf = build_text_pdf([[], ["Con texto"], []])

    output, done, _ = _render(tmp_path, pdf)

    with zipfile.ZipFile(output) as archive:
        assert len(archive.namelist()) == 3
        blank = _png(archive.read(archive.namelist()[0]))
    assert blank.size == raster_size(PagePoints(400, 400), DPI)
    assert blank.getextrema() == ((255, 255), (255, 255), (255, 255))  # blanca
    assert done.pages == 3


@pytest.mark.req("8.4")
def test_pages_are_rasterized_as_displayed_with_rotation_and_cropbox(tmp_path: Path) -> None:
    specs = [
        PageSpec(width=200, height=300),
        PageSpec(width=200, height=300, rotate=90),  # se muestra apaisada
        PageSpec(width=612, height=792, crop_box=(100, 100, 400, 200)),  # 300 × 100
        PageSpec(width=612, height=792, crop_box=(100, 100, 400, 200), rotate=270),
    ]

    output, _, _ = _render(tmp_path, build_blank_pdf(specs), dpi=72)

    with zipfile.ZipFile(output) as archive:
        sizes = [_png(archive.read(name)).size for name in archive.namelist()]
    assert sizes == [(200, 300), (300, 200), (300, 100), (100, 300)]


@pytest.mark.req("8.4")
@pytest.mark.parametrize("dpi", [36, 72, 150, 300])
def test_the_resolution_scales_the_image_within_one_pixel(tmp_path: Path, dpi: int) -> None:
    output, _, _ = _render(tmp_path, build_blank_pdf([PageSpec(width=612, height=792)]), dpi=dpi)

    width, height = _png(output.read_bytes()).size

    assert abs(width - 612 / 72 * dpi) <= 1
    assert abs(height - 792 / 72 * dpi) <= 1


@pytest.mark.req("8.4")
def test_png_has_no_metadata_and_a_fixed_encoding(tmp_path: Path) -> None:
    output, _, _ = _render(tmp_path, build_text_pdf([["Texto"]]))

    image = _png(output.read_bytes())

    assert image.info == {}
    assert b"tIME" not in output.read_bytes()


def _form_pdf() -> bytes:
    """PDF de una página con un campo de formulario cuyo aspecto es un cuadrado negro."""
    appearance = b"0 0 0 rg 0 0 100 40 re f"
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [5 0 R] >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Annots [5 0 R] >>",
        b"<< /Type /XObject /Subtype /Form /BBox [0 0 100 40] /Length %d >>\nstream\n%s\nendstream"
        % (len(appearance), appearance),
        b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (campo) /V (hola) /Rect [50 80 150 120] "
        b"/F 4 /AP << /N 4 0 R >> >>",
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(bodies) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(bodies) + 1,
        xref,
    )
    return bytes(out)


@pytest.mark.req("8.4")
def test_form_fields_are_drawn(tmp_path: Path) -> None:
    output, _, _ = _render(tmp_path, _form_pdf(), dpi=72)

    image = _png(output.read_bytes())

    assert image.size == (200, 200)
    assert image.getpixel((100, 100)) == (0, 0, 0)  # el aspecto del campo, en el centro
    assert image.getpixel((10, 10)) == (255, 255, 255)


@pytest.mark.req("8.10")
def test_a_pdf_with_restricted_permissions_gives_the_same_image(tmp_path: Path) -> None:
    pages = [["Texto", "reservado"], ["Segunda página"]]
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    plain, _, _ = _render(tmp_path / "a", build_text_pdf(pages))
    restricted, done, _ = _render(
        tmp_path / "b", build_text_pdf(pages, owner_password=b"propietario")
    )

    with zipfile.ZipFile(plain) as left, zipfile.ZipFile(restricted) as right:
        assert left.namelist() == right.namelist()
        for name in left.namelist():
            assert _png(left.read(name)).tobytes() == _png(right.read(name)).tobytes()
    assert done.pages == 2


@pytest.mark.req("8.10")
def test_a_blank_pdf_with_owner_encryption_is_rasterized_without_a_password(
    tmp_path: Path,
) -> None:
    data = build_blank_pdf([PageSpec(width=100, height=50)], encryption=Encryption())

    output, _, _ = _render(tmp_path, data, dpi=72)

    assert _png(output.read_bytes()).size == (100, 50)


def test_a_pdf_without_pages_is_not_opened_by_pdfium(tmp_path: Path) -> None:
    with pytest.raises(pypdfium2.PdfiumError):
        _render(tmp_path, build_blank_pdf([]))


def test_a_pdf_pdfium_cannot_open_raises_the_pdfium_error(tmp_path: Path) -> None:
    with pytest.raises(pypdfium2.PdfiumError):
        _render(tmp_path, b"%PDF-1.4\nno es un PDF")


def test_the_page_size_check_fails_instead_of_returning_images_of_another_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Si el redondeo de raster_size y el de PDFium difirieran, el worker no entrega la imagen
    monkeypatch.setattr(
        "converter.workers.pdf_raster.raster_size", lambda page, dpi: (1, 1), raising=True
    )

    with pytest.raises(RuntimeError, match="raster_size"):
        _render(tmp_path, build_text_pdf([["Texto"]]))


# --- El worker como proceso ---


def _run_worker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-m", MODULE, *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.req("8.5", "8.9")
def test_the_worker_process_writes_the_result_and_one_event_line_per_page(
    tmp_path: Path,
) -> None:
    source = tmp_path / "origen.pdf"
    output = tmp_path / "resultado"
    source.write_bytes(build_text_pdf([["Hola"], [], ["Mundo"]]))
    args = WorkerArgs(source=source, output=output, entry_base="doc", dpi=72)

    completed = _run_worker(*format_args(args))

    assert completed.returncode == 0, completed.stderr
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["doc_1.png", "doc_2.png", "doc_3.png"]
    assert [load_event(line) for line in completed.stdout.splitlines()] == [
        PageEvent(1, 3),
        PageEvent(2, 3),
        PageEvent(3, 3),
        DoneEvent(pages=3),
    ]


def test_the_worker_exits_with_the_pdfium_error_on_stderr_and_no_events(tmp_path: Path) -> None:
    source = tmp_path / "origen.pdf"
    source.write_bytes(b"%PDF-1.4\nno es un PDF")
    args = WorkerArgs(source=source, output=tmp_path / "resultado", entry_base="doc", dpi=150)

    completed = _run_worker(*format_args(args))

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "Failed to load document" in completed.stderr
    assert not (tmp_path / "resultado").exists()


def test_the_worker_rejects_invalid_arguments_with_exit_code_2() -> None:
    completed = _run_worker("solo-uno")

    assert completed.returncode == EXIT_USAGE
    assert completed.stdout == ""
