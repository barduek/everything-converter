"""Pruebas de src/converter/converters/media_rules.py: frecuencia de muestreo, canales, mezcla a
2 canales, Pista_de_Audio_Predeterminada y dimensiones de WEBM a MP4 (Req 9.2 a 9.5, 10.4, 10.9,
10.11; D16).

Las distribuciones estándar esperadas se escriben a mano como las muestra `ffmpeg -layouts` de
FFmpeg 7.1, ordenando los canales de cada máscara `AV_CH_LAYOUT_*` en el orden de
`enum AVChannel`, y no a partir de la tabla del módulo.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from converter.converters.media_rules import (
    MAX_CHANNELS,
    MP3_SAMPLE_RATES,
    WAV_SAMPLE_RATES,
    DownmixMatrix,
    SampleRateRange,
    build_downmix_matrix,
    choose_sample_rate,
    default_audio_track,
    layout_channels,
    padded_size,
    unused_audio_tracks,
)

_OGG_DEFAULT = SampleRateRange(8000, 192_000)  # predeterminados de la configuración
_MINUS_3_DB = math.sqrt(0.5)

# Distribuciones estándar de FFmpeg 7.1 y sus canales en orden
_STANDARD = {
    "mono": "FC",
    "stereo": "FL+FR",
    "2.1": "FL+FR+LFE",
    "3.0": "FL+FR+FC",
    "3.0(back)": "FL+FR+BC",
    "4.0": "FL+FR+FC+BC",
    "quad": "FL+FR+BL+BR",
    "quad(side)": "FL+FR+SL+SR",
    "3.1": "FL+FR+FC+LFE",
    "5.0": "FL+FR+FC+BL+BR",
    "5.0(side)": "FL+FR+FC+SL+SR",
    "4.1": "FL+FR+FC+LFE+BC",
    "5.1": "FL+FR+FC+LFE+BL+BR",
    "5.1(side)": "FL+FR+FC+LFE+SL+SR",
    "6.0": "FL+FR+FC+BC+SL+SR",
    "6.0(front)": "FL+FR+FLC+FRC+SL+SR",
    "3.1.2": "FL+FR+FC+LFE+TFL+TFR",
    "hexagonal": "FL+FR+FC+BL+BR+BC",
    "6.1": "FL+FR+FC+LFE+BC+SL+SR",
    "6.1(back)": "FL+FR+FC+LFE+BL+BR+BC",
    "6.1(front)": "FL+FR+LFE+FLC+FRC+SL+SR",
    "7.0": "FL+FR+FC+BL+BR+SL+SR",
    "7.0(front)": "FL+FR+FC+FLC+FRC+SL+SR",
    "7.1": "FL+FR+FC+LFE+BL+BR+SL+SR",
    "7.1(wide)": "FL+FR+FC+LFE+BL+BR+FLC+FRC",
    "7.1(wide-side)": "FL+FR+FC+LFE+FLC+FRC+SL+SR",
    "5.1.2": "FL+FR+FC+LFE+BL+BR+TFL+TFR",
    "octagonal": "FL+FR+FC+BL+BR+BC+SL+SR",
    "cube": "FL+FR+BL+BR+TFL+TFR+TBL+TBR",
    "5.1.4": "FL+FR+FC+LFE+BL+BR+TFL+TFR+TBL+TBR",
    "7.1.2": "FL+FR+FC+LFE+BL+BR+SL+SR+TFL+TFR",
    "7.1.4": "FL+FR+FC+LFE+BL+BR+SL+SR+TFL+TFR+TBL+TBR",
    "7.2.3": "FL+FR+FC+LFE+BL+BR+SL+SR+TFL+TFR+TBC+LFE2",
    "9.1.4": "FL+FR+FC+LFE+BL+BR+FLC+FRC+SL+SR+TFL+TFR+TBL+TBR",
    "hexadecagonal": "FL+FR+FC+BL+BR+BC+SL+SR+TFL+TFC+TFR+TBL+TBC+TBR+WL+WR",
    "downmix": "DL+DR",
    "22.2": (
        "FL+FR+FC+LFE+BL+BR+FLC+FRC+BC+SL+SR+TC+TFL+TFC+TFR+TBL+TBC+TBR+LFE2+TSL+TSR+BFC+BFL+BFR"
    ),
}


@dataclass(frozen=True)
class _Stream:
    index: int
    kind: str
    is_default: bool = False


def _audio(index: int, *, default: bool = False) -> _Stream:
    return _Stream(index, "audio", default)


def _video(index: int, *, default: bool = False) -> _Stream:
    return _Stream(index, "video", default)


# --- Frecuencia de muestreo ---


@pytest.mark.req("9.4", "9.5")
def test_mp3_sample_rates_are_the_glossary_ones() -> None:
    assert sorted(MP3_SAMPLE_RATES) == [
        8000,
        11025,
        12000,
        16000,
        22050,
        24000,
        32000,
        44100,
        48000,
    ]


@pytest.mark.req("9.5")
@pytest.mark.parametrize(
    ("source", "admitted"),
    [
        (44100, MP3_SAMPLE_RATES),
        (8000, MP3_SAMPLE_RATES),
        (48000, MP3_SAMPLE_RATES),
        (8000, _OGG_DEFAULT),
        (96000, _OGG_DEFAULT),
        (192_000, _OGG_DEFAULT),
        (1, WAV_SAMPLE_RATES),
        (7350, WAV_SAMPLE_RATES),
        (384_000, WAV_SAMPLE_RATES),
    ],
)
def test_an_admitted_rate_is_kept(source: int, admitted: SampleRateRange | frozenset[int]) -> None:
    assert choose_sample_rate(source, admitted) == source


@pytest.mark.req("9.4")
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (7999, 8000),
        (1, 8000),
        (96000, 48000),
        (192_000, 48000),
        (46000, 44100),  # 1900 Hz de 44100 y 2000 Hz de 48000
        (46100, 48000),
        (11500, 11025),
        (11600, 12000),
    ],
)
def test_mp3_resamples_to_the_nearest_rate(source: int, expected: int) -> None:
    assert choose_sample_rate(source, MP3_SAMPLE_RATES) == expected


@pytest.mark.req("9.4")
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (14000, 16000),
        (19025, 22050),
        (23025, 24000),
        (28000, 32000),
        (38050, 44100),
        (46050, 48000),
    ],
)
def test_mp3_takes_the_higher_rate_on_a_tie(source: int, expected: int) -> None:
    assert choose_sample_rate(source, MP3_SAMPLE_RATES) == expected


@pytest.mark.req("9.4")
def test_a_tie_in_an_arbitrary_set_takes_the_higher_rate() -> None:
    assert choose_sample_rate(150, frozenset({100, 200})) == 200
    assert choose_sample_rate(149, frozenset({100, 200})) == 100


@pytest.mark.req("9.4")
@pytest.mark.parametrize(
    ("source", "expected"),
    [(8000, 22050), (22049, 22050), (48001, 48000), (192_000, 48000)],
)
def test_an_interval_resamples_to_the_nearest_end(source: int, expected: int) -> None:
    assert choose_sample_rate(source, SampleRateRange(22050, 48000)) == expected


@pytest.mark.req("9.4", "9.5")
def test_an_interval_of_one_rate_admits_only_that_rate() -> None:
    single = SampleRateRange(44100, 44100)
    assert choose_sample_rate(44100, single) == 44100
    assert choose_sample_rate(8000, single) == 44100
    assert choose_sample_rate(96000, single) == 44100


def test_sample_rate_range_contains_its_ends() -> None:
    assert 8000 in _OGG_DEFAULT
    assert 192_000 in _OGG_DEFAULT
    assert 7999 not in _OGG_DEFAULT
    assert 192_001 not in _OGG_DEFAULT


@pytest.mark.parametrize(("minimum", "maximum"), [(0, 8000), (48000, 44100), (-1, -1)])
def test_sample_rate_range_rejects_invalid_ends(minimum: int, maximum: int) -> None:
    with pytest.raises(ValueError, match="minimum"):
        SampleRateRange(minimum, maximum)


@pytest.mark.parametrize("source", [0, -44100])
def test_choose_sample_rate_rejects_a_rate_below_1(source: int) -> None:
    with pytest.raises(ValueError, match="source"):
        choose_sample_rate(source, MP3_SAMPLE_RATES)


def test_choose_sample_rate_needs_admitted_rates() -> None:
    with pytest.raises(ValueError, match="admitted"):
        choose_sample_rate(44100, frozenset())


# --- Canales ---


@pytest.mark.req("9.2")
def test_max_channels_of_each_audio_format() -> None:
    assert dict(MAX_CHANNELS) == {"MP3": 2, "OGG": 255, "WAV": 65535}


@pytest.mark.req("9.3", "10.9")
@pytest.mark.parametrize(("layout", "channels"), list(_STANDARD.items()))
def test_standard_layouts_give_their_channels_in_order(layout: str, channels: str) -> None:
    names = tuple(channels.split("+"))
    assert layout_channels(layout, len(names)) == names


@pytest.mark.req("9.3", "10.9")
def test_listed_layout_gives_its_channels_and_drops_custom_names() -> None:
    layout = "7 channels (FL+FR+FC+LFE+BL@detrás+BR+SL)"
    assert layout_channels(layout, 7) == ("FL", "FR", "FC", "LFE", "BL", "BR", "SL")


@pytest.mark.req("9.3", "10.9")
def test_channels_without_a_position_are_unnamed() -> None:
    layout = "6 channels (FL+FR+USR63+LFE+UNK+AMBI0)"
    assert layout_channels(layout, 6) == ("FL", "FR", None, "LFE", None, None)


@pytest.mark.req("9.3", "10.9")
@pytest.mark.parametrize(
    ("layout", "channels"),
    [
        (None, 6),
        ("unknown", 6),
        ("6 channels", 6),
        ("ambisonic 1", 4),
        ("ambisonic 1+stereo", 6),
        ("", 3),
        ("5.1", 8),  # la distribución tiene 6 canales y el flujo, 8
        ("3 channels (FL+FR)", 3),  # la cantidad declarada no coincide con la lista
        ("3 channels (FL+FR+FC)", 4),
        ("FL+FR+FC", 3),  # sin la forma "N channels (...)"
    ],
)
def test_unrecognized_layouts_leave_every_channel_unnamed(
    layout: str | None, channels: int
) -> None:
    assert layout_channels(layout, channels) == (None,) * channels


@pytest.mark.parametrize("channels", [0, -2])
def test_layout_channels_rejects_less_than_one_channel(channels: int) -> None:
    with pytest.raises(ValueError, match="channels"):
        layout_channels("stereo", channels)


# --- Mezcla a 2 canales ---


def _assert_valid(matrix: DownmixMatrix, layout: tuple[str | None, ...]) -> None:
    """Los invariantes de Req 9.3: cada canal salvo el LFE suena y ninguna salida satura."""
    assert len(matrix.left) == len(matrix.right) == len(layout)
    for name, left, right in zip(layout, matrix.left, matrix.right, strict=True):
        if name in ("LFE", "LFE2"):
            assert left == right == 0
        else:
            assert left > 0 or right > 0
            assert left >= 0 and right >= 0
    for row in (matrix.left, matrix.right):
        assert sum(row) <= 1
        assert sum(reversed(row)) <= 1
        assert math.fsum(row) <= 1


@pytest.mark.req("9.3", "10.9")
def test_5_1_mixes_sides_and_center_and_drops_the_lfe() -> None:
    layout = layout_channels("5.1", 6)  # FL FR FC LFE BL BR
    matrix = build_downmix_matrix(layout)
    # Cada salida pesa 1 + 1 + 1/√2 antes de normalizar
    full = 1 / (2 + _MINUS_3_DB)
    center = _MINUS_3_DB * full
    assert matrix.left == pytest.approx((full, 0, center, 0, full, 0), abs=1e-8)
    assert matrix.right == pytest.approx((0, full, center, 0, 0, full), abs=1e-8)
    _assert_valid(matrix, layout)


@pytest.mark.req("9.3", "10.9")
def test_unnamed_channels_alternate_between_left_and_right() -> None:
    matrix = build_downmix_matrix((None, None, None))
    assert matrix == DownmixMatrix(left=(0.5, 0, 0.5), right=(0, 0.5, 0))
    matrix = build_downmix_matrix((None,) * 4)
    assert matrix == DownmixMatrix(left=(0.5, 0, 0.5, 0), right=(0, 0.5, 0, 0.5))


@pytest.mark.req("9.3", "10.9")
def test_names_outside_the_groups_alternate_like_unnamed_channels() -> None:
    matrix = build_downmix_matrix(("FL", "XYZ", "FR", None))
    assert matrix == DownmixMatrix(left=(0.5, 0.5, 0, 0), right=(0, 0, 0.5, 0.5))


@pytest.mark.req("9.3", "10.9")
def test_outputs_that_do_not_exceed_1_are_not_scaled() -> None:
    assert build_downmix_matrix(("FL", "FR", "LFE")) == DownmixMatrix(
        left=(1.0, 0, 0), right=(0, 1.0, 0)
    )
    mono = build_downmix_matrix(("FC",))
    assert mono.left == mono.right == pytest.approx((_MINUS_3_DB,), abs=1e-8)


@pytest.mark.req("9.3", "10.9")
def test_both_outputs_share_the_divisor_so_the_center_stays_centered() -> None:
    matrix = build_downmix_matrix(("FL", "FC", "LFE"))
    # La izquierda pesa 1 + 1/√2 y la derecha 1/√2: las dos se dividen por 1 + 1/√2
    assert matrix.left == pytest.approx(
        (1 / (1 + _MINUS_3_DB), _MINUS_3_DB / (1 + _MINUS_3_DB), 0), abs=1e-8
    )
    assert matrix.right[1] == matrix.left[1]


@pytest.mark.req("9.3", "10.9")
@pytest.mark.parametrize("layout", ["4.0", "5.1(side)", "7.1", "7.2.3", "hexadecagonal", "22.2"])
def test_standard_layouts_mix_every_channel_but_the_lfe(layout: str) -> None:
    names = layout_channels(layout, len(_STANDARD[layout].split("+")))
    _assert_valid(build_downmix_matrix(names), names)


@pytest.mark.req("9.3", "10.9")
def test_many_unnamed_channels_keep_positive_coefficients() -> None:
    layout = (None,) * 255  # máximo de canales de OGG
    matrix = build_downmix_matrix(layout)
    _assert_valid(matrix, layout)


@pytest.mark.req("9.3")
def test_only_lfe_channels_give_a_silent_mix() -> None:
    matrix = build_downmix_matrix(("LFE", "LFE2", "LFE"))
    assert matrix == DownmixMatrix(left=(0, 0, 0), right=(0, 0, 0))


def test_build_downmix_matrix_needs_a_channel() -> None:
    with pytest.raises(ValueError, match="layout"):
        build_downmix_matrix(())


# --- Pista_de_Audio_Predeterminada ---


@pytest.mark.req("10.4")
def test_default_track_is_the_first_audio_marked_as_default() -> None:
    streams = [_video(0, default=True), _audio(1), _audio(2, default=True), _audio(3, default=True)]
    assert default_audio_track(streams) == streams[2]


@pytest.mark.req("10.4")
def test_without_marks_the_default_track_is_the_first_audio() -> None:
    streams = [_video(0), _Stream(1, "other"), _audio(2), _audio(3)]
    assert default_audio_track(streams) == streams[2]


@pytest.mark.req("10.4")
def test_marks_on_other_streams_do_not_count() -> None:
    streams = [_video(0, default=True), _Stream(1, "other", True), _audio(2), _audio(3)]
    assert default_audio_track(streams) == streams[2]


@pytest.mark.req("10.4")
def test_no_audio_tracks_gives_none() -> None:
    assert default_audio_track([_video(0, default=True)]) is None
    assert default_audio_track([]) is None


@pytest.mark.req("10.4")
def test_default_audio_track_accepts_an_iterator() -> None:
    assert default_audio_track(iter([_video(0), _audio(1)])) == _audio(1)


@pytest.mark.req("10.4")
@pytest.mark.parametrize(("audio_tracks", "unused"), [(0, 0), (1, 0), (2, 1), (5, 4)])
def test_unused_audio_tracks_are_all_but_one(audio_tracks: int, unused: int) -> None:
    streams = [_video(0), _Stream(1, "other")]
    streams += [_audio(2 + n, default=n == 1) for n in range(audio_tracks)]
    assert unused_audio_tracks(streams) == unused


# --- Dimensiones de WEBM a MP4 ---


@pytest.mark.req("10.5", "10.11")
@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (1920, 1080, (1920, 1080)),
        (2, 2, (2, 2)),
        (641, 480, (642, 480)),
        (640, 361, (640, 362)),
        (1, 1, (2, 2)),
        (1279, 719, (1280, 720)),
    ],
)
def test_padded_size_adds_1_pixel_to_each_odd_dimension(
    width: int, height: int, expected: tuple[int, int]
) -> None:
    assert padded_size(width, height) == expected


@pytest.mark.parametrize(("width", "height"), [(0, 480), (640, 0), (-2, 2)])
def test_padded_size_rejects_dimensions_below_1(width: int, height: int) -> None:
    with pytest.raises(ValueError, match="width"):
        padded_size(width, height)
