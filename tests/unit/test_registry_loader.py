"""Pruebas de src/converter/registry/loader.py y del catálogo de Formatos empaquetado
(Req 2.6, 4.3, 4.4, 4.5, 4.6, 4.9, 12.1).

Las pruebas del catálogo empaquetado cargan `formats.toml` con un `pairs.toml` vacío: los pares
los agrega cada familia de convertidores, con convertidores que estas pruebas no registran.
"""

from __future__ import annotations

import io
import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from converter.eventlog import EventLog
from converter.models import Category, LossClass
from converter.registry.loader import (
    CATALOG_FILE_REJECTED,
    FORMATS_PATH,
    PAIRS_PATH,
    CatalogError,
    load_catalog,
)
from converter.registry.model import MagicSignature, ProbeSignature, Signature
from converter.registry.registry import ConversionRegistry, RegistrationError, RegistryErrorCode

FIXED_TS = datetime(2025, 1, 15, 10, 32, 7, tzinfo=UTC)
CONVERTERS = ("image.raster", "image.pdf")


def _magic(*parts: tuple[int, str]) -> MagicSignature:
    return MagicSignature(tuple((offset, bytes.fromhex(hex_text)) for offset, hex_text in parts))


# Categoría, extensiones, Extensión_Principal, firma, inspector y sondas de variante
type Expected = tuple[Category, tuple[str, ...], str, Signature | None, str, tuple[str, ...]]

# Formatos de formats.toml en orden de registro, con las firmas de la tabla "Firmas" de design.md
EXPECTED_FORMATS: dict[str, Expected] = {
    "PNG": (Category.IMAGE, (".png",), ".png", _magic((0, "89504E470D0A1A0A")), "image", ()),
    "JPG": (Category.IMAGE, (".jpg", ".jpeg"), ".jpg", _magic((0, "FFD8FF")), "image", ()),
    "WEBP": (
        Category.IMAGE,
        (".webp",),
        ".webp",
        _magic((0, "52494646"), (8, "57454250")),  # "RIFF" en 0 y "WEBP" en 8
        "image",
        (),
    ),
    "PDF": (Category.DOCUMENT, (".pdf",), ".pdf", ProbeSignature("pdf_header"), "pdf", ()),
    "TXT": (Category.DOCUMENT, (".txt",), ".txt", None, "text", ()),
    "MD": (Category.DOCUMENT, (".md",), ".md", None, "text", ()),
    "HTML": (Category.DOCUMENT, (".html", ".htm"), ".html", None, "text", ()),
    "DOCX": (
        Category.DOCUMENT,
        (".docx",),
        ".docx",
        ProbeSignature("ooxml_wordprocessing"),
        "docx",
        ("docx_encrypted",),
    ),
    "MP3": (Category.AUDIO, (".mp3",), ".mp3", ProbeSignature("mpeg_audio"), "media", ()),
    "WAV": (
        Category.AUDIO,
        (".wav",),
        ".wav",
        _magic((0, "52494646"), (8, "57415645")),  # "RIFF" en 0 y "WAVE" en 8
        "media",
        (),
    ),
    "OGG": (Category.AUDIO, (".ogg",), ".ogg", _magic((0, "4F676753")), "media", ()),
    "MP4": (Category.VIDEO, (".mp4",), ".mp4", ProbeSignature("isobmff_mp4"), "media", ()),
    "WEBM": (Category.VIDEO, (".webm",), ".webm", ProbeSignature("ebml_webm"), "media", ()),
    "SRT": (Category.DOCUMENT, (".srt",), ".srt", None, "text", ()),
}

FORMATS_TOML = """
[[format]]
name = "PNG"
category = "imagen"
extensions = [".png"]
primary_extension = ".png"
mime_type = "image/png"
signature = { magic = [{ offset = 0, hex = "89504E470D0A1A0A" }] }

[[format]]
name = "JPG"
category = "imagen"
extensions = [".jpg", ".jpeg"]
primary_extension = ".jpg"
mime_type = "image/jpeg"
signature = { magic = [{ offset = 0, hex = "FFD8FF" }] }

[[format]]
name = "PDF"
category = "documento"
extensions = [".pdf"]
primary_extension = ".pdf"
mime_type = "application/pdf"
signature = { probe = "pdf_header" }
"""

PNG_JPG_DESCRIPTION = "Compresión con pérdida; la transparencia se reemplaza por fondo blanco."


def _pair_toml(source: str, target: str, description: str = "Nota del par.") -> str:
    return f"""
[[pair]]
source = "{source}"
target = "{target}"
loss_class = "Con_Pérdida"
loss_description = "{description}"
converter = "image.raster"
"""


PNG_JPG_TOML = _pair_toml("PNG", "JPG", PNG_JPG_DESCRIPTION)


class _Catalog:
    """Catálogo escrito en un directorio temporal y su Log_de_Eventos."""

    def __init__(
        self,
        root: Path,
        *,
        formats: str = FORMATS_TOML,
        pairs: str = PNG_JPG_TOML,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.formats_path = _write(root / "formats.toml", formats)
        self.pairs_path = _write(root / "pairs.toml", pairs)
        self.extra_dir = root / "pairs.d"
        for name, text in (extra or {}).items():
            _write(self.extra_dir / name, text)
        self.stream = io.StringIO()

    def load(self) -> ConversionRegistry:
        return load_catalog(
            CONVERTERS,
            event_log=EventLog(self.stream, clock=lambda: FIXED_TS),
            formats_path=self.formats_path,
            pairs_path=self.pairs_path,
            extra_pairs_dir=self.extra_dir,
        )

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines()]


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _packaged_formats(tmp_path: Path) -> ConversionRegistry:
    return load_catalog(
        (),
        event_log=EventLog(io.StringIO()),
        formats_path=FORMATS_PATH,
        pairs_path=_write(tmp_path / "pairs.toml", ""),
        extra_pairs_dir=tmp_path / "pairs.d",  # no existe: sin catálogos adicionales
    )


# --------------------------------------------------------------------------------------------
# Catálogo empaquetado


@pytest.mark.req("2.6", "4.4", "4.9")
def test_packaged_formats_toml_declares_the_14_formats(tmp_path: Path) -> None:
    formats = _packaged_formats(tmp_path).formats()

    assert [fmt.name for fmt in formats] == list(EXPECTED_FORMATS)
    for fmt in formats:
        category, extensions, primary, signature, inspector, variants = EXPECTED_FORMATS[fmt.name]
        assert fmt.category is category, fmt.name
        assert fmt.extensions == extensions, fmt.name
        assert fmt.primary_extension == primary, fmt.name
        assert fmt.signature == signature, fmt.name
        assert fmt.inspector == inspector, fmt.name
        assert fmt.variant_probes == variants, fmt.name
        assert fmt.mime_type, fmt.name


@pytest.mark.req("12.1")
def test_packaged_srt_is_a_text_document_format_with_srt_extension(tmp_path: Path) -> None:
    registry = _packaged_formats(tmp_path)
    srt = next(fmt for fmt in registry.formats() if fmt.name == "SRT")

    assert srt.category is Category.DOCUMENT
    assert srt.signature is None
    assert srt.extensions == (".srt",)
    assert srt.primary_extension == ".srt"
    assert registry.text_format_for_extension(".SRT") == srt


@pytest.mark.req("2.6", "4.4")
@pytest.mark.parametrize(
    ("extension", "expected"),
    [(".txt", "TXT"), (".md", "MD"), (".html", "HTML"), (".htm", "HTML"), (".srt", "SRT")],
)
def test_packaged_text_formats_are_found_by_extension(
    tmp_path: Path, extension: str, expected: str
) -> None:
    fmt = _packaged_formats(tmp_path).text_format_for_extension(extension)

    assert fmt is not None
    assert fmt.name == expected


@pytest.mark.req("4.9")
def test_packaged_pairs_toml_only_declares_pair_tables() -> None:
    with PAIRS_PATH.open("rb") as stream:
        document = tomllib.load(stream)

    assert document.keys() <= {"pair"}
    assert all(isinstance(entry, dict) for entry in document.get("pair", []))


# --------------------------------------------------------------------------------------------
# Catálogo inicial


@pytest.mark.req("4.3", "4.4")
def test_initial_catalog_registers_formats_and_pairs(tmp_path: Path) -> None:
    catalog = _Catalog(tmp_path)
    registry = catalog.load()

    assert [fmt.name for fmt in registry.formats()] == ["PNG", "JPG", "PDF"]
    (pair,) = registry.targets_for("PNG")
    assert (pair.source, pair.target, pair.loss_class) == ("PNG", "JPG", LossClass.LOSSY)
    assert pair.loss_description == PNG_JPG_DESCRIPTION
    assert pair.converter_id == "image.raster"
    assert catalog.events() == []


@pytest.mark.req("4.5", "4.6", "4.9")
@pytest.mark.parametrize(
    ("file", "formats", "pairs", "code"),
    [
        pytest.param(
            "formats.toml",
            "[[format]]\nname = 'PNG'\n",
            "",
            RegistryErrorCode.INVALID_FORMAT,
            id="formato-invalido",
        ),
        pytest.param(
            "pairs.toml",
            FORMATS_TOML,
            _pair_toml("PNG", "GIF"),
            RegistryErrorCode.MISSING_FORMAT,
            id="formato-faltante",
        ),
        pytest.param(
            "pairs.toml",
            FORMATS_TOML,
            PNG_JPG_TOML + PNG_JPG_TOML,
            RegistryErrorCode.DUPLICATE_PAIR,
            id="par-duplicado",
        ),
    ],
)
def test_rejected_initial_entry_raises_catalog_error(
    tmp_path: Path, file: str, formats: str, pairs: str, code: RegistryErrorCode
) -> None:
    catalog = _Catalog(tmp_path, formats=formats, pairs=pairs)

    with pytest.raises(CatalogError) as caught:
        catalog.load()

    assert caught.value.path == tmp_path / file
    cause = caught.value.__cause__
    assert isinstance(cause, RegistrationError)
    assert cause.code is code
    # El registro anotó el rechazo antes de que el cargador abortara
    assert [(event["event"], event["error"]) for event in catalog.events()] == [
        ("registry_rejected", code.value)
    ]


@pytest.mark.req("4.9")
@pytest.mark.parametrize(
    ("formats", "pairs", "file"),
    [
        pytest.param("[[format]\n", "", "formats.toml", id="toml-invalido"),
        pytest.param(FORMATS_TOML, "[[pairs]]\nsource = 'PNG'\n", "pairs.toml", id="clave-mal"),
        pytest.param(FORMATS_TOML, "pair = 'PNG→JPG'\n", "pairs.toml", id="no-es-tabla"),
    ],
)
def test_malformed_initial_file_raises_catalog_error(
    tmp_path: Path, formats: str, pairs: str, file: str
) -> None:
    catalog = _Catalog(tmp_path, formats=formats, pairs=pairs)

    with pytest.raises(CatalogError) as caught:
        catalog.load()

    assert caught.value.path == tmp_path / file
    assert caught.value.reason


@pytest.mark.req("4.9")
def test_missing_initial_file_raises_catalog_error(tmp_path: Path) -> None:
    catalog = _Catalog(tmp_path)
    catalog.pairs_path.unlink()

    with pytest.raises(CatalogError) as caught:
        catalog.load()

    assert caught.value.path == catalog.pairs_path


# --------------------------------------------------------------------------------------------
# Catálogos adicionales


@pytest.mark.req("4.3", "4.5", "4.6")
def test_invalid_additional_entry_is_rejected_and_the_rest_loads(tmp_path: Path) -> None:
    catalog = _Catalog(
        tmp_path,
        extra={
            # Se cargan en orden de nombre de archivo
            "b.toml": _pair_toml("PDF", "PNG"),
            "a.toml": _pair_toml("PNG", "JPG", "Otra nota.")  # par_duplicado
            + _pair_toml("JPG", "GIF")  # formato_faltante
            + _pair_toml("JPG", "PNG"),
        },
    )
    registry = catalog.load()

    assert [(p.source, p.target) for p in registry.conversion_map()] == [
        ("PNG", "JPG"),
        ("JPG", "PNG"),
        ("PDF", "PNG"),
    ]
    # El par existente conserva los atributos de pairs.toml
    assert registry.targets_for("PNG")[0].loss_description == PNG_JPG_DESCRIPTION
    assert [(e["event"], e["entry"], e["error"]) for e in catalog.events()] == [
        ("registry_rejected", "PNG→JPG", "par_duplicado"),
        ("registry_rejected", "JPG→GIF", "formato_faltante"),
    ]


@pytest.mark.req("4.3")
def test_malformed_additional_file_is_rejected_whole_and_the_rest_loads(tmp_path: Path) -> None:
    catalog = _Catalog(
        tmp_path,
        extra={
            "a.toml": "[[pair]\n",  # TOML inválido
            "b.toml": "[[format]]\nname = 'GIF'\n",  # solo admite [[pair]]
            "c.toml": _pair_toml("JPG", "PDF"),
            "notas.txt": "[[pair]\n",  # no es *.toml: no se lee
        },
    )
    registry = catalog.load()

    assert [(p.source, p.target) for p in registry.conversion_map()] == [
        ("PNG", "JPG"),
        ("JPG", "PDF"),
    ]
    events = catalog.events()
    assert [(e["event"], Path(e["file"]).name) for e in events] == [
        (CATALOG_FILE_REJECTED, "a.toml"),
        (CATALOG_FILE_REJECTED, "b.toml"),
    ]
    assert all(e["level"] == "warning" and e["reason"] for e in events)
