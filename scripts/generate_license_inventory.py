"""Inventario de licencias de terceros (Req 20.12).

Junta tres fuentes:

- los paquetes Python del entorno (`importlib.metadata`);
- `licenses/manual.toml`, para lo que no está en el venv (FFmpeg de Debian, fuentes,
  LibreOffice);
- con `--system`, `dpkg-query` y `/usr/share/doc/*/copyright`, que es lo que se ve dentro
  de la imagen.

Escribe `licenses/inventory.json` y `THIRD_PARTY_LICENSES.md`. `--check` termina con 0 solo
si esos archivos coinciden con lo que generaría ahora, sin `--system` (el inventario que
viaja en el repositorio).
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "licenses" / "manual.toml"
INVENTORY = ROOT / "licenses" / "inventory.json"
MARKDOWN = ROOT / "THIRD_PARTY_LICENSES.md"

_COPYLEFT = re.compile(r"LGPL|MPL|GPL", re.IGNORECASE)


def _text(value: object, limit: int = 240) -> str:
    if not isinstance(value, str) or not value.strip():
        return "desconocida"
    line = value.strip().splitlines()[0].strip()
    return line[:limit]


def python_entries() -> list[dict[str, Any]]:
    """Dependencias Python instaladas en este intérprete."""
    entries: list[dict[str, Any]] = []
    for dist in importlib.metadata.distributions():
        meta = dist.metadata
        license_name = _text(meta.get("License-Expression") or meta.get("License"))
        entries.append(
            {
                "name": dist.metadata["Name"],
                "version": dist.version,
                "license": license_name,
                "variant": "ambas",
                "weak_copyleft": bool(re.search(r"LGPL|MPL", license_name, re.IGNORECASE)),
                "patent_codecs": [],
                "source": "python",
            }
        )
    return entries


def manual_entries(path: Path = MANUAL) -> list[dict[str, Any]]:
    """Filas de `licenses/manual.toml`."""
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    entries: list[dict[str, Any]] = []
    for row in document.get("component", []):
        license_name = _text(row.get("license"))
        codecs = row.get("patent_codecs") or []
        entries.append(
            {
                "name": str(row["name"]),
                "version": str(row.get("version") or ""),
                "license": license_name,
                "variant": str(row.get("variant") or "ambas"),
                "weak_copyleft": bool(row.get("weak_copyleft")),
                "patent_codecs": [str(item) for item in codecs],
                "source": "manual",
                "notes": str(row.get("notes") or ""),
            }
        )
    return entries


def dpkg_entries() -> list[dict[str, Any]]:
    """Paquetes de Debian de esta máquina. Vacío si no hay `dpkg-query`."""
    try:
        completed = subprocess.run(
            ["dpkg-query", "-W", "-f", "${Package}\t${Version}\n"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return []
    if completed.returncode != 0:
        return []
    entries: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        name, _, version = line.partition("\t")
        if not name:
            continue
        copyright_file = Path("/usr/share/doc") / name / "copyright"
        license_name = "ver /usr/share/doc"
        if copyright_file.is_file():
            head = copyright_file.read_text(encoding="utf-8", errors="replace")[:400]
            match = re.search(r"(?i)license:\s*(\S+)", head)
            if match:
                license_name = match.group(1)
        entries.append(
            {
                "name": name,
                "version": version,
                "license": license_name,
                "variant": "ambas",
                "weak_copyleft": bool(_COPYLEFT.search(license_name)),
                "patent_codecs": [],
                "source": "dpkg",
            }
        )
    return entries


def collect(*, system: bool) -> list[dict[str, Any]]:
    """Entradas ordenadas por origen y nombre."""
    entries = python_entries() + manual_entries()
    if system:
        entries.extend(dpkg_entries())
    return sorted(entries, key=lambda item: (item["source"], item["name"].casefold()))


def to_json(entries: list[dict[str, Any]]) -> str:
    return json.dumps(entries, ensure_ascii=False, indent=2) + "\n"


def _cell(value: object) -> str:
    return str(value).replace("|", "/").replace("\n", " ")


def to_markdown(entries: list[dict[str, Any]]) -> str:
    lines = [
        "# Licencias de terceros",
        "",
        "Inventario generado por `scripts/generate_license_inventory.py`.",
        "",
        "| Nombre | Versión | Licencia | Variante | Copyleft débil | Códecs con patentes |",
        "|---|---|---|---|---|---|",
    ]
    for item in entries:
        codecs = ", ".join(item["patent_codecs"]) or "—"
        copyleft = "sí" if item["weak_copyleft"] else "no"
        lines.append(
            f"| {_cell(item['name'])} | {_cell(item['version'])} | {_cell(item['license'])} "
            f"| {_cell(item['variant'])} | {copyleft} | {_cell(codecs)} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera el inventario de licencias")
    parser.add_argument(
        "--system",
        action="store_true",
        help="suma los paquetes de dpkg (para correr dentro de la imagen)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="falla si el inventario del repositorio no coincide (sin --system)",
    )
    args = parser.parse_args(argv)
    entries = collect(system=args.system)
    rendered_json = to_json(entries)
    rendered_md = to_markdown(entries)
    if args.check:
        current_json = INVENTORY.read_text(encoding="utf-8") if INVENTORY.is_file() else ""
        current_md = MARKDOWN.read_text(encoding="utf-8") if MARKDOWN.is_file() else ""
        if current_json != rendered_json or current_md != rendered_md:
            print("el inventario no coincide: corré el script sin --check", file=sys.stderr)
            return 1
        return 0
    INVENTORY.write_text(rendered_json, encoding="utf-8")
    MARKDOWN.write_text(rendered_md, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
