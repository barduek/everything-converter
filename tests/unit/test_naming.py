"""Pruebas de src/converter/naming.py con los ejemplos del Requisito 15 y del glosario
(Req 15.2, 15.4, 15.5, 15.10).

Los valores esperados se escriben literales, sin calcularlos con naming.py, para que un error del
módulo haga fallar la prueba. `result_name` recibe el tipo de entrega ("single" o "zip") y no la
cantidad de páginas: elegir "zip" para un PDF de más de una página le corresponde al convertidor
de PDF a PNG, así que acá el PDF de 3 páginas se representa con `kind="zip"`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from converter.naming import ResultKind, normalize_base, result_name, split_name


@dataclass(frozen=True)
class _Target:
    """Lo que `result_name` usa del Formato_de_Destino."""

    primary_extension: str


# Extensiones_Principales del glosario
_JPG = _Target(".jpg")
_PNG = _Target(".png")
_PDF = _Target(".pdf")


@pytest.mark.req("15.2")
def test_keeps_base_and_uses_primary_extension_of_target() -> None:
    # Req 15.2: el espacio y las mayúsculas del Nombre_Base quedan; ".PNG" pasa a ".jpg"
    assert result_name("Vacaciones 2024.PNG", _JPG, "single") == "Vacaciones 2024.jpg"


@pytest.mark.req("15.4")
def test_empty_base_becomes_resultado() -> None:
    # Glosario: ".png" tiene Nombre_Base vacío y Extensión_del_Nombre ".png"
    assert split_name(".png") == ("", ".png")
    assert result_name(".png", _JPG, "single") == "resultado.jpg"


@pytest.mark.req("15.2")
def test_name_ending_in_dot_has_no_extension() -> None:
    # Glosario: un Nombre_Original que termina en punto no tiene Extensión_del_Nombre, y su
    # Nombre_Base es lo anterior a ese punto
    assert split_name("notas.") == ("notas", None)
    assert result_name("notas.", _PDF, "single") == "notas.pdf"


@pytest.mark.req("15.2")
def test_splits_at_last_dot() -> None:
    assert split_name("informe.final.docx") == ("informe.final", ".docx")
    assert result_name("informe.final.docx", _PDF, "single") == "informe.final.pdf"


@pytest.mark.req("15.10")
@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        pytest.param("zip", "informe.zip", id="3-paginas"),
        pytest.param("single", "informe.png", id="1-pagina"),
    ],
)
def test_pdf_to_png_names_zip_or_single_image(kind: ResultKind, expected: str) -> None:
    assert result_name("informe.pdf", _PNG, kind) == expected


# Caracteres de 2, 3 y 4 bytes en UTF-8
_WIDE_CHARS = {2: "é", 3: "€", 4: "😀"}


@pytest.mark.req("15.5")
@pytest.mark.parametrize(
    ("width", "bytes_before_200"),
    [
        pytest.param(width, inside, id=f"{width}-bytes-{inside}-adentro")
        for width in (2, 3, 4)
        for inside in range(1, width)
    ],
)
def test_truncation_drops_character_that_crosses_byte_200(
    width: int, bytes_before_200: int
) -> None:
    # El carácter multibyte empieza `bytes_before_200` bytes antes del byte 200 y termina
    # después: no entra completo, así que el Nombre_Base queda en el prefijo ASCII
    ascii_prefix = "a" * (200 - bytes_before_200)
    base = ascii_prefix + _WIDE_CHARS[width] + "fin"
    assert normalize_base(base) == ascii_prefix
    assert len(normalize_base(base).encode("utf-8")) == 200 - bytes_before_200


@pytest.mark.req("15.5")
@pytest.mark.parametrize("width", [2, 3, 4], ids=lambda width: f"{width}-bytes")
def test_truncation_keeps_character_that_ends_at_byte_200(width: int) -> None:
    kept = "a" * (200 - width) + _WIDE_CHARS[width]
    assert normalize_base(kept) == kept  # justo 200 bytes: no se trunca
    assert normalize_base(kept + "b") == kept


@pytest.mark.req("15.5", "15.2")
def test_truncation_leaves_extension_outside_the_200_bytes() -> None:
    # "ñ" ocupa 2 bytes: 101 ocupan 202 y se truncan a 100; con una "a" adelante ocupan 201 y
    # el corte cae en medio de la última "ñ"
    assert result_name("ñ" * 101 + ".txt", _PDF, "single") == "ñ" * 100 + ".pdf"
    assert result_name("a" + "ñ" * 100 + ".txt", _PDF, "single") == "a" + "ñ" * 99 + ".pdf"
