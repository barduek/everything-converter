"""Variables_de_Configuración del Conversor (Req 20.5, 20.6).

`load_config(env)` lee las variables `CONVERTER_*` y devuelve un `Config` inmutable o, si alguna
es inválida, una lista con un `ConfigError` por cada variable inválida, en el orden de la tabla.

Reglas de validación:

- Cada valor es un entero en base 10 escrito solo con los dígitos ASCII del 0 al 9: sin signo,
  sin espacios, sin separadores y sin dígitos de otros sistemas de escritura. Se admiten ceros a
  la izquierda ("0080" vale 80).
- Una variable no definida toma su valor predeterminado. Una variable definida con un valor
  vacío, que no es un entero escrito así o que está fuera de su rango admitido es inválida.
- Rangos dependientes: el máximo de cada Límite_de_Tamaño por Categoría es el valor efectivo de
  `CONVERTER_MAX_UPLOAD_MB`, y el mínimo de `CONVERTER_OGG_MAX_SAMPLE_RATE_HZ` es el valor
  efectivo de `CONVERTER_OGG_MIN_SAMPLE_RATE_HZ`. El valor efectivo de una variable es el
  definido, si es válido, o el predeterminado, si no está definida.
- Si la variable de la que depende un rango es inválida, no tiene valor efectivo. La variable
  dependiente se juzga entonces solo con su rango fijo, que es el más amplio que la dependencia
  puede admitir (1 a 100 000 MB; 8000 a 192 000 Hz). Así cada error nombra una variable cuyo
  propio valor es inválido, y una referencia inválida no vuelve inválidas a las que dependen de
  ella.
- Solo se validan las variables definidas. Un Límite_de_Tamaño por Categoría no definido
  conserva su predeterminado aunque supere un `CONVERTER_MAX_UPLOAD_MB` menor: ese límite nunca
  se alcanza, porque la recepción se corta antes en el Límite_de_Tamaño_Global.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

ENV_PREFIX = "CONVERTER_"
BYTES_PER_MB = 1_000_000
SECONDS_PER_MINUTE = 60

_MAX_UPLOAD_MB = "CONVERTER_MAX_UPLOAD_MB"
_OGG_MIN_SAMPLE_RATE_HZ = "CONVERTER_OGG_MIN_SAMPLE_RATE_HZ"

# Solo dígitos ASCII: `int()` aceptaría además signos, espacios, "_" y dígitos no ASCII
_DIGITS = re.compile(r"[0-9]+")


@dataclass(frozen=True, slots=True, kw_only=True)
class VariableSpec:
    """Una fila de la tabla de Variables_de_Configuración del diseño."""

    name: str  # p. ej. "CONVERTER_PORT"
    setting: str  # valor configurable de S7, p. ej. "Puerto_de_la_Interfaz"
    unit: str  # unidad en que se escribe el valor, p. ej. "MB"
    minimum: int  # mínimo fijo del rango admitido
    maximum: int  # máximo fijo del rango admitido
    default: int
    # Variable cuyo valor efectivo reemplaza al mínimo o al máximo fijo
    minimum_from: str | None = None
    maximum_from: str | None = None

    @property
    def field(self) -> str:
        """Nombre del campo de `Config`: "CONVERTER_MAX_UPLOAD_MB" da "max_upload_mb"."""
        return self.name.removeprefix(ENV_PREFIX).lower()


def _size_limit(key: str, spanish: str, default: int) -> VariableSpec:
    # Límite_de_Tamaño por Categoría. El máximo fijo es el máximo de CONVERTER_MAX_UPLOAD_MB.
    return VariableSpec(
        name=f"CONVERTER_MAX_{key}_MB",
        setting=f"Límite_de_Tamaño {spanish}",
        unit="MB",
        minimum=1,
        maximum=100_000,
        default=default,
        maximum_from=_MAX_UPLOAD_MB,
    )


def _timeout(key: str, spanish: str, default: int) -> VariableSpec:
    return VariableSpec(
        name=f"CONVERTER_TIMEOUT_{key}_MINUTES",
        setting=f"Tiempo_Máximo_de_Procesamiento {spanish}",
        unit="minutos",
        minimum=1,
        maximum=1440,
        default=default,
    )


# Orden de la tabla del diseño. Cada variable de referencia va antes de las que dependen de ella.
VARIABLES: tuple[VariableSpec, ...] = (
    VariableSpec(
        name="CONVERTER_PORT",
        setting="Puerto_de_la_Interfaz",
        unit="puerto TCP",
        minimum=1,
        maximum=65_535,
        default=8080,
    ),
    VariableSpec(
        name=_MAX_UPLOAD_MB,
        setting="Límite_de_Tamaño_Global",
        unit="MB",
        minimum=1,
        maximum=100_000,
        default=500,
    ),
    _size_limit("IMAGE", "imagen", 25),
    _size_limit("DOCUMENT", "documento", 25),
    _size_limit("AUDIO", "audio", 200),
    _size_limit("VIDEO", "video", 500),
    VariableSpec(
        name="CONVERTER_MAX_IMAGE_PIXELS",
        setting="Límite_de_Píxeles",
        unit="píxeles",
        minimum=1,
        maximum=1_000_000_000,
        default=100_000_000,
    ),
    VariableSpec(
        name="CONVERTER_MAX_TRANSCRIPTION_MINUTES",
        setting="Duración_Máxima_de_Transcripción",
        unit="minutos",
        minimum=1,
        maximum=5999,
        default=60,
    ),
    _timeout("IMAGE", "imagen", 2),
    _timeout("DOCUMENT", "documento", 2),
    _timeout("AUDIO", "audio", 10),
    _timeout("VIDEO", "video", 30),
    _timeout("TRANSCRIPTION", "transcripción", 120),
    VariableSpec(
        name="CONVERTER_RETENTION_MINUTES",
        setting="Período_de_Retención",
        unit="minutos",
        minimum=1,
        maximum=10_080,
        default=60,
    ),
    VariableSpec(
        name="CONVERTER_CONCURRENCY",
        setting="Límite_de_Concurrencia",
        unit="Tareas",
        minimum=1,
        maximum=64,
        default=2,
    ),
    VariableSpec(
        name="CONVERTER_RASTER_DPI",
        setting="Resolución_de_Rasterizado",
        unit="ppp",
        minimum=36,
        maximum=600,
        default=150,
    ),
    VariableSpec(
        name="CONVERTER_MIN_FREE_SPACE_MB",
        setting="Espacio_Libre_Mínimo",
        unit="MB",
        minimum=0,
        maximum=1_000_000,
        default=1000,
    ),
    VariableSpec(
        name="CONVERTER_PROGRESS_INTERVAL_SECONDS",
        setting="Intervalo_de_Progreso",
        unit="segundos",
        minimum=1,
        maximum=60,
        default=5,
    ),
    VariableSpec(
        name=_OGG_MIN_SAMPLE_RATE_HZ,
        setting="Frecuencias_de_Muestreo_Admitidas de OGG (mínimo)",
        unit="Hz",
        minimum=8000,
        maximum=192_000,
        default=8000,
    ),
    VariableSpec(
        name="CONVERTER_OGG_MAX_SAMPLE_RATE_HZ",
        setting="Frecuencias_de_Muestreo_Admitidas de OGG (máximo)",
        unit="Hz",
        # El mínimo fijo coincide con el mínimo de CONVERTER_OGG_MIN_SAMPLE_RATE_HZ
        minimum=8000,
        maximum=192_000,
        default=192_000,
        minimum_from=_OGG_MIN_SAMPLE_RATE_HZ,
    ),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """Valores de las Variables_de_Configuración en su unidad, más sus derivados.

    Los campos se llaman como la variable sin el prefijo `CONVERTER_` y en minúsculas. Las
    propiedades `*_bytes` y `*_seconds` dan los mismos valores en bytes y en segundos.
    """

    port: int
    max_upload_mb: int
    max_image_mb: int
    max_document_mb: int
    max_audio_mb: int
    max_video_mb: int
    max_image_pixels: int
    max_transcription_minutes: int
    timeout_image_minutes: int
    timeout_document_minutes: int
    timeout_audio_minutes: int
    timeout_video_minutes: int
    timeout_transcription_minutes: int
    retention_minutes: int
    concurrency: int
    raster_dpi: int
    min_free_space_mb: int
    progress_interval_seconds: int
    ogg_min_sample_rate_hz: int
    ogg_max_sample_rate_hz: int

    # Límites_de_Tamaño y Espacio_Libre_Mínimo en bytes (1 MB = 1 000 000 bytes)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * BYTES_PER_MB

    @property
    def max_image_bytes(self) -> int:
        return self.max_image_mb * BYTES_PER_MB

    @property
    def max_document_bytes(self) -> int:
        return self.max_document_mb * BYTES_PER_MB

    @property
    def max_audio_bytes(self) -> int:
        return self.max_audio_mb * BYTES_PER_MB

    @property
    def max_video_bytes(self) -> int:
        return self.max_video_mb * BYTES_PER_MB

    @property
    def min_free_space_bytes(self) -> int:
        return self.min_free_space_mb * BYTES_PER_MB

    # Duraciones en segundos. El Intervalo_de_Progreso y las frecuencias de OGG ya están en
    # segundos y en Hz.

    @property
    def max_transcription_seconds(self) -> int:
        return self.max_transcription_minutes * SECONDS_PER_MINUTE

    @property
    def timeout_image_seconds(self) -> int:
        return self.timeout_image_minutes * SECONDS_PER_MINUTE

    @property
    def timeout_document_seconds(self) -> int:
        return self.timeout_document_minutes * SECONDS_PER_MINUTE

    @property
    def timeout_audio_seconds(self) -> int:
        return self.timeout_audio_minutes * SECONDS_PER_MINUTE

    @property
    def timeout_video_seconds(self) -> int:
        return self.timeout_video_minutes * SECONDS_PER_MINUTE

    @property
    def timeout_transcription_seconds(self) -> int:
        return self.timeout_transcription_minutes * SECONDS_PER_MINUTE

    @property
    def retention_seconds(self) -> int:
        return self.retention_minutes * SECONDS_PER_MINUTE


@dataclass(frozen=True, slots=True)
class ConfigError:
    """Una Variable_de_Configuración inválida y el motivo, para el Log_de_Eventos."""

    variable: str  # p. ej. "CONVERTER_PORT"
    reason: str  # p. ej. "está fuera de rango; debe ser un entero entre 1 y 65535"


def load_config(env: Mapping[str, str]) -> Config | list[ConfigError]:
    """Lee las Variables_de_Configuración de `env` (normalmente `os.environ`).

    Devuelve el `Config` si todas las variables definidas son válidas. En otro caso devuelve
    todos los errores juntos, uno por variable inválida, en el orden de `VARIABLES`.
    """
    # Valor efectivo de cada variable válida o no definida; las inválidas no tienen
    effective: dict[str, int] = {}
    errors: list[ConfigError] = []
    for spec in VARIABLES:
        raw = env.get(spec.name)
        if raw is None:
            effective[spec.name] = spec.default
            continue
        minimum, minimum_text = _bound(spec.minimum, spec.minimum_from, effective)
        maximum, maximum_text = _bound(spec.maximum, spec.maximum_from, effective)
        result = _parse(raw, minimum, maximum)
        if isinstance(result, int):
            effective[spec.name] = result
        else:
            reason = f"{result}; debe ser un entero entre {minimum_text} y {maximum_text}"
            errors.append(ConfigError(variable=spec.name, reason=reason))
    if errors:
        return errors
    return Config(**{spec.field: effective[spec.name] for spec in VARIABLES})


def _bound(fixed: int, source: str | None, effective: Mapping[str, int]) -> tuple[int, str]:
    """Extremo del rango y su texto para el motivo.

    Si el extremo depende de otra variable con valor efectivo, se usa ese valor. Si esa variable
    es inválida, se usa el extremo fijo.
    """
    if source is not None and source in effective:
        value = effective[source]
        return value, f"el valor de {source} ({value})"
    return fixed, str(fixed)


def _parse(raw: str, minimum: int, maximum: int) -> int | str:
    """Devuelve el entero de `raw` si está en el rango, o el problema encontrado."""
    if raw == "":
        return "está vacía"
    if _DIGITS.fullmatch(raw) is None:
        return "no es un entero en base 10 sin signo ni espacios"
    digits = raw.lstrip("0") or "0"
    # Con más cifras que el máximo ya está fuera de rango; evita convertir textos enormes
    if len(digits) > len(str(maximum)):
        return "está fuera de rango"
    value = int(digits)
    if not minimum <= value <= maximum:
        return "está fuera de rango"
    return value
