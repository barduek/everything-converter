"""Pruebas de src/converter/detection/detector.py: el flujo del Detector_de_Tipo, el orden de los
Rechazos_de_Origen y el aviso de extensión distinta (Req 2.1 a 2.10, 2.12, 3.6, 4.4, 16.1).

El registro tiene los Formatos de `catalog/formats.toml` y pares de prueba propios, para no
depender de los pares que agregue cada familia de convertidores. SRT queda sin pares, como en el
Mapa_de_Conversión_Inicial.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import pytest

from converter.config import Config, load_config
from converter.detection.detector import (
    DetectionSetupError,
    InspectionRequest,
    NeedsInspection,
    TypeDetector,
    extension_notice,
    size_limit_mb,
)
from converter.detection.source_info import (
    DetectionResult,
    NoInfo,
    SourceInfo,
    SourceRejection,
    TextInfo,
)
from converter.detection.text import BOM_UTF8, TextKind
from converter.eventlog import EventLog
from converter.messages_es import FailureReason, NoticeCode, render_failure, render_notice
from converter.models import Category, Notice, Stage
from converter.registry.loader import load_catalog
from converter.registry.registry import ConversionRegistry
from converter.storage.workspace import WorkspaceManager
from tests import format_samples as samples

CONVERTER = "test.copy"

type Pairs = tuple[tuple[str, str], ...]

# Un par por Formato_de_Origen; SRT no tiene pares
PAIRS: Pairs = (
    ("PNG", "JPG"),
    ("JPG", "PNG"),
    ("WEBP", "PNG"),
    ("PDF", "TXT"),
    ("TXT", "HTML"),
    ("MD", "HTML"),
    ("HTML", "TXT"),
    ("DOCX", "PDF"),
    ("MP3", "WAV"),
    ("WAV", "MP3"),
    ("OGG", "WAV"),
    ("MP4", "WEBM"),
    ("WEBM", "MP4"),
)
SOURCE_FORMATS = [source for source, _ in PAIRS]

# Contenido con la firma de cada Formato y el inspector que declara en el catálogo
SIGNED = {
    "PNG": (samples.PNG, "image"),
    "JPG": (samples.JPG, "image"),
    "WEBP": (samples.WEBP, "image"),
    "PDF": (samples.PDF, "pdf"),
    "DOCX": (samples.docx(), "docx"),
    "MP3": (samples.mp3(), "media"),
    "WAV": (samples.WAV, "media"),
    "OGG": (samples.OGG, "media"),
    "MP4": (samples.mp4(), "media"),
    "WEBM": (samples.webm(), "media"),
}

TEXT = "# Título\n\nEl año pasó rápido.\n".encode()
WINDOWS_1252 = b"Espa\xf1a, \xbfqu\xe9 tal?\r\n"
SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHola\n"
BINARY = b"\x00\x01\x02 sin firma"


@dataclass(frozen=True, slots=True)
class _Inspected(SourceInfo):
    """Información de prueba: el inspector y el Tipo_Real con los que se llamó."""

    inspector: str
    real_type: str


class FakeInspector:
    """Función de inspección que registra cada llamada y devuelve `result` o `_Inspected`."""

    def __init__(self, result: object = None) -> None:
        self.requests: list[InspectionRequest] = []
        self.result = result

    async def __call__(self, request: InspectionRequest) -> SourceInfo | SourceRejection:
        self.requests.append(request)
        if self.result is None:
            return _Inspected(request.inspector, request.real_type)
        return self.result  # type: ignore[return-value]  # las pruebas de errores devuelven otros


async def _never_inspect(request: InspectionRequest) -> SourceInfo | SourceRejection:
    raise AssertionError("la detección no debía llegar al inspector")


def _config(**variables: str) -> Config:
    config = load_config({f"CONVERTER_{name}": value for name, value in variables.items()})
    assert isinstance(config, Config)
    return config


def _pair(source: str, target: str) -> dict[str, object]:
    return {"source": source, "target": target, "loss_class": "Sin_Pérdida", "converter": CONVERTER}


def _format(name: str, extension: str, **extra: object) -> dict[str, object]:
    return {
        "name": name,
        "category": "documento",
        "extensions": [extension],
        "primary_extension": extension,
        "mime_type": "application/octet-stream",
        **extra,
    }


def _magic(hex_text: str) -> dict[str, object]:
    return {"magic": [{"offset": 0, "hex": hex_text}]}


def _build_registry(directory: Path, pairs: Pairs = PAIRS) -> ConversionRegistry:
    """Formatos del catálogo inicial con `pairs`, sin los pares de `catalog/pairs.toml`."""
    empty = directory / "pairs.toml"
    empty.write_text("", encoding="utf-8")
    registry = load_catalog(
        [CONVERTER],
        event_log=EventLog(io.StringIO()),
        pairs_path=empty,
        extra_pairs_dir=directory / "pairs.d",
    )
    for source, target in pairs:
        registry.register_pair(_pair(source, target))
    return registry


@pytest.fixture
def registry(tmp_path: Path) -> ConversionRegistry:
    return _build_registry(tmp_path)


def _identify(
    registry: ConversionRegistry, content: bytes, name: str, config: Config | None = None
) -> DetectionResult | SourceRejection | NeedsInspection:
    detector = TypeDetector(registry, config or _config(), _never_inspect)
    return detector.identify(io.BytesIO(content), name)


def _mismatch(registry: ConversionRegistry, real_type: str, extension: str) -> Notice:
    (fmt,) = (fmt for fmt in registry.formats() if fmt.name == real_type)
    notice = extension_notice(fmt, extension)
    assert notice is not None
    return notice


# --- Paso 1: firma ---


@pytest.mark.req("2.1", "2.8")
@pytest.mark.parametrize("real_type", list(SIGNED))
@pytest.mark.parametrize("name", ["archivo.txt", "archivo", "archivo.", ".bin", "a.b.c.srt"])
def test_the_signature_decides_the_real_type_with_any_name(
    registry: ConversionRegistry, real_type: str, name: str
) -> None:
    content, inspector = SIGNED[real_type]

    outcome = _identify(registry, content, name)

    assert isinstance(outcome, NeedsInspection)
    assert outcome.format.name == real_type
    assert outcome.inspector == inspector


@pytest.mark.req("2.6")
@pytest.mark.parametrize(
    ("name", "extension"),
    [
        ("archivo.txt", ".txt"),
        ("archivo.Png", ".Png"),
        ("archivo", None),
        ("archivo.", None),
        ("ARCHIVO.JPEG", None),
        ("archivo.jpg", None),
    ],
)
def test_the_extension_notice_appears_only_for_a_foreign_extension(
    registry: ConversionRegistry, name: str, extension: str | None
) -> None:
    outcome = _identify(registry, samples.JPG, name)

    assert isinstance(outcome, NeedsInspection)
    if extension is None:
        assert outcome.notices == ()
        return
    (notice,) = outcome.notices
    assert notice.code == NoticeCode.EXTENSION_MISMATCH
    assert dict(notice.params) == {"extension": extension, "tipo_real": "JPG"}
    text = render_notice(notice)
    assert f"«{extension}»" in text and "JPG" in text


# --- Pasos 2 y 3: variante y texto ---


@pytest.mark.req("2.2", "2.3", "2.6", "2.12")
@pytest.mark.parametrize(
    ("name", "real_type", "foreign_extension"),
    [
        ("notas.md", "MD", None),
        ("NOTAS.MD", "MD", None),
        ("pagina.html", "HTML", None),
        ("pagina.HTM", "HTML", None),
        ("notas.txt", "TXT", None),
        ("notas", "TXT", None),
        ("notas.", "TXT", None),
        ("notas.xyz", "TXT", ".xyz"),
        ("foto.png", "TXT", ".png"),
    ],
)
def test_valid_text_takes_the_text_format_of_its_extension(
    registry: ConversionRegistry, name: str, real_type: str, foreign_extension: str | None
) -> None:
    outcome = _identify(registry, TEXT, name)

    assert isinstance(outcome, DetectionResult)
    assert outcome.real_type == real_type
    assert outcome.info == TextInfo(encoding=TextKind.UTF8, had_bom=False)
    notices = [(notice.code, dict(notice.params)) for notice in outcome.notices]
    if foreign_extension is None:
        assert notices == []
    else:
        expected = {"extension": foreign_extension, "tipo_real": real_type}
        assert notices == [(NoticeCode.EXTENSION_MISMATCH, expected)]


@pytest.mark.req("2.4")
@pytest.mark.parametrize("name", ["notas.md", "notas.htm", "notas"])
def test_the_utf8_bom_does_not_change_the_real_type(
    registry: ConversionRegistry, name: str
) -> None:
    plain = _identify(registry, TEXT, name)
    with_bom = _identify(registry, BOM_UTF8 + TEXT, name)

    assert isinstance(plain, DetectionResult) and isinstance(with_bom, DetectionResult)
    assert with_bom.real_type == plain.real_type
    assert with_bom.info == TextInfo(encoding=TextKind.UTF8, had_bom=True)
    assert isinstance(with_bom.info, TextInfo)
    assert with_bom.info.decode(BOM_UTF8 + TEXT) == TEXT.decode()


@pytest.mark.req("2.2", "2.12")
def test_windows_1252_text_is_decoded_with_its_classified_encoding(
    registry: ConversionRegistry,
) -> None:
    content = b'<meta charset="utf-8">' + WINDOWS_1252
    outcome = _identify(registry, content, "carta.html")

    assert isinstance(outcome, DetectionResult)
    assert outcome.real_type == "HTML"
    assert outcome.info == TextInfo(encoding=TextKind.WINDOWS_1252, had_bom=False)
    assert isinstance(outcome.info, TextInfo)
    # La etiqueta meta charset no cambia la decodificación
    assert outcome.info.decode(content) == '<meta charset="utf-8">España, ¿qué tal?\r\n'


@pytest.mark.req("2.5", "16.1")
@pytest.mark.parametrize("content", [BINARY, b"\xff" + b"\x01" * 10, samples.encrypted_docx()])
@pytest.mark.parametrize("name", ["datos.txt", "datos", "datos.doc"])
def test_content_without_signature_that_is_not_text_is_unrecognized(
    registry: ConversionRegistry, content: bytes, name: str
) -> None:
    outcome = _identify(registry, content, name)

    assert outcome == SourceRejection(FailureReason.UNRECOGNIZED_FORMAT)
    assert isinstance(outcome, SourceRejection)
    assert outcome.real_type is None
    assert outcome.failure.stage is Stage.DETECTION
    message = render_failure(outcome.failure).message
    assert "no es reconocido" in message
    assert "UTF-8" in message and "Windows-1252" in message


@pytest.mark.req("2.10", "16.1")
@pytest.mark.parametrize("name", ["secreto.docx", "SECRETO.DOCX"])
def test_an_encrypted_docx_is_docx_with_its_rejection(
    registry: ConversionRegistry, name: str
) -> None:
    outcome = _identify(registry, samples.encrypted_docx(), name)

    assert outcome == SourceRejection(FailureReason.ENCRYPTED_DOCX, real_type="DOCX")


# --- Paso 4: Formato_de_Origen ---


@pytest.mark.req("2.7", "3.6", "16.1")
def test_a_type_without_targets_is_rejected_with_the_source_formats(
    registry: ConversionRegistry,
) -> None:
    outcome = _identify(registry, SRT, "subtitulos.srt")

    assert isinstance(outcome, SourceRejection)
    assert outcome.reason is FailureReason.NO_TARGETS
    assert outcome.real_type == "SRT"
    assert dict(outcome.params) == {"tipo_real": "SRT", "formatos_origen": SOURCE_FORMATS}
    message = render_failure(outcome.failure).message
    assert "SRT" in message and ", ".join(SOURCE_FORMATS) in message


@pytest.mark.req("2.7")
def test_a_signed_type_without_targets_is_rejected(tmp_path: Path) -> None:
    registry = _build_registry(tmp_path, tuple(p for p in PAIRS if p[0] != "OGG"))

    outcome = _identify(registry, samples.OGG, "audio.ogg")

    assert outcome == SourceRejection(
        FailureReason.NO_TARGETS,
        {"tipo_real": "OGG", "formatos_origen": [s for s in SOURCE_FORMATS if s != "OGG"]},
        real_type="OGG",
    )


# --- Paso 5: Límite_de_Tamaño de la Categoría ---


@pytest.mark.req("2.9", "16.1")
@pytest.mark.parametrize(
    ("content", "name", "variable", "category"),
    [
        (samples.PNG, "foto.png", "MAX_IMAGE_MB", Category.IMAGE),
        (TEXT, "notas.md", "MAX_DOCUMENT_MB", Category.DOCUMENT),
        (samples.OGG, "audio.ogg", "MAX_AUDIO_MB", Category.AUDIO),
        (samples.mp4(), "video.mp4", "MAX_VIDEO_MB", Category.VIDEO),
    ],
)
def test_the_size_limit_of_the_category_applies(
    registry: ConversionRegistry, content: bytes, name: str, variable: str, category: Category
) -> None:
    config = _config(**{variable: "1"})
    padding = b"a" if category is Category.DOCUMENT else b"\x00"
    at_limit = content.ljust(1_000_000, padding)

    accepted = _identify(registry, at_limit, name, config)
    rejected = _identify(registry, at_limit + padding, name, config)

    assert not isinstance(accepted, SourceRejection)
    assert isinstance(rejected, SourceRejection)
    assert rejected.reason is FailureReason.CATEGORY_SIZE_LIMIT
    assert dict(rejected.params) == {"categoria": category.value, "limite_mb": 1}
    assert rejected.real_type is not None
    message = render_failure(rejected.failure).message
    assert category.value in message and "1 MB" in message


def test_size_limit_mb_reads_the_limit_of_each_category() -> None:
    config = _config(MAX_IMAGE_MB="11", MAX_DOCUMENT_MB="12", MAX_AUDIO_MB="13", MAX_VIDEO_MB="14")
    assert [size_limit_mb(config, category) for category in Category] == [11, 12, 13, 14]


# --- Orden de los rechazos ---


@pytest.mark.req("2.7", "2.9", "16.1")
def test_rejections_follow_the_design_order(tmp_path: Path) -> None:
    registry = _build_registry(tmp_path)
    without_docx = _build_registry(tmp_path, tuple(p for p in PAIRS if p[0] != "DOCX"))
    small = _config(MAX_DOCUMENT_MB="1")
    # Sectores libres al final: la sonda de DOCX_Cifrado sigue coincidiendo
    large_encrypted = samples.encrypted_docx() + bytes(512 * 2000)
    large_srt = SRT.ljust(1_000_001, b"a")

    # Tipo sin destinos antes que el Límite_de_Tamaño y que DOCX_Cifrado
    assert _identify(without_docx, large_encrypted, "x.docx", small) == SourceRejection(
        FailureReason.NO_TARGETS,
        {"tipo_real": "DOCX", "formatos_origen": [s for s in SOURCE_FORMATS if s != "DOCX"]},
        real_type="DOCX",
    )
    srt = _identify(registry, large_srt, "x.srt", small)
    assert isinstance(srt, SourceRejection) and srt.reason is FailureReason.NO_TARGETS
    # Límite_de_Tamaño antes que DOCX_Cifrado
    assert _identify(registry, large_encrypted, "x.docx", small) == SourceRejection(
        FailureReason.CATEGORY_SIZE_LIMIT,
        {"categoria": "documento", "limite_mb": 1},
        real_type="DOCX",
    )
    assert _identify(registry, large_encrypted, "x.docx") == SourceRejection(
        FailureReason.ENCRYPTED_DOCX, real_type="DOCX"
    )


# --- Paso 6: inspector ---


@pytest.mark.req("2.1", "2.6")
@pytest.mark.asyncio
async def test_detect_runs_the_injected_inspector_last(
    registry: ConversionRegistry, tmp_path: Path
) -> None:
    workspace = WorkspaceManager(tmp_path).create()
    workspace.source_file.write_bytes(samples.mp4())
    inspector = FakeInspector()

    result = await TypeDetector(registry, _config(), inspector).detect(workspace, "video.webm")

    assert inspector.requests == [
        InspectionRequest(inspector="media", real_type="MP4", workspace=workspace)
    ]
    notice = _mismatch(registry, "MP4", ".webm")
    assert result == DetectionResult("MP4", _Inspected("media", "MP4"), (notice,))


@pytest.mark.req("16.1")
@pytest.mark.asyncio
async def test_an_inspector_rejection_gets_the_real_type(
    registry: ConversionRegistry, tmp_path: Path
) -> None:
    workspace = WorkspaceManager(tmp_path).create()
    workspace.source_file.write_bytes(samples.PNG)
    inspector = FakeInspector(SourceRejection(FailureReason.ANIMATED_IMAGE))

    result = await TypeDetector(registry, _config(), inspector).detect(workspace, "a.png")

    assert result == SourceRejection(FailureReason.ANIMATED_IMAGE, real_type="PNG")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returned", "error"),
    [
        (
            SourceRejection(
                FailureReason.NO_TARGETS, {"tipo_real": "PNG", "formatos_origen": ["PNG"]}
            ),
            ValueError,
        ),
        (object(), TypeError),
    ],
)
async def test_an_invalid_inspector_result_raises(
    registry: ConversionRegistry, tmp_path: Path, returned: object, error: type[Exception]
) -> None:
    workspace = WorkspaceManager(tmp_path).create()
    workspace.source_file.write_bytes(samples.PNG)
    detector = TypeDetector(registry, _config(), FakeInspector(returned))

    with pytest.raises(error):
        await detector.detect(workspace, "a.png")


@pytest.mark.req("2.2", "2.12")
@pytest.mark.asyncio
async def test_the_text_inspector_runs_in_the_main_process(
    registry: ConversionRegistry, tmp_path: Path
) -> None:
    workspace = WorkspaceManager(tmp_path).create()
    workspace.source_file.write_bytes(WINDOWS_1252)

    result = await TypeDetector(registry, _config(), _never_inspect).detect(workspace, "a.txt")

    assert result == DetectionResult("TXT", TextInfo(TextKind.WINDOWS_1252, had_bom=False))


# --- Formatos registrados después ---


@pytest.mark.req("4.4")
def test_formats_registered_later_are_detected(registry: ConversionRegistry) -> None:
    registry.register_format(_format("ABC", ".abc", signature=_magic("41424321")))
    registry.register_format(
        _format("CSVX", ".csvx", signature=_magic("2363737678"), inspector="text")
    )
    registry.register_format(_format("LOG", ".log", inspector="text"))
    for source in ("ABC", "CSVX", "LOG"):
        registry.register_pair(_pair(source, "TXT"))

    # Con firma y sin inspector: no hay datos del origen
    assert _identify(registry, b"ABC!\x00\x01", "x.txt") == DetectionResult(
        "ABC", NoInfo(), (_mismatch(registry, "ABC", ".txt"),)
    )
    # Con firma e inspector `text`: se clasifica el contenido
    assert _identify(registry, b"#csvx;a;b\n", "x.csvx") == DetectionResult(
        "CSVX", TextInfo(TextKind.UTF8, had_bom=False)
    )
    assert _identify(registry, b"#csvx\x00", "x.csvx") == SourceRejection(
        FailureReason.DAMAGED, real_type="CSVX"
    )
    # Formato_de_Texto nuevo: por su extensión
    assert _identify(registry, TEXT, "registro.LOG") == DetectionResult(
        "LOG", TextInfo(TextKind.UTF8, had_bom=False)
    )


# --- Errores que no dependen del archivo ---


def test_an_empty_conversion_map_raises(tmp_path: Path) -> None:
    registry = _build_registry(tmp_path, ())
    with pytest.raises(DetectionSetupError, match="Formatos_de_Origen"):
        _identify(registry, samples.PNG, "a.png")


def test_a_registry_without_txt_raises_for_text_without_text_format() -> None:
    registry = ConversionRegistry([CONVERTER], event_log=EventLog(io.StringIO()))
    registry.register_format(_format("MD", ".md"))
    registry.register_format(_format("HTML", ".html"))
    registry.register_pair(_pair("MD", "HTML"))

    with pytest.raises(DetectionSetupError, match="TXT"):
        _identify(registry, TEXT, "notas.txt")
    result = _identify(registry, TEXT, "notas.md")
    assert isinstance(result, DetectionResult) and result.real_type == "MD"


def test_a_variant_probe_without_rejection_raises(registry: ConversionRegistry) -> None:
    detector = TypeDetector(registry, _config(), _never_inspect, variant_rejections={})
    with pytest.raises(DetectionSetupError, match="docx_encrypted"):
        detector.identify(io.BytesIO(samples.encrypted_docx()), "x.docx")
