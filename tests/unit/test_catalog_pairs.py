"""El catálogo empaquetado tiene los 30 pares del MVP, con su clase y su convertidor (Req 4.9)."""

from __future__ import annotations

import io

import pytest

from converter.converters import load_converters
from converter.eventlog import EventLog
from converter.models import LossClass
from converter.registry.loader import load_catalog

# (origen, destino, clase, convertidor), en el orden de catalog/pairs.toml
EXPECTED: tuple[tuple[str, str, LossClass, str], ...] = (
    ("PNG", "JPG", LossClass.LOSSY, "image.raster"),
    ("PNG", "WEBP", LossClass.LOSSLESS, "image.raster"),
    ("PNG", "PDF", LossClass.LOSSY, "image.pdf"),
    ("JPG", "PNG", LossClass.LOSSLESS, "image.raster"),
    ("JPG", "WEBP", LossClass.LOSSY, "image.raster"),
    ("JPG", "PDF", LossClass.LOSSY, "image.pdf"),
    ("WEBP", "PNG", LossClass.LOSSLESS, "image.raster"),
    ("WEBP", "JPG", LossClass.LOSSY, "image.raster"),
    ("WEBP", "PDF", LossClass.LOSSY, "image.pdf"),
    ("TXT", "PDF", LossClass.LOSSLESS, "text.pdf"),
    ("TXT", "HTML", LossClass.LOSSLESS, "text.html"),
    ("MD", "HTML", LossClass.LOSSLESS, "text.html"),
    ("MD", "PDF", LossClass.LOSSLESS, "text.pdf"),
    ("MD", "TXT", LossClass.LOSSY, "text.plain"),
    ("HTML", "PDF", LossClass.LOSSY, "text.pdf"),
    ("HTML", "MD", LossClass.LOSSY, "text.markdown"),
    ("HTML", "TXT", LossClass.LOSSY, "text.plain"),
    ("DOCX", "PDF", LossClass.LOSSY, "docx.libreoffice"),
    ("PDF", "TXT", LossClass.LIMITED, "pdf.text"),
    ("PDF", "PNG", LossClass.LOSSY, "pdf.raster"),
    ("WAV", "MP3", LossClass.LOSSY, "audio.ffmpeg"),
    ("WAV", "OGG", LossClass.LOSSY, "audio.ffmpeg"),
    ("MP3", "WAV", LossClass.LOSSLESS, "audio.ffmpeg"),
    ("MP3", "OGG", LossClass.LOSSY, "audio.ffmpeg"),
    ("OGG", "WAV", LossClass.LOSSLESS, "audio.ffmpeg"),
    ("OGG", "MP3", LossClass.LOSSY, "audio.ffmpeg"),
    ("MP4", "MP3", LossClass.LOSSY, "video.extract_audio"),
    ("MP4", "WAV", LossClass.LOSSY, "video.extract_audio"),
    ("MP4", "WEBM", LossClass.LOSSY, "video.ffmpeg"),
    ("WEBM", "MP4", LossClass.LOSSY, "video.ffmpeg"),
)


@pytest.mark.req("3.1", "4.9")
def test_the_packaged_catalog_registers_the_thirty_mvp_pairs() -> None:
    registry = load_catalog(load_converters(), event_log=EventLog(io.StringIO()))

    found = tuple(
        (pair.source, pair.target, pair.loss_class, pair.converter_id)
        for pair in registry.conversion_map()
    )
    assert found == EXPECTED
    assert all(pair.loss_description.strip() for pair in registry.conversion_map())
    assert "SRT" not in registry.source_formats()
