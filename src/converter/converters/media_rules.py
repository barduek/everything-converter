"""Reglas de audio y video que no dependen de FFmpeg (Req 9.2 a 9.5, 10.4, 10.9, 10.11; D16).

Los convertidores `audio.ffmpeg`, `video.ffmpeg` y `video.extract_audio` arman sus comandos con
estas funciones a partir de lo que informa el inspector `media`:

- `choose_sample_rate` elige la frecuencia de muestreo del Archivo_Resultado: la del origen si
  está entre las Frecuencias_de_Muestreo_Admitidas del destino y, si no, la admitida más
  cercana, o la mayor de las dos ante un empate (Req 9.4, 9.5). Las admitidas son un conjunto,
  como `MP3_SAMPLE_RATES`, o un intervalo `SampleRateRange`. El intervalo de OGG sale de la
  configuración (`Config.ogg_min_sample_rate_hz` y `Config.ogg_max_sample_rate_hz`), y el de
  WAV (`WAV_SAMPLE_RATES`) cubre todo valor que admite la cabecera.
- `MAX_CHANNELS` tiene el máximo de canales de cada Formato_de_Destino de audio (Req 9.2).
- `layout_channels` traduce la distribución de canales que informa ffprobe a la posición de
  cada canal, y `build_downmix_matrix` arma con esas posiciones la mezcla a 2 canales de un
  origen con más canales que los que admite MP3 (Req 9.3, 10.9).
- `default_audio_track` elige la Pista_de_Audio_Predeterminada y `unused_audio_tracks` cuenta
  las Pistas_de_Audio que quedan sin usar (Req 10.4).
- `padded_size` da el ancho y el alto de la Pista_de_Video en WEBM a MP4 (D16; Req 10.11).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Protocol

# --- Frecuencias de muestreo ---


@dataclass(frozen=True, slots=True)
class SampleRateRange:
    """Intervalo cerrado de frecuencias de muestreo enteras, en Hz."""

    minimum: int
    maximum: int

    def __post_init__(self) -> None:
        if not 1 <= self.minimum <= self.maximum:
            raise ValueError(
                f"debe cumplirse 1 <= minimum <= maximum: {self.minimum}, {self.maximum}"
            )

    def __contains__(self, rate: int) -> bool:
        return self.minimum <= rate <= self.maximum


# Frecuencias_de_Muestreo_Admitidas de MP3 (glosario): las de MPEG-1, MPEG-2 y MPEG-2.5 Layer III
MP3_SAMPLE_RATES: frozenset[int] = frozenset(
    {8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000}
)
# WAV admite cualquier valor: el campo de la cabecera es un entero de 32 bits sin signo
WAV_SAMPLE_RATES = SampleRateRange(1, 0xFFFF_FFFF)

type SampleRates = AbstractSet[int] | SampleRateRange


def choose_sample_rate(source: int, admitted: SampleRates) -> int:
    """Frecuencia de muestreo del Archivo_Resultado, en Hz (Req 9.4, 9.5).

    Devuelve `source` si está en `admitted`. Si no, devuelve la frecuencia de `admitted` a
    menor distancia de `source` y, si dos están a la misma distancia, la mayor. En un
    intervalo es el extremo más cercano. El aviso al usuario corresponde cuando el resultado
    es distinto de `source`.
    """
    if source < 1:
        raise ValueError(f"source debe ser 1 o mayor: {source}")
    if isinstance(admitted, SampleRateRange):
        return min(max(source, admitted.minimum), admitted.maximum)
    if not admitted:
        raise ValueError("admitted no tiene frecuencias")
    if source in admitted:
        return source
    # La menor distancia y, a igual distancia, la frecuencia mayor
    return min(admitted, key=lambda rate: (abs(rate - source), -rate))


# --- Canales ---

# Máximo de canales de cada Formato_de_Destino de audio, por nombre de Formato (Req 9.2)
MAX_CHANNELS: Mapping[str, int] = {"MP3": 2, "OGG": 255, "WAV": 65_535}

# Nombres de las posiciones de canal de FFmpeg 7.1 (`channel_names` en
# libavutil/channel_layout.c), en el orden de `enum AVChannel`, que es el orden nativo
_NATIVE_ORDER: tuple[str, ...] = (
    "FL", "FR", "FC", "LFE", "BL", "BR", "FLC", "FRC", "BC", "SL", "SR", "TC",
    "TFL", "TFC", "TFR", "TBL", "TBC", "TBR", "DL", "DR", "WL", "WR", "SDL", "SDR",
    "LFE2", "TSL", "TSR", "BFC", "BFL", "BFR", "SSL", "SSR", "TTL", "TTR",
)  # fmt: skip
CHANNEL_NAMES: frozenset[str] = frozenset(_NATIVE_ORDER)

# Canal_LFE: efectos de baja frecuencia
LFE_CHANNELS: frozenset[str] = frozenset({"LFE", "LFE2"})
# Posiciones a la izquierda, a la derecha y en el centro del oyente
_LEFT = frozenset(
    {"FL", "BL", "FLC", "SL", "TFL", "TBL", "DL", "WL", "SDL", "TSL", "BFL", "SSL", "TTL"}
)
_RIGHT = frozenset(
    {"FR", "BR", "FRC", "SR", "TFR", "TBR", "DR", "WR", "SDR", "TSR", "BFR", "SSR", "TTR"}
)
_CENTER = frozenset({"FC", "BC", "TC", "TFC", "TBC", "BFC"})


def _native(*groups: str) -> tuple[str, ...]:
    """Distribución con los canales de `groups` (nombres separados por espacios) en orden nativo."""
    names = {name for group in groups for name in group.split()}
    return tuple(name for name in _NATIVE_ORDER if name in names)


# Distribuciones estándar de FFmpeg 7.1 (`channel_layout_map`), armadas como las máscaras
# `AV_CH_LAYOUT_*` de libavutil/channel_layout.h. Un nombre estándar siempre está en orden nativo.
_STEREO = "FL FR"
_SURROUND = f"{_STEREO} FC"
_QUAD = f"{_STEREO} BL BR"
_2_2 = f"{_STEREO} SL SR"
_5POINT0 = f"{_SURROUND} SL SR"
_5POINT0_BACK = f"{_SURROUND} BL BR"
_5POINT1 = f"{_5POINT0} LFE"
_5POINT1_BACK = f"{_5POINT0_BACK} LFE"
_6POINT0_FRONT = f"{_2_2} FLC FRC"
_7POINT1 = f"{_5POINT1} BL BR"
_OCTAGONAL = f"{_5POINT0} BL BC BR"
_5POINT1POINT2_BACK = f"{_5POINT1_BACK} TFL TFR"
_7POINT1POINT2 = f"{_7POINT1} TFL TFR"
_7POINT1POINT4_BACK = f"{_7POINT1POINT2} TBL TBR"
_STANDARD_LAYOUTS: Mapping[str, tuple[str, ...]] = {
    "mono": _native("FC"),
    "stereo": _native(_STEREO),
    "2.1": _native(_STEREO, "LFE"),
    "3.0": _native(_SURROUND),
    "3.0(back)": _native(_STEREO, "BC"),
    "4.0": _native(_SURROUND, "BC"),
    "quad": _native(_QUAD),
    "quad(side)": _native(_2_2),
    "3.1": _native(_SURROUND, "LFE"),
    "5.0": _native(_5POINT0_BACK),
    "5.0(side)": _native(_5POINT0),
    "4.1": _native(_SURROUND, "BC LFE"),
    "5.1": _native(_5POINT1_BACK),
    "5.1(side)": _native(_5POINT1),
    "6.0": _native(_5POINT0, "BC"),
    "6.0(front)": _native(_6POINT0_FRONT),
    "3.1.2": _native(_SURROUND, "LFE TFL TFR"),
    "hexagonal": _native(_5POINT0_BACK, "BC"),
    "6.1": _native(_5POINT1, "BC"),
    "6.1(back)": _native(_5POINT1_BACK, "BC"),
    "6.1(front)": _native(_6POINT0_FRONT, "LFE"),
    "7.0": _native(_5POINT0, "BL BR"),
    "7.0(front)": _native(_5POINT0, "FLC FRC"),
    "7.1": _native(_7POINT1),
    "7.1(wide)": _native(_5POINT1_BACK, "FLC FRC"),
    "7.1(wide-side)": _native(_5POINT1, "FLC FRC"),
    "5.1.2": _native(_5POINT1POINT2_BACK),
    "octagonal": _native(_OCTAGONAL),
    "cube": _native(_QUAD, "TFL TFR TBL TBR"),
    "5.1.4": _native(_5POINT1POINT2_BACK, "TBL TBR"),
    "7.1.2": _native(_7POINT1POINT2),
    "7.1.4": _native(_7POINT1POINT4_BACK),
    "7.2.3": _native(_7POINT1POINT2, "TBC LFE2"),
    "9.1.4": _native(_7POINT1POINT4_BACK, "FLC FRC"),
    "hexadecagonal": _native(_OCTAGONAL, "WL WR TBL TBR TBC TFC TFL TFR"),
    "downmix": _native("DL DR"),
    "22.2": _native(_7POINT1POINT4_BACK, "FLC FRC BC LFE2 TFC TC TSL TSR TBC BFC BFL BFR"),
}

# Forma de `av_channel_layout_describe` para una distribución sin nombre estándar
_LISTED_LAYOUT = re.compile(r"([0-9]+) channels \((.+)\)")


def layout_channels(layout: str | None, channels: int) -> tuple[str | None, ...]:
    """Posición de cada canal, en orden, según la distribución que informa ffprobe.

    `layout` es el campo `channel_layout` de ffprobe y `channels`, el campo `channels`. Se
    reconocen las dos formas que escribe FFmpeg 7.1 (`av_channel_layout_describe`):

    - un nombre estándar, como "5.1" o "7.1(wide)";
    - "N channels (FL+FR+LFE)", con el nombre de cada canal en orden. El nombre propio que
      FFmpeg agrega después de "@" ("FL@comentario") se descarta.

    Un canal sin posición de FFmpeg ("USR5", "UNK", "AMBI0"...) queda sin nombre (None). Si
    `layout` es None, "unknown", "N channels", una distribución ambisónica u otro texto, o si
    su cantidad de canales no coincide con `channels`, todos los canales quedan sin nombre.
    """
    if channels < 1:
        raise ValueError(f"channels debe ser 1 o mayor: {channels}")
    unnamed: tuple[str | None, ...] = (None,) * channels
    if layout is None:
        return unnamed
    names: tuple[str | None, ...]
    if layout in _STANDARD_LAYOUTS:
        names = _STANDARD_LAYOUTS[layout]
    elif (listed := _LISTED_LAYOUT.fullmatch(layout)) is not None:
        tokens = [token.partition("@")[0] for token in listed.group(2).split("+")]
        if int(listed.group(1)) != len(tokens):
            return unnamed
        names = tuple(token if token in CHANNEL_NAMES else None for token in tokens)
    else:
        return unnamed
    return names if len(names) == channels else unnamed


@dataclass(frozen=True, slots=True)
class DownmixMatrix:
    """Coeficientes de la mezcla a 2 canales, uno por canal del origen en cada salida.

    La salida izquierda es la suma de `left[i]` por el canal i del origen, y la derecha, la
    suma de `right[i]` por el canal i.
    """

    left: tuple[float, ...]
    right: tuple[float, ...]


# Pesos enteros en una escala en la que 1 vale 2**30, para normalizar sin errores de redondeo
_SCALE = 1 << 30
_MINUS_3_DB = round(_SCALE * math.sqrt(0.5))  # 1/√2: la mitad de la potencia


def build_downmix_matrix(layout: Sequence[str | None]) -> DownmixMatrix:
    """Mezcla a 2 canales de un origen con la distribución `layout` (Req 9.3, 10.9).

    `layout` tiene la posición de cada canal del origen, en orden, como la devuelve
    `layout_channels`; None es un canal sin nombre. Cada canal recibe un peso en cada salida:

    - un canal izquierdo (`FL`, `BL`, `SL`...) pesa 1 en la izquierda, y uno derecho, 1 en la
      derecha;
    - un canal central (`FC`, `BC`, `TC`...) pesa −3 dB (1/√2) en las dos;
    - el Canal_LFE (`LFE`, `LFE2`) pesa 0 en las dos;
    - los demás, sin nombre o con un nombre fuera de esos grupos, pesan 1 en la izquierda y en
      la derecha de forma alternada, empezando por la izquierda.

    Si alguna salida suma más de 1, las dos se dividen por la mayor de sus sumas, como hace
    libswresample: ninguna satura y el balance entre izquierda y derecha no cambia.

    Los coeficientes son múltiplos de 2**-30 redondeados hacia abajo. Por eso la suma de cada
    salida en coma flotante es exacta en cualquier orden y nunca supera 1, todo canal que no es
    Canal_LFE conserva un coeficiente positivo en al menos una salida, y el `repr` de cada
    coeficiente escrito en el filtro `pan` se lee como el mismo valor. Si todos los canales son
    Canal_LFE, todos los coeficientes son 0.
    """
    if not layout:
        raise ValueError("layout debe tener al menos un canal")
    left: list[int] = []
    right: list[int] = []
    alternate = 0  # canales que se alternaron hasta ahora
    for name in layout:
        if name in LFE_CHANNELS:
            weights = (0, 0)
        elif name in _LEFT:
            weights = (_SCALE, 0)
        elif name in _RIGHT:
            weights = (0, _SCALE)
        elif name in _CENTER:
            weights = (_MINUS_3_DB, _MINUS_3_DB)
        else:
            weights = (_SCALE, 0) if alternate % 2 == 0 else (0, _SCALE)
            alternate += 1
        left.append(weights[0])
        right.append(weights[1])
    divisor = max(_SCALE, sum(left), sum(right))
    # Con la división entera hacia abajo, cada salida suma _SCALE o menos. El divisor no supera
    # la cantidad de canales por _SCALE, y el menor peso positivo (_MINUS_3_DB) por _SCALE lo
    # alcanza con hasta _MINUS_3_DB canales (759 250 125), así que ninguno queda en 0.
    return DownmixMatrix(
        left=tuple((weight * _SCALE // divisor) / _SCALE for weight in left),
        right=tuple((weight * _SCALE // divisor) / _SCALE for weight in right),
    )


# --- Pistas ---

AUDIO = "audio"  # valor de `kind` de una Pista_de_Audio


class TrackFlags(Protocol):
    """Lo que `default_audio_track` necesita de un flujo; `MediaStream` lo cumple."""

    @property
    def kind(self) -> str:  # "audio", "video" u "other"
        ...

    @property
    def is_default(self) -> bool:  # `disposition.default` = 1 en ffprobe
        ...


def default_audio_track[S: TrackFlags](streams: Iterable[S]) -> S | None:
    """Pista_de_Audio_Predeterminada de `streams`, que están en el orden del archivo (Req 10.4).

    Es la primera Pista_de_Audio marcada como predeterminada o, si ninguna lo está, la primera
    Pista_de_Audio. La marca de una pista de otro tipo no cuenta. Devuelve None si no hay
    Pistas_de_Audio.
    """
    first: S | None = None
    for stream in streams:
        if stream.kind != AUDIO:
            continue
        if stream.is_default:
            return stream
        if first is None:
            first = stream
    return first


def unused_audio_tracks(streams: Iterable[TrackFlags]) -> int:
    """Cantidad de Pistas_de_Audio que no se usan: todas menos la predeterminada (Req 10.4).

    Con una Pista_de_Audio o ninguna es 0, y no corresponde avisar.
    """
    return max(sum(1 for stream in streams if stream.kind == AUDIO) - 1, 0)


# --- Dimensiones ---


def padded_size(width: int, height: int) -> tuple[int, int]:
    """Ancho y alto de la Pista_de_Video de WEBM a MP4 (D16; Req 10.5, 10.11).

    H.264 en 4:2:0 exige dimensiones pares. Una dimensión impar suma 1 píxel, que es la columna
    negra a la derecha o la fila negra abajo que agrega el filtro `pad`; una par queda igual.
    """
    if width < 1 or height < 1:
        raise ValueError(f"width y height deben ser 1 o mayores: {width}, {height}")
    return width + width % 2, height + height % 2
