"""Pruebas del inspector `pdf` en src/converter/detection/inspectors/pdf.py (Req 8.7, 8.10, 16.1,
16.2).

Los PDF se arman con `tests/pdf_samples.py` (páginas en blanco con `/CropBox`, `/Rotate` y cifrado
RC4 estándar) y con el propio pypdfium2, sin archivos externos.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# pypdfium2 no publica anotaciones de tipos; sus módulos quedan como Any
import pypdfium2  # type: ignore[import-untyped]
import pypdfium2.raw as pdfium_raw  # type: ignore[import-untyped]
import pytest

from converter.detection.inspectors import InspectorInput, get_inspector, registered_inspectors
from converter.detection.inspectors.pdf import PdfInfo, PdfPage, inspect_pdf, is_restricted
from converter.detection.source_info import (
    InspectorOutputError,
    SourceInfo,
    SourceRejection,
    dump_inspector_output,
    load_inspector_output,
)
from converter.eventlog import Tool
from converter.messages_es import FailureReason
from converter.models import Stage
from converter.sandbox.limits import SandboxTool
from converter.workers.inspect import MODULE
from tests.pdf_samples import (
    ACCESSIBILITY,
    ALL_PERMISSIONS,
    ANNOTATE,
    ASSEMBLE,
    COPY,
    FILL_FORMS,
    MODIFY,
    PRINT,
    PRINT_HIGH_QUALITY,
    Encryption,
    PageSpec,
    build_pdf,
    permissions_without,
)


def inspect_bytes(tmp_path: Path, data: bytes) -> SourceInfo | SourceRejection:
    """Ejecuta el inspector `pdf` sobre un archivo con el contenido `data`."""
    path = tmp_path / "source"
    path.write_bytes(data)
    return inspect_pdf(InspectorInput(path=path, real_type="PDF", max_image_pixels=1_000_000))


def inspect_info(tmp_path: Path, data: bytes) -> PdfInfo:
    result = inspect_bytes(tmp_path, data)
    assert isinstance(result, PdfInfo), result
    return result


def sizes(info: PdfInfo) -> list[tuple[float, float]]:
    return [(page.width_pt, page.height_pt) for page in info.pages]


# --- Registro ---


def test_inspector_is_registered_with_the_catalog_name() -> None:
    assert "pdf" in registered_inspectors()
    spec = get_inspector("pdf")
    assert spec.function is inspect_pdf
    assert spec.info_type is PdfInfo
    assert spec.tool is Tool.PYPDFIUM2
    assert spec.sandbox_tool is SandboxTool.PYTHON


# --- Tamaño mostrado ---


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        pytest.param(PageSpec(612, 792), (612, 792), id="sin_rotacion"),
        pytest.param(PageSpec(300, 400, rotate=0), (300, 400), id="rotate_0"),
        pytest.param(PageSpec(300, 400, rotate=90), (400, 300), id="rotate_90"),
        pytest.param(PageSpec(300, 400, rotate=180), (300, 400), id="rotate_180"),
        pytest.param(PageSpec(300, 400, rotate=270), (400, 300), id="rotate_270"),
        pytest.param(PageSpec(300, 400, rotate=-90), (400, 300), id="rotate_negativo"),
        pytest.param(PageSpec(300, 400, rotate=450), (400, 300), id="rotate_mayor_que_360"),
        pytest.param(PageSpec(300, 400, crop_box=(10, 20, 110, 70)), (100, 50), id="cropbox"),
        pytest.param(
            PageSpec(300, 400, rotate=90, crop_box=(10, 20, 110, 70)),
            (50, 100),
            id="cropbox_rotate",
        ),
        pytest.param(
            PageSpec(300, 400, crop_box=(-50, -50, 1000, 1000)), (300, 400), id="cropbox_mayor"
        ),
        pytest.param(
            PageSpec(300, 400, crop_box=(100, 100, 1000, 1000)), (200, 300), id="cropbox_parcial"
        ),
    ],
)
def test_page_size_is_the_displayed_size(
    tmp_path: Path, page: PageSpec, expected: tuple[float, float]
) -> None:
    info = inspect_info(tmp_path, build_pdf([page]))

    assert sizes(info) == [expected]
    assert info.permissions_restricted is False


def test_pages_are_listed_in_page_number_order(tmp_path: Path) -> None:
    pages = [
        PageSpec(100, 200),
        PageSpec(300, 150, rotate=90),
        PageSpec(50, 60, crop_box=(0, 0, 25, 30)),
        PageSpec(612, 792),
    ]

    info = inspect_info(tmp_path, build_pdf(pages))

    assert sizes(info) == [(100, 200), (150, 300), (25, 30), (612, 792)]


def test_pdf_written_by_pdfium_is_inspected(tmp_path: Path) -> None:
    document = pypdfium2.PdfDocument.new()
    for width, height, rotation in [(200, 100, 0), (200, 100, 90), (595.276, 841.89, 270)]:
        page = document.new_page(width, height)
        page.set_rotation(rotation)
        page.close()
    path = tmp_path / "written.pdf"
    document.save(path)
    document.close()

    info = inspect_info(tmp_path, path.read_bytes())

    # PDFium guarda los tamaños en float de 32 bits
    assert sizes(info) == [
        (200, 100),
        (100, 200),
        pytest.approx((841.89, 595.276), rel=1e-6),
    ]
    assert info.permissions_restricted is False


# --- PDF_Protegido ---


@pytest.mark.req("8.7", "16.1")
@pytest.mark.parametrize("revision", [2, 3])
def test_pdf_that_needs_a_password_to_open_is_rejected_as_protected(
    tmp_path: Path, revision: int
) -> None:
    encryption = Encryption(user_password=b"secreta", revision=revision)  # type: ignore[arg-type]

    result = inspect_bytes(tmp_path, build_pdf([PageSpec()], encryption=encryption))

    assert isinstance(result, SourceRejection)
    assert result.reason is FailureReason.PROTECTED_PDF
    assert result.failure.stage is Stage.DETECTION
    assert dict(result.params) == {}


@pytest.mark.req("8.7")
def test_open_password_error_code_is_the_one_that_rejects(tmp_path: Path) -> None:
    # La condición del rechazo es FPDF_ERR_PASSWORD, no cualquier PDF cifrado
    path = tmp_path / "protected.pdf"
    path.write_bytes(build_pdf(encryption=Encryption(user_password=b"secreta")))

    with pytest.raises(pypdfium2.PdfiumError) as error:
        pypdfium2.PdfDocument(path)

    assert error.value.err_code == pdfium_raw.FPDF_ERR_PASSWORD


# --- PDF_con_Permisos_Restringidos ---


@pytest.mark.req("8.10")
@pytest.mark.parametrize(
    ("encryption", "restricted"),
    [
        pytest.param(None, False, id="sin_cifrado"),
        pytest.param(Encryption(revision=2), False, id="r2_todos_los_permisos"),
        pytest.param(Encryption(revision=3), False, id="r3_todos_los_permisos"),
        pytest.param(
            Encryption(revision=2, permissions=permissions_without(PRINT)),
            True,
            id="r2_sin_imprimir",
        ),
        pytest.param(
            Encryption(revision=2, permissions=permissions_without(COPY)), True, id="r2_sin_copiar"
        ),
        pytest.param(
            Encryption(revision=2, permissions=permissions_without(MODIFY)),
            True,
            id="r2_sin_modificar",
        ),
        pytest.param(
            Encryption(revision=2, permissions=permissions_without(ANNOTATE)),
            True,
            id="r2_sin_anotar",
        ),
        pytest.param(
            Encryption(revision=3, permissions=permissions_without(PRINT, COPY)),
            True,
            id="r3_sin_imprimir_ni_copiar",
        ),
        pytest.param(
            Encryption(revision=3, owner_password=b"", permissions=permissions_without(COPY)),
            True,
            id="r3_sin_contrasena_de_propietario",
        ),
        # Los bits 9 a 12 de /P solo cuentan desde la revisión 3
        pytest.param(
            Encryption(
                revision=2,
                permissions=permissions_without(
                    FILL_FORMS, ACCESSIBILITY, ASSEMBLE, PRINT_HIGH_QUALITY
                ),
            ),
            False,
            id="r2_sin_bits_9_a_12",
        ),
        *(
            pytest.param(
                Encryption(revision=3, permissions=permissions_without(bit)),
                True,
                id=f"r3_sin_{name}",
            )
            for name, bit in [
                ("formularios", FILL_FORMS),
                ("accesibilidad", ACCESSIBILITY),
                ("ensamblar", ASSEMBLE),
                ("alta_calidad", PRINT_HIGH_QUALITY),
            ]
        ),
    ],
)
def test_permissions_restricted_follows_the_p_entry(
    tmp_path: Path, encryption: Encryption | None, restricted: bool
) -> None:
    info = inspect_info(tmp_path, build_pdf([PageSpec(300, 400)], encryption=encryption))

    assert info.permissions_restricted is restricted
    # Se procesa como cualquier otro PDF: sin contraseña y con sus páginas
    assert sizes(info) == [(300, 400)]


@pytest.mark.req("8.10")
def test_an_unencrypted_pdf_is_never_restricted() -> None:
    assert is_restricted(-1, 0) is False
    assert is_restricted(-1, 0xFFFFFFFF) is False


def test_all_permissions_constant_leaves_every_permission_bit_set() -> None:
    for bit in (PRINT, MODIFY, COPY, ANNOTATE, FILL_FORMS, ACCESSIBILITY, ASSEMBLE):
        assert ALL_PERMISSIONS & bit
    assert ALL_PERMISSIONS & PRINT_HIGH_QUALITY


# --- Archivo dañado ---


def damaged_samples() -> dict[str, bytes]:
    valid = build_pdf([PageSpec(), PageSpec(300, 400)])
    return {
        "vacio": b"",
        "solo_cabecera": b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n",
        "basura_con_cabecera": b"%PDF-1.7\n" + bytes(range(256)) * 4,
        "truncado_a_la_mitad": valid[: len(valid) // 2],
        "truncado_antes_de_xref": valid[: valid.index(b"xref")],
        "bytes_alterados": valid[:100] + bytes(50) + valid[150:],
        "sin_paginas": build_pdf([]),
        "pagina_declarada_que_no_existe": build_pdf([PageSpec()], page_count=3),
        "pagina_sin_area_visible": build_pdf(
            [PageSpec(300, 400, crop_box=(1000, 1000, 2000, 2000))]
        ),
    }


@pytest.mark.req("16.1", "16.2")
@pytest.mark.parametrize("name", list(damaged_samples()))
def test_pdf_with_invalid_structure_is_rejected_as_damaged(tmp_path: Path, name: str) -> None:
    result = inspect_bytes(tmp_path, damaged_samples()[name])

    assert isinstance(result, SourceRejection)
    assert result.reason is FailureReason.DAMAGED
    assert result.failure.stage is Stage.DETECTION
    assert dict(result.params) == {}


@pytest.mark.req("16.2")
def test_other_pdfium_errors_are_not_classified_by_the_inspector(tmp_path: Path) -> None:
    # Un esquema de cifrado no admitido (FPDF_ERR_SECURITY) no es PDF_Protegido ni archivo
    # dañado: se propaga para que el proceso principal lo clasifique
    data = build_pdf(encryption=Encryption()).replace(b"/Filter /Standard", b"/Filter /Custom")

    with pytest.raises(pypdfium2.PdfiumError) as error:
        inspect_bytes(tmp_path, data)

    assert error.value.err_code == pdfium_raw.FPDF_ERR_SECURITY


# --- Datos del SourceInfo ---


@pytest.mark.parametrize(
    ("width", "height"),
    [(0, 10), (10, 0), (-1, 10), (10, -0.5), (float("nan"), 10), (10, float("inf"))],
)
def test_pdf_page_rejects_sizes_that_are_not_positive_and_finite(
    width: float, height: float
) -> None:
    with pytest.raises(ValueError, match="positivo y finito"):
        PdfPage(width_pt=width, height_pt=height)


def test_pdf_page_and_info_reject_values_of_other_types() -> None:
    page = PdfPage(10, 20)
    with pytest.raises(TypeError):
        PdfPage(width_pt=True, height_pt=20)
    with pytest.raises(TypeError):
        PdfPage(width_pt="10", height_pt=20)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="tupla de PdfPage"):
        PdfInfo(pages=[page], permissions_restricted=False)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="tupla de PdfPage"):
        PdfInfo(pages=((10, 20),), permissions_restricted=False)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="booleano"):
        PdfInfo(pages=(page,), permissions_restricted=1)  # type: ignore[arg-type]


def test_info_round_trips_through_the_json_of_the_worker() -> None:
    info = PdfInfo(
        pages=(PdfPage(612, 792), PdfPage(595.2760009765625, 841.8900146484375)),
        permissions_restricted=True,
    )

    assert load_inspector_output(dump_inspector_output(info)) == info


@pytest.mark.parametrize(
    "pages",
    ['[{"width_pt":0,"height_pt":10}]', '[{"width_pt":10}]', '[{"width_pt":"10","height_pt":10}]'],
)
def test_json_with_invalid_pages_is_rejected(pages: str) -> None:
    text = f'{{"info":{{"type":"pdf","data":{{"pages":{pages},"permissions_restricted":false}}}}}}'

    with pytest.raises(InspectorOutputError):
        load_inspector_output(text)


# --- Worker ---


def run_worker(path: Path) -> SourceInfo | SourceRejection:
    completed = subprocess.run(
        [sys.executable, "-I", "-m", MODULE, "--real-type", "PDF", "--", "pdf", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return load_inspector_output(completed.stdout)


@pytest.mark.req("8.7", "8.10", "16.2")
def test_worker_writes_the_result_of_the_pdf_inspector(tmp_path: Path) -> None:
    restricted = tmp_path / "restricted.pdf"
    restricted.write_bytes(
        build_pdf(
            [PageSpec(595.276, 841.89), PageSpec(300, 400, rotate=90)],
            encryption=Encryption(revision=3, permissions=permissions_without(COPY)),
        )
    )
    protected = tmp_path / "protected.pdf"
    protected.write_bytes(build_pdf(encryption=Encryption(user_password=b"secreta")))
    damaged = tmp_path / "damaged.pdf"
    damaged.write_bytes(b"%PDF-1.7\n" + bytes(range(256)))

    info = run_worker(restricted)
    protected_result = run_worker(protected)
    damaged_result = run_worker(damaged)

    assert isinstance(info, PdfInfo)
    assert info.permissions_restricted is True
    assert sizes(info) == sizes(inspect_info(tmp_path, restricted.read_bytes()))
    assert info.pages[1] == PdfPage(400, 300)
    assert protected_result == SourceRejection(FailureReason.PROTECTED_PDF)
    assert damaged_result == SourceRejection(FailureReason.DAMAGED)
