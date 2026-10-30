"""Pruebas de src/converter/detection/zipcheck.py: Límites_de_Descompresión de DOCX con los
tamaños declarados y con los bytes producidos, corte temprano y entradas dañadas (Req 7.7, 16.2,
2.11).

Los paquetes se escriben con `zipfile` y se modifican campo por campo con las posiciones de
APPNOTE.TXT, para declarar tamaños distintos de los reales o dañar una entrada. Los límites se
escalan para que cada caso use pocos bytes; los valores reales se prueban con DOCX completos.
"""

from __future__ import annotations

import io
import struct
import zipfile
import zlib
from collections.abc import Callable

import pytest

from converter.detection.zipcheck import (
    DECOMPRESSION_LIMITS,
    DecompressionLimits,
    ExceededLimit,
    LimitVerdict,
    ZipEntryStat,
    check_zip_package,
)

_ZEROS = bytes(1_000_000)  # deflate lo comprime unas 1000 veces
_TEXT = b"<w:p><w:r><w:t>hola mundo</w:t></w:r></w:p>" * 20  # razón de deflate bien menor a 200

# Posiciones de los campos de un registro del directorio central
_CD_FLAGS, _CD_METHOD, _CD_CRC, _CD_COMPRESSED, _CD_SIZE = 8, 10, 16, 20, 24
_CD_HEADER_OFFSET = 42
# Posiciones de las cantidades de entradas en el registro de fin (sin comentario)
_END_SIZE = 22
_END_DISK_ENTRIES, _END_TOTAL_ENTRIES = 8, 10


def _zip(*entries: tuple[str, bytes, int], comment: bytes = b"") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data, method in entries:
            archive.writestr(zipfile.ZipInfo(name), data, compress_type=method)
        archive.comment = comment
    return buffer.getvalue()


def _central_records(data: bytes) -> list[int]:
    """Posición de cada registro del directorio central de un paquete sin comentario."""
    count, _, offset = struct.unpack_from("<HLL", data, len(data) - _END_SIZE + 10)
    positions = []
    position = offset
    for _ in range(count):
        positions.append(position)
        name_length, extra_length, comment_length = struct.unpack_from("<3H", data, position + 28)
        position += 46 + name_length + extra_length + comment_length
    return positions


def _central_field(data: bytes, index: int, field: int) -> int:
    fmt = "<H" if field in (_CD_FLAGS, _CD_METHOD) else "<L"
    value: int = struct.unpack_from(fmt, data, _central_records(data)[index] + field)[0]
    return value


def _patch_central(data: bytes, index: int, field: int, value: int) -> bytes:
    fmt = "<H" if field in (_CD_FLAGS, _CD_METHOD) else "<L"
    patched = bytearray(data)
    struct.pack_into(fmt, patched, _central_records(data)[index] + field, value)
    return bytes(patched)


def _patch_declared_entries(data: bytes, count: int) -> bytes:
    patched = bytearray(data)
    end = len(data) - _END_SIZE
    struct.pack_into("<HH", patched, end + _END_DISK_ENTRIES, count, count)
    return bytes(patched)


def _patch_bytes(data: bytes, position: int, new: bytes) -> bytes:
    return data[:position] + new + data[position + len(new) :]


def _data_position(data: bytes, index: int) -> int:
    """Posición de los datos comprimidos de una entrada, según su cabecera local."""
    header = _central_field(data, index, _CD_HEADER_OFFSET)
    name_length, extra_length = struct.unpack_from("<HH", data, header + 26)
    return header + 30 + int(name_length) + int(extra_length)


def _check(data: bytes, limits: DecompressionLimits = DECOMPRESSION_LIMITS) -> LimitVerdict:
    return check_zip_package(io.BytesIO(data), limits)


def _limits(**changes: int) -> DecompressionLimits:
    values = {"total_bytes": 10**12, "max_entries": 10**6, "max_ratio": 10**9} | changes
    return DecompressionLimits(**values)


# --- Valores y modelos ---


@pytest.mark.req("7.7")
def test_fixed_limits_are_the_values_of_d18() -> None:
    assert DECOMPRESSION_LIMITS.total_bytes == 512_000_000
    assert DECOMPRESSION_LIMITS.max_entries == 10_000
    assert DECOMPRESSION_LIMITS.max_ratio == 200
    assert DecompressionLimits() == DECOMPRESSION_LIMITS


@pytest.mark.parametrize("name", ["total_bytes", "max_entries", "max_ratio"])
def test_limits_reject_negative_values(name: str) -> None:
    with pytest.raises(ValueError, match=name):
        DecompressionLimits(**{name: -1})


@pytest.mark.req("7.7")
def test_uncompressed_size_is_the_largest_of_declared_and_produced() -> None:
    stat = ZipEntryStat(declared_size=10, compressed_size=5, produced_size=7, damaged=False)
    assert stat.uncompressed_size == 10
    stat = ZipEntryStat(declared_size=3, compressed_size=5, produced_size=7, damaged=False)
    assert stat.uncompressed_size == 7


# --- Paquetes válidos ---


@pytest.mark.req("7.7", "16.2")
def test_valid_package_is_measured_completely() -> None:
    data = _zip(
        ("[Content_Types].xml", _TEXT, zipfile.ZIP_DEFLATED),
        ("word/media/image1.png", bytes(range(256)) * 4, zipfile.ZIP_STORED),
        ("word/vacio.xml", b"", zipfile.ZIP_DEFLATED),
        ("word/", b"", zipfile.ZIP_STORED),
    )
    verdict = _check(data)
    assert verdict.exceeded is None
    assert not verdict.damaged
    sizes = [info.file_size for info in zipfile.ZipFile(io.BytesIO(data)).infolist()]
    assert [entry.declared_size for entry in verdict.entries] == sizes
    assert [entry.produced_size for entry in verdict.entries] == sizes
    assert not any(entry.damaged for entry in verdict.entries)
    assert verdict.decompressed_bytes == sum(sizes)


@pytest.mark.req("7.7")
def test_data_before_the_package_and_comment_are_supported() -> None:
    data = b"x" * 100 + _zip(("a.xml", _TEXT, zipfile.ZIP_DEFLATED), comment=b"PK comentario")
    assert zipfile.ZipFile(io.BytesIO(data)).read("a.xml") == _TEXT
    verdict = _check(data)
    assert verdict.exceeded is None
    assert not verdict.damaged
    assert verdict.decompressed_bytes == len(_TEXT)


def _zip64_package(content: bytes) -> bytes:
    """Paquete con una entrada cuyos tamaños y posición están en los registros ZIP64."""
    compressor = zlib.compressobj(6, zlib.DEFLATED, -15)
    compressed = compressor.compress(content) + compressor.flush()
    name = b"word/document.xml"
    crc = zlib.crc32(content)
    mark = 0xFFFFFFFF
    local_extra = struct.pack("<HHQQ", 1, 16, len(content), len(compressed))
    local = struct.pack(
        "<4s5H3L2H", b"PK\x03\x04", 45, 0, 8, 0, 0x21, crc, mark, mark, len(name), len(local_extra)
    )
    central_extra = struct.pack("<HH3Q", 1, 24, len(content), len(compressed), 0)
    central = struct.pack(
        "<4s6H3L5H2L",
        *(b"PK\x01\x02", 45, 45, 0, 8, 0, 0x21, crc, mark, mark),
        *(len(name), len(central_extra), 0, 0, 0, 0, mark),
    )
    central += name + central_extra
    central_offset = len(local) + len(name) + len(local_extra) + len(compressed)
    zip64_offset = central_offset + len(central)
    zip64_end = struct.pack(
        "<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, 1, 1, len(central), central_offset
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, zip64_offset, 1)
    end = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, mark, mark, 0)
    return local + name + local_extra + compressed + central + zip64_end + locator + end


@pytest.mark.req("7.7")
def test_zip64_records_are_read() -> None:
    data = _zip64_package(_TEXT)
    assert zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml") == _TEXT
    verdict = _check(data)
    assert verdict.exceeded is None
    assert not verdict.damaged
    assert [entry.produced_size for entry in verdict.entries] == [len(_TEXT)]


# --- Cantidad de Entradas_ZIP ---


@pytest.mark.req("7.7")
def test_entry_count_limit() -> None:
    data = _zip(*((f"{i}.xml", b"x", zipfile.ZIP_STORED) for i in range(3)))
    assert _check(data, _limits(max_entries=3)).exceeded is None
    verdict = _check(data, _limits(max_entries=2))
    assert verdict.exceeded is ExceededLimit.ENTRY_COUNT
    assert verdict.decompressed_bytes == 0


@pytest.mark.req("7.7")
def test_declared_entry_count_rejects_without_reading_the_directory() -> None:
    data = _zip(("a.xml", _TEXT, zipfile.ZIP_DEFLATED))
    # Directorio central ilegible: solo cuenta lo que declara el registro de fin
    data = _patch_bytes(data, _central_records(data)[0], b"XXXX")
    data = _patch_declared_entries(data, 5)
    verdict = _check(data, _limits(max_entries=4))
    assert verdict.exceeded is ExceededLimit.ENTRY_COUNT
    assert not verdict.damaged
    assert verdict.entries == ()


@pytest.mark.req("7.7", "16.2")
def test_more_records_than_declared_exceed_the_count_before_damage() -> None:
    data = _zip(*((f"{i}.xml", b"x", zipfile.ZIP_STORED) for i in range(5)))
    data = _patch_declared_entries(data, 3)
    assert _check(data, _limits(max_entries=3)).exceeded is ExceededLimit.ENTRY_COUNT
    # Por debajo del máximo, la cantidad distinta de la declarada es un directorio dañado
    verdict = _check(data, _limits(max_entries=10))
    assert verdict.exceeded is None
    assert verdict.damaged


# --- Suma de Tamaños_Descomprimidos ---


@pytest.mark.req("7.7")
def test_total_size_limit_with_declared_sizes() -> None:
    data = _zip(
        ("a.xml", _TEXT, zipfile.ZIP_DEFLATED), ("b.bin", bytes(range(200)), zipfile.ZIP_STORED)
    )
    total = len(_TEXT) + 200
    assert _check(data, _limits(total_bytes=total)).exceeded is None
    verdict = _check(data, _limits(total_bytes=total - 1))
    assert verdict.exceeded is ExceededLimit.TOTAL_SIZE
    assert verdict.decompressed_bytes == 0  # rechazo inmediato, sin descomprimir


@pytest.mark.req("7.7", "2.11")
def test_total_size_counts_produced_bytes_and_stops_early() -> None:
    data = _zip(("a.bin", _ZEROS, zipfile.ZIP_DEFLATED), ("b.bin", _ZEROS, zipfile.ZIP_DEFLATED))
    # El directorio central declara 0 bytes descomprimidos para las dos entradas
    data = _patch_central(_patch_central(data, 0, _CD_SIZE, 0), 1, _CD_SIZE, 0)
    limits = _limits(total_bytes=1_500_000)
    verdict = _check(data, limits)
    assert verdict.exceeded is ExceededLimit.TOTAL_SIZE
    assert verdict.entries[0].uncompressed_size == len(_ZEROS)
    assert verdict.entries[0].damaged  # produce más de lo que declara
    assert verdict.decompressed_bytes == limits.total_bytes + 1
    assert verdict.damaged


@pytest.mark.req("7.7", "2.11")
def test_stored_entry_is_cut_at_the_total_size() -> None:
    data = _zip(("a.bin", bytes(range(256)) * 400, zipfile.ZIP_STORED))
    data = _patch_central(data, 0, _CD_SIZE, 0)
    verdict = _check(data, _limits(total_bytes=50_000))
    assert verdict.exceeded is ExceededLimit.TOTAL_SIZE
    assert verdict.decompressed_bytes == 50_001


# --- Razón_de_Compresión ---


@pytest.mark.req("7.7")
def test_ratio_limit_with_declared_sizes() -> None:
    data = _zip(("a.bin", _ZEROS, zipfile.ZIP_DEFLATED))
    compressed = _central_field(data, 0, _CD_COMPRESSED)
    smallest_allowed = -(-len(_ZEROS) // compressed)  # menor razón entera que no se supera
    assert _check(data, _limits(max_ratio=smallest_allowed)).exceeded is None
    verdict = _check(data, _limits(max_ratio=smallest_allowed - 1))
    assert verdict.exceeded is ExceededLimit.COMPRESSION_RATIO
    assert verdict.decompressed_bytes == 0


@pytest.mark.req("7.7", "2.11")
def test_entry_producing_more_than_declared_is_cut_at_the_ratio() -> None:
    data = _zip(("a.bin", _ZEROS, zipfile.ZIP_DEFLATED))
    data = _patch_central(data, 0, _CD_SIZE, 0)
    compressed = _central_field(data, 0, _CD_COMPRESSED)
    assert 200 * compressed < len(_ZEROS)
    verdict = _check(data)
    assert verdict.exceeded is ExceededLimit.COMPRESSION_RATIO
    assert verdict.entries[0].produced_size == 200 * compressed + 1
    assert verdict.decompressed_bytes == 200 * compressed + 1


@pytest.mark.req("7.7")
def test_zero_compressed_size_has_infinite_ratio_and_empty_entry_ratio_zero() -> None:
    data = _zip(("vacio.xml", b"", zipfile.ZIP_STORED))
    assert _check(data, _limits(max_ratio=0)).exceeded is None
    declared_one = _patch_central(data, 0, _CD_SIZE, 1)
    verdict = _check(declared_one, _limits(max_ratio=10**9))
    assert verdict.exceeded is ExceededLimit.COMPRESSION_RATIO


# --- Entradas dañadas ---


def _bad_crc(data: bytes) -> bytes:
    return _patch_central(data, 0, _CD_CRC, _central_field(data, 0, _CD_CRC) ^ 1)


def _invalid_deflate(data: bytes) -> bytes:
    # Primer bloque final con tipo 3, reservado por deflate
    return _patch_bytes(data, _data_position(data, 0), b"\xff")


def _truncated_deflate(data: bytes) -> bytes:
    return _patch_central(data, 0, _CD_COMPRESSED, _central_field(data, 0, _CD_COMPRESSED) - 1)


def _data_after_deflate_end(data: bytes) -> bytes:
    return _patch_central(data, 0, _CD_COMPRESSED, _central_field(data, 0, _CD_COMPRESSED) + 1)


def _declared_larger_than_produced(data: bytes) -> bytes:
    return _patch_central(data, 0, _CD_SIZE, len(_TEXT) + 1)


def _unsupported_method(data: bytes) -> bytes:
    return _patch_central(data, 0, _CD_METHOD, zipfile.ZIP_BZIP2)


def _encrypted(data: bytes) -> bytes:
    return _patch_central(data, 0, _CD_FLAGS, _central_field(data, 0, _CD_FLAGS) | 1)


def _bad_local_header(data: bytes) -> bytes:
    return _patch_bytes(data, _central_field(data, 0, _CD_HEADER_OFFSET), b"XXXX")


@pytest.mark.req("16.2")
@pytest.mark.parametrize(
    "damage",
    [
        _bad_crc,
        _invalid_deflate,
        _truncated_deflate,
        _data_after_deflate_end,
        _declared_larger_than_produced,
        _unsupported_method,
        _encrypted,
        _bad_local_header,
    ],
)
def test_damaged_entry_does_not_stop_verification(damage: Callable[[bytes], bytes]) -> None:
    data = _zip(("a.xml", _TEXT, zipfile.ZIP_DEFLATED), ("b.xml", _TEXT, zipfile.ZIP_DEFLATED))
    verdict = _check(damage(data))
    assert verdict.exceeded is None
    assert verdict.damaged
    assert verdict.entries[0].damaged
    # La entrada siguiente se mide completa
    assert verdict.entries[1] == ZipEntryStat(
        declared_size=len(_TEXT),
        compressed_size=_central_field(data, 1, _CD_COMPRESSED),
        produced_size=len(_TEXT),
        damaged=False,
    )


@pytest.mark.req("7.7", "16.2")
def test_limit_after_a_damaged_entry_takes_priority() -> None:
    data = _zip(("a.xml", _TEXT, zipfile.ZIP_DEFLATED), ("b.bin", _ZEROS, zipfile.ZIP_DEFLATED))
    data = _patch_central(_bad_crc(data), 1, _CD_SIZE, 0)
    verdict = _check(data)
    assert verdict.exceeded is ExceededLimit.COMPRESSION_RATIO
    assert verdict.damaged
    assert verdict.entries[0].damaged


@pytest.mark.req("16.2")
def test_compressed_sizes_beyond_the_file_are_damaged_without_decompressing() -> None:
    data = _zip(("a.xml", _TEXT, zipfile.ZIP_DEFLATED))
    verdict = _check(_patch_central(data, 0, _CD_COMPRESSED, len(data) + 1))
    assert verdict.exceeded is None
    assert verdict.damaged
    assert verdict.decompressed_bytes == 0


_ONE_ENTRY = _zip(("a.xml", _TEXT, zipfile.ZIP_DEFLATED))


@pytest.mark.req("16.2")
@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"no es un paquete ZIP",
        _ONE_ENTRY[:-5],  # registro de fin truncado
        _patch_bytes(_ONE_ENTRY, _central_records(_ONE_ENTRY)[0], b"XXXX"),
    ],
    ids=["vacio", "texto", "fin_truncado", "firma_del_directorio"],
)
def test_unreadable_directory_is_damaged(data: bytes) -> None:
    assert _check(data) == LimitVerdict(exceeded=None, damaged=True, entries=())
