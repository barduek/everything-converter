"""La Interfaz_de_Usuario del MVP: página en español, módulos y sondeo (Req 18.1, 18.3, 18.12)."""

from __future__ import annotations

from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "src" / "converter" / "web"
MODULES = ("api.js", "store.js", "upload.js", "cards.js", "poller.js", "app.js")


def _text(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


@pytest.mark.req("18.1", "18.2", "18.15")
def test_the_page_is_spanish_and_has_no_external_resources() -> None:
    page = _text("index.html")

    assert '<html lang="es">' in page
    assert "Elegir archivo" in page
    assert 'type="file"' in page
    assert "multiple" not in page
    assert 'src="/static/app.js"' in page
    assert 'href="/static/styles.css"' in page
    assert "http://" not in page and "https://" not in page
    assert "onclick" not in page and "<style" not in page
    assert 'aria-live="polite"' in page
    for name in MODULES:
        assert (WEB / name).is_file()


@pytest.mark.req("18.3", "18.12", "18.13")
def test_the_modules_talk_to_the_api_and_keep_task_ids() -> None:
    upload = _text("upload.js")
    store = _text("store.js")
    api = _text("api.js")
    poller = _text("poller.js")
    cards = _text("cards.js")

    assert "X-Converter-Request" in upload and "X-Converter-Request" in api
    assert "encodeURIComponent" in upload
    assert "XMLHttpRequest" in upload
    assert "webkitGetAsEntry" in upload
    assert 'converter.tasks' in store
    assert "storage" in store
    assert "return 200" in poller
    assert "15000" in poller
    assert "textContent" in cards
    assert "Idioma" not in cards
    assert "innerHTML" not in cards and "innerHTML" not in _text("app.js")
