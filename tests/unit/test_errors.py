"""Pruebas de src/converter/converters/errors.py: tabla de patrones, orden de las reglas de la
clasificación de errores de las herramientas y un caso por fila de la tabla "Clasificación de
errores de herramientas" del diseño (Req 16.2, 16.9, 16.10, 14.7, 8.7, 9.6, 10.8, 11.6).

Las excepciones de Pillow y pypdfium2 se obtienen usando esas bibliotecas. FFmpeg, ffprobe,
LibreOffice y whisper-cli no están fuera del contenedor, así que su salida se arma con líneas
copiadas de esas herramientas.
"""

from __future__ import annotations

import errno
import io
import os
import signal
import struct
import traceback
import zipfile
import zlib
from collections.abc import Callable

# pypdfium2 no publica anotaciones de tipos; `pdfium` queda como Any
import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pytest
from PIL import Image

from converter.converters.errors import (
    LAUNCHER_EXIT_CODES,
    MIN_FREE_BYTES,
    PATTERN_REASONS,
    classify_failure,
    classify_zip_verdict,
)
from converter.detection.zipcheck import (
    DECOMPRESSION_LIMITS,
    DecompressionLimits,
    ExceededLimit,
    LimitVerdict,
    check_zip_package,
)
from converter.eventlog import PATTERNS, Tool, ToolDetail, ToolPattern, sanitize_tool_output
from converter.messages_es import FailureReason
from converter.sandbox import launch

R = FailureReason


def _detail(tool: Tool, *lines: str, returncode: int | None = 1) -> ToolDetail:
    return sanitize_tool_output(tool, lines, returncode=returncode)


def _enospc() -> OSError:
    return OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


def _raised(action: Callable[[], object]) -> Exception:
    try:
        action()
    except Exception as exc:
        return exc
    raise AssertionError("la acción no lanzó una excepción")


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 200, 30)).save(buffer, "PNG")
    return buffer.getvalue()


def _password_pdf() -> bytes:
    """PDF de una página con el manejador de seguridad estándar y valores /O y /U que no
    corresponden a la contraseña vacía: PDFium lo rechaza con FPDF_ERR_PASSWORD."""
    encrypt = b"<< /Filter /Standard /V 1 /R 2 /Length 40 /P -4 /O <%s> /U <%s> >>" % (
        b"11" * 32,
        b"22" * 32,
    )
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        encrypt,
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(bodies, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(bodies) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    file_id = b"<" + b"00" * 16 + b">"
    out += b"trailer\n<< /Size %d /Root 1 0 R /Encrypt 4 0 R /ID [%s %s] >>\n" % (
        len(bodies) + 1,
        file_id,
        file_id,
    )
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


# --------------------------------------------------------------------------------------------
# Tabla de patrones


def test_every_pattern_of_the_event_log_has_an_explicit_decision() -> None:
    assert set(PATTERN_REASONS) == set(Tool)
    for tool, patterns in PATTERNS.items():
        assert set(PATTERN_REASONS[tool]) == {pattern.id for pattern in patterns}


_ALL_PATTERNS = [
    pytest.param(tool, pattern, id=f"{tool.value}-{pattern.id}")
    for tool, patterns in PATTERNS.items()
    for pattern in patterns
]


@pytest.mark.req("16.2", "16.9", "16.10")
@pytest.mark.parametrize(("tool", "pattern"), _ALL_PATTERNS)
def test_a_pattern_gives_its_reason_or_internal_error(tool: Tool, pattern: ToolPattern) -> None:
    detail = ToolDetail(tool=tool, exit_code=1, signal=None, pattern=pattern, unrecognized_lines=0)
    expected = PATTERN_REASONS[tool][pattern.id] or R.INTERNAL_ERROR
    assert classify_failure(detail=detail) is expected


@pytest.mark.req("16.9")
def test_without_evidence_or_with_unknown_output_the_reason_is_internal_error() -> None:
    assert classify_failure() is R.INTERNAL_ERROR
    # La herramienta terminó bien pero no produjo su archivo (Req 16.3)
    assert classify_failure(detail=_detail(Tool.FFMPEG, returncode=0)) is R.INTERNAL_ERROR
    unknown = ToolDetail(
        tool=Tool.FFMPEG,
        exit_code=1,
        signal=None,
        pattern=ToolPattern("otro", "otro"),
        unrecognized_lines=0,
    )
    assert classify_failure(detail=unknown) is R.INTERNAL_ERROR


# --------------------------------------------------------------------------------------------
# Orden de las reglas


@pytest.mark.req("14.7")
def test_timeout_wins_over_every_other_evidence() -> None:
    detail = _detail(
        Tool.FFMPEG,
        "av_interleaved_write_frame(): No space left on device",
        returncode=-signal.SIGKILL,
    )
    reason = classify_failure(detail=detail, exception=_enospc(), timed_out=True, free_bytes=0)
    assert reason is R.TIMED_OUT
    assert classify_failure(timed_out=True) is R.TIMED_OUT


@pytest.mark.req("16.10")
def test_no_space_wins_over_the_signals_of_the_tool() -> None:
    damaged = _detail(
        Tool.FFMPEG, "Error while decoding stream #0:0: Invalid data found when processing input"
    )
    assert classify_failure(detail=damaged) is R.DAMAGED
    assert classify_failure(detail=damaged, free_bytes=MIN_FREE_BYTES - 1) is R.NO_SPACE
    assert classify_failure(detail=damaged, exception=_enospc()) is R.NO_SPACE
    no_space = _detail(Tool.LIBREOFFICE, "write error: No space left on device")
    assert classify_failure(detail=no_space, package_damaged=True) is R.NO_SPACE


@pytest.mark.req("16.10")
def test_free_space_below_one_mb_means_no_space() -> None:
    assert MIN_FREE_BYTES == 1_000_000
    unknown = _detail(Tool.WHISPER_CPP, "salida sin patrón")
    assert classify_failure(detail=unknown, free_bytes=MIN_FREE_BYTES - 1) is R.NO_SPACE
    assert classify_failure(detail=unknown, free_bytes=MIN_FREE_BYTES) is R.INTERNAL_ERROR
    assert classify_failure(exception=KeyError("x"), free_bytes=0) is R.NO_SPACE


@pytest.mark.req("16.9")
def test_launcher_failures_are_internal_errors_before_the_tool_conditions() -> None:
    assert {125, 126, 127} == LAUNCHER_EXIT_CODES
    not_found = _detail(
        Tool.FFPROBE,
        f"{launch.MODULE}: no se encontró el programa 'ffprobe'",
        returncode=launch.EXIT_NOT_FOUND,
    )
    assert not_found.pattern is not None and not_found.pattern.id == "launch_not_found"
    assert classify_failure(detail=not_found, no_streams=True) is R.INTERNAL_ERROR
    for code in sorted(LAUNCHER_EXIT_CODES):
        # Sin el mensaje del lanzador, el código de salida alcanza
        lost = _detail(Tool.FFPROBE, returncode=code)
        assert classify_failure(detail=lost, no_streams=True) is R.INTERNAL_ERROR
        libreoffice = _detail(Tool.LIBREOFFICE, returncode=code)
        assert classify_failure(detail=libreoffice, package_damaged=True) is R.INTERNAL_ERROR


@pytest.mark.req("16.2", "16.9")
@pytest.mark.parametrize(
    "returncode",
    [pytest.param(1, id="error"), pytest.param(0, id="pdf-ausente"), -signal.SIGSEGV],
)
def test_libreoffice_failure_depends_on_the_structural_check(returncode: int) -> None:
    detail = _detail(
        Tool.LIBREOFFICE, "Error: source file could not be loaded", returncode=returncode
    )
    assert classify_failure(detail=detail, package_damaged=True) is R.DAMAGED
    assert classify_failure(detail=detail) is R.INTERNAL_ERROR


@pytest.mark.req("16.2")
@pytest.mark.parametrize("tool", [Tool.FFMPEG, Tool.FFPROBE])
def test_no_streams_is_damage_only_with_a_nonzero_exit_code(tool: Tool) -> None:
    line = "[mp3 @ 0x55d0] Failed to read frame size: Could not seek to 1026."
    assert classify_failure(detail=_detail(tool, line), no_streams=True) is R.DAMAGED
    assert classify_failure(detail=_detail(tool, line)) is R.INTERNAL_ERROR
    for returncode in (0, -signal.SIGKILL):
        detail = _detail(tool, line, returncode=returncode)
        assert classify_failure(detail=detail, no_streams=True) is R.INTERNAL_ERROR


@pytest.mark.req("16.9")
@pytest.mark.parametrize("signum", [signal.SIGSEGV, signal.SIGKILL, signal.SIGXFSZ])
def test_termination_by_a_signal_is_internal_error_unless_a_pattern_decides(signum: int) -> None:
    for tool in Tool:
        detail = _detail(tool, "salida sin patrón", returncode=-signum)
        assert classify_failure(detail=detail) is R.INTERNAL_ERROR
    model = _detail(
        Tool.WHISPER_CPP,
        "whisper_init_with_params_no_state: failed to load model",
        returncode=-signum,
    )
    assert classify_failure(detail=model) is R.MODEL_UNAVAILABLE


# --------------------------------------------------------------------------------------------
# Excepciones


@pytest.mark.req("16.10", "16.9")
def test_enospc_is_found_in_the_chained_exceptions() -> None:
    def wrapped(*, suppress_context: bool) -> Exception:
        def action() -> None:
            try:
                raise _enospc()
            except OSError as exc:
                if suppress_context:
                    raise RuntimeError("al empaquetar") from None
                raise RuntimeError("al empaquetar") from exc

        return _raised(action)

    assert classify_failure(exception=_enospc()) is R.NO_SPACE
    assert classify_failure(exception=wrapped(suppress_context=False)) is R.NO_SPACE
    assert classify_failure(exception=wrapped(suppress_context=True)) is R.NO_SPACE
    group = ExceptionGroup("tareas", [ValueError("x"), _enospc()])
    assert classify_failure(exception=group) is R.NO_SPACE

    assert classify_failure(exception=PermissionError(errno.EACCES, "x")) is R.INTERNAL_ERROR
    assert classify_failure(exception=KeyError("x")) is R.INTERNAL_ERROR
    first, second = ValueError("a"), ValueError("b")
    first.__context__, second.__context__ = second, first  # un ciclo no cuelga la búsqueda
    assert classify_failure(exception=first) is R.INTERNAL_ERROR


def _library_exceptions(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Tool, Exception, R]]:
    png = _png()
    unidentified = _raised(lambda: Image.open(io.BytesIO(b"no es una imagen")))
    truncated = _raised(lambda: Image.open(io.BytesIO(png[: len(png) // 2])).load())
    with monkeypatch.context() as patch:
        patch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
        bomb = _raised(lambda: Image.open(io.BytesIO(png)))
    damaged_pdf = _raised(lambda: pdfium.PdfDocument(b"%PDF-1.4\nno es un PDF"))
    protected_pdf = _raised(lambda: pdfium.PdfDocument(_password_pdf()))
    return [
        (Tool.PILLOW, unidentified, R.DAMAGED),
        (Tool.PILLOW, truncated, R.DAMAGED),
        (Tool.PILLOW, bomb, R.PIXEL_LIMIT),
        (Tool.PYPDFIUM2, damaged_pdf, R.DAMAGED),
        (Tool.PYPDFIUM2, protected_pdf, R.PROTECTED_PDF),
    ]


@pytest.mark.req("16.2", "8.7", "5.9")
def test_library_exceptions_classify_like_the_traceback_of_a_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for tool, exc, expected in _library_exceptions(monkeypatch):
        assert classify_failure(exception=exc, raised_by=tool) is expected
        # El proceso principal lee el traceback que el worker escribe en stderr
        lines = "".join(traceback.format_exception(exc)).splitlines()
        assert classify_failure(detail=_detail(tool, *lines)) is expected
        # Sin `raised_by` solo se busca ENOSPC
        assert classify_failure(exception=exc) is R.INTERNAL_ERROR


def test_exceptions_from_other_code_are_internal_errors_even_with_raised_by() -> None:
    reason = classify_failure(exception=ZeroDivisionError("división"), raised_by=Tool.PILLOW)
    assert reason is R.INTERNAL_ERROR


def test_incoherent_evidence_is_rejected() -> None:
    ffmpeg = _detail(Tool.FFMPEG)
    with pytest.raises(ValueError, match="raised_by"):
        classify_failure(raised_by=Tool.PILLOW)
    with pytest.raises(ValueError, match="no_streams"):
        classify_failure(detail=_detail(Tool.PILLOW), no_streams=True)
    with pytest.raises(ValueError, match="no_streams"):
        classify_failure(no_streams=True)
    with pytest.raises(ValueError, match="package_damaged"):
        classify_failure(detail=ffmpeg, package_damaged=True)
    with pytest.raises(ValueError, match="free_bytes"):
        classify_failure(detail=ffmpeg, free_bytes=-1)


# --------------------------------------------------------------------------------------------
# Lector ZIP de DOCX


def _zip(*entries: tuple[str, bytes], compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.mark.req("7.7", "16.2")
def test_zip_verdicts_give_limits_before_damage() -> None:
    package = _zip(("word/document.xml", b"<w:document/>"), ("[Content_Types].xml", b"<Types/>"))
    assert classify_zip_verdict(check_zip_package(io.BytesIO(package))) is None
    damaged = check_zip_package(io.BytesIO(b"PK\x03\x04 no es un paquete"))
    assert classify_zip_verdict(damaged) is R.DAMAGED
    exceeded = check_zip_package(io.BytesIO(package), DecompressionLimits(max_entries=1))
    assert classify_zip_verdict(exceeded) is R.DECOMPRESSION_LIMITS
    both = LimitVerdict(exceeded=ExceededLimit.COMPRESSION_RATIO, damaged=True, entries=())
    assert classify_zip_verdict(both) is R.DECOMPRESSION_LIMITS


# --------------------------------------------------------------------------------------------
# Un caso por fila de la tabla "Clasificación de errores de herramientas" del diseño
#
# Cada fila tiene un caso por señal que nombra. Pillow, pypdfium2 y el lector ZIP fallan con
# archivos armados en la prueba; las líneas de FFmpeg, ffprobe, LibreOffice y whisper-cli son
# copias de la salida de esas herramientas.

_SOURCE_PATH = "/var/lib/converter/storage/ws-1/source/original"
_MODEL_PATH = "/opt/converter/models/ggml-small.bin"
_DEFAULT_PIXEL_LIMIT = 100_000_000  # Límite_de_Píxeles predeterminado

# Las dos entradas que bastan para que el lector ZIP mida un DOCX
_DOCX_ENTRIES = (
    ("[Content_Types].xml", b"<Types/>"),
    ("word/document.xml", b"<w:document>" + b"texto " * 50 + b"</w:document>"),
)


def _docx(*extra: tuple[str, bytes], compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    return _zip(*_DOCX_ENTRIES, *extra, compression=compression)


def _deflate_error_docx() -> bytes:
    """DOCX cuyo flujo deflate de word/document.xml empieza con un tipo de bloque reservado."""
    package = bytearray(_docx())
    name = b"word/document.xml"
    package[package.index(name) + len(name)] = 0xFF  # la cabecera local no tiene campo extra
    return bytes(package)


def _crc_error_docx() -> bytes:
    """DOCX sin compresión con un byte de word/document.xml cambiado después de calcular el CRC."""
    package = bytearray(_docx(compression=zipfile.ZIP_STORED))
    package[package.index(b"texto")] ^= 0x20  # "texto" → "Texto"
    return bytes(package)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _bomb_png() -> bytes:
    """PNG que declara 20000 × 20000 píxeles en IHDR, sin datos de imagen."""
    header = struct.pack(">IIBBBBB", 20_000, 20_000, 8, 2, 0, 0, 0)  # RGB de 8 bits por canal
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b""))
        + _png_chunk(b"IEND", b"")
    )


def _broken_png() -> bytes:
    """PNG con bytes alterados en el flujo zlib de IDAT."""
    png = bytearray(_png())
    start = png.index(b"IDAT") + 4 + 2  # después del tipo de bloque y de la cabecera zlib
    for index in range(start, start + 8):
        png[index] ^= 0xFF
    return bytes(png)


def _pillow(data: bytes) -> FailureReason:
    """Motivo del fallo de un worker que abre y decodifica `data` con Pillow."""

    def decode() -> None:
        with Image.open(io.BytesIO(data)) as image:
            image.load()

    return classify_failure(exception=_raised(decode), raised_by=Tool.PILLOW)


def _pypdfium2(data: bytes) -> FailureReason:
    """Motivo del fallo de un worker que abre `data` con pypdfium2."""
    exc = _raised(lambda: pdfium.PdfDocument(data))
    return classify_failure(exception=exc, raised_by=Tool.PYPDFIUM2)


def _stderr(
    tool: Tool,
    *lines: str,
    returncode: int | None = 1,
    timed_out: bool = False,
    free_bytes: int | None = None,
    no_streams: bool = False,
    package_damaged: bool = False,
) -> FailureReason:
    """Motivo del fallo de un subproceso de `tool` según su salida de errores y su evidencia."""
    return classify_failure(
        detail=_detail(tool, *lines, returncode=returncode),
        timed_out=timed_out,
        free_bytes=free_bytes,
        no_streams=no_streams,
        package_damaged=package_damaged,
    )


def _package(
    package: bytes, limits: DecompressionLimits = DECOMPRESSION_LIMITS
) -> FailureReason | None:
    """Motivo del Rechazo_de_Origen según el lector ZIP de DOCX."""
    return classify_zip_verdict(check_zip_package(io.BytesIO(package), limits))


_TABLE_ROWS = [
    # Pillow: UnidentifiedImageError, OSError de imagen truncada, error del decodificador
    pytest.param(lambda: _pillow(b"no es una imagen"), R.DAMAGED, id="pillow-no-identificada"),
    pytest.param(lambda: _pillow(_png()[: len(_png()) // 2]), R.DAMAGED, id="pillow-truncada"),
    pytest.param(lambda: _pillow(_broken_png()), R.DAMAGED, id="pillow-decodificador"),
    # Pillow: DecompressionBombError
    pytest.param(lambda: _pillow(_bomb_png()), R.PIXEL_LIMIT, id="pillow-bomba"),
    # pypdfium2: FPDF_ERR_FORMAT, FPDF_ERR_FILE. FPDF_ERR_FILE aparece con un archivo sin permiso
    # de lectura, que no se puede armar si la prueba corre como root: la línea se copia
    pytest.param(
        lambda: _pypdfium2(b"%PDF-1.4\nno es un PDF"), R.DAMAGED, id="pypdfium2-err-format"
    ),
    pytest.param(
        lambda: _stderr(
            Tool.PYPDFIUM2,
            "Traceback (most recent call last):",
            "pypdfium2._helpers.misc.PdfiumError: Failed to load document "
            "(PDFium: File access error).",
        ),
        R.DAMAGED,
        id="pypdfium2-err-file",
    ),
    # pypdfium2: FPDF_ERR_PASSWORD
    pytest.param(lambda: _pypdfium2(_password_pdf()), R.PROTECTED_PDF, id="pypdfium2-err-password"),
    # Lector ZIP de DOCX: suma de Tamaños_Descomprimidos, cantidad de Entradas_ZIP o
    # Razón_de_Compresión por encima de los Límites_de_Descompresión
    pytest.param(
        lambda: _package(_docx(), DecompressionLimits(total_bytes=100)),
        R.DECOMPRESSION_LIMITS,
        id="zip-suma",
    ),
    pytest.param(
        lambda: _package(_docx(), DecompressionLimits(max_entries=1)),
        R.DECOMPRESSION_LIMITS,
        id="zip-entradas",
    ),
    pytest.param(
        lambda: _package(_docx(("word/media/image1.bin", bytes(200_000)))),
        R.DECOMPRESSION_LIMITS,
        id="zip-razon",
    ),
    # Lector ZIP de DOCX: directorio central ilegible, error de deflate o de CRC o método de
    # compresión no admitido, sin superar los Límites_de_Descompresión
    pytest.param(lambda: _package(_docx()[:-10]), R.DAMAGED, id="zip-directorio-central"),
    pytest.param(lambda: _package(_deflate_error_docx()), R.DAMAGED, id="zip-deflate"),
    pytest.param(lambda: _package(_crc_error_docx()), R.DAMAGED, id="zip-crc"),
    pytest.param(lambda: _package(_docx(compression=zipfile.ZIP_LZMA)), R.DAMAGED, id="zip-metodo"),
    # FFmpeg y ffprobe: "Invalid data found when processing input", "moov atom not found",
    # "EBML header parsing failed", código distinto de 0 sin flujos
    pytest.param(
        lambda: _stderr(
            Tool.FFMPEG,
            "[in#0 @ 0x600001b6c000] Error opening input: Invalid data found when processing input",
            f"Error opening input file {_SOURCE_PATH}.",
            "Error opening input files: Invalid data found when processing input",
        ),
        R.DAMAGED,
        id="ffmpeg-invalid-data",
    ),
    pytest.param(
        lambda: _stderr(
            Tool.FFPROBE, "[mov,mp4,m4a,3gp,3g2,mj2 @ 0x55d0c8a3e2c0] moov atom not found"
        ),
        R.DAMAGED,
        id="ffprobe-moov",
    ),
    pytest.param(
        lambda: _stderr(
            Tool.FFPROBE, "[matroska,webm @ 0x55d0c8a3e2c0] EBML header parsing failed"
        ),
        R.DAMAGED,
        id="ffprobe-ebml",
    ),
    pytest.param(
        lambda: _stderr(
            Tool.FFPROBE,
            "[mp3 @ 0x55d0c8a3e2c0] Failed to find two consecutive MPEG audio frames.",
            no_streams=True,
        ),
        R.DAMAGED,
        id="ffprobe-sin-flujos",
    ),
    # FFmpeg: flujo usado sin decodificador en la build (mensajes de FFmpeg 7 y de FFmpeg 5 y 6)
    pytest.param(
        lambda: _stderr(
            Tool.FFMPEG,
            "[vist#0:0/hevc @ 0x6000039b0000] Decoding requested, but no decoder found for: hevc",
        ),
        R.UNSUPPORTED_CODEC,
        id="ffmpeg-sin-decodificador",
    ),
    pytest.param(
        lambda: _stderr(Tool.FFMPEG, "Decoder (codec hevc) not found for input stream #0:0"),
        R.UNSUPPORTED_CODEC,
        id="ffmpeg-6-sin-decodificador",
    ),
    # LibreOffice: código distinto de 0 o PDF ausente con un paquete que no pasó la comprobación
    # estructural da archivo_danado; en otro caso, error_interno
    pytest.param(
        lambda: _stderr(
            Tool.LIBREOFFICE, "Error: source file could not be loaded", package_damaged=True
        ),
        R.DAMAGED,
        id="libreoffice-error-paquete-danado",
    ),
    pytest.param(
        lambda: _stderr(
            Tool.LIBREOFFICE,
            "Error: source file could not be loaded",
            returncode=0,
            package_damaged=True,
        ),
        R.DAMAGED,
        id="libreoffice-pdf-ausente-paquete-danado",
    ),
    pytest.param(
        lambda: _stderr(Tool.LIBREOFFICE, "Error: source file could not be loaded"),
        R.INTERNAL_ERROR,
        id="libreoffice-paquete-sano",
    ),
    # whisper-cli: "failed to load model", "failed to initialize whisper context"
    pytest.param(
        lambda: _stderr(
            Tool.WHISPER_CPP,
            f"whisper_init_from_file_with_params_no_state: loading model from '{_MODEL_PATH}'",
            "whisper_model_load: invalid model data (bad magic)",
            "whisper_init_with_params_no_state: failed to load model",
            "error: failed to initialize whisper context",
        ),
        R.MODEL_UNAVAILABLE,
        id="whisper-modelo-corrupto",
    ),
    pytest.param(
        lambda: _stderr(
            Tool.WHISPER_CPP,
            f"whisper_init_from_file_with_params_no_state: failed to open '{_MODEL_PATH}'",
            "error: failed to initialize whisper context",
        ),
        R.MODEL_UNAVAILABLE,
        id="whisper-modelo-ausente",
    ),
    # Cualquiera: ENOSPC en la salida, o espacio libre por debajo de 1 MB al fallar
    pytest.param(
        lambda: _stderr(
            Tool.FFMPEG,
            "[out#0/mp4 @ 0x600003a2c000] Error writing trailer: No space left on device",
        ),
        R.NO_SPACE,
        id="ffmpeg-enospc",
    ),
    pytest.param(
        lambda: _stderr(Tool.PILLOW, *"".join(traceback.format_exception(_enospc())).splitlines()),
        R.NO_SPACE,
        id="worker-enospc",
    ),
    pytest.param(lambda: classify_failure(exception=_enospc()), R.NO_SPACE, id="proceso-enospc"),
    pytest.param(
        lambda: _stderr(Tool.FFMPEG, "Conversion failed!", free_bytes=MIN_FREE_BYTES - 1),
        R.NO_SPACE,
        id="espacio-libre-bajo",
    ),
    # Cualquiera: terminado por el timeout del Gestor_de_Tareas
    pytest.param(
        lambda: _stderr(Tool.WHISPER_CPP, returncode=-signal.SIGKILL, timed_out=True),
        R.TIMED_OUT,
        id="timeout",
    ),
    # Cualquiera: señal (SIGSEGV, SIGKILL por RLIMIT_AS) u otra salida no clasificada
    pytest.param(
        lambda: _stderr(Tool.FFMPEG, returncode=-signal.SIGSEGV), R.INTERNAL_ERROR, id="sigsegv"
    ),
    pytest.param(
        lambda: _stderr(Tool.WHISPER_CPP, returncode=-signal.SIGKILL),
        R.INTERNAL_ERROR,
        id="sigkill",
    ),
    pytest.param(
        lambda: _stderr(
            Tool.WHISPER_CPP,
            "terminate called after throwing an instance of 'std::bad_alloc'",
            "  what():  std::bad_alloc",
            returncode=-signal.SIGABRT,
        ),
        R.INTERNAL_ERROR,
        id="bad-alloc",
    ),
    pytest.param(
        lambda: _stderr(
            Tool.FFMPEG, "[vost#0:0 @ 0x6000] Unknown encoder 'libx264'", "Conversion failed!"
        ),
        R.INTERNAL_ERROR,
        id="salida-no-clasificada",
    ),
]


@pytest.mark.req("16.2", "16.9", "16.10", "14.7", "5.9", "7.7", "8.7", "9.6", "10.8", "11.6")
@pytest.mark.parametrize(("classify", "expected"), _TABLE_ROWS)
def test_each_row_of_the_classification_table_gives_its_reason(
    classify: Callable[[], FailureReason | None],
    expected: FailureReason,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Como el inspector de imágenes, que fija el límite de Pillow en el Límite_de_Píxeles
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", _DEFAULT_PIXEL_LIMIT)
    assert classify() is expected
