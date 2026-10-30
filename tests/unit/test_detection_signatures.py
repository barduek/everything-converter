"""Pruebas de src/converter/detection/signatures.py: coincidencia de firmas, elección por
especificidad y sondas de variante (Req 2.1, 2.8, 2.10, 4.4, 7.2).

`INITIAL_FORMATS` reproduce las firmas de la tabla "Firmas" de design.md en el orden de
registro del catálogo, sin depender de `catalog/formats.toml`.
"""

from __future__ import annotations

import io

import pytest

from converter.detection.probes import ProbeInput, UnknownProbeError, register_probe
from converter.detection.probes import base as probes_base
from converter.detection.signatures import (
    VariantMatch,
    fixed_byte_count,
    magic_matches,
    match_signature,
    match_variant,
    specificity,
)
from converter.models import Category
from converter.registry.model import FormatDef, MagicSignature, ProbeSignature, Signature
from converter.registry.registry import SIGNATURE_WINDOW
from tests import format_samples as samples


def _format(
    name: str,
    signature: Signature | None,
    *,
    category: Category = Category.DOCUMENT,
    variant_probes: tuple[str, ...] = (),
) -> FormatDef:
    extension = f".{name.lower()}"
    return FormatDef(
        name=name,
        category=category,
        extensions=(extension,),
        primary_extension=extension,
        mime_type="application/octet-stream",
        signature=signature,
        inspector=None,
        variant_probes=variant_probes,
    )


def _magic(*parts: tuple[int, bytes]) -> MagicSignature:
    return MagicSignature(parts)


INITIAL_FORMATS = (
    _format("PNG", _magic((0, bytes.fromhex("89504E470D0A1A0A"))), category=Category.IMAGE),
    _format("JPG", _magic((0, b"\xff\xd8\xff"))),
    _format("WEBP", _magic((0, b"RIFF"), (8, b"WEBP"))),
    _format("PDF", ProbeSignature("pdf_header")),
    _format("TXT", None),
    _format("MD", None),
    _format("HTML", None),
    _format("DOCX", ProbeSignature("ooxml_wordprocessing"), variant_probes=("docx_encrypted",)),
    _format("MP3", ProbeSignature("mpeg_audio")),
    _format("WAV", _magic((0, b"RIFF"), (8, b"WAVE"))),
    _format("OGG", _magic((0, b"OggS"))),
    _format("MP4", ProbeSignature("isobmff_mp4")),
    _format("WEBM", ProbeSignature("ebml_webm")),
    _format("SRT", None),
)
BY_NAME = {fmt.name: fmt for fmt in INITIAL_FORMATS}

SAMPLES = {
    "PNG": samples.PNG,
    "JPG": samples.JPG,
    "WEBP": samples.WEBP,
    "PDF": samples.PDF,
    "DOCX": samples.docx(),
    "MP3": samples.mp3(),
    "WAV": samples.WAV,
    "OGG": samples.OGG,
    "MP4": samples.mp4(),
    "WEBM": samples.webm(),
}


def _input(data: bytes, extension: str | None = None) -> ProbeInput:
    return ProbeInput.from_stream(io.BytesIO(data), extension)


def _detected(data: bytes, extension: str | None = None) -> str | None:
    fmt = match_signature(INITIAL_FORMATS, _input(data, extension))
    return None if fmt is None else fmt.name


@pytest.fixture
def isolated_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Las sondas que registra la prueba no quedan en el registro global."""
    monkeypatch.setattr(probes_base, "_REGISTRY", dict(probes_base._REGISTRY))


# --- Firmas del Mapa_de_Conversión_Inicial ---


@pytest.mark.req("2.1")
@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_each_initial_signature_detects_its_format(name: str) -> None:
    assert _detected(SAMPLES[name]) == name


@pytest.mark.req("2.8")
@pytest.mark.parametrize("name", sorted(SAMPLES))
@pytest.mark.parametrize("extension", [None, ".txt", ".srt", ".bin", ".DOCX", ".mp3"])
def test_signature_detection_does_not_depend_on_the_extension(
    name: str, extension: str | None
) -> None:
    assert _detected(SAMPLES[name], extension) == name


@pytest.mark.req("2.1")
@pytest.mark.parametrize(
    "data",
    [
        b"",
        "Canción de cuna\n".encode(),
        ("ÿûPara el lunes: café, té y ñoquis.\r\n" * 30).encode("cp1252"),
        b"RIFF\x00\x00\x00\x00AVI LIST",
        samples.mp4(b"qt  ", b"qt  "),
        samples.mp4(b"M4A ", b"M4A ", b"mp42", b"isom"),
        samples.mp4(b"heic", b"mif1", b"heic"),
        samples.webm(b"matroska"),
        samples.encrypted_docx(),  # sin firma: lo reconoce la sonda de variante
    ],
)
def test_content_without_a_registered_signature(data: bytes) -> None:
    assert _detected(data, ".docx") is None


# --- MagicSignature ---


@pytest.mark.req("2.1")
def test_magic_matches_every_part() -> None:
    riff_wave = _magic((0, b"RIFF"), (8, b"WAVE"))
    assert magic_matches(riff_wave, samples.WAV)
    assert not magic_matches(riff_wave, samples.WEBP)
    assert not magic_matches(riff_wave, b"RIFF")  # contenido más corto que la firma
    assert not magic_matches(_magic((0, b"OggS")), b"Ogg")


def test_magic_part_must_end_within_the_signature_window() -> None:
    data = bytes(SIGNATURE_WINDOW - 2) + b"ABCD"  # 2 bytes más que la ventana
    assert magic_matches(_magic((SIGNATURE_WINDOW - 4, b"\x00\x00AB")), data)
    assert not magic_matches(_magic((SIGNATURE_WINDOW - 2, b"ABCD")), data)
    assert len(_input(data).head) == SIGNATURE_WINDOW


def test_fixed_byte_count_counts_distinct_positions() -> None:
    assert fixed_byte_count(_magic((0, b"RIFF"), (8, b"WAVE"))) == 8
    assert fixed_byte_count(_magic((8, b"WAVE"), (0, b"RIFF"))) == 8
    assert fixed_byte_count(_magic((0, b"ABCD"), (2, b"CDEF"))) == 6  # se solapan 2 bytes
    assert fixed_byte_count(_magic((0, b"ABCD"), (1, b"BC"))) == 4  # una dentro de la otra


# --- Especificidad ---


@pytest.mark.req("2.1")
def test_specificity_puts_probes_above_byte_signatures() -> None:
    longest = _magic((0, bytes(SIGNATURE_WINDOW)))
    assert specificity(ProbeSignature("pdf_header")) > specificity(longest)
    assert specificity(_magic((0, b"RIFF"), (8, b"WAVE"))) > specificity(_magic((0, b"RIFF")))


@pytest.mark.req("2.1")
def test_more_specific_magic_wins_in_any_registration_order() -> None:
    riff = _format("RIFF", _magic((0, b"RIFF")))
    wav = _format("WAV", _magic((0, b"RIFF"), (8, b"WAVE")))
    assert match_signature([riff, wav], _input(samples.WAV)) == wav
    assert match_signature([wav, riff], _input(samples.WAV)) == wav
    assert match_signature([riff, wav], _input(samples.WEBP)) == riff


@pytest.mark.req("2.1")
def test_equal_specificity_uses_registration_order(isolated_probes: None) -> None:
    first = _format("A", _magic((0, b"Og")))
    second = _format("B", _magic((2, b"gS")))
    assert match_signature([first, second], _input(samples.OGG)) == first
    assert match_signature([second, first], _input(samples.OGG)) == second

    register_probe("always_one")(lambda source: True)
    register_probe("always_two")(lambda source: True)
    one = _format("ONE", ProbeSignature("always_one"))
    two = _format("TWO", ProbeSignature("always_two"))
    assert match_signature([one, two], _input(b"")) == one
    assert match_signature([two, one], _input(b"")) == two


@pytest.mark.req("2.1", "4.4")
def test_structural_probe_wins_over_byte_signature(isolated_probes: None) -> None:
    calls: list[bytes] = []

    @register_probe("ogg_with_marker")
    def ogg_with_marker(source: ProbeInput) -> bool:
        calls.append(source.head)
        return b"MARK" in source.head

    ogg = _format("OGG", _magic((0, b"OggS")))
    marked = _format("MARKED", ProbeSignature("ogg_with_marker"))
    assert match_signature([ogg, marked], _input(samples.OGG + b"MARK")) == marked
    assert match_signature([ogg, marked], _input(samples.OGG)) == ogg
    assert len(calls) == 2


@pytest.mark.req("4.4")
def test_new_format_with_new_probe_is_detected_without_changing_the_detector(
    isolated_probes: None,
) -> None:
    @register_probe("flac_stream")
    def flac_stream(source: ProbeInput) -> bool:
        return source.head.startswith(b"fLaC")

    flac = _format("FLAC", ProbeSignature("flac_stream"), category=Category.AUDIO)
    formats = (*INITIAL_FORMATS, flac)
    assert match_signature(formats, _input(b"fLaC\x00\x00\x00\x22")) == flac
    assert match_signature(formats, _input(samples.OGG)) == BY_NAME["OGG"]


def test_unknown_probe_in_a_format_raises() -> None:
    broken = _format("X", ProbeSignature("no_registrada"))
    with pytest.raises(UnknownProbeError):
        match_signature([broken], _input(samples.PDF))


def test_formats_without_signature_are_ignored() -> None:
    text_only = [fmt for fmt in INITIAL_FORMATS if fmt.signature is None]
    assert match_signature(text_only, _input(samples.PNG)) is None


# --- Sondas de variante ---


@pytest.mark.req("2.10", "7.2")
def test_encrypted_docx_matches_the_docx_variant_with_docx_extension() -> None:
    match = match_variant(INITIAL_FORMATS, _input(samples.encrypted_docx(), ".docx"))
    assert match == VariantMatch(format=BY_NAME["DOCX"], probe="docx_encrypted")


@pytest.mark.req("2.10", "7.2")
@pytest.mark.parametrize("extension", [None, ".doc", ".txt"])
def test_encrypted_docx_without_docx_extension_matches_no_variant(
    extension: str | None,
) -> None:
    assert match_variant(INITIAL_FORMATS, _input(samples.encrypted_docx(), extension)) is None


@pytest.mark.req("2.10")
def test_plain_docx_matches_no_variant() -> None:
    assert match_variant(INITIAL_FORMATS, _input(samples.docx(), ".docx")) is None
