"""Límites_de_Descompresión de DOCX (Req 7.7, 16.1, 16.2, 2.11; D18, S13).

Un DOCX es un paquete ZIP. Antes de mostrar los Formatos_de_Destino, el inspector `docx` comprueba
con `check_zip_package` que el paquete no supere los Límites_de_Descompresión: una suma de
Tamaños_Descomprimidos de hasta 512 000 000 bytes, hasta 10 000 Entradas_ZIP y una
Razón_de_Compresión de hasta 200 en cada Entrada_ZIP. Esos valores son fijos y no se configuran
(`DECOMPRESSION_LIMITS`); las pruebas usan otros con `DecompressionLimits`.

La verificación sigue este orden:

1. Registro de fin del directorio central, con ZIP64. Si declara más Entradas_ZIP que el máximo,
   el rechazo es inmediato y los registros del directorio central no se leen.
2. Registros del directorio central, como mucho el máximo más uno. El rechazo es inmediato si hay
   más Entradas_ZIP que el máximo, si la suma de los tamaños descomprimidos declarados supera el
   límite o si alguna entrada declara una razón mayor al máximo.
3. Medición real. `zipfile` deja de leer al alcanzar el tamaño declarado, así que no sirve para
   medir una entrada que produce más bytes de los que declara. `ZipEntryReader.measure` lee los
   datos de cada entrada desde su cabecera local, con el tamaño comprimido del directorio
   central, y los descomprime en bloques con `zlib.decompressobj(-15)` descartando la salida.
   Solo admite los métodos sin compresión y deflate, los que producen Word y LibreOffice.

Definiciones del glosario, en enteros: el Tamaño_Descomprimido es el mayor entre el declarado y
el producido, y la razón supera el máximo si `tamaño > max_ratio * comprimido`. Así, una entrada
con tamaño comprimido 0 y Tamaño_Descomprimido mayor que 0 tiene razón infinita, y una entrada
vacía tiene razón 0.

Corte temprano. Cada entrada se descomprime hasta un presupuesto: lo que le falta para superar su
razón máxima o la suma, contando las entradas no medidas por su tamaño declarado. Al pasarlo por un
byte, la verificación entera se detiene con el límite superado. Por eso los bytes descomprimidos
durante la verificación nunca superan `total_bytes + 1`, y el trabajo máximo es descomprimir unos
512 MB, que con deflate tarda pocos segundos y queda dentro del timeout del inspector (Req 2.11).
Además, si la suma de los tamaños comprimidos supera el tamaño del archivo, las entradas se
solapan o declaran datos que no existen; el paquete queda dañado sin descomprimir nada, porque
de otro modo un mismo bloque de datos podría leerse miles de veces.

Daño. Una entrada queda dañada por un error de deflate o de CRC, un flujo deflate incompleto o con
datos después de su final, un tamaño producido distinto del declarado, una cabecera local que no
coincide con el directorio central, un archivo que termina antes que los datos de la entrada,
cifrado o un método no admitido. La verificación sigue con las demás entradas. Un directorio
central ilegible deja el paquete dañado sin entradas medidas.

Resultado. `LimitVerdict.exceeded` tiene prioridad sobre `LimitVerdict.damaged`: si se supera un
límite, el rechazo es `docx_excede_limites_descompresion` aunque además haya daño; si no y hubo
daño, es `archivo_danado` (Req 16.2). El veredicto no incluye nombres ni contenido de las entradas,
para que pueda ir al Log_de_Eventos (Req 16.8).

La lectura del directorio central imita a `zipfile` (búsqueda del registro de fin, ZIP64, datos
antepuestos al paquete y campo extra ZIP64) para medir las mismas entradas que leen después
`zipfile` y LibreOffice.
"""

from __future__ import annotations

import os
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import islice
from typing import BinaryIO

READ_BLOCK = 64 * 1024  # bytes comprimidos leídos por vez
OUTPUT_BLOCK = 1024 * 1024  # máximo de bytes descomprimidos por llamada a zlib

STORED = 0  # método sin compresión
DEFLATED = 8  # método deflate

_LOCAL_SIGNATURE = b"PK\x03\x04"
_CENTRAL_SIGNATURE = b"PK\x01\x02"
_END_SIGNATURE = b"PK\x05\x06"
_ZIP64_END_SIGNATURE = b"PK\x06\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"

# Estructuras de APPNOTE.TXT, en little endian
_LOCAL = struct.Struct("<4s5H3L2H")  # 30 bytes
_CENTRAL = struct.Struct("<4s6H3L5H2L")  # 46 bytes
_END = struct.Struct("<4s4H2LH")  # 22 bytes
_ZIP64_LOCATOR = struct.Struct("<4sLQL")  # 20 bytes
_ZIP64_END = struct.Struct("<4sQ2H2L4Q")  # 56 bytes, sin datos extensibles
_EXTRA_HEADER = struct.Struct("<HH")

_MAX_COMMENT = 0xFFFF
_ZIP64_EXTRA_ID = 0x0001
_ZIP64_MARK = 0xFFFFFFFF  # valor de 32 bits que remite al campo extra ZIP64

# Cifrado (bit 0), datos parcheados (bit 5) y cifrado fuerte (bit 6): no se pueden medir
_UNSUPPORTED_FLAGS = 0x0001 | 0x0020 | 0x0040


class ZipFormatError(ValueError):
    """El registro de fin o el directorio central del paquete ZIP no se pueden leer."""


class ExceededLimit(StrEnum):
    """Límite de los Límites_de_Descompresión que supera un paquete."""

    ENTRY_COUNT = "cantidad_de_entradas"
    TOTAL_SIZE = "suma_de_tamaños_descomprimidos"
    COMPRESSION_RATIO = "razón_de_compresión"


@dataclass(frozen=True, slots=True, kw_only=True)
class DecompressionLimits:
    """Límites_de_Descompresión. Los predeterminados son los fijos de D18."""

    total_bytes: int = 512_000_000  # suma máxima de Tamaños_Descomprimidos
    max_entries: int = 10_000  # cantidad máxima de Entradas_ZIP
    max_ratio: int = 200  # Razón_de_Compresión máxima de cada Entrada_ZIP

    def __post_init__(self) -> None:
        for name in ("total_bytes", "max_entries", "max_ratio"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} no puede ser negativo: {getattr(self, name)}")


DECOMPRESSION_LIMITS = DecompressionLimits()


@dataclass(frozen=True, slots=True, kw_only=True)
class ZipEntryStat:
    """Medición de una Entrada_ZIP."""

    declared_size: int  # tamaño descomprimido declarado en el directorio central
    compressed_size: int  # tamaño comprimido declarado en el directorio central
    produced_size: int  # bytes producidos al descomprimir, hasta donde se midió (0 sin medir)
    damaged: bool  # error de deflate o de CRC, u otro daño de la entrada al descomprimirla

    @property
    def uncompressed_size(self) -> int:
        """Tamaño_Descomprimido: el mayor entre el declarado y el producido."""
        return max(self.declared_size, self.produced_size)


@dataclass(frozen=True, slots=True, kw_only=True)
class LimitVerdict:
    """Resultado de la verificación de un paquete ZIP."""

    exceeded: ExceededLimit | None  # primer límite detectado como superado; None si ninguno
    damaged: bool  # directorio central ilegible o alguna Entrada_ZIP dañada
    entries: tuple[ZipEntryStat, ...]  # entradas leídas del directorio central, en orden

    @property
    def decompressed_bytes(self) -> int:
        """Bytes descomprimidos durante la verificación; nunca más que el límite de suma + 1."""
        return sum(entry.produced_size for entry in self.entries)


def _exceeds_ratio(size: int, compressed_size: int, max_ratio: int) -> bool:
    # size / compressed_size > max_ratio sin divisiones: con compressed_size 0 la razón es infinita
    # si size > 0 y 0 si la entrada está vacía
    return size > max_ratio * compressed_size


@dataclass(frozen=True, slots=True, kw_only=True)
class ZipEntryReader:
    """Entrada_ZIP según el directorio central, lista para medirse desde su cabecera local."""

    source: BinaryIO = field(repr=False, compare=False)
    header_offset: int  # posición de la cabecera local en `source`
    method: int
    flags: int
    crc: int
    declared_size: int
    compressed_size: int
    name_length: int
    name_crc: int  # CRC-32 del nombre, para compararlo con la cabecera local sin guardarlo

    def unmeasured(self) -> ZipEntryStat:
        """Estadística con los tamaños declarados, antes de descomprimir."""
        return self._stat(0, damaged=False)

    def measure(self, budget: int) -> ZipEntryStat:
        """Descomprime la entrada descartando la salida, hasta el final o hasta producir
        `budget + 1` bytes.

        Si `produced_size` supera `budget`, la medición se cortó ahí y no se comprobó el CRC.
        """
        if budget < 0:
            raise ValueError(f"budget no puede ser negativo: {budget}")
        if self.flags & _UNSUPPORTED_FLAGS or self.method not in (STORED, DEFLATED):
            return self._stat(0, damaged=True)
        data_offset = self._data_offset()
        if data_offset is None:
            return self._stat(0, damaged=True)
        produced, damaged = self._inflate(data_offset, budget)
        return self._stat(produced, damaged=damaged)

    def _stat(self, produced: int, *, damaged: bool) -> ZipEntryStat:
        return ZipEntryStat(
            declared_size=self.declared_size,
            compressed_size=self.compressed_size,
            produced_size=produced,
            damaged=damaged,
        )

    def _data_offset(self) -> int | None:
        """Posición de los datos según la cabecera local; None si no coincide con el directorio."""
        self.source.seek(self.header_offset)
        header = self.source.read(_LOCAL.size)
        if len(header) < _LOCAL.size:
            return None
        signature, *_, name_length, extra_length = _LOCAL.unpack(header)
        if signature != _LOCAL_SIGNATURE or name_length != self.name_length:
            return None
        name = self.source.read(name_length)
        if len(name) < name_length or zlib.crc32(name) != self.name_crc:
            return None
        return self.header_offset + _LOCAL.size + self.name_length + int(extra_length)

    def _inflate(self, data_offset: int, budget: int) -> tuple[int, bool]:
        """Devuelve los bytes producidos y si la entrada está dañada."""
        source = self.source
        source.seek(data_offset)
        inflater = zlib.decompressobj(-15) if self.method == DEFLATED else None
        produced = 0
        crc = 0
        left = self.compressed_size
        while left > 0:
            want = min(READ_BLOCK, left)
            if inflater is None:
                # Sin compresión, cada byte leído es un byte producido
                want = min(want, budget + 1 - produced)
            chunk = source.read(want)
            if not chunk:
                return produced, True  # el archivo termina antes que los datos de la entrada
            left -= len(chunk)
            if inflater is None:
                produced += len(chunk)
                if produced > budget:
                    return produced, False
                crc = zlib.crc32(chunk, crc)
                continue
            data = chunk
            while True:
                # Con salida acotada, un bloque muy comprimido no se expande de una vez
                limit = min(OUTPUT_BLOCK, budget + 1 - produced)
                try:
                    out = inflater.decompress(data, limit)
                except zlib.error:
                    return produced, True
                produced += len(out)
                if produced > budget:
                    return produced, False
                crc = zlib.crc32(out, crc)
                data = inflater.unconsumed_tail
                # Una salida igual a `limit` puede dejar bytes pendientes en zlib aunque no quede
                # entrada sin consumir: se vuelve a llamar hasta que la salida sea menor
                if inflater.eof or (not data and len(out) < limit):
                    break
            if inflater.eof:
                if inflater.unused_data or left:
                    return produced, True  # datos después del final del flujo deflate
                break
        if inflater is not None and self.compressed_size and not inflater.eof:
            return produced, True  # flujo deflate incompleto
        return produced, crc != self.crc or produced != self.declared_size


@dataclass(frozen=True, slots=True, kw_only=True)
class CentralDirectory:
    """Directorio central según el registro de fin; sus registros se leen con `entries`."""

    source: BinaryIO = field(repr=False, compare=False)
    source_size: int  # tamaño del archivo
    declared_entries: int  # cantidad de Entradas_ZIP que declara el registro de fin
    end_position: int  # posición del registro de fin (el ZIP64 si lo hay)
    size: int  # tamaño declarado del directorio central
    offset: int  # posición declarada del directorio central, sin datos antepuestos

    def entries(self) -> Iterator[ZipEntryReader]:
        """Lee los registros en orden.

        Lanza `ZipFormatError` si el directorio está fuera del archivo, si un registro es ilegible
        o, al terminar, si la cantidad leída difiere de `declared_entries`.
        """
        start = self.end_position - self.size
        # Datos antepuestos al paquete (base > 0) desplazan todas las posiciones, como en zipfile
        base = start - self.offset
        if start < 0 or base < 0:
            raise ZipFormatError("posición del directorio central fuera del archivo")
        source = self.source
        position, end = start, self.end_position
        count = 0
        while position < end:
            if end - position < _CENTRAL.size:
                raise ZipFormatError("registro del directorio central truncado")
            source.seek(position)
            header = source.read(_CENTRAL.size)
            if len(header) < _CENTRAL.size:
                raise ZipFormatError("registro del directorio central truncado")
            (
                signature,
                _made_by,
                _needed,
                flags,
                method,
                _time,
                _date,
                crc,
                compressed_size,
                size,
                name_length,
                extra_length,
                comment_length,
                _disk,
                _internal,
                _external,
                header_offset,
            ) = _CENTRAL.unpack(header)
            if signature != _CENTRAL_SIGNATURE:
                raise ZipFormatError("firma inválida en el directorio central")
            record_end = position + _CENTRAL.size + name_length + extra_length + comment_length
            if record_end > end:
                raise ZipFormatError("registro del directorio central truncado")
            name = source.read(name_length)
            extra = source.read(extra_length)
            if len(name) < name_length or len(extra) < extra_length:
                raise ZipFormatError("registro del directorio central truncado")
            size, compressed_size, header_offset = _apply_zip64_extra(
                extra, size, compressed_size, header_offset
            )
            count += 1
            yield ZipEntryReader(
                source=source,
                header_offset=base + header_offset,
                method=method,
                flags=flags,
                crc=crc,
                declared_size=size,
                compressed_size=compressed_size,
                name_length=name_length,
                name_crc=zlib.crc32(name),
            )
            position = record_end
        if count != self.declared_entries:
            raise ZipFormatError(
                f"el registro de fin declara {self.declared_entries} entradas y hay {count}"
            )


def _apply_zip64_extra(
    extra: bytes, size: int, compressed_size: int, header_offset: int
) -> tuple[int, int, int]:
    """Reemplaza los valores de 32 bits marcados con los del campo extra ZIP64, como zipfile."""
    position = 0
    while len(extra) - position >= _EXTRA_HEADER.size:
        kind, length = _EXTRA_HEADER.unpack_from(extra, position)
        start = position + _EXTRA_HEADER.size
        stop = start + length
        if stop > len(extra):
            raise ZipFormatError("campo extra inválido en el directorio central")
        if kind == _ZIP64_EXTRA_ID:
            values = iter(struct.unpack_from(f"<{length // 8}Q", extra, start))
            try:
                if size == _ZIP64_MARK:
                    size = int(next(values))
                if compressed_size == _ZIP64_MARK:
                    compressed_size = int(next(values))
                if header_offset == _ZIP64_MARK:
                    header_offset = int(next(values))
            except StopIteration:
                raise ZipFormatError("campo extra ZIP64 incompleto") from None
        position = stop
    return size, compressed_size, header_offset


def _read_at(source: BinaryIO, position: int, size: int) -> bytes:
    source.seek(position)
    return source.read(size)


def _find_end_record(source: BinaryIO, source_size: int) -> tuple[int, int, int, int]:
    """Posición del registro de fin, entradas declaradas, tamaño y posición del directorio."""
    window = min(source_size, _END.size + _MAX_COMMENT)
    tail = _read_at(source, source_size - window, window)
    position = len(tail) - _END.size
    if position < 0:
        raise ZipFormatError("archivo demasiado corto para ser un paquete ZIP")
    if not (tail.startswith(_END_SIGNATURE, position) and tail.endswith(b"\x00\x00")):
        # Hay un comentario al final: vale la última firma, como en zipfile
        position = tail.rfind(_END_SIGNATURE)
        if position < 0 or position + _END.size > len(tail):
            raise ZipFormatError("no se encontró el registro de fin del directorio central")
    _, _, _, _, entries, size, offset, _ = _END.unpack_from(tail, position)
    return source_size - window + position, int(entries), int(size), int(offset)


def _read_zip64_end(source: BinaryIO, end_position: int) -> tuple[int, int, int, int] | None:
    """Datos del registro de fin ZIP64 si hay localizador, con las mismas reglas que zipfile."""
    locator_position = end_position - _ZIP64_LOCATOR.size
    if locator_position < 0:
        return None
    locator = _read_at(source, locator_position, _ZIP64_LOCATOR.size)
    signature, disk, record_offset, disks = _ZIP64_LOCATOR.unpack(locator)
    if signature != _ZIP64_LOCATOR_SIGNATURE:
        return None
    if disk != 0 or disks > 1:
        raise ZipFormatError("paquete ZIP repartido en varios discos")
    # Sin datos extensibles, el registro ZIP64 termina justo antes del localizador
    slot = locator_position - _ZIP64_END.size
    if record_offset > slot:
        raise ZipFormatError("localizador ZIP64 inválido")
    record_position = int(record_offset)
    record = _read_at(source, record_position, _ZIP64_END.size)
    if not record.startswith(_ZIP64_END_SIGNATURE) and record_position != slot:
        # Con datos antepuestos la posición del localizador es relativa: se prueba junto a él
        record_position = slot
        record = _read_at(source, record_position, _ZIP64_END.size)
    if len(record) < _ZIP64_END.size or not record.startswith(_ZIP64_END_SIGNATURE):
        raise ZipFormatError("no se encontró el registro de fin ZIP64")
    _, record_size, _, _, _, _, _, entries, size, offset = _ZIP64_END.unpack(record)
    extensible = slot - record_position
    if offset + size != record_offset or record_size + 12 != _ZIP64_END.size + extensible:
        raise ZipFormatError("registro de fin ZIP64 inválido")
    return record_position, int(entries), int(size), int(offset)


def read_central_directory(source: BinaryIO) -> CentralDirectory:
    """Lee el registro de fin (y el ZIP64 si lo hay) de `source`, abierto en modo binario.

    Lanza `ZipFormatError` si no se encuentra o es inválido.
    """
    source_size = source.seek(0, os.SEEK_END)
    end_position, entries, size, offset = _find_end_record(source, source_size)
    zip64 = _read_zip64_end(source, end_position)
    if zip64 is not None:
        end_position, entries, size, offset = zip64
    return CentralDirectory(
        source=source,
        source_size=source_size,
        declared_entries=entries,
        end_position=end_position,
        size=size,
        offset=offset,
    )


def check_decompression_limits(
    directory: CentralDirectory, limits: DecompressionLimits = DECOMPRESSION_LIMITS
) -> LimitVerdict:
    """Aplica los Límites_de_Descompresión a `directory`, en el orden del docstring del módulo."""
    if directory.declared_entries > limits.max_entries:
        return LimitVerdict(exceeded=ExceededLimit.ENTRY_COUNT, damaged=False, entries=())
    try:
        # Uno más que el máximo alcanza para saber que se supera, sin leer el resto
        readers = list(islice(directory.entries(), limits.max_entries + 1))
    except ZipFormatError:
        return LimitVerdict(exceeded=None, damaged=True, entries=())
    stats = [reader.unmeasured() for reader in readers]

    exceeded: ExceededLimit | None = None
    declared_total = sum(reader.declared_size for reader in readers)
    if len(readers) > limits.max_entries:
        exceeded = ExceededLimit.ENTRY_COUNT
    elif declared_total > limits.total_bytes:
        exceeded = ExceededLimit.TOTAL_SIZE
    elif any(
        _exceeds_ratio(reader.declared_size, reader.compressed_size, limits.max_ratio)
        for reader in readers
    ):
        exceeded = ExceededLimit.COMPRESSION_RATIO
    if exceeded is not None:
        return LimitVerdict(exceeded=exceeded, damaged=False, entries=tuple(stats))
    if sum(reader.compressed_size for reader in readers) > directory.source_size:
        return LimitVerdict(exceeded=None, damaged=True, entries=tuple(stats))

    # Cota de la suma de Tamaños_Descomprimidos: las entradas medidas con su Tamaño_Descomprimido
    # y las demás con su tamaño declarado. Nunca supera total_bytes mientras sigue la medición.
    total = declared_total
    damaged = False
    for index, reader in enumerate(readers):
        budget = min(
            reader.declared_size + limits.total_bytes - total,
            limits.max_ratio * reader.compressed_size,
        )
        stat = reader.measure(budget)
        stats[index] = stat
        total += stat.uncompressed_size - reader.declared_size
        damaged = damaged or stat.damaged
        if stat.produced_size > budget:
            if _exceeds_ratio(stat.uncompressed_size, reader.compressed_size, limits.max_ratio):
                exceeded = ExceededLimit.COMPRESSION_RATIO
            else:
                exceeded = ExceededLimit.TOTAL_SIZE
            return LimitVerdict(exceeded=exceeded, damaged=damaged, entries=tuple(stats))
    return LimitVerdict(exceeded=None, damaged=damaged, entries=tuple(stats))


def check_zip_package(
    source: BinaryIO, limits: DecompressionLimits = DECOMPRESSION_LIMITS
) -> LimitVerdict:
    """Verifica el paquete ZIP de `source`, abierto en modo binario y con posicionamiento.

    Un registro de fin ilegible deja el paquete dañado. Los errores de E/S de `source` no se
    capturan: no indican un archivo dañado sino un fallo del Equipo_de_Ejecución.
    """
    try:
        directory = read_central_directory(source)
    except ZipFormatError:
        return LimitVerdict(exceeded=None, damaged=True, entries=())
    return check_decompression_limits(directory, limits)
