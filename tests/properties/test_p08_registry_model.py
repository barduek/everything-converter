"""Propiedad 8: el Registro_de_Conversiones se comporta como su modelo (Req 4.1, 4.2, 4.5 a 4.8,
4.10, 4.12, 4.13).

Una máquina de estados de Hypothesis registra secuencias de especificaciones de Formatos y de
Pares_de_Conversión, válidas e inválidas, de `tests.strategies.registry`, a la vez en un
`ConversionRegistry` y en `RegistryModel`. Después de cada entrada comprueba:

- que el registro acepta exactamente lo que acepta el modelo y devuelve los mismos atributos;
- que cada rechazo tiene el código de la regla incumplida que va primero en el orden de la tabla
  "Validación del registro", nombra el atributo, el Formato en conflicto, el par existente o los
  Formatos faltantes, deja iguales los Formatos y el Mapa_de_Conversión y escribe un solo evento
  `registry_rejected` en el Log_de_Eventos; y que una entrada aceptada no escribe eventos;
- que vaciar las listas y diccionarios de la especificación después de registrarla no cambia el
  registro (guarda copias, Req 4.10);
- que las consultas coinciden con el modelo: `formats` y `conversion_map` en orden de registro,
  con cada par una sola vez y una sola Clasificación_de_Pérdida válida (Req 4.2, 4.8),
  `targets_for`, `source_formats` y `text_format_for_extension`.

El modelo está escrito a partir de la tabla "Validación del registro" de design.md, del glosario y
de las decisiones documentadas en registry.py (forma de nombres, extensiones y tipos MIME, ventana
de 64 KiB de las firmas, precedencia de las reglas), sin usar sus funciones de validación.
"""

from __future__ import annotations

import copy
import io
import json
import string
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import pytest
from hypothesis import event, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)

from converter.eventlog import EventLog
from converter.models import Category, LossClass
from converter.registry.model import (
    ConversionPair,
    FormatDef,
    MagicSignature,
    ProbeSignature,
)
from converter.registry.registry import ConversionRegistry, RegistrationError
from tests.strategies.names import FORBIDDEN_CHARS
from tests.strategies.registry import CONVERTER_IDS, SpecContext, SpecEntry, registry_specs

# Valores del glosario
_CATEGORIES: Final = {
    "imagen": Category.IMAGE,
    "documento": Category.DOCUMENT,
    "audio": Category.AUDIO,
    "video": Category.VIDEO,
}
_LOSS_CLASSES: Final = {
    "Sin_Pérdida": LossClass.LOSSLESS,
    "Con_Pérdida": LossClass.LOSSY,
    "Limitada": LossClass.LIMITED,
    "Aproximada": LossClass.APPROXIMATE,
}
_LOSSLESS: Final = "Sin_Pérdida"
_MAX_DESCRIPTION: Final = 500

# Decisiones de registry.py
_NAME_FIRST: Final = frozenset(string.ascii_letters + string.digits)
_NAME_CHARS: Final = _NAME_FIRST | frozenset("._+-")
_MAX_NAME: Final = 32
_FORBIDDEN: Final = frozenset(FORBIDDEN_CHARS)
_HEX_DIGITS: Final = frozenset(string.hexdigits)
_SIGNATURE_WINDOW: Final = 64 * 1024

_STEPS: Final = 20  # reglas por ejemplo; cada una registra de 1 a 3 entradas
_TS: Final = datetime(2025, 1, 15, 10, 32, 7, tzinfo=UTC)


@dataclass(frozen=True)
class Rejection:
    """Rechazo esperado: código y lo que el error debe nombrar (puede nombrar más cosas)."""

    code: str
    details: Mapping[str, object]


class _Rejected(Exception):
    def __init__(self, code: str, **details: object) -> None:
        super().__init__(code)
        self.rejection = Rejection(code, details)


def _invalid(attribute: str) -> _Rejected:
    return _Rejected("formato_invalido", attribute=attribute)


def _extension(value: object) -> str | None:
    """La extensión en minúsculas: un punto y al menos un carácter, sin otro punto ni
    Caracteres_No_Permitidos. None si no tiene esa forma."""
    if not isinstance(value, str):
        return None
    lowered = value.lower()
    rest = lowered[1:]
    if not lowered.startswith(".") or not rest or "." in rest or not _FORBIDDEN.isdisjoint(rest):
        return None
    return lowered


def _magic(value: object) -> MagicSignature:
    if not isinstance(value, list) or not value:
        raise _invalid("signature")
    parts: list[tuple[int, bytes]] = []
    for part in value:
        if not isinstance(part, Mapping) or set(part) != {"offset", "hex"}:
            raise _invalid("signature")
        offset, hex_text = part["offset"], part["hex"]
        # Un booleano no es un desplazamiento, aunque en Python sea un entero
        if type(offset) is not int or offset < 0:
            raise _invalid("signature")
        if (
            not isinstance(hex_text, str)
            or not hex_text
            or len(hex_text) % 2
            or not _HEX_DIGITS.issuperset(hex_text)
        ):
            raise _invalid("signature")
        data = bytes(int(hex_text[i : i + 2], 16) for i in range(0, len(hex_text), 2))
        if offset + len(data) > _SIGNATURE_WINDOW:
            raise _invalid("signature")
        parts.append((offset, data))
    return MagicSignature(tuple(parts))


def _signature(value: object) -> MagicSignature | ProbeSignature | None:
    if value is None:
        return None  # Formato_de_Texto
    if not isinstance(value, Mapping) or set(value) not in ({"magic"}, {"probe"}):
        raise _invalid("signature")
    if "magic" in value:
        return _magic(value["magic"])
    probe = value["probe"]
    if not isinstance(probe, str) or not probe:
        raise _invalid("signature")
    return ProbeSignature(probe)


def _same_signature(a: MagicSignature | ProbeSignature, b: MagicSignature | ProbeSignature) -> bool:
    # Misma lista de pares (desplazamiento, bytes) en cualquier orden, o la misma sonda
    if isinstance(a, MagicSignature) and isinstance(b, MagicSignature):
        return sorted(a.parts) == sorted(b.parts)
    if isinstance(a, ProbeSignature) and isinstance(b, ProbeSignature):
        return a.probe == b.probe
    return False


def _names(value: object, attribute: str) -> tuple[str, ...]:
    """Lista opcional de nombres no vacíos y sin repetir (sondas de variante)."""
    if value is None:
        return ()
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) and item for item in value)
        or len(set(value)) != len(value)
    ):
        raise _invalid(attribute)
    return tuple(value)


class RegistryModel:
    """Modelo de referencia del Registro_de_Conversiones."""

    def __init__(self, converters: Collection[str]) -> None:
        self.converters = frozenset(converters)
        self.formats: list[FormatDef] = []
        self.pairs: list[ConversionPair] = []

    def register(self, entry: SpecEntry) -> FormatDef | ConversionPair | Rejection:
        try:
            if entry.kind == "format":
                fmt = self._format(entry.spec)
                self.formats.append(fmt)
                return fmt
            pair = self._pair(entry.spec)
            self.pairs.append(pair)
            return pair
        except _Rejected as rejected:
            return rejected.rejection

    def targets_for(self, source: str) -> tuple[ConversionPair, ...]:
        return tuple(pair for pair in self.pairs if pair.source == source)

    def source_formats(self) -> tuple[str, ...]:
        sources = {pair.source for pair in self.pairs}
        return tuple(fmt.name for fmt in self.formats if fmt.name in sources)

    def text_format_for_extension(self, extension: str | None) -> FormatDef | None:
        if extension is None:
            return None
        for fmt in self.formats:
            if extension.lower() in fmt.extensions:
                return fmt if fmt.signature is None else None
        return None

    # Formatos: primero cada atributo en el orden de las claves, después los conflictos

    def _format(self, spec: Mapping[str, object]) -> FormatDef:
        name = spec.get("name")
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= _MAX_NAME
            or name[0] not in _NAME_FIRST
            or not _NAME_CHARS.issuperset(name)
        ):
            raise _invalid("name")

        category = spec.get("category")
        if not isinstance(category, str) or category not in _CATEGORIES:
            raise _invalid("category")

        declared = spec.get("extensions")
        if not isinstance(declared, list) or not declared:
            raise _invalid("extensions")
        extensions: list[str] = []
        for item in declared:
            extension = _extension(item)
            if extension is None or extension in extensions:
                raise _invalid("extensions")
            extensions.append(extension)

        primary = _extension(spec.get("primary_extension"))
        if primary is None or primary not in extensions:
            raise _invalid("primary_extension")

        mime_type = spec.get("mime_type")
        if (
            not isinstance(mime_type, str)
            or not mime_type
            or not all(" " <= ch <= "~" for ch in mime_type)
            or mime_type[0] == " "
            or mime_type[-1] == " "
        ):
            raise _invalid("mime_type")

        signature = _signature(spec.get("signature"))

        inspector = spec.get("inspector")
        if inspector is not None and (not isinstance(inspector, str) or not inspector):
            raise _invalid("inspector")

        variant_probes = _names(spec.get("variant_probes"), "variant_probes")

        for fmt in self.formats:
            if fmt.name.lower() == name.lower():
                raise _Rejected(
                    "formato_en_conflicto", attribute="name", value=name, existing=fmt.name
                )
        for extension in extensions:
            for fmt in self.formats:
                if extension in fmt.extensions:
                    raise _Rejected(
                        "formato_en_conflicto",
                        attribute="extensions",
                        value=extension,
                        existing=fmt.name,
                    )
        if signature is not None:
            for fmt in self.formats:
                if fmt.signature is not None and _same_signature(fmt.signature, signature):
                    raise _Rejected("firma_duplicada", existing=fmt.name)

        return FormatDef(
            name=name,
            category=_CATEGORIES[category],
            extensions=tuple(extensions),
            primary_extension=primary,
            mime_type=mime_type,
            signature=signature,
            inspector=inspector if isinstance(inspector, str) else None,
            variant_probes=variant_probes,
        )

    # Pares: en el orden de la tabla "Validación del registro"

    def _pair(self, spec: Mapping[str, object]) -> ConversionPair:
        registered = {fmt.name: fmt for fmt in self.formats}
        source, target = spec.get("source"), spec.get("target")
        missing: list[str] = []
        attributes: list[str] = []
        for attribute, value in (("source", source), ("target", target)):
            if isinstance(value, str) and value in registered:
                continue  # los nombres de un par se comparan tal cual
            attributes.append(attribute)
            if isinstance(value, str) and value not in missing:
                missing.append(value)
        if attributes or not isinstance(source, str) or not isinstance(target, str):
            raise _Rejected(
                "formato_faltante", missing=tuple(missing), attributes=tuple(attributes)
            )

        if any(pair.source == source and pair.target == target for pair in self.pairs):
            raise _Rejected("par_duplicado", existing=f"{source}→{target}")

        loss_class = spec.get("loss_class")
        if not isinstance(loss_class, str) or loss_class not in _LOSS_CLASSES:
            raise _Rejected("clasificacion_invalida")

        description = spec.get("loss_description")
        if description is None:
            description = ""  # ausente
        if not isinstance(description, str):
            raise _Rejected("descripcion_faltante")
        blank = description == "" or description.isspace()
        if blank and loss_class != _LOSSLESS:
            raise _Rejected("descripcion_faltante")
        if len(description) > _MAX_DESCRIPTION:
            raise _Rejected("descripcion_excedida")

        converter = spec.get("converter")
        if not isinstance(converter, str) or converter not in self.converters:
            raise _Rejected("convertidor_desconocido")

        return ConversionPair(
            source=source,
            target=target,
            category=registered[source].category,
            loss_class=_LOSS_CLASSES[loss_class],
            loss_description=description,
            converter_id=converter,
        )


def _entry_label(entry: SpecEntry) -> str | None:
    """Cómo identifica el Log_de_Eventos la entrada rechazada."""
    if entry.kind == "format":
        name = entry.spec.get("name")
        return name if isinstance(name, str) and name else None
    source, target = entry.spec.get("source"), entry.spec.get("target")
    if isinstance(source, str) and source and isinstance(target, str) and target:
        return f"{source}→{target}"
    return None


def _scramble(value: object) -> None:
    """Vacía en el lugar las listas y diccionarios de `value`, también los anidados."""
    if isinstance(value, dict):
        for item in value.values():
            _scramble(item)
        value.clear()
    elif isinstance(value, list):
        for item in value:
            _scramble(item)
        value.clear()


class RegistryMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.log = io.StringIO()
        self.registry = ConversionRegistry(
            CONVERTER_IDS, event_log=EventLog(self.log, clock=lambda: _TS)
        )
        self.model = RegistryModel(CONVERTER_IDS)
        # Entradas aceptadas: los generadores buscan conflictos y forman pares con ellas
        self.context = SpecContext()
        # Nombres y extensiones de todas las entradas, aceptadas o no, para las consultas
        self.names: dict[str, None] = {}
        self.extensions: dict[str, None] = {}

    @rule(data=st.data())
    def register_specs(self, data: st.DataObject) -> None:
        for entry in data.draw(registry_specs(context=self.context, min_size=1, max_size=3)):
            self._register(entry)
            self.queries_match_model()

    def _register(self, entry: SpecEntry) -> None:
        self._remember(entry)
        expected = self.model.register(entry)
        formats_before = self.registry.formats()
        map_before = self.registry.conversion_map()
        events_before = len(self._events())
        spec = copy.deepcopy(entry.spec)
        try:
            if entry.kind == "format":
                registered: FormatDef | ConversionPair = self.registry.register_format(spec)
            else:
                registered = self.registry.register_pair(spec)
        except RegistrationError as error:
            assert isinstance(expected, Rejection), f"se esperaba aceptar: {error}"
            assert error.code == expected.code, (str(error), expected)
            for key, value in expected.details.items():
                assert error.details.get(key) == value, (key, dict(error.details), expected)
            # Un rechazo no cambia nada (Req 4.5, 4.6, 4.7, 4.12, 4.13)
            assert self.registry.formats() == formats_before
            assert self.registry.conversion_map() == map_before
            new_events = self._events()[events_before:]
            assert len(new_events) == 1, new_events
            self._check_rejected_event(new_events[0], entry, error)
            label = expected.code
            if expected.code == "formato_invalido":
                label += f" ({expected.details['attribute']})"
        else:
            assert not isinstance(expected, Rejection), f"se esperaba {expected}"
            assert registered == expected  # Req 4.1, 4.10
            assert len(self._events()) == events_before
            self.context = self.context.with_entry(entry)
            label = "aceptado"
        # El registro no puede depender de los objetos que recibió (Req 4.10)
        _scramble(spec)
        event(f"{entry.kind}: {label}")

    def _remember(self, entry: SpecEntry) -> None:
        for key in ("name", "source", "target"):
            if isinstance(value := entry.spec.get(key), str):
                self.names[value] = None
        if isinstance(extensions := entry.spec.get("extensions"), list):
            for extension in extensions:
                if isinstance(extension, str):
                    self.extensions[extension] = None

    def _events(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.log.getvalue().splitlines()]

    @staticmethod
    def _check_rejected_event(
        record: Mapping[str, object], entry: SpecEntry, error: RegistrationError
    ) -> None:
        assert record["event"] == "registry_rejected", record
        assert record["entry_kind"] == ("formato" if entry.kind == "format" else "par"), record
        assert record["entry"] == _entry_label(entry), record
        assert record["error"] == error.code, record
        assert record["details"] == json.loads(json.dumps(dict(error.details))), record

    @invariant()
    def queries_match_model(self) -> None:
        formats = tuple(self.model.formats)
        pairs = tuple(self.model.pairs)
        assert self.registry.formats() == formats

        # Req 4.2, 4.8, 4.10: cada par aceptado una vez, con sus atributos y una sola
        # Clasificación_de_Pérdida válida
        conversion_map = self.registry.conversion_map()
        assert conversion_map == pairs
        keys = [(pair.source, pair.target) for pair in conversion_map]
        assert len(set(keys)) == len(keys), keys
        assert all(isinstance(pair.loss_class, LossClass) for pair in conversion_map)

        for name in (*(fmt.name for fmt in formats), *self.names):
            assert self.registry.targets_for(name) == self.model.targets_for(name), name
        assert self.registry.source_formats() == self.model.source_formats()

        for extension in (None, *self.extensions, *(ext.upper() for ext in self.extensions)):
            found = self.registry.text_format_for_extension(extension)
            assert found == self.model.text_format_for_extension(extension), extension


# Feature: file-converter, Property 8: El Registro_de_Conversiones se comporta como su modelo
@pytest.mark.req("4.1", "4.2", "4.5", "4.6", "4.7", "4.8", "4.10", "4.12", "4.13")
def test_registry_matches_model() -> None:
    # Sin `parent`, settings() toma max_examples y deadline del perfil cargado en conftest.py
    run_state_machine_as_test(RegistryMachine, settings=settings(stateful_step_count=_STEPS))
