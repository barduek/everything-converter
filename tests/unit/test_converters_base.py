"""Pruebas de src/converter/converters/base.py: registro de convertidores, solicitud, contexto,
resultado, fallos, avisos y funciones de arranque (Req 4.3).

Cada prueba que registra algo trabaja sobre copias de los registros del módulo, así no deja
convertidores ni funciones de arranque para las demás.
"""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest

from converter.config import Config, load_config
from converter.converters import base
from converter.converters.base import (
    PROCESSING_STAGES,
    STARTUP_HOOK_FAILED,
    ConversionContext,
    ConversionFailure,
    ConversionOutcome,
    ConversionRequest,
    NoticeCollector,
    StartupContext,
    UnknownConverterError,
    get_converter,
    hook_name,
    on_startup,
    register_converter,
    registered_converters,
    run_startup_hooks,
    startup_hooks,
)
from converter.detection.source_info import TextInfo
from converter.detection.text import TextKind
from converter.eventlog import EventLog, Tool, sanitize_tool_output
from converter.messages_es import (
    CodecTrack,
    FailureReason,
    InvalidMessageError,
    NoticeCode,
    RejectionCode,
)
from converter.models import Category, DestinationRejection, LossClass, Stage
from converter.registry.model import ConversionPair
from converter.sandbox.runner import SandboxRunner
from converter.storage.workspace import WorkspaceManager
from converter.tasks.progress import ProgressReporter


@pytest.fixture(autouse=True)
def isolated_registries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_CONVERTERS", dict(base._CONVERTERS))
    monkeypatch.setattr(base, "_STARTUP_HOOKS", list(base._STARTUP_HOOKS))


def _config() -> Config:
    config = load_config({})
    assert isinstance(config, Config)
    return config


# Información del origen que recibe el convertidor de prueba; no la usa
_SOURCE = TextInfo(encoding=TextKind.UTF8, had_bom=False)


class _Copy:
    """Convertidor de prueba: copia el Archivo_de_Origen y rechaza el destino "BIG".

    Nunca se decora: el decorador fija `id` en la clase, así que cada prueba registra una
    subclase nueva de `_new_copy()`.
    """

    def preflight(
        self, source: object, request: ConversionRequest, config: Config
    ) -> DestinationRejection | None:
        if request.target == "BIG":
            return DestinationRejection(RejectionCode.DIMENSIONS_EXCEEDED, {})
        return None

    async def run(self, ctx: ConversionContext) -> ConversionOutcome:
        output = ctx.workspace.work_dir / "copy"
        shutil.copyfile(ctx.workspace.source_file, output)
        ctx.notices.add(NoticeCode.BITS_REDUCED)
        ctx.progress.report(1.0)
        return ConversionOutcome(result_path=output, result_kind="single")


def _new_copy() -> type[_Copy]:
    class Copy(_Copy):
        pass

    return Copy


# --- Registro de convertidores ---


@pytest.mark.req("4.3")
def test_register_converter_sets_the_id_and_registers_an_instance() -> None:
    cls = _new_copy()
    decorated = register_converter("fake.copy")(cls)

    assert decorated is cls
    assert vars(cls)["id"] == "fake.copy"
    converter = get_converter("fake.copy")
    assert isinstance(converter, cls)
    assert converter.id == "fake.copy"
    assert list(registered_converters())[-1] == "fake.copy"
    assert registered_converters()["fake.copy"] is converter
    request = ConversionRequest(target="BIG")
    assert converter.preflight(_SOURCE, request, _config()) == DestinationRejection(
        RejectionCode.DIMENSIONS_EXCEEDED, {}
    )
    assert converter.preflight(_SOURCE, ConversionRequest(target="JPG"), _config()) is None


@pytest.mark.req("4.3")
@pytest.mark.asyncio
async def test_registered_converter_runs_with_a_conversion_context(tmp_path: Path) -> None:
    register_converter("fake.copy")(_new_copy())
    workspace = WorkspaceManager(tmp_path).create()
    workspace.source_file.write_bytes(b"contenido")
    pair = ConversionPair(
        source="PNG",
        target="JPG",
        category=Category.IMAGE,
        loss_class=LossClass.LOSSY,
        loss_description="Compresión con pérdida.",
        converter_id="fake.copy",
    )
    ctx = ConversionContext(
        task_id="3b9f0c1e-0000-4000-8000-000000000000",
        pair=pair,
        request=ConversionRequest(target="JPG"),
        source=_SOURCE,
        workspace=workspace,
        config=_config(),
        runner=SandboxRunner(),
        progress=ProgressReporter(),
        notices=NoticeCollector(),
    )

    outcome = await get_converter(pair.converter_id).run(ctx)

    assert outcome.result_path == workspace.work_dir / "copy"
    assert outcome.result_path.read_bytes() == b"contenido"
    assert outcome.result_kind == "single"
    assert outcome.detected_language is None
    assert ctx.progress.percent == 100
    assert [notice.code for notice in ctx.notices.notices] == [NoticeCode.BITS_REDUCED]


def test_registered_converters_is_an_immutable_copy() -> None:
    register_converter("fake.copy")(_new_copy())
    snapshot = registered_converters()
    with pytest.raises(TypeError):
        snapshot["fake.other"] = get_converter("fake.copy")  # type: ignore[index]

    register_converter("fake.other")(_new_copy())
    assert "fake.other" not in snapshot
    assert "fake.other" in registered_converters()


@pytest.mark.parametrize(
    "converter_id",
    ["", "image", "Image.raster", "image.", ".raster", "image.raster.v2", " image.raster", "1a.b"],
)
def test_register_converter_rejects_invalid_ids(converter_id: str) -> None:
    with pytest.raises(ValueError, match="identificador"):
        register_converter(converter_id)


def test_register_converter_rejects_duplicates_and_conflicting_ids() -> None:
    registered = register_converter("fake.copy")(_new_copy())
    before = dict(registered_converters())

    with pytest.raises(ValueError, match="fake.copy"):
        register_converter("fake.copy")(_new_copy())

    class Declared(_Copy):
        id = "fake.declared"

    with pytest.raises(ValueError, match="fake.declared"):
        register_converter("fake.other")(Declared)
    # La clase ya registrada tiene su id: no se registra con otro
    with pytest.raises(ValueError, match="fake.copy"):
        register_converter("fake.alias")(registered)
    assert dict(registered_converters()) == before


def test_register_converter_accepts_a_class_that_declares_the_same_id() -> None:
    class Declared(_Copy):
        id = "fake.declared"

    register_converter("fake.declared")(Declared)
    assert get_converter("fake.declared").id == "fake.declared"


def test_register_converter_requires_preflight_and_an_async_run() -> None:
    class SyncRun:
        def preflight(
            self, source: object, request: ConversionRequest, config: Config
        ) -> DestinationRejection | None:
            return None

        def run(self, ctx: ConversionContext) -> ConversionOutcome:
            raise NotImplementedError

    class NoPreflight:
        async def run(self, ctx: ConversionContext) -> ConversionOutcome:
            raise NotImplementedError

    before = dict(registered_converters())
    with pytest.raises(TypeError, match="corrutina"):
        register_converter("fake.sync")(SyncRun)  # type: ignore[type-var]
    with pytest.raises(TypeError, match="preflight"):
        register_converter("fake.nopreflight")(NoPreflight)  # type: ignore[type-var]
    assert dict(registered_converters()) == before


def test_unknown_converter_raises() -> None:
    with pytest.raises(UnknownConverterError) as error:
        get_converter("fake.missing")
    assert error.value.converter_id == "fake.missing"


# --- Solicitud y resultado ---


def test_conversion_request_requires_a_target_and_a_nonempty_language() -> None:
    assert ConversionRequest(target="SRT", language="auto").language == "auto"
    assert ConversionRequest(target="JPG").language is None
    with pytest.raises(ValueError):
        ConversionRequest(target="")
    with pytest.raises(ValueError):
        ConversionRequest(target="SRT", language="")


def test_conversion_outcome_rejects_unknown_result_kinds(tmp_path: Path) -> None:
    assert ConversionOutcome(tmp_path / "pages.zip", "zip").result_kind == "zip"
    with pytest.raises(ValueError, match="result_kind"):
        ConversionOutcome(tmp_path / "out", "folder")  # type: ignore[arg-type]


# --- Fallos ---


def test_conversion_failure_carries_the_failure_reason_and_detail() -> None:
    detail = sanitize_tool_output(Tool.FFMPEG, ["Decoder (codec h264) not found"], returncode=1)
    params: dict[str, str | int | list[str]] = {"codec": "h264", "pista": CodecTrack.VIDEO.value}

    error = ConversionFailure(FailureReason.UNSUPPORTED_CODEC, params=params, detail=detail)

    assert error.failure.reason is FailureReason.UNSUPPORTED_CODEC
    assert error.failure.stage is Stage.CONVERSION
    assert dict(error.failure.params) == params
    assert error.detail is detail
    # El texto de la excepción es el código, sin los parámetros
    assert str(error) == "codec_no_soportado"
    assert "h264" not in str(error)


def test_conversion_failure_accepts_each_processing_stage_of_its_reason() -> None:
    assert ConversionFailure(FailureReason.DAMAGED).failure.params == {}
    transcription = ConversionFailure(FailureReason.MODEL_UNAVAILABLE, stage=Stage.TRANSCRIPTION)
    assert transcription.failure.stage is Stage.TRANSCRIPTION
    delivery = ConversionFailure(FailureReason.NO_SPACE, stage=Stage.DELIVERY)
    assert delivery.failure.stage is Stage.DELIVERY
    assert {Stage.CONVERSION, Stage.TRANSCRIPTION, Stage.DELIVERY} == PROCESSING_STAGES


def test_conversion_failure_rejects_inconsistent_failures() -> None:
    # Una conversión no ocurre en la recepción ni en la detección
    for stage in (Stage.RECEPTION, Stage.DETECTION):
        with pytest.raises(ValueError, match="etapa"):
            ConversionFailure(FailureReason.DAMAGED, stage=stage)
    # El modelo solo falla en la transcripción
    with pytest.raises(InvalidMessageError):
        ConversionFailure(FailureReason.MODEL_UNAVAILABLE)
    # Faltan el Códec y la pista
    with pytest.raises(InvalidMessageError):
        ConversionFailure(FailureReason.UNSUPPORTED_CODEC)
    with pytest.raises(ValueError):
        ConversionFailure("no_es_un_motivo")  # type: ignore[arg-type]


# --- Avisos ---


def test_notice_collector_keeps_validated_notices_in_order() -> None:
    notices = NoticeCollector()
    first = notices.add(NoticeCode.SAMPLE_RATE_CHANGED, {"origen_hz": 44100, "resultado_hz": 48000})
    second = notices.add(NoticeCode.BITS_REDUCED)

    assert notices.notices == (first, second)
    assert first.code == "frecuencia_cambiada"
    assert dict(first.params) == {"origen_hz": 44100, "resultado_hz": 48000}


def test_notice_collector_rejects_invalid_notices() -> None:
    notices = NoticeCollector()
    with pytest.raises(InvalidMessageError):
        notices.add(NoticeCode.CHANNELS_MIXED, {"canales_origen": 6})
    with pytest.raises(ValueError):
        notices.add("aviso_inventado")  # type: ignore[arg-type]
    assert notices.notices == ()


# --- Funciones de arranque ---


def _startup_context(tmp_path: Path, stream: io.StringIO) -> StartupContext:
    return StartupContext(
        config=_config(),
        runner=SandboxRunner(),
        storage=WorkspaceManager(tmp_path),
        event_log=EventLog(stream),
    )


@pytest.mark.asyncio
async def test_startup_hooks_run_in_order_sync_and_async(tmp_path: Path) -> None:
    seen: list[tuple[str, StartupContext]] = []

    @on_startup
    def decoders(ctx: StartupContext) -> None:
        seen.append(("decoders", ctx))

    @on_startup
    async def model(ctx: StartupContext) -> None:
        seen.append(("model", ctx))

    assert startup_hooks()[-2:] == (decoders, model)
    ctx = _startup_context(tmp_path, io.StringIO())
    await run_startup_hooks(ctx)

    assert seen == [("decoders", ctx), ("model", ctx)]


@pytest.mark.asyncio
async def test_a_failing_startup_hook_is_logged_and_does_not_stop_the_rest(
    tmp_path: Path,
) -> None:
    ran: list[str] = []

    @on_startup
    async def broken(ctx: StartupContext) -> None:
        raise RuntimeError("detalle /ruta/secreta")

    @on_startup
    def after(ctx: StartupContext) -> None:
        ran.append("after")

    stream = io.StringIO()
    await run_startup_hooks(_startup_context(tmp_path, stream))

    assert ran == ["after"]
    (line,) = stream.getvalue().splitlines()
    event = json.loads(line)
    assert event["event"] == STARTUP_HOOK_FAILED
    assert event["level"] == "error"
    assert event["hook"] == hook_name(broken)
    assert event["hook"].endswith("broken")
    assert event["exception"] == "RuntimeError"
    # Sin el mensaje de la excepción (Req 16.8)
    assert "secreta" not in line


def test_on_startup_rejects_duplicates() -> None:
    def hook(ctx: StartupContext) -> None:
        return None

    on_startup(hook)
    with pytest.raises(ValueError, match="hook"):
        on_startup(hook)
    assert startup_hooks().count(hook) == 1
