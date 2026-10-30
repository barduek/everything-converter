"""Pruebas de src/converter/registry/model.py y src/converter/registry/registry.py: atributos de
Formatos y Pares_de_Conversión, consultas y cada regla de "Validación del registro"
(Req 4.1, 4.2, 4.5, 4.6, 4.7, 4.8, 4.10, 4.12, 4.13).

Cada rechazo se comprueba en tres lugares: el código de `RegistrationError`, el evento
`registry_rejected` del Log_de_Eventos y el estado del registro, que debe quedar igual.
"""

from __future__ import annotations

import dataclasses
import io
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from converter.eventlog import EventLog
from converter.models import Category, LossClass
from converter.naming import result_name
from converter.registry.model import ConversionPair, FormatDef, MagicSignature, ProbeSignature
from converter.registry.registry import (
    MAX_LOSS_DESCRIPTION,
    ConversionRegistry,
    RegistrationError,
    RegistryErrorCode,
)

FIXED_TS = datetime(2025, 1, 15, 10, 32, 7, tzinfo=UTC)
CONVERTERS = ("image.raster", "image.pdf", "text.docs", "audio.ffmpeg", "transcription.whisper")

PNG: dict[str, object] = {
    "name": "PNG",
    "category": "imagen",
    "extensions": [".png"],
    "primary_extension": ".png",
    "mime_type": "image/png",
    "signature": {"magic": [{"offset": 0, "hex": "89504E470D0A1A0A"}]},
    "inspector": "image",
}
JPG: dict[str, object] = {
    "name": "JPG",
    "category": "imagen",
    "extensions": [".jpg", ".jpeg"],
    "primary_extension": ".jpg",
    "mime_type": "image/jpeg",
    "signature": {"magic": [{"offset": 0, "hex": "FFD8FF"}]},
    "inspector": "image",
}
DOCX: dict[str, object] = {
    "name": "DOCX",
    "category": "documento",
    "extensions": [".docx"],
    "primary_extension": ".docx",
    "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "signature": {"probe": "ooxml_wordprocessing"},
    "inspector": "docx",
    "variant_probes": ["docx_encrypted"],
}
TXT: dict[str, object] = {
    "name": "TXT",
    "category": "documento",
    "extensions": [".txt"],
    "primary_extension": ".txt",
    "mime_type": "text/plain; charset=utf-8",
    "inspector": "text",
}
HTML: dict[str, object] = {
    "name": "HTML",
    "category": "documento",
    "extensions": [".html", ".htm"],
    "primary_extension": ".html",
    "mime_type": "text/html; charset=utf-8",
    "inspector": "text",
}
MP3: dict[str, object] = {
    "name": "MP3",
    "category": "audio",
    "extensions": [".mp3"],
    "primary_extension": ".mp3",
    "mime_type": "audio/mpeg",
    "signature": {"probe": "mpeg_audio"},
    "inspector": "media",
}
FORMATS = (PNG, JPG, DOCX, TXT, HTML, MP3)

PNG_JPG: dict[str, object] = {
    "source": "PNG",
    "target": "JPG",
    "loss_class": "Con_Pérdida",
    "loss_description": "Compresión con pérdida; la transparencia se reemplaza por fondo blanco.",
    "converter": "image.raster",
}


def _pair(
    source: str, target: str, loss_class: str = "Sin_Pérdida", **extra: object
) -> dict[str, object]:
    spec: dict[str, object] = {
        "source": source,
        "target": target,
        "loss_class": loss_class,
        "loss_description": "Nota del par.",
        "converter": "image.raster",
    }
    spec.update(extra)
    return spec


def _registry() -> tuple[ConversionRegistry, io.StringIO]:
    stream = io.StringIO()
    log = EventLog(stream, clock=lambda: FIXED_TS)
    return ConversionRegistry(CONVERTERS, event_log=log), stream


def _loaded() -> tuple[ConversionRegistry, io.StringIO]:
    registry, stream = _registry()
    for spec in FORMATS:
        registry.register_format(spec)
    registry.register_pair(PNG_JPG)
    return registry, stream


def _events(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def _with(spec: Mapping[str, object], **changes: object) -> dict[str, object]:
    """Copia de `spec` con `changes`; un valor `...` quita la clave."""
    result = dict(spec)
    for key, value in changes.items():
        if value is ...:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def _assert_rejected(
    registry: ConversionRegistry,
    stream: io.StringIO,
    spec: Mapping[str, object],
    code: RegistryErrorCode,
    *,
    kind: str,
) -> RegistrationError:
    """Comprueba el código, el evento del log y que el registro no cambió."""
    formats, pairs = registry.formats(), registry.conversion_map()
    logged = len(_events(stream))
    register: Callable[[Mapping[str, object]], object] = (
        registry.register_format if kind == "formato" else registry.register_pair
    )
    with pytest.raises(RegistrationError) as info:
        register(spec)
    error = info.value
    assert error.code is code
    assert registry.formats() == formats
    assert registry.conversion_map() == pairs
    events = _events(stream)
    assert len(events) == logged + 1
    event = events[-1]
    assert event["event"] == "registry_rejected"
    assert (event["entry_kind"], event["error"]) == (kind, code.value)
    assert event["entry"] == error.entry
    assert event["details"] == json.loads(json.dumps(dict(error.details)))
    return error


# --------------------------------------------------------------------------------------------
# Modelo


@pytest.mark.req("4.1")
@pytest.mark.parametrize(
    ("category", "loss_class", "transcription", "long_running", "confirmation"),
    [
        (Category.IMAGE, LossClass.LOSSLESS, False, False, False),
        (Category.IMAGE, LossClass.LOSSY, False, False, True),
        (Category.DOCUMENT, LossClass.LIMITED, False, False, True),
        (Category.AUDIO, LossClass.LOSSLESS, False, True, False),
        (Category.VIDEO, LossClass.LOSSY, False, True, True),
        (Category.AUDIO, LossClass.APPROXIMATE, True, True, True),
        # Una transcripción es de larga duración aunque su Categoría no sea audio ni video
        (Category.DOCUMENT, LossClass.APPROXIMATE, True, True, True),
    ],
)
def test_pair_derived_properties(
    category: Category,
    loss_class: LossClass,
    transcription: bool,
    long_running: bool,
    confirmation: bool,
) -> None:
    pair = ConversionPair("A", "B", category, loss_class, "nota", "x.y")
    assert pair.is_transcription is transcription
    assert pair.is_long_running is long_running
    assert pair.requires_confirmation is confirmation


def test_models_are_immutable() -> None:
    pair = ConversionPair("A", "B", Category.IMAGE, LossClass.LOSSY, "nota", "x.y")
    with pytest.raises(dataclasses.FrozenInstanceError):
        pair.loss_class = LossClass.LOSSLESS  # type: ignore[misc]
    signature = MagicSignature(((0, b"\x00"),))
    with pytest.raises(dataclasses.FrozenInstanceError):
        signature.parts = ()  # type: ignore[misc]


def test_signature_key_ignores_the_order_of_the_parts() -> None:
    riff_wave = MagicSignature(((0, b"RIFF"), (8, b"WAVE")))
    assert riff_wave.key() == MagicSignature(((8, b"WAVE"), (0, b"RIFF"))).key()
    assert riff_wave.key() != MagicSignature(((0, b"RIFF"), (8, b"WEBP"))).key()
    assert ProbeSignature("mpeg_audio").key() == ("probe", "mpeg_audio")


# --------------------------------------------------------------------------------------------
# Formatos aceptados


@pytest.mark.req("4.13")
def test_register_format_stores_every_attribute() -> None:
    registry, stream = _loaded()
    png, jpg, docx, txt, html, mp3 = registry.formats()
    assert png == FormatDef(
        name="PNG",
        category=Category.IMAGE,
        extensions=(".png",),
        primary_extension=".png",
        mime_type="image/png",
        signature=MagicSignature(((0, bytes.fromhex("89504E470D0A1A0A")),)),
        inspector="image",
    )
    assert jpg.extensions == (".jpg", ".jpeg")
    assert docx.signature == ProbeSignature("ooxml_wordprocessing")
    assert docx.variant_probes == ("docx_encrypted",)
    assert txt.signature is None
    assert html.category is Category.DOCUMENT
    assert mp3.inspector == "media"
    assert [f.name for f in registry.formats()] == ["PNG", "JPG", "DOCX", "TXT", "HTML", "MP3"]
    assert _events(stream) == []


@pytest.mark.req("4.13")
def test_extensions_are_stored_in_lowercase() -> None:
    registry, _ = _registry()
    fmt = registry.register_format(
        _with(JPG, extensions=[".JPG", ".Jpeg"], primary_extension=".JPG")
    )
    assert fmt.extensions == (".jpg", ".jpeg")
    assert fmt.primary_extension == ".jpg"


def test_format_def_names_the_result_file() -> None:
    registry, _ = _loaded()
    jpg = registry.formats()[1]
    assert result_name("Vacaciones 2024.PNG", jpg, "single") == "Vacaciones 2024.jpg"


@pytest.mark.req("4.10")
def test_registered_format_does_not_follow_later_changes_to_its_spec() -> None:
    registry, _ = _registry()
    extensions = [".jpg", ".jpeg"]
    parts = [{"offset": 0, "hex": "FFD8FF"}]
    registry.register_format(_with(JPG, extensions=extensions, signature={"magic": parts}))
    extensions.append(".jfif")
    parts[0]["hex"] = "000000"
    (jpg,) = registry.formats()
    assert jpg.extensions == (".jpg", ".jpeg")
    assert jpg.signature == MagicSignature(((0, b"\xff\xd8\xff"),))


def test_text_format_for_extension() -> None:
    registry, _ = _loaded()
    assert registry.text_format_for_extension(".txt") == registry.formats()[3]
    assert registry.text_format_for_extension(".HTM") == registry.formats()[4]
    # PNG tiene firma: no es un Formato_de_Texto aunque la extensión le pertenezca
    assert registry.text_format_for_extension(".png") is None
    assert registry.text_format_for_extension(".srt") is None
    assert registry.text_format_for_extension(None) is None


# --------------------------------------------------------------------------------------------
# Formatos rechazados


@pytest.mark.req("4.13")
@pytest.mark.parametrize(
    ("changes", "attribute"),
    [
        ({"name": ""}, "name"),
        ({"name": ...}, "name"),
        ({"name": 7}, "name"),
        ({"name": "Mi formato"}, "name"),
        ({"name": "-PNG"}, "name"),
        ({"name": "P" * 33}, "name"),
        ({"category": "imagenes"}, "category"),
        ({"category": ...}, "category"),
        ({"extensions": []}, "extensions"),
        ({"extensions": ...}, "extensions"),
        ({"extensions": ".png"}, "extensions"),
        ({"extensions": ["png"]}, "extensions"),
        ({"extensions": ["."]}, "extensions"),
        ({"extensions": [".tar.gz"]}, "extensions"),
        ({"extensions": [".p/g"]}, "extensions"),
        ({"extensions": [".png", ".PNG"]}, "extensions"),
        ({"primary_extension": ...}, "primary_extension"),
        ({"primary_extension": ".gif"}, "primary_extension"),
        ({"primary_extension": "png"}, "primary_extension"),
        ({"mime_type": ...}, "mime_type"),
        ({"mime_type": ""}, "mime_type"),
        ({"mime_type": "image/png\r\nX-Inyectada: 1"}, "mime_type"),
        ({"signature": "89504E47"}, "signature"),
        ({"signature": {}}, "signature"),
        ({"signature": {"magic": [], "probe": "x"}}, "signature"),
        ({"signature": {"magic": []}}, "signature"),
        ({"signature": {"magic": [{"offset": -1, "hex": "89"}]}}, "signature"),
        ({"signature": {"magic": [{"offset": True, "hex": "89"}]}}, "signature"),
        ({"signature": {"magic": [{"offset": 0, "hex": "zz"}]}}, "signature"),
        ({"signature": {"magic": [{"offset": 0, "hex": ""}]}}, "signature"),
        ({"signature": {"magic": [{"offset": 65535, "hex": "8950"}]}}, "signature"),
        ({"signature": {"probe": ""}}, "signature"),
        ({"inspector": ""}, "inspector"),
        ({"variant_probes": "docx_encrypted"}, "variant_probes"),
        ({"variant_probes": ["a", "a"]}, "variant_probes"),
    ],
)
def test_invalid_format_names_the_attribute(changes: dict[str, object], attribute: str) -> None:
    registry, stream = _loaded()
    apng = _with(
        PNG,
        name="APNG",
        extensions=[".apng"],
        primary_extension=".apng",
        signature={"probe": "apng"},
    )
    spec = _with(apng, **changes)
    error = _assert_rejected(
        registry, stream, spec, RegistryErrorCode.INVALID_FORMAT, kind="formato"
    )
    assert error.details["attribute"] == attribute
    assert error.details["reason"]


@pytest.mark.req("4.13")
def test_signature_part_may_end_exactly_at_64_kib() -> None:
    registry, _ = _registry()
    spec = _with(PNG, signature={"magic": [{"offset": 65534, "hex": "8950"}]})
    fmt = registry.register_format(spec)
    assert fmt.signature == MagicSignature(((65534, b"\x89\x50"),))


@pytest.mark.req("4.13")
@pytest.mark.parametrize(
    ("changes", "attribute", "value", "existing"),
    [
        ({"name": "png"}, "name", "png", "PNG"),
        (
            {"extensions": [".gif", ".PNG"], "primary_extension": ".gif"},
            "extensions",
            ".png",
            "PNG",
        ),
        ({"extensions": [".JPEG"], "primary_extension": ".jpeg"}, "extensions", ".jpeg", "JPG"),
    ],
)
def test_conflicting_name_or_extension_names_the_existing_format(
    changes: dict[str, object], attribute: str, value: str, existing: str
) -> None:
    registry, stream = _loaded()
    spec = _with(_with(PNG, name="GIF", signature={"probe": "gif"}), **changes)
    error = _assert_rejected(
        registry, stream, spec, RegistryErrorCode.FORMAT_CONFLICT, kind="formato"
    )
    assert error.details == {"attribute": attribute, "value": value, "existing": existing}


@pytest.mark.req("4.13")
@pytest.mark.parametrize(
    ("signature", "existing"),
    [
        ({"magic": [{"offset": 0, "hex": "89504e470d0a1a0a"}]}, "PNG"),
        ({"probe": "mpeg_audio"}, "MP3"),
    ],
)
def test_duplicate_signature_names_the_existing_format(
    signature: dict[str, object], existing: str
) -> None:
    registry, stream = _loaded()
    spec = _with(
        PNG, name="GIF", extensions=[".gif"], primary_extension=".gif", signature=signature
    )
    error = _assert_rejected(
        registry, stream, spec, RegistryErrorCode.DUPLICATE_SIGNATURE, kind="formato"
    )
    assert error.details == {"existing": existing}
    assert error.entry == "GIF"


@pytest.mark.req("4.13")
def test_duplicate_signature_with_parts_in_another_order() -> None:
    registry, stream = _registry()
    wav = {"magic": [{"offset": 0, "hex": "52494646"}, {"offset": 8, "hex": "57415645"}]}
    registry.register_format(
        _with(PNG, name="WAV", extensions=[".wav"], primary_extension=".wav", signature=wav)
    )
    reordered = {"magic": [{"offset": 8, "hex": "57415645"}, {"offset": 0, "hex": "52494646"}]}
    spec = _with(PNG, signature=reordered)
    _assert_rejected(registry, stream, spec, RegistryErrorCode.DUPLICATE_SIGNATURE, kind="formato")


def test_several_text_formats_can_coexist_without_signature() -> None:
    registry, _ = _loaded()
    md = _with(TXT, name="MD", extensions=[".md"], primary_extension=".md")
    assert registry.register_format(md).signature is None


@pytest.mark.req("4.13")
def test_invalid_attribute_is_reported_before_a_conflict() -> None:
    registry, stream = _loaded()
    spec = _with(PNG, mime_type="")  # nombre, extensión y firma también en conflicto
    error = _assert_rejected(
        registry, stream, spec, RegistryErrorCode.INVALID_FORMAT, kind="formato"
    )
    assert error.details["attribute"] == "mime_type"


# --------------------------------------------------------------------------------------------
# Pares aceptados


@pytest.mark.req("4.1", "4.8", "4.10")
def test_conversion_map_returns_each_pair_once_with_its_attributes() -> None:
    registry, stream = _loaded()
    jpg_png = registry.register_pair(_pair("JPG", "PNG"))
    txt_html = registry.register_pair(_pair("TXT", "HTML", "Limitada", converter="text.docs"))
    mp3_txt = registry.register_pair(
        _pair("MP3", "TXT", "Aproximada", converter="transcription.whisper")
    )
    png_jpg = ConversionPair(
        source="PNG",
        target="JPG",
        category=Category.IMAGE,
        loss_class=LossClass.LOSSY,
        loss_description="Compresión con pérdida; la transparencia se reemplaza por fondo blanco.",
        converter_id="image.raster",
    )
    expected = (png_jpg, jpg_png, txt_html, mp3_txt)
    assert registry.conversion_map() == expected
    assert registry.conversion_map() == expected  # cada consulta da lo mismo (Req 4.10)
    assert txt_html.category is Category.DOCUMENT  # Categoría del Formato_de_Origen
    assert mp3_txt.category is Category.AUDIO
    assert mp3_txt.is_transcription and mp3_txt.is_long_running
    assert _events(stream) == []


@pytest.mark.req("4.2")
@pytest.mark.parametrize("loss_class", list(LossClass))
def test_each_loss_class_is_accepted(loss_class: LossClass) -> None:
    registry, _ = _loaded()
    pair = registry.register_pair(_pair("JPG", "PNG", loss_class.value))
    assert pair.loss_class is loss_class


def test_targets_for_and_source_formats() -> None:
    registry, _ = _loaded()
    registry.register_pair(_pair("TXT", "HTML", converter="text.docs"))
    registry.register_pair(_pair("PNG", "DOCX"))
    registry.register_pair(_pair("JPG", "PNG"))
    assert [p.target for p in registry.targets_for("PNG")] == ["JPG", "DOCX"]
    assert registry.targets_for("HTML") == ()
    assert registry.targets_for("XYZ") == ()
    # En el orden de registro de los Formatos, no de los pares
    assert registry.source_formats() == ("PNG", "JPG", "TXT")


@pytest.mark.req("4.7")
@pytest.mark.parametrize("description", ["", "   ", ...])
def test_lossless_pair_may_have_no_description(description: object) -> None:
    registry, _ = _loaded()
    pair = registry.register_pair(_with(_pair("JPG", "PNG"), loss_description=description))
    assert pair.loss_description == ("" if description is ... else description)


@pytest.mark.req("4.7")
@pytest.mark.parametrize("loss_class", list(LossClass))
def test_description_of_exactly_500_characters_is_accepted(loss_class: LossClass) -> None:
    registry, _ = _loaded()
    description = "x" * MAX_LOSS_DESCRIPTION
    spec = _pair("JPG", "PNG", loss_class.value, loss_description=description)
    assert registry.register_pair(spec).loss_description == description


# --------------------------------------------------------------------------------------------
# Pares rechazados


@pytest.mark.req("4.6")
@pytest.mark.parametrize(
    ("source", "target", "missing", "attributes", "entry"),
    [
        ("XYZ", "PNG", ("XYZ",), ("source",), "XYZ→PNG"),
        ("PNG", "ABC", ("ABC",), ("target",), "PNG→ABC"),
        ("XYZ", "ABC", ("XYZ", "ABC"), ("source", "target"), "XYZ→ABC"),
        ("XYZ", "XYZ", ("XYZ",), ("source", "target"), "XYZ→XYZ"),
        ("png", "JPG", ("png",), ("source",), "png→JPG"),  # los nombres se comparan tal cual
        (..., "JPG", (), ("source",), None),
        ("PNG", 3, (), ("target",), None),
    ],
)
def test_missing_format_names_each_missing_format(
    source: object,
    target: object,
    missing: tuple[str, ...],
    attributes: tuple[str, ...],
    entry: str | None,
) -> None:
    registry, stream = _loaded()
    spec = _with(PNG_JPG, source=source, target=target)
    error = _assert_rejected(registry, stream, spec, RegistryErrorCode.MISSING_FORMAT, kind="par")
    assert error.details == {"missing": missing, "attributes": attributes}
    assert error.entry == entry


@pytest.mark.req("4.5")
def test_duplicate_pair_keeps_the_existing_attributes() -> None:
    registry, stream = _loaded()
    (original,) = registry.conversion_map()
    spec = _with(PNG_JPG, loss_class="Sin_Pérdida", loss_description="Otra", converter="image.pdf")
    error = _assert_rejected(registry, stream, spec, RegistryErrorCode.DUPLICATE_PAIR, kind="par")
    assert error.details == {"existing": "PNG→JPG"}
    assert error.entry == "PNG→JPG"
    assert registry.conversion_map() == (original,)
    assert registry.targets_for("PNG") == (original,)


@pytest.mark.req("4.12")
@pytest.mark.parametrize(
    "loss_class",
    [
        ...,
        "",
        "sin_pérdida",
        "Sin Pérdida",
        "Perdida",
        ["Con_Pérdida", "Limitada"],
        ["Limitada"],
        1,
    ],
)
def test_invalid_loss_class(loss_class: object) -> None:
    registry, stream = _loaded()
    spec = _with(_pair("JPG", "PNG"), loss_class=loss_class)
    error = _assert_rejected(
        registry, stream, spec, RegistryErrorCode.INVALID_LOSS_CLASS, kind="par"
    )
    assert error.details == {"allowed": ("Sin_Pérdida", "Con_Pérdida", "Limitada", "Aproximada")}


@pytest.mark.req("4.7")
@pytest.mark.parametrize("loss_class", ["Con_Pérdida", "Limitada", "Aproximada"])
@pytest.mark.parametrize("description", [..., "", " \t\n ", "\u3000", " " * 501])
def test_missing_description(loss_class: str, description: object) -> None:
    registry, stream = _loaded()
    spec = _with(_pair("JPG", "PNG", loss_class), loss_description=description)
    error = _assert_rejected(
        registry, stream, spec, RegistryErrorCode.MISSING_DESCRIPTION, kind="par"
    )
    assert error.details == {"loss_class": loss_class}


@pytest.mark.req("4.7")
def test_description_that_is_not_text_is_missing() -> None:
    registry, stream = _loaded()
    spec = _pair("JPG", "PNG", loss_description=["nota"])
    _assert_rejected(registry, stream, spec, RegistryErrorCode.MISSING_DESCRIPTION, kind="par")


@pytest.mark.req("4.7")
@pytest.mark.parametrize("loss_class", list(LossClass))
def test_description_longer_than_500_characters(loss_class: LossClass) -> None:
    registry, stream = _loaded()
    spec = _pair("JPG", "PNG", loss_class.value, loss_description="x" * 501)
    error = _assert_rejected(
        registry, stream, spec, RegistryErrorCode.DESCRIPTION_TOO_LONG, kind="par"
    )
    assert error.details == {"length": 501, "limit": 500}


@pytest.mark.parametrize("converter", ["video.ffmpeg", "", ..., 5])
def test_unknown_converter(converter: object) -> None:
    registry, stream = _loaded()
    spec = _with(_pair("JPG", "PNG"), converter=converter)
    error = _assert_rejected(
        registry, stream, spec, RegistryErrorCode.UNKNOWN_CONVERTER, kind="par"
    )
    assert error.details == {"converter": converter if isinstance(converter, str) else None}


@pytest.mark.req("4.6", "4.12")
def test_missing_format_is_reported_before_the_other_rules() -> None:
    registry, stream = _loaded()
    spec = {"source": "XYZ", "target": "PNG", "loss_class": "Otra", "converter": "nada"}
    _assert_rejected(registry, stream, spec, RegistryErrorCode.MISSING_FORMAT, kind="par")


@pytest.mark.req("4.5", "4.6", "4.13")
def test_rejected_entries_do_not_block_later_registrations() -> None:
    registry, stream = _registry()
    with pytest.raises(RegistrationError):
        registry.register_format(_with(PNG, category="imagenes"))
    registry.register_format(PNG)
    with pytest.raises(RegistrationError):
        registry.register_pair(PNG_JPG)  # JPG todavía no está registrado
    registry.register_format(JPG)
    registry.register_pair(PNG_JPG)
    assert [f.name for f in registry.formats()] == ["PNG", "JPG"]
    assert [(p.source, p.target) for p in registry.conversion_map()] == [("PNG", "JPG")]
    assert [e["error"] for e in _events(stream)] == ["formato_invalido", "formato_faltante"]
