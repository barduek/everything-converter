"""Formatos y Pares_de_Conversión del Registro_de_Conversiones (Req 4.1, 4.2, 4.10).

Son dataclasses inmutables cuyos campos son textos, enteros, bytes, enumeraciones o tuplas de
esos valores. El Registro_de_Conversiones las construye a partir de especificaciones ya
validadas y las devuelve tal cual en cada consulta, así una consulta devuelve exactamente los
atributos registrados (Req 4.10).

- `MagicSignature` y `ProbeSignature` son las dos formas de una Firma_de_Formato. Un Formato sin
  firma es un Formato_de_Texto.
- `FormatDef` es un Formato: nombre, Categoría, extensiones, Extensión_Principal, tipo MIME,
  firma, inspector y sondas de variante.
- `ConversionPair` es un Par_de_Conversión con su Categoría (la del Formato_de_Origen), su
  Clasificación_de_Pérdida, su Descripción_de_Pérdida y el convertidor que lo ejecuta.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from converter.models import Category, LossClass

type MagicKey = tuple[Literal["magic"], tuple[tuple[int, bytes], ...]]
type ProbeKey = tuple[Literal["probe"], str]
type SignatureKey = MagicKey | ProbeKey


@dataclass(frozen=True, slots=True)
class MagicSignature:
    """Firma por bytes: todos los pares (desplazamiento, bytes) deben coincidir.

    El Detector_de_Tipo la evalúa sobre los primeros 64 KiB del archivo.
    """

    parts: tuple[tuple[int, bytes], ...]

    def key(self) -> MagicKey:
        """Identidad de la firma: dos firmas con los mismos pares son la misma firma.

        Los pares se ordenan por desplazamiento, así el orden en que se declararon no cambia
        la identidad.
        """
        return ("magic", tuple(sorted(self.parts)))


@dataclass(frozen=True, slots=True)
class ProbeSignature:
    """Firma estructural: la decide la sonda registrada con este nombre."""

    probe: str  # p. ej. "ooxml_wordprocessing"

    def key(self) -> ProbeKey:
        """Identidad de la firma: dos firmas con la misma sonda son la misma firma."""
        return ("probe", self.probe)


type Signature = MagicSignature | ProbeSignature


@dataclass(frozen=True, slots=True)
class FormatDef:
    """Formato registrado. Cumple `naming.HasPrimaryExtension`."""

    name: str  # "PNG"
    category: Category
    extensions: tuple[str, ...]  # en minúsculas, con punto inicial
    primary_extension: str  # Extensión_Principal; figura en `extensions`
    mime_type: str
    signature: Signature | None  # None: Formato_de_Texto
    inspector: str | None  # p. ej. "image"; None si el Formato no tiene inspector
    variant_probes: tuple[str, ...] = ()  # p. ej. ("docx_encrypted",)


@dataclass(frozen=True, slots=True)
class ConversionPair:
    """Par_de_Conversión registrado (Req 4.1)."""

    source: str  # nombre del Formato_de_Origen
    target: str  # nombre del Formato_de_Destino
    category: Category  # Categoría del Formato_de_Origen
    loss_class: LossClass
    loss_description: str  # Descripción_de_Pérdida, de 0 a 500 caracteres
    converter_id: str  # p. ej. "image.raster"

    @property
    def is_transcription(self) -> bool:
        """Par_de_Transcripción: Clasificación_de_Pérdida Aproximada."""
        return self.loss_class is LossClass.APPROXIMATE

    @property
    def is_long_running(self) -> bool:
        """Tarea_de_Larga_Duración: Categoría audio o video, o Par_de_Transcripción."""
        return self.category in (Category.AUDIO, Category.VIDEO) or self.is_transcription

    @property
    def requires_confirmation(self) -> bool:
        """El usuario confirma la pérdida antes de convertir: toda clase salvo Sin_Pérdida."""
        return self.loss_class is not LossClass.LOSSLESS
