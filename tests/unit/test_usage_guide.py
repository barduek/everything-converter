"""La Guía_de_Uso lista las mismas variables que config.py (Req 20.5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from converter.config import VARIABLES

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "usage-guide.md"


def _rows() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for line in GUIDE.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| CONVERTER_"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        found[cells[0]] = cells
    return found


@pytest.mark.req("20.5")
def test_the_usage_guide_lists_every_configuration_variable() -> None:
    rows = _rows()

    assert list(rows) == [spec.name for spec in VARIABLES]
    for spec in VARIABLES:
        name, kind, unit, low, high, default = rows[spec.name]
        assert (name, kind, unit) == (spec.name, "entero", spec.unit)
        assert default == str(spec.default)
        if spec.minimum_from:
            assert spec.minimum_from in low
        else:
            assert low == str(spec.minimum)
        if spec.maximum_from:
            assert spec.maximum_from in high
        else:
            assert high == str(spec.maximum)
    assert "docker run --rm -p 8080:8080 converter:1.0.0" in GUIDE.read_text(encoding="utf-8")
    text = GUIDE.read_text(encoding="utf-8")
    assert "/var/lib/converter/storage" in text
    assert "--read-only" in text and "--cap-drop ALL" in text
    assert "ws-" in text
