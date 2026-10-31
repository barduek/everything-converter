"""El inventario de licencias se regenera igual y el manual entra en la lista (Req 20.12)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "generate_license_inventory.py"


@pytest.mark.req("20.12")
def test_the_committed_inventory_matches_a_fresh_generation() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.req("20.12")
def test_the_manual_components_are_in_the_inventory() -> None:
    document = json.loads((ROOT / "licenses" / "inventory.json").read_text(encoding="utf-8"))
    manual = [item for item in document if item["source"] == "manual"]
    names = {item["name"] for item in manual}

    assert "FFmpeg (Debian)" in names
    assert any("OpenH264" in item["patent_codecs"] for item in manual)
    assert any(item["weak_copyleft"] for item in manual)
    assert "fastapi" in {item["name"].casefold() for item in document}
