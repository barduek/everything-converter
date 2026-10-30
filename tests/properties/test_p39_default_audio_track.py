"""Propiedad 39: se elige la Pista_de_Audio_Predeterminada (Req 10.4).

Los flujos son una dataclass local con lo que pide `TrackFlags` más el índice del flujo en el
archivo, porque `MediaStream` todavía no existe. El índice es la posición en la lista, como en
ffprobe, y permite comprobar que se devuelve ese mismo flujo y no otro igual.

El oráculo sigue el glosario sin usar `default_audio_track` ni `unused_audio_tracks`: separa las
Pistas_de_Audio, toma la primera marcada como predeterminada o, si ninguna lo está, la primera.
El aviso `pistas_no_usadas` corresponde solo con más de una Pista_de_Audio e informa todas menos
la predeterminada; se comprueba con `unused_audio_tracks` y con el texto que arma
`render_notice`, sin depender de los convertidores de video.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from converter.converters.media_rules import default_audio_track, unused_audio_tracks
from converter.messages_es import NoticeCode, render_notice
from converter.models import Notice

type _Kind = Literal["audio", "video", "other"]
_KINDS: tuple[_Kind, ...] = ("audio", "video", "other")
_MAX_STREAMS = 12


@dataclass(frozen=True)
class _Stream:
    """Lo que `default_audio_track` usa de un flujo, más su índice en el archivo."""

    index: int
    kind: _Kind
    is_default: bool


@st.composite
def _stream_lists(draw: st.DrawFn) -> list[_Stream]:
    """Flujos en el orden del archivo, de cualquier tipo y con marcas de predeterminado al azar."""
    flags = draw(st.lists(st.tuples(st.sampled_from(_KINDS), st.booleans()), max_size=_MAX_STREAMS))
    return [_Stream(index, kind, is_default) for index, (kind, is_default) in enumerate(flags)]


def _expected_default(streams: list[_Stream]) -> _Stream | None:
    # Glosario: la primera Pista_de_Audio marcada como predeterminada en orden de aparición o, si
    # ninguna lo está, la primera Pista_de_Audio. La marca de un flujo de otro tipo no cuenta.
    audio = [stream for stream in streams if stream.kind == "audio"]
    marked = [stream for stream in audio if stream.is_default]
    if marked:
        return marked[0]
    return audio[0] if audio else None


# Feature: file-converter, Property 39: Se elige la Pista_de_Audio_Predeterminada
@pytest.mark.req("10.4")
@given(streams=_stream_lists())
def test_default_audio_track_is_chosen(streams: list[_Stream]) -> None:
    audio_count = sum(1 for stream in streams if stream.kind == "audio")
    expected = _expected_default(streams)

    chosen = default_audio_track(streams)
    # Devuelve el mismo flujo, no uno igual; el índice identifica cuál es
    assert chosen is expected, (chosen, expected)
    # Recorre `streams` una sola vez: con un iterador elige el mismo flujo
    assert default_audio_track(iter(streams)) is expected

    # Req 10.4: se usa solo la predeterminada y quedan sin usar las demás Pistas_de_Audio
    unused = unused_audio_tracks(streams)
    assert unused == (audio_count - 1 if audio_count else 0), (unused, audio_count)
    assert unused + (chosen is not None) == audio_count
    # El aviso `pistas_no_usadas` corresponde si y solo si hay más de una Pista_de_Audio
    assert (unused > 0) == (audio_count > 1)
    if unused > 0:
        text = render_notice(Notice(NoticeCode.UNUSED_TRACKS, {"cantidad": unused}))
        # La cantidad de Pistas_de_Audio no usadas es el único número del aviso
        assert re.findall(r"[0-9]+", text) == [str(audio_count - 1)], text

    event(f"Pistas_de_Audio: {audio_count if audio_count < 3 else '3 o más'}")
    if expected is not None:
        marked = sum(1 for stream in streams if stream.kind == "audio" and stream.is_default)
        event(f"Pistas_de_Audio marcadas: {marked if marked < 2 else '2 o más'}")
        # Un flujo marcado que no es de audio y está antes de la elegida no debe ganarle
        before = streams[: expected.index]
        if any(stream.is_default and stream.kind != "audio" for stream in before):
            event("marca en un flujo anterior que no es de audio")
