"""Pruebas de src/converter/detection/source_info.py: registro de tipos de `SourceInfo`, su forma
JSON, `TextInfo`, `SourceRejection`, `DetectionResult` y la salida de los inspectores
(Req 2.12, 16.1).

Las pruebas que registran tipos trabajan sobre copias del registro del módulo, así no dejan
tipos de prueba registrados para las demás.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

import pytest

from converter.detection import source_info
from converter.detection.source_info import (
    INSPECTION_REJECTIONS,
    INSPECTOR_REASONS,
    DetectionResult,
    InspectorOutputError,
    NoInfo,
    SourceInfo,
    SourceRejection,
    TextInfo,
    dump_inspector_output,
    load_inspector_output,
    register_source_info,
    registered_source_infos,
)
from converter.detection.text import BOM_UTF8, TextKind
from converter.eventlog import ErrorDetail, Tool, describe_exception, sanitize_tool_output
from converter.messages_es import FailureReason, InvalidMessageError, NoticeCode
from converter.models import Notice, Stage


class _Mode(StrEnum):
    COLOR = "color"
    GRAY = "gris"


@dataclass(frozen=True, slots=True)
class _Part:
    width: float
    label: str | None


@dataclass(frozen=True, slots=True)
class _SampleInfo(SourceInfo):
    """Tipo de prueba con un campo de cada forma admitida; se registra en cada prueba."""

    count: int
    flag: bool
    kind: Literal["audio", "video"]
    mode: _Mode
    parts: tuple[_Part, ...]
    note: str | None = None

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("count no puede ser negativo")


@dataclass(frozen=True, slots=True)
class _Unregistered(SourceInfo):
    value: int


class _NotDataclass(SourceInfo):
    pass


@dataclass(slots=True)
class _Mutable(SourceInfo):
    value: int


@dataclass(frozen=True, slots=True)
class _WithMapping(SourceInfo):
    values: dict[str, int]


SAMPLE = _SampleInfo(
    count=3,
    flag=True,
    kind="video",
    mode=_Mode.GRAY,
    parts=(_Part(width=612.5, label="A4"), _Part(width=595, label=None)),
)


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_info, "_BY_NAME", dict(source_info._BY_NAME))
    monkeypatch.setattr(source_info, "_NAME_OF", dict(source_info._NAME_OF))


@pytest.fixture
def sample_registered() -> None:
    register_source_info("sample")(_SampleInfo)


# --- Registro de tipos ---


def test_text_and_no_info_are_registered() -> None:
    registered = registered_source_infos()
    assert registered["text"] is TextInfo
    assert registered["none"] is NoInfo
    with pytest.raises(TypeError):
        registered["otro"] = NoInfo  # type: ignore[index]


def test_register_source_info_adds_the_type(sample_registered: None) -> None:
    assert list(registered_source_infos())[-1] == "sample"
    assert registered_source_infos()["sample"] is _SampleInfo


@pytest.mark.parametrize("name", ["", "Image", "1image", "image-info", " image"])
def test_register_source_info_rejects_invalid_names(name: str) -> None:
    with pytest.raises(ValueError, match="nombre de SourceInfo inválido"):
        register_source_info(name)


def test_register_source_info_rejects_repeated_names_and_types(sample_registered: None) -> None:
    with pytest.raises(ValueError, match="ya hay un SourceInfo"):
        register_source_info("sample")(_Unregistered)
    with pytest.raises(ValueError, match="ya está registrado"):
        register_source_info("sample_again")(_SampleInfo)


@pytest.mark.parametrize("cls", [_NotDataclass, _Mutable])
def test_register_source_info_requires_a_frozen_dataclass(cls: type[SourceInfo]) -> None:
    with pytest.raises(TypeError, match="frozen=True"):
        register_source_info("bad")(cls)


def test_register_source_info_requires_a_source_info_subclass() -> None:
    @dataclass(frozen=True)
    class Plain:
        value: int

    with pytest.raises(TypeError, match="subclase de SourceInfo"):
        register_source_info("plain")(Plain)  # type: ignore[type-var]


# --- Forma JSON ---


def test_to_json_and_from_json_round_trip(sample_registered: None) -> None:
    data = SAMPLE.to_json()

    assert data == {
        "type": "sample",
        "data": {
            "count": 3,
            "flag": True,
            "kind": "video",
            "mode": "gris",
            "parts": [{"width": 612.5, "label": "A4"}, {"width": 595, "label": None}],
            "note": None,
        },
    }
    # Pasa por texto JSON, como la salida de un subproceso
    loaded = SourceInfo.from_json(json.loads(json.dumps(data)))
    assert loaded == SAMPLE
    assert isinstance(loaded, _SampleInfo)
    assert loaded.parts[1].width == 595.0 and isinstance(loaded.parts[1].width, float)
    assert loaded.mode is _Mode.GRAY
    assert _SampleInfo.from_json(data) == SAMPLE


def test_to_json_rejects_unregistered_types_and_unsupported_values() -> None:
    with pytest.raises(TypeError, match="no está registrado"):
        _Unregistered(value=1).to_json()
    register_source_info("mapping")(_WithMapping)
    with pytest.raises(TypeError, match="tipo no admitido"):
        _WithMapping(values={"a": 1}).to_json()


def test_to_json_rejects_non_finite_numbers(sample_registered: None) -> None:
    info = _SampleInfo(1, False, "audio", _Mode.COLOR, (_Part(float("inf"), None),))
    with pytest.raises(ValueError, match="no es finito"):
        info.to_json()


def test_from_json_requires_the_requested_type(sample_registered: None) -> None:
    with pytest.raises(InspectorOutputError, match="no corresponde a TextInfo"):
        TextInfo.from_json(SAMPLE.to_json())
    with pytest.raises(InspectorOutputError, match="no es un tipo de SourceInfo registrado"):
        SourceInfo.from_json({"type": "desconocido", "data": {}})
    with pytest.raises(InspectorOutputError, match="no es un tipo de SourceInfo registrado"):
        SourceInfo.from_json({"type": ["text"], "data": {}})
    with pytest.raises(InspectorOutputError, match="claves type y data"):
        SourceInfo.from_json({"type": "text"})


_SECRET = "texto-del-archivo"


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"count": True}, r"info\.data\.count: debe ser un número entero"),
        ({"count": 1.5}, r"info\.data\.count: debe ser un número entero"),
        ({"flag": 1}, r"info\.data\.flag: debe ser un booleano"),
        ({"kind": _SECRET}, r"info\.data\.kind: no es uno de los valores admitidos"),
        ({"mode": _SECRET}, r"info\.data\.mode: no es un valor de _Mode"),
        ({"parts": {"width": 1}}, r"info\.data\.parts: debe ser una lista"),
        ({"parts": [{"width": "1", "label": None}]}, r"parts\[0\]\.width: debe ser un número"),
        ({"parts": [{"width": 1, "label": 2}]}, r"parts\[0\]\.label: debe ser un texto"),
        ({"parts": [{"width": 1}]}, r"parts\[0\]: faltan los campos label"),
        ({"parts": [_SECRET]}, r"parts\[0\]: debe ser un objeto"),
        ({"note": None, _SECRET: 1}, r"info\.data: tiene campos que no son de _SampleInfo"),
        ({"flag": None}, r"info\.data\.flag: debe ser un booleano"),
        ({"kind": None}, r"info\.data\.kind: no es uno de los valores admitidos"),
        ({"count": -1}, r"info\.data: los valores no forman un _SampleInfo válido"),
    ],
)
def test_from_json_validates_every_field(
    sample_registered: None, change: dict[str, object], problem: str
) -> None:
    data = SAMPLE.to_json()
    fields = data["data"]
    assert isinstance(fields, dict)
    with pytest.raises(InspectorOutputError, match=problem) as error:
        SourceInfo.from_json({"type": "sample", "data": {**fields, **change}})
    # El mensaje nombra el campo, nunca el valor recibido, y no encadena otra excepción
    assert _SECRET not in str(error.value)
    assert error.value.__cause__ is None and error.value.__context__ is None


def test_from_json_reports_missing_fields(sample_registered: None) -> None:
    with pytest.raises(InspectorOutputError, match="faltan los campos count, flag"):
        SourceInfo.from_json({"type": "sample", "data": {"kind": "audio"}})


# --- TextInfo y NoInfo ---


@pytest.mark.req("2.12")
@pytest.mark.parametrize(
    ("info", "data", "text"),
    [
        (TextInfo(TextKind.UTF8, had_bom=True), BOM_UTF8 + "<p>Año</p>".encode(), "<p>Año</p>"),
        (TextInfo(TextKind.UTF8, had_bom=False), "señal".encode(), "señal"),
        # La codificación es la clasificada, aunque el documento declare otra
        (
            TextInfo(TextKind.WINDOWS_1252, had_bom=False),
            b'<meta charset="utf-8"><p>Espa\xf1a \x81</p>',
            '<meta charset="utf-8"><p>España \x81</p>',
        ),
    ],
)
def test_text_info_decodes_with_the_classified_encoding(
    info: TextInfo, data: bytes, text: str
) -> None:
    assert info.decode(data) == text


@pytest.mark.req("2.12")
def test_text_info_round_trips_through_inspector_output() -> None:
    for info in (TextInfo(TextKind.UTF8, True), TextInfo(TextKind.WINDOWS_1252, False)):
        assert load_inspector_output(dump_inspector_output(info)) == info
    assert TextInfo(TextKind.UTF8, True).to_json() == {
        "type": "text",
        "data": {"encoding": "utf8", "had_bom": True},
    }


def test_text_info_rejects_inconsistent_values() -> None:
    with pytest.raises(ValueError, match="UTF-8 o Windows-1252"):
        TextInfo(TextKind.INVALID, had_bom=False)
    with pytest.raises(ValueError, match="BOM_UTF8"):
        TextInfo(TextKind.WINDOWS_1252, had_bom=True)
    with pytest.raises(InspectorOutputError, match="no forman un TextInfo válido"):
        SourceInfo.from_json({"type": "text", "data": {"encoding": "invalid", "had_bom": False}})


def test_no_info_round_trips() -> None:
    assert NoInfo().to_json() == {"type": "none", "data": {}}
    assert load_inspector_output(dump_inspector_output(NoInfo())) == NoInfo()


# --- SourceRejection ---


@pytest.mark.req("16.1")
def test_source_rejection_is_a_detection_failure() -> None:
    sources = ["PNG", "TXT"]
    rejection = SourceRejection(
        FailureReason.NO_TARGETS, {"tipo_real": "SRT", "formatos_origen": sources}, real_type="SRT"
    )
    sources.append("MD")  # el rechazo guarda una copia

    failure = rejection.failure
    assert failure.reason is FailureReason.NO_TARGETS
    assert failure.stage is Stage.DETECTION
    assert dict(failure.params) == {"tipo_real": "SRT", "formatos_origen": ["PNG", "TXT"]}
    assert isinstance(rejection.params, MappingProxyType)
    assert rejection.real_type == "SRT"
    assert SourceRejection("archivo_danado").reason is FailureReason.DAMAGED  # type: ignore[arg-type]


@pytest.mark.req("16.1")
def test_source_rejection_validates_reason_stage_and_params() -> None:
    with pytest.raises(ValueError):
        SourceRejection("no_existe")  # type: ignore[arg-type]
    # Un fallo durante el procesamiento no es un Rechazo_de_Origen
    with pytest.raises(InvalidMessageError, match="no ocurre en la etapa detección"):
        SourceRejection(FailureReason.UNSUPPORTED_CODEC, {"codec": "x", "pista": "audio"})
    with pytest.raises(InvalidMessageError, match="faltan los parámetros"):
        SourceRejection(FailureReason.PIXEL_LIMIT, {"ancho": 10})
    with pytest.raises(ValueError, match="no puede estar vacío"):
        SourceRejection(FailureReason.DAMAGED, real_type="")


@pytest.mark.req("16.7")
def test_source_rejection_carries_a_log_detail_that_is_not_compared() -> None:
    detail = sanitize_tool_output(Tool.PILLOW, ["cannot identify image file"], returncode=1)
    rejection = SourceRejection(FailureReason.DAMAGED, detail=detail)
    assert rejection == SourceRejection(FailureReason.DAMAGED)
    # El Detector_de_Tipo agrega el Tipo_Real con `replace`, que conserva el detalle
    assert replace(rejection, real_type="PNG").detail is detail
    # El detalle no viaja en la salida del inspector
    loaded = load_inspector_output(dump_inspector_output(rejection))
    assert isinstance(loaded, SourceRejection) and loaded.detail is None
    error = SourceRejection(FailureReason.INTERNAL_ERROR, detail=describe_exception(KeyError()))
    assert isinstance(error.detail, ErrorDetail)
    with pytest.raises(TypeError, match="ToolDetail o un ErrorDetail"):
        SourceRejection(FailureReason.DAMAGED, detail="texto")  # type: ignore[arg-type]


# --- DetectionResult ---


def test_detection_result_holds_a_source_info_and_notices() -> None:
    notice = Notice(NoticeCode.EXTENSION_MISMATCH, {"extension": ".txt", "tipo_real": "PNG"})
    result = DetectionResult("PNG", NoInfo(), [notice])  # type: ignore[arg-type]
    assert result.notices == (notice,)
    assert DetectionResult("TXT", TextInfo(TextKind.UTF8, False)).notices == ()
    with pytest.raises(TypeError, match="SourceInfo"):
        DetectionResult("PNG", object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Tipo_Real"):
        DetectionResult("", NoInfo())


# --- Salida de un inspector ---


def test_inspector_rejections_follow_the_design_order() -> None:
    assert INSPECTION_REJECTIONS == (
        FailureReason.PROTECTED_PDF,
        FailureReason.ENCRYPTED_DOCX,
        FailureReason.DECOMPRESSION_LIMITS,
        FailureReason.DAMAGED,
        FailureReason.ANIMATED_IMAGE,
        FailureReason.PIXEL_LIMIT,
    )
    assert set(INSPECTOR_REASONS) == {*INSPECTION_REJECTIONS, FailureReason.INTERNAL_ERROR}


@pytest.mark.req("16.1")
def test_inspector_output_round_trips_a_rejection() -> None:
    rejection = SourceRejection(
        FailureReason.PIXEL_LIMIT,
        {"ancho": 12000, "alto": 9000, "limite_pixeles": 100_000_000},
        real_type="PNG",
    )
    text = dump_inspector_output(rejection)

    assert json.loads(text) == {
        "rejection": {
            "reason": "excede_limite_pixeles",
            "params": {"ancho": 12000, "alto": 9000, "limite_pixeles": 100_000_000},
        }
    }
    # El Tipo_Real no viaja: lo agrega el Detector_de_Tipo
    assert load_inspector_output(text.encode()) == SourceRejection(
        FailureReason.PIXEL_LIMIT, rejection.params
    )


def test_dump_inspector_output_rejects_other_results() -> None:
    with pytest.raises(ValueError, match="no produce el rechazo tipo_sin_destinos"):
        dump_inspector_output(
            SourceRejection(
                FailureReason.NO_TARGETS, {"tipo_real": "SRT", "formatos_origen": ["TXT"]}
            )
        )
    with pytest.raises(TypeError):
        dump_inspector_output(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("", "no es JSON válido"),
        (b"\xff\xfe{", "no es JSON válido"),
        ("[" * 100_000, "no es JSON válido"),
        ('{"info": {"type": "none", "data": {}}, "rejection": {}}', "una sola clave"),
        ('{"otra": {}}', "una sola clave"),
        ("[]", "una sola clave"),
        ('{"rejection": {"reason": "archivo_danado"}}', "claves reason y params"),
        ('{"rejection": {"reason": "tipo_sin_destinos", "params": {}}}', "no es un rechazo"),
        ('{"rejection": {"reason": 1, "params": {}}}', "no es un rechazo"),
        ('{"rejection": {"reason": "archivo_danado", "params": []}}', "debe ser un objeto"),
        (
            '{"rejection": {"reason": "excede_limite_pixeles", '
            '"params": {"ancho": true, "alto": 1, "limite_pixeles": 1}}}',
            "cada valor",
        ),
        (
            '{"rejection": {"reason": "archivo_danado", "params": {"x": [1]}}}',
            "cada valor",
        ),
        (
            '{"rejection": {"reason": "archivo_danado", "params": {"x": "y"}}}',
            "no son los parámetros del motivo archivo_danado",
        ),
        ('{"info": {"type": "text", "data": {"encoding": NaN, "had_bom": false}}}', "JSON"),
        ('{"info": {"type": "text", "data": {"encoding": "utf8"}}}', "faltan los campos"),
    ],
)
def test_load_inspector_output_rejects_invalid_output(text: str | bytes, problem: str) -> None:
    with pytest.raises(InspectorOutputError, match=problem):
        load_inspector_output(text)
