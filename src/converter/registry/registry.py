"""Registro_de_Conversiones: valida y guarda Formatos y Pares_de_Conversión (Req 4).

`ConversionRegistry` recibe especificaciones como las tablas de `catalog/formats.toml` y
`catalog/pairs.toml` (un `Mapping` por entrada) y aplica las reglas de "Validación del registro"
de design.md. Una especificación inválida lanza `RegistrationError` con un código de
`RegistryErrorCode`, se registra en el Log_de_Eventos como `registry_rejected` y deja sin
cambios los Formatos y el Mapa_de_Conversión: cada validación termina antes de modificar el
estado (Req 4.5, 4.6, 4.7, 4.12, 4.13).

Claves de un Formato:

- `name`: hasta 32 letras o dígitos ASCII, puntos, guiones o "+", empezando con letra o dígito.
  Es la forma de nombre de Formato que `messages_es` puede mostrar al usuario.
- `category`: "imagen", "documento", "audio" o "video".
- `extensions`: lista no vacía, sin repetidos. Cada extensión es un punto seguido de al menos un
  carácter, sin otro punto ni Caracteres_No_Permitidos. Se guarda en minúsculas.
- `primary_extension`: Extensión_Principal, con la misma forma y entre las extensiones.
- `mime_type`: texto ASCII imprimible no vacío, sin espacios al principio ni al final.
- `signature` (opcional): `{magic = [{offset = N, hex = "…"}, …]}` o `{probe = "nombre"}`. Cada
  par de `magic` tiene bytes no vacíos y termina dentro de los primeros 64 KiB, que es donde
  el Detector_de_Tipo la evalúa. Sin firma, el Formato es un Formato_de_Texto.
- `inspector` (opcional): nombre no vacío.
- `variant_probes` (opcional): lista de nombres de sonda no vacíos y sin repetidos.

Claves de un Par_de_Conversión: `source`, `target`, `loss_class` (uno de los cuatro valores, como
texto; una lista no es válida aunque tenga un solo valor), `loss_description` (opcional si la
clase es Sin_Pérdida; ausente equivale a "", y un valor que no es texto cuenta como faltante) y
`converter`. La Categoría del par es la de su Formato_de_Origen. Los nombres de Formato de un
par se comparan tal cual.

Las claves que no figuran arriba se ignoran. Si una especificación incumple varias reglas, el
error es el de la primera, en el orden de la tabla:

- Formato: `formato_invalido` (atributos en el orden de la lista de claves),
  `formato_en_conflicto` (nombre y después cada extensión) y `firma_duplicada`.
- Par: `formato_faltante`, `par_duplicado`, `clasificacion_invalida`, `descripcion_faltante` o
  `descripcion_excedida` (una descripción en blanco con una clase que exige descripción es
  `descripcion_faltante` aunque supere los 500 caracteres) y `convertidor_desconocido`.

El registro guarda dataclasses inmutables y las devuelve en orden de registro. Se completa al
iniciar el Conversor, desde un solo hilo; después solo se consulta.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType

from converter.eventlog import EventLog, JsonValue, RegistryEntryKind, pair_label
from converter.models import Category, LossClass
from converter.naming import FORBIDDEN
from converter.registry.model import (
    ConversionPair,
    FormatDef,
    MagicSignature,
    ProbeSignature,
    Signature,
    SignatureKey,
)

MAX_LOSS_DESCRIPTION = 500  # caracteres de una Descripción_de_Pérdida (Req 4.7)
SIGNATURE_WINDOW = 64 * 1024  # bytes iniciales sobre los que se evalúa una MagicSignature

# Misma forma que acepta `messages_es` para mostrar un nombre de Formato
_FORMAT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+\-]{0,31}")


class RegistryErrorCode(StrEnum):
    """Código del error con el que el Registro_de_Conversiones rechaza una entrada."""

    INVALID_FORMAT = "formato_invalido"  # Req 4.13
    FORMAT_CONFLICT = "formato_en_conflicto"  # Req 4.13
    DUPLICATE_SIGNATURE = "firma_duplicada"  # Req 4.13
    MISSING_FORMAT = "formato_faltante"  # Req 4.6
    DUPLICATE_PAIR = "par_duplicado"  # Req 4.5
    INVALID_LOSS_CLASS = "clasificacion_invalida"  # Req 4.12
    MISSING_DESCRIPTION = "descripcion_faltante"  # Req 4.7
    DESCRIPTION_TOO_LONG = "descripcion_excedida"  # Req 4.7
    UNKNOWN_CONVERTER = "convertidor_desconocido"  # decisión de diseño


class RegistrationError(Exception):
    """El Registro_de_Conversiones rechazó una entrada y no cambió.

    `details` nombra lo que causó el rechazo, con las mismas claves que el evento
    `registry_rejected`:

    - `formato_invalido`: `attribute` (clave inválida) y `reason`.
    - `formato_en_conflicto`: `attribute` ("name" o "extensions"), `value` (el nombre o la
      extensión en conflicto) y `existing` (el Formato registrado que ya lo usa).
    - `firma_duplicada`: `existing`.
    - `formato_faltante`: `missing` (cada nombre no registrado, sin repetir) y `attributes`
      ("source", "target" o ambos). Un origen o destino ausente o que no es texto figura solo
      en `attributes`.
    - `par_duplicado`: `existing` (p. ej. "PNG→JPG").
    - `clasificacion_invalida`: `allowed` (los cuatro valores admitidos).
    - `descripcion_faltante`: `loss_class`.
    - `descripcion_excedida`: `length` y `limit`.
    - `convertidor_desconocido`: `converter` (None si falta o no es texto).
    """

    def __init__(
        self,
        code: RegistryErrorCode,
        *,
        entry_kind: RegistryEntryKind,
        entry: str | None,
        details: Mapping[str, JsonValue],
    ) -> None:
        self.code = code
        self.entry_kind = entry_kind
        self.entry = entry  # nombre del Formato o "PNG→JPG"; None si la entrada no lo tiene
        self.details: Mapping[str, JsonValue] = MappingProxyType(dict(details))
        super().__init__(f"{entry_kind} {entry!r} rechazado: {code} {dict(details)}")


class _Invalid(Exception):
    """Regla incumplida, antes de saber qué entrada la incumple."""

    def __init__(self, code: RegistryErrorCode, details: Mapping[str, JsonValue]) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


def _invalid_format(attribute: str, reason: str) -> _Invalid:
    return _Invalid(RegistryErrorCode.INVALID_FORMAT, {"attribute": attribute, "reason": reason})


def _nonempty_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


# --------------------------------------------------------------------------------------------
# Atributos de un Formato


def _format_name(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid_format("name", "falta o está vacío")
    if _FORMAT_NAME.fullmatch(value) is None:
        raise _invalid_format(
            "name",
            "debe tener hasta 32 letras o dígitos ASCII, puntos, guiones o '+' y empezar con "
            "letra o dígito",
        )
    return value


def _category(value: object) -> Category:
    if isinstance(value, str):
        try:
            return Category(value)
        except ValueError:
            pass
    raise _invalid_format("category", "debe ser imagen, documento, audio o video")


def _extension(value: object) -> str | None:
    """La extensión en minúsculas, o None si no tiene la forma de una extensión."""
    if not isinstance(value, str):
        return None
    extension = value.lower()
    rest = extension[1:]
    if not extension.startswith(".") or not rest or "." in rest:
        return None
    if any(ch in FORBIDDEN for ch in rest):
        return None
    return extension


_EXTENSION_SHAPE = (
    "un punto seguido de al menos un carácter, sin otro punto ni caracteres no permitidos"
)


def _extensions(value: object) -> tuple[str, ...]:
    # Un texto también es una secuencia: solo se acepta una lista (o tupla) de extensiones
    if not isinstance(value, list | tuple) or not value:
        raise _invalid_format("extensions", "debe ser una lista no vacía")
    extensions: list[str] = []
    for item in value:
        extension = _extension(item)
        if extension is None:
            raise _invalid_format("extensions", f"cada extensión debe ser {_EXTENSION_SHAPE}")
        if extension in extensions:
            raise _invalid_format("extensions", "extensión repetida")
        extensions.append(extension)
    return tuple(extensions)


def _primary_extension(value: object, extensions: tuple[str, ...]) -> str:
    if value is None:
        raise _invalid_format("primary_extension", "falta")
    extension = _extension(value)
    if extension is None:
        raise _invalid_format("primary_extension", f"debe ser {_EXTENSION_SHAPE}")
    if extension not in extensions:
        raise _invalid_format("primary_extension", "no figura entre las extensiones")
    return extension


def _mime_type(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or not value.isprintable()
        or value != value.strip()
    ):
        raise _invalid_format(
            "mime_type", "debe ser texto ASCII imprimible, sin espacios al principio ni al final"
        )
    return value


def _magic_parts(value: object) -> tuple[tuple[int, bytes], ...]:
    if not isinstance(value, list | tuple) or not value:
        raise _invalid_format("signature", "magic debe ser una lista no vacía")
    parts: list[tuple[int, bytes]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"offset", "hex"}:
            raise _invalid_format("signature", "cada parte de magic tiene solo offset y hex")
        offset = item["offset"]
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise _invalid_format("signature", "offset debe ser un entero mayor o igual a 0")
        hex_text = item["hex"]
        try:
            data = bytes.fromhex(hex_text) if isinstance(hex_text, str) else b""
        except ValueError:
            data = b""
        if not data:
            raise _invalid_format("signature", "hex debe ser una secuencia no vacía de bytes")
        if offset + len(data) > SIGNATURE_WINDOW:
            raise _invalid_format("signature", "cada parte debe terminar en los primeros 64 KiB")
        parts.append((offset, data))
    return tuple(parts)


def _signature(value: object) -> Signature | None:
    if value is None:
        return None  # Formato_de_Texto
    if not isinstance(value, Mapping) or set(value) not in ({"magic"}, {"probe"}):
        raise _invalid_format("signature", "debe tener exactamente una clave: magic o probe")
    if "probe" in value:
        probe = _nonempty_text(value["probe"])
        if probe is None:
            raise _invalid_format("signature", "probe debe ser un nombre no vacío")
        return ProbeSignature(probe)
    return MagicSignature(_magic_parts(value["magic"]))


def _inspector(value: object) -> str | None:
    if value is None:
        return None
    inspector = _nonempty_text(value)
    if inspector is None:
        raise _invalid_format("inspector", "debe ser un nombre no vacío")
    return inspector


def _variant_probes(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise _invalid_format("variant_probes", "debe ser una lista")
    probes: list[str] = []
    for item in value:
        probe = _nonempty_text(item)
        if probe is None:
            raise _invalid_format("variant_probes", "cada sonda debe ser un nombre no vacío")
        if probe in probes:
            raise _invalid_format("variant_probes", "sonda repetida")
        probes.append(probe)
    return tuple(probes)


# --------------------------------------------------------------------------------------------
# Atributos de un Par_de_Conversión


def _loss_class(value: object) -> LossClass:
    # Una lista, aun de un solo valor, no es una Clasificación_de_Pérdida (Req 4.12)
    if isinstance(value, str):
        try:
            return LossClass(value)
        except ValueError:
            pass
    raise _Invalid(
        RegistryErrorCode.INVALID_LOSS_CLASS, {"allowed": tuple(c.value for c in LossClass)}
    )


def _loss_description(value: object, loss_class: LossClass) -> str:
    description = "" if value is None else value
    missing = _Invalid(RegistryErrorCode.MISSING_DESCRIPTION, {"loss_class": loss_class.value})
    if not isinstance(description, str):
        raise missing
    if loss_class is not LossClass.LOSSLESS and not description.strip():
        raise missing
    if len(description) > MAX_LOSS_DESCRIPTION:
        raise _Invalid(
            RegistryErrorCode.DESCRIPTION_TOO_LONG,
            {"length": len(description), "limit": MAX_LOSS_DESCRIPTION},
        )
    return description


# --------------------------------------------------------------------------------------------
# Registro


class ConversionRegistry:
    """Formatos y Mapa_de_Conversión del Conversor."""

    __slots__ = (
        "_converter_ids",
        "_extensions",
        "_folded_names",
        "_formats",
        "_log",
        "_pairs",
        "_signatures",
    )

    def __init__(self, converter_ids: Iterable[str], *, event_log: EventLog | None = None) -> None:
        self._converter_ids = frozenset(converter_ids)  # convertidores registrados
        self._log = event_log if event_log is not None else EventLog()
        self._formats: dict[str, FormatDef] = {}  # por nombre, en orden de registro
        self._folded_names: dict[str, str] = {}  # nombre en minúsculas → nombre
        self._extensions: dict[str, str] = {}  # extensión → nombre del Formato
        self._signatures: dict[SignatureKey, str] = {}  # identidad de la firma → nombre
        self._pairs: dict[tuple[str, str], ConversionPair] = {}  # en orden de registro

    # Registro

    def register_format(self, spec: Mapping[str, object]) -> FormatDef:
        """Valida y registra un Formato (Req 4.13). Lanza `RegistrationError`."""
        try:
            fmt = self._check_format(spec)
        except _Invalid as invalid:
            entry = _nonempty_text(spec.get("name"))
            raise self._rejected(RegistryEntryKind.FORMAT, entry, invalid) from None
        self._formats[fmt.name] = fmt
        self._folded_names[fmt.name.lower()] = fmt.name
        for extension in fmt.extensions:
            self._extensions[extension] = fmt.name
        if fmt.signature is not None:
            self._signatures[fmt.signature.key()] = fmt.name
        return fmt

    def register_pair(self, spec: Mapping[str, object]) -> ConversionPair:
        """Valida y registra un Par_de_Conversión (Req 4.5 a 4.7, 4.12).

        Lanza `RegistrationError`.
        """
        try:
            pair = self._check_pair(spec)
        except _Invalid as invalid:
            source = _nonempty_text(spec.get("source"))
            target = _nonempty_text(spec.get("target"))
            entry = pair_label(source, target) if source and target else None
            raise self._rejected(RegistryEntryKind.PAIR, entry, invalid) from None
        self._pairs[(pair.source, pair.target)] = pair
        return pair

    # Consultas

    def formats(self) -> tuple[FormatDef, ...]:
        """Formatos registrados, en orden de registro."""
        return tuple(self._formats.values())

    def conversion_map(self) -> tuple[ConversionPair, ...]:
        """Mapa_de_Conversión: cada par registrado una vez, en orden de registro (Req 4.8)."""
        return tuple(self._pairs.values())

    def targets_for(self, source: str) -> tuple[ConversionPair, ...]:
        """Pares con `source` como Formato_de_Origen, en orden de registro."""
        return tuple(pair for pair in self._pairs.values() if pair.source == source)

    def source_formats(self) -> tuple[str, ...]:
        """Formatos_de_Origen de algún par, en el orden en que se registraron los Formatos."""
        sources = {pair.source for pair in self._pairs.values()}
        return tuple(name for name in self._formats if name in sources)

    def text_format_for_extension(self, ext: str | None) -> FormatDef | None:
        """Formato_de_Texto que tiene la Extensión_del_Nombre `ext`, sin distinguir mayúsculas.

        None si `ext` es None, si ningún Formato la tiene o si la tiene un Formato con firma.
        """
        if ext is None:
            return None
        owner = self._extensions.get(ext.lower())
        if owner is None:
            return None
        fmt = self._formats[owner]
        return fmt if fmt.signature is None else None

    # Validación

    def _check_format(self, spec: Mapping[str, object]) -> FormatDef:
        name = _format_name(spec.get("name"))
        category = _category(spec.get("category"))
        extensions = _extensions(spec.get("extensions"))
        primary_extension = _primary_extension(spec.get("primary_extension"), extensions)
        mime_type = _mime_type(spec.get("mime_type"))
        signature = _signature(spec.get("signature"))
        inspector = _inspector(spec.get("inspector"))
        variant_probes = _variant_probes(spec.get("variant_probes"))

        existing = self._folded_names.get(name.lower())
        if existing is not None:
            raise _Invalid(
                RegistryErrorCode.FORMAT_CONFLICT,
                {"attribute": "name", "value": name, "existing": existing},
            )
        for extension in extensions:
            existing = self._extensions.get(extension)
            if existing is not None:
                raise _Invalid(
                    RegistryErrorCode.FORMAT_CONFLICT,
                    {"attribute": "extensions", "value": extension, "existing": existing},
                )
        if signature is not None:
            existing = self._signatures.get(signature.key())
            if existing is not None:
                raise _Invalid(RegistryErrorCode.DUPLICATE_SIGNATURE, {"existing": existing})

        return FormatDef(
            name=name,
            category=category,
            extensions=extensions,
            primary_extension=primary_extension,
            mime_type=mime_type,
            signature=signature,
            inspector=inspector,
            variant_probes=variant_probes,
        )

    def _check_pair(self, spec: Mapping[str, object]) -> ConversionPair:
        source = spec.get("source")
        target = spec.get("target")
        missing: list[str] = []
        attributes: list[str] = []
        for attribute, value in (("source", source), ("target", target)):
            if isinstance(value, str) and value in self._formats:
                continue
            attributes.append(attribute)
            if isinstance(value, str) and value not in missing:
                missing.append(value)
        # Los `isinstance` repiten lo que ya garantiza `attributes` vacío; acotan los tipos
        if attributes or not isinstance(source, str) or not isinstance(target, str):
            raise _Invalid(
                RegistryErrorCode.MISSING_FORMAT,
                {"missing": tuple(missing), "attributes": tuple(attributes)},
            )

        if (source, target) in self._pairs:
            raise _Invalid(
                RegistryErrorCode.DUPLICATE_PAIR, {"existing": pair_label(source, target)}
            )

        loss_class = _loss_class(spec.get("loss_class"))
        loss_description = _loss_description(spec.get("loss_description"), loss_class)

        converter = spec.get("converter")
        if not isinstance(converter, str) or converter not in self._converter_ids:
            raise _Invalid(
                RegistryErrorCode.UNKNOWN_CONVERTER,
                {"converter": converter if isinstance(converter, str) else None},
            )

        return ConversionPair(
            source=source,
            target=target,
            category=self._formats[source].category,
            loss_class=loss_class,
            loss_description=loss_description,
            converter_id=converter,
        )

    def _rejected(
        self, kind: RegistryEntryKind, entry: str | None, invalid: _Invalid
    ) -> RegistrationError:
        self._log.registry_rejected(
            entry_kind=kind, entry=entry, error=invalid.code.value, details=invalid.details
        )
        return RegistrationError(
            invalid.code, entry_kind=kind, entry=entry, details=invalid.details
        )
