"""Pruebas de src/converter/detection/probes/: registro, descubrimiento y cada sonda (Req 2.1,
2.8, 2.10, 4.4, 7.2).

Los archivos se arman en código con `tests.format_samples`. Los casos mal formados comprueban
que cada sonda devuelve False sin lanzar excepciones y sin leer ni descomprimir más que su tope.
"""

from __future__ import annotations

import io
import time
import zipfile

import docx as python_docx
import pytest

from converter.detection.probes import (
    ProbeInput,
    UnknownProbeError,
    get_probe,
    register_probe,
    registered_probes,
    run_probe,
)
from converter.detection.probes import base as probes_base
from converter.detection.probes import ooxml_wordprocessing as ooxml
from converter.detection.probes.ebml_webm import ebml_doctype
from converter.detection.probes.isobmff_mp4 import ADMITTED_BRANDS, EXCLUDED_MAJOR_BRANDS
from converter.detection.probes.mpeg_audio import id3v2_size, layer3_frame_length
from converter.detection.text import TextKind, classify_text
from converter.registry.registry import SIGNATURE_WINDOW
from tests import format_samples as samples

INITIAL_PROBES = (
    "pdf_header",
    "ooxml_wordprocessing",
    "mpeg_audio",
    "isobmff_mp4",
    "ebml_webm",
    "docx_encrypted",
)


def _input(data: bytes, extension: str | None = None) -> ProbeInput:
    return ProbeInput.from_stream(io.BytesIO(data), extension)


def _probe(name: str, data: bytes, extension: str | None = None) -> bool:
    return run_probe(name, _input(data, extension))


# --- Registro y descubrimiento ---


@pytest.mark.req("4.4")
def test_package_import_discovers_the_initial_probes() -> None:
    assert set(INITIAL_PROBES) <= set(registered_probes())
    for name in INITIAL_PROBES:
        assert callable(get_probe(name))


@pytest.mark.req("4.4")
def test_register_probe_adds_a_probe_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probes_base, "_REGISTRY", dict(probes_base._REGISTRY))

    @register_probe("always")
    def always(source: ProbeInput) -> bool:
        return True

    assert get_probe("always") is always
    assert run_probe("always", _input(b"")) is True
    assert registered_probes()[-1] == "always"


def test_register_probe_rejects_duplicate_and_invalid_names() -> None:
    before = registered_probes()
    with pytest.raises(ValueError, match="pdf_header"):
        register_probe("pdf_header")(lambda source: True)
    for name in ("", " pdf"):
        with pytest.raises(ValueError):
            register_probe(name)
    assert registered_probes() == before


def test_unknown_probe_raises() -> None:
    with pytest.raises(UnknownProbeError) as error:
        run_probe("no_existe", _input(b"%PDF-"))
    assert error.value.name == "no_existe"


def test_probe_input_reads_the_head_and_beyond() -> None:
    data = bytes(range(256)) * 300  # 76 800 bytes, más que SIGNATURE_WINDOW
    source = _input(data, ".bin")
    assert source.head == data[:SIGNATURE_WINDOW]
    assert source.read(10, 4) == data[10:14]
    assert source.read(SIGNATURE_WINDOW - 2, 4) == data[SIGNATURE_WINDOW - 2 : SIGNATURE_WINDOW + 2]
    assert source.read(len(data) - 2, 10) == data[-2:]
    assert source.read(2**64, 4) == b""
    with pytest.raises(ValueError):
        source.read(-1, 4)


# --- pdf_header ---


@pytest.mark.req("2.1")
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (samples.PDF, True),
        (b"\x00" * 1000 + samples.PDF, True),
        (b"x" * 1019 + b"%PDF-1.4", True),  # el marcador termina en el byte 1024
        (b"x" * 1020 + b"%PDF-1.4", False),  # termina después de los primeros 1024 bytes
        (b"%PDF", False),
        (b"Hola %pdf-1.4", False),
    ],
)
def test_pdf_header(data: bytes, expected: bool) -> None:
    assert _probe("pdf_header", data) is expected


# --- mpeg_audio ---


def test_layer3_frame_length() -> None:
    assert layer3_frame_length(samples.MPEG1_128K) == samples.MPEG1_128K_LENGTH
    assert layer3_frame_length(samples.MPEG1_128K_PADDED) == samples.MPEG1_128K_LENGTH + 1
    assert layer3_frame_length(samples.MPEG2_64K) == samples.MPEG2_64K_LENGTH
    assert layer3_frame_length(samples.MPEG25_8K) == samples.MPEG25_8K_LENGTH


@pytest.mark.req("2.1")
@pytest.mark.parametrize(
    ("header", "length"),
    [
        (samples.MPEG1_128K, samples.MPEG1_128K_LENGTH),
        (samples.MPEG1_128K_PADDED, samples.MPEG1_128K_LENGTH + 1),
        (samples.MPEG2_64K, samples.MPEG2_64K_LENGTH),
        (samples.MPEG25_8K, samples.MPEG25_8K_LENGTH),
    ],
)
def test_mpeg_audio_two_consecutive_layer3_headers(header: bytes, length: int) -> None:
    two_frames = samples.mpeg_frame(header, length) * 2
    assert _probe("mpeg_audio", two_frames) is True
    # Solo hace falta la segunda cabecera, no su cuadro completo
    assert _probe("mpeg_audio", samples.mpeg_frame(header, length) + header) is True


@pytest.mark.req("2.1")
@pytest.mark.parametrize(
    "tag",
    [
        samples.id3v2_tag(100, major=3),
        samples.id3v2_tag(0, major=2),
        samples.id3v2_tag(1000, major=4, flags=0x10),  # con pie
        samples.id3v2_tag(200_000),  # termina mucho después de los primeros 64 KiB
    ],
)
def test_mpeg_audio_skips_id3v2_tag(tag: bytes) -> None:
    assert id3v2_size(tag) == len(tag)
    assert _probe("mpeg_audio", samples.mp3(tag=tag)) is True


@pytest.mark.req("2.1")
def test_mpeg_audio_rejects_windows_1252_text_starting_with_y_u_circumflex() -> None:
    text = ("ÿûPara el lunes: café, té y ñoquis. ¿Quién trae el pan?\r\n" * 30).encode("cp1252")
    assert classify_text([text]) is TextKind.WINDOWS_1252
    # Los primeros 4 bytes forman una cabecera Layer III válida: solo la segunda la descarta
    assert layer3_frame_length(text[:4]) is not None
    assert _probe("mpeg_audio", text) is False


@pytest.mark.req("2.1")
@pytest.mark.parametrize(
    "data",
    [
        samples.mpeg_frame(samples.MPEG1_128K, samples.MPEG1_128K_LENGTH),  # un solo cuadro
        samples.mpeg_frame(samples.MPEG1_128K, samples.MPEG1_128K_LENGTH) + b"\xff",
        samples.MPEG1_128K,
        samples.mpeg_frame(samples.MPEG1_128K, samples.MPEG1_128K_LENGTH + 1) * 2,  # desfasado
        samples.mpeg_frame(bytes.fromhex("FFFD9000"), 417) * 2,  # Layer II
        samples.mpeg_frame(bytes.fromhex("FFFF9000"), 417) * 2,  # Layer I
        samples.mpeg_frame(bytes.fromhex("FFEB9000"), 417) * 2,  # versión reservada
        samples.mpeg_frame(bytes.fromhex("FFFB0000"), 417) * 2,  # formato libre
        samples.mpeg_frame(bytes.fromhex("FFFBF000"), 417) * 2,  # tasa de bits inválida
        samples.mpeg_frame(bytes.fromhex("FFFB9C00"), 417) * 2,  # frecuencia reservada
        b"ID3\x05\x00\x00\x00\x00\x00\x00" + samples.mp3(),  # versión de ID3v2 inválida
        b"ID3\x04\x00\x00\x00\x00\x80\x00" + samples.mp3(),  # tamaño no "syncsafe"
        samples.id3v2_tag(100),  # etiqueta sin audio
        samples.id3v2_tag(100) + b"\x00" + samples.mp3(),  # el audio no empieza tras la etiqueta
        b"ID3",
        b"",
    ],
)
def test_mpeg_audio_rejects_invalid_content(data: bytes) -> None:
    assert _probe("mpeg_audio", data) is False


# --- isobmff_mp4 ---


@pytest.mark.req("2.1")
@pytest.mark.parametrize("brand", sorted(ADMITTED_BRANDS))
def test_isobmff_mp4_admitted_major_brand(brand: bytes) -> None:
    assert _probe("isobmff_mp4", samples.mp4(brand, b"abcd")) is True


@pytest.mark.req("2.1")
def test_isobmff_mp4_admitted_compatible_brand() -> None:
    assert _probe("isobmff_mp4", samples.mp4(b"3gp4", b"3gp4", b"mp42")) is True
    assert _probe("isobmff_mp4", samples.mp4(b"XAVC", b"XAVC", b"iso6")) is True


@pytest.mark.req("2.1")
@pytest.mark.parametrize("brand", sorted(EXCLUDED_MAJOR_BRANDS))
def test_isobmff_mp4_excluded_major_brand(brand: bytes) -> None:
    # Las marcas compatibles admitidas no alcanzan si la principal está excluida
    assert _probe("isobmff_mp4", samples.mp4(brand, b"isom", b"mp42", b"M4V ")) is False


@pytest.mark.req("2.1")
def test_isobmff_mp4_large_size_box() -> None:
    body = b"mp42" + bytes(4) + b"mp42isom"
    box = (1).to_bytes(4, "big") + b"ftyp" + (16 + len(body)).to_bytes(8, "big") + body
    assert _probe("isobmff_mp4", box + b"\x00\x00\x00\x08mdat") is True


@pytest.mark.parametrize(
    "data",
    [
        samples.mp4(b"abcd", b"efgh", b"qt  "),  # ninguna marca admitida
        samples.ftyp(b"isom")[:-1],  # caja truncada
        (12).to_bytes(4, "big") + b"ftypisom",  # caja menor que marca y versión
        (0).to_bytes(4, "big") + b"ftypisom" + bytes(8),  # tamaño 0: hasta el final
        (1).to_bytes(4, "big") + b"ftyp" + (8).to_bytes(8, "big"),  # tamaño de 64 bits inválido
        b"\x00\x00\x00\x18moovisom\x00\x00\x00\x00isomiso2",  # otra caja en el comienzo
        b"\x00\x00\x00\x18",
        b"",
    ],
)
def test_isobmff_mp4_rejects_invalid_content(data: bytes) -> None:
    assert _probe("isobmff_mp4", data) is False


# --- ebml_webm ---


@pytest.mark.req("2.1")
@pytest.mark.parametrize(
    "data",
    [
        samples.webm(),
        samples.webm(b"webm\x00\x00"),  # relleno nulo admitido por EBML
        samples.ebml_header(size=b"\x01" + (31).to_bytes(7, "big")),  # tamaño en 8 bytes
        samples.ebml_header(size=b"\xff"),  # tamaño desconocido: se recorre hasta el final
    ],
)
def test_ebml_webm(data: bytes) -> None:
    assert _probe("ebml_webm", data) is True


@pytest.mark.req("2.1")
@pytest.mark.parametrize(
    "data",
    [
        samples.webm(b"matroska"),
        samples.webm(b"WEBM"),
        samples.ebml_header()[:-14],  # cortada dentro de DocType
        samples.ebml_header(size=b"\x8a"),  # la cabecera termina antes de DocType
        samples.ebml_header(size=b"\x00"),  # tamaño de más de 8 bytes
        b"\x1a\x45\xdf\xa3",
        b"\x1a\x45\xdf\xa3\x84\x00\x00\x00\x00",  # identificador de hijo inválido
        b"\x1a\x45\xdf\xa2\x80",
        b"",
    ],
)
def test_ebml_webm_rejects_other_content(data: bytes) -> None:
    assert _probe("ebml_webm", data) is False


def test_ebml_doctype_reads_the_doctype() -> None:
    assert ebml_doctype(samples.webm(b"matroska")) == b"matroska"


# --- ooxml_wordprocessing ---


def _python_docx() -> bytes:
    buffer = io.BytesIO()
    document = python_docx.Document()
    document.add_paragraph("Hola")
    document.save(buffer)
    return buffer.getvalue()


@pytest.mark.req("2.1")
def test_ooxml_wordprocessing_document_written_by_python_docx() -> None:
    assert _probe("ooxml_wordprocessing", _python_docx()) is True


@pytest.mark.req("2.1")
def test_ooxml_wordprocessing_content_types_anywhere_and_in_any_case() -> None:
    others = [(f"word/media/image{i}.png", samples.PNG) for i in range(30)]
    late = samples.zip_package(
        *others,
        (
            "[Content_Types].xml",
            samples.content_types(samples.SPREADSHEET_MAIN, samples.WORDPROCESSING_MAIN),
        ),
    )
    assert _probe("ooxml_wordprocessing", late) is True
    upper = samples.docx(content_types_name="[CONTENT_TYPES].XML")
    assert _probe("ooxml_wordprocessing", upper) is True
    stored = samples.zip_package(
        ("[Content_Types].xml", samples.content_types(samples.WORDPROCESSING_MAIN)),
        compression=zipfile.ZIP_STORED,
    )
    assert _probe("ooxml_wordprocessing", stored) is True


@pytest.mark.req("2.1")
@pytest.mark.parametrize(
    "content_type",
    [samples.SPREADSHEET_MAIN, samples.WORD_TEMPLATE_MAIN, samples.WORD_MACRO_MAIN],
)
def test_ooxml_wordprocessing_rejects_other_ooxml_documents(content_type: str) -> None:
    package = samples.zip_package(("[Content_Types].xml", samples.content_types(content_type)))
    assert _probe("ooxml_wordprocessing", package) is False


def _patch(data: bytes, position: int, value: bytes) -> bytes:
    return data[:position] + value + data[position + len(value) :]


def _encrypted_flag(package: bytes) -> bytes:
    """Marca como cifrada la primera entrada en su cabecera local y en el directorio central."""
    central = package.index(b"PK\x01\x02")
    return _patch(_patch(package, 6, b"\x01\x00"), central + 8, b"\x01\x00")


def _broken_deflate(package: bytes) -> bytes:
    """Reemplaza el primer byte de datos de la primera entrada por un bloque deflate reservado."""
    name_length = int.from_bytes(package[26:28], "little")
    extra_length = int.from_bytes(package[28:30], "little")
    return _patch(package, 30 + name_length + extra_length, b"\x07")


@pytest.mark.parametrize(
    "data",
    [
        samples.zip_package(("word/document.xml", b"<w:document/>")),  # sin [Content_Types].xml
        samples.zip_package(("[Content_Types].xml", b"<Types><Override")),  # XML mal formado
        samples.zip_package(("[Content_Types].xml", b"")),
        samples.zip_package(  # otro elemento raíz
            (
                "[Content_Types].xml",
                samples.content_types(samples.WORDPROCESSING_MAIN)
                .replace(b"<Types", b"<Tipos")
                .replace(b"</Types>", b"</Tipos>"),
            )
        ),
        samples.zip_package(  # entidades en una DTD: OPC no la admite
            (
                "[Content_Types].xml",
                samples.content_types(
                    samples.WORDPROCESSING_MAIN,
                    prolog='<!DOCTYPE Types [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>',
                ),
            )
        ),
        _encrypted_flag(samples.docx()),
        _broken_deflate(samples.docx()),
        samples.docx()[:-22],  # sin registro de fin del directorio central
        samples.docx()[:200],
        b"PK\x03\x04",
        b"MZ" + samples.docx(),  # datos antepuestos: no empieza con la cabecera local
        samples.PDF,
    ],
)
def test_ooxml_wordprocessing_rejects_malformed_packages(data: bytes) -> None:
    assert _probe("ooxml_wordprocessing", data) is False


@pytest.mark.req("2.1")
def test_ooxml_wordprocessing_caps_content_types_decompression() -> None:
    # Unos 4 KB comprimidos que se expanden a más de 4 MB
    bomb = samples.content_types(samples.WORDPROCESSING_MAIN).replace(
        b"<Types ", b"<Types " + b" " * (4 * ooxml.MAX_CONTENT_TYPES_BYTES), 1
    )
    package = samples.zip_package(("[Content_Types].xml", bomb))
    assert len(package) < 64 * 1024
    started = time.monotonic()
    assert _probe("ooxml_wordprocessing", package) is False
    assert time.monotonic() - started < 1
    # Sin compresión, una entrada más grande que el tope no se lee
    stored = samples.zip_package(("[Content_Types].xml", bomb), compression=zipfile.ZIP_STORED)
    assert _probe("ooxml_wordprocessing", stored) is False


@pytest.mark.req("2.1")
def test_ooxml_wordprocessing_scans_a_bounded_number_of_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    others = [(f"word/media/image{i}.png", b"") for i in range(5)]
    package = samples.zip_package(
        *others, ("[Content_Types].xml", samples.content_types(samples.WORDPROCESSING_MAIN))
    )
    monkeypatch.setattr(ooxml, "MAX_SCANNED_ENTRIES", 6)
    assert _probe("ooxml_wordprocessing", package) is True
    monkeypatch.setattr(ooxml, "MAX_SCANNED_ENTRIES", 5)
    assert _probe("ooxml_wordprocessing", package) is False


# --- docx_encrypted ---


@pytest.mark.req("2.10", "7.2")
@pytest.mark.parametrize("extension", [".docx", ".DOCX", ".Docx"])
def test_docx_encrypted_with_docx_extension(extension: str) -> None:
    stream = io.BytesIO(samples.encrypted_docx())
    assert run_probe("docx_encrypted", ProbeInput.from_stream(stream, extension)) is True
    assert not stream.closed  # olefile no cierra el archivo que recibe


@pytest.mark.req("2.10", "7.2")
@pytest.mark.parametrize("extension", [None, ".doc", ".zip", ".docx.bin", ".docm"])
def test_docx_encrypted_needs_the_docx_extension(extension: str | None) -> None:
    assert _probe("docx_encrypted", samples.encrypted_docx(), extension) is False


@pytest.mark.req("2.10")
@pytest.mark.parametrize(
    "data",
    [
        samples.ole_file(("EncryptionInfo", b"\x04\x00\x04\x00")),
        samples.ole_file(("EncryptedPackage", b"\x00" * 8)),
        samples.ole_file(("WordDocument", b"\xec\xa5"), ("1Table", b"")),  # .doc sin cifrar
        samples.ole_file(("EncryptionInfo", b"\x04\x00"), storages=("EncryptedPackage",)),
        samples.ole_file(),
        _python_docx(),  # DOCX sin contraseña de apertura
        samples.OLE_SIGNATURE + bytes(1000),  # cabecera OLE inválida
        samples.encrypted_docx()[:700],  # truncado dentro del sector de la FAT
        samples.OLE_SIGNATURE,
    ],
)
def test_docx_encrypted_rejects_other_content(data: bytes) -> None:
    assert _probe("docx_encrypted", data, ".docx") is False


@pytest.mark.req("2.10")
def test_docx_encrypted_bounds_reading_of_a_hostile_ole_file() -> None:
    # Sin el tope de lectura, olefile lee este archivo de 1536 bytes sin terminar
    started = time.monotonic()
    assert _probe("docx_encrypted", samples.ole_circular_difat(), ".docx") is False
    assert time.monotonic() - started < 2


# --- Independencia de la extensión ---


@pytest.mark.req("2.8")
@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("pdf_header", samples.PDF),
        ("ooxml_wordprocessing", samples.docx()),
        ("mpeg_audio", samples.mp3()),
        ("isobmff_mp4", samples.mp4()),
        ("ebml_webm", samples.webm()),
    ],
)
@pytest.mark.parametrize("extension", [None, ".txt", ".png", ".DOCX", ".mp3"])
def test_signature_probes_ignore_the_extension(
    name: str, data: bytes, extension: str | None
) -> None:
    assert _probe(name, data, extension) is True
