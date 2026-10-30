"""Prueba de docker/fontconfig/default-only.conf con fontconfig real (Req 6.11).

Copia la configuración cambiando solo el directorio de fuentes y la caché por directorios de la
prueba, con fuentes creadas con fontTools, y comprueba con `fc-list` y `fc-match` que fontconfig
no ve otras fuentes y resuelve las familias genéricas a la Fuente_Predeterminada. Se omite si
fontconfig no está instalado; corre dentro de la Imagen_de_Contenedor, donde hay otras fuentes
del sistema que no deben aparecer.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from converter.converters.text_docs.layout import DEFAULT_FONT_DIR, LAYOUT_FONT_FILE
from tests.fontbuild import build_font

pytestmark = [pytest.mark.integration, pytest.mark.requires_tool("fc-list", "fc-match")]

_CONF = Path(__file__).resolve().parents[2] / "docker" / "fontconfig" / "default-only.conf"
_IMAGE_CACHE = "/opt/converter/fontconfig/cache"


@pytest.fixture
def fontconfig_env(tmp_path: Path) -> tuple[dict[str, str], list[Path]]:
    font_dir = tmp_path / "fonts"
    fonts = [
        build_font(font_dir / LAYOUT_FONT_FILE, " abc", family="DejaVu Sans Mono"),
        build_font(font_dir / "sans" / "DejaVuSans.ttf", " abc", family="DejaVu Sans"),
    ]
    text = _CONF.read_text(encoding="utf-8")
    dir_element = f"<dir>{DEFAULT_FONT_DIR}</dir>"
    cache_element = f"<cachedir>{_IMAGE_CACHE}</cachedir>"
    assert text.count(dir_element) == text.count(cache_element) == 1
    text = text.replace(dir_element, f"<dir>{font_dir}</dir>")
    text = text.replace(cache_element, f"<cachedir>{tmp_path / 'cache'}</cachedir>")
    conf = tmp_path / "default-only.conf"
    conf.write_text(text, encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "FONTCONFIG_FILE": str(conf),
    }
    return env, fonts


def _run(args: list[str], env: dict[str, str]) -> str:
    return subprocess.run(
        args, env=env, capture_output=True, text=True, check=True, timeout=60
    ).stdout


@pytest.mark.req("6.11")
def test_fontconfig_lists_only_the_default_font_dir(
    fontconfig_env: tuple[dict[str, str], list[Path]],
) -> None:
    env, fonts = fontconfig_env
    listed = _run(["fc-list", "--format", "%{file}\n"], env).splitlines()
    assert sorted(listed) == sorted(str(font) for font in fonts)


@pytest.mark.req("6.11")
@pytest.mark.parametrize(
    ("requested", "family"),
    [
        ("monospace", "DejaVu Sans Mono"),
        ("sans-serif", "DejaVu Sans"),
        ("serif", "DejaVu Sans"),
        ("Arial", "DejaVu Sans"),
    ],
)
def test_fontconfig_resolves_families_to_the_default_font(
    fontconfig_env: tuple[dict[str, str], list[Path]], requested: str, family: str
) -> None:
    env, _ = fontconfig_env
    assert _run(["fc-match", "--format", "%{family}", requested], env) == family
