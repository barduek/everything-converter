"""Pruebas de src/converter/sandbox/cpus.py: cuota de `cpu.max`, afinidad y cálculo único de
las CPU efectivas (Req 11.8, 13.4)."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from converter.sandbox import cpus
from converter.sandbox.cpus import (
    affinity_cpus,
    compute_effective_cpus,
    effective_cpus,
    parse_cpu_max,
    read_cpu_quota,
)


@pytest.fixture
def fresh_effective_cpus() -> Iterator[None]:
    # La prueba recalcula el valor guardado y deja el caché vacío para las demás
    effective_cpus.cache_clear()
    yield
    effective_cpus.cache_clear()


def _cpu_max(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "cpu.max"
    path.write_text(text, encoding="ascii")
    return path


@pytest.mark.req("11.8", "13.4")
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("max 100000\n", None, id="sin-cuota"),
        pytest.param("200000 100000\n", 2, id="dos-cpu"),
        pytest.param("150000 100000\n", 2, id="fracción-hacia-arriba"),
        pytest.param("50000 100000\n", 1, id="media-cpu"),
        pytest.param("1 100000", 1, id="cuota-mínima"),
        pytest.param("400000 50000", 8, id="otro-período"),
    ],
)
def test_parse_cpu_max_rounds_quota_up(text: str, expected: int | None) -> None:
    assert parse_cpu_max(text) == expected


@pytest.mark.req("11.8", "13.4")
@pytest.mark.parametrize(
    "text",
    ["", "200000", "200000 100000 1", "0 100000", "100000 0", "-1 100000", "abc 100000", "max"],
)
def test_parse_cpu_max_rejects_malformed_text(text: str) -> None:
    assert parse_cpu_max(text) is None


@pytest.mark.req("11.8", "13.4")
def test_read_cpu_quota_without_file_or_with_unreadable_content(tmp_path: Path) -> None:
    assert read_cpu_quota(tmp_path / "no-existe") is None
    assert read_cpu_quota(tmp_path) is None  # un directorio no se puede leer como archivo
    binary = tmp_path / "binario"
    binary.write_bytes(b"\xff\xfe 100000")
    assert read_cpu_quota(binary) is None


@pytest.mark.req("11.8", "13.4")
@pytest.mark.parametrize(
    ("text", "affinity", "expected"),
    [
        pytest.param("200000 100000", 8, 2, id="cuota-menor"),
        pytest.param("800000 100000", 4, 4, id="afinidad-menor"),
        pytest.param("max 100000", 6, 6, id="sin-cuota"),
        pytest.param("basura", 3, 3, id="cuota-inválida"),
    ],
)
def test_effective_cpus_is_the_lower_of_quota_and_affinity(
    tmp_path: Path, text: str, affinity: int, expected: int
) -> None:
    cpu_max = _cpu_max(tmp_path, text)
    assert compute_effective_cpus(cpu_max=cpu_max, affinity=lambda: affinity) == expected


@pytest.mark.req("11.8", "13.4")
def test_effective_cpus_without_cgroup_file_uses_affinity(tmp_path: Path) -> None:
    assert compute_effective_cpus(cpu_max=tmp_path / "no-existe", affinity=lambda: 5) == 5


@pytest.mark.req("11.8", "13.4")
def test_effective_cpus_is_at_least_one(tmp_path: Path) -> None:
    assert compute_effective_cpus(cpu_max=tmp_path / "no-existe", affinity=lambda: 0) == 1


@pytest.mark.req("11.8", "13.4")
def test_affinity_cpus_matches_the_process_affinity() -> None:
    if sys.platform.startswith("linux"):
        assert affinity_cpus() == len(os.sched_getaffinity(0))
    else:
        assert affinity_cpus() == (os.cpu_count() or 1)


@pytest.mark.req("11.8", "13.4")
@pytest.mark.usefixtures("fresh_effective_cpus")
def test_effective_cpus_is_computed_once_per_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if affinity_cpus() < 2:
        pytest.skip("hace falta una afinidad de al menos 2 CPU para distinguir la cuota")
    cpu_max = _cpu_max(tmp_path, "100000 100000\n")
    monkeypatch.setattr(cpus, "CGROUP_CPU_MAX", cpu_max)
    assert effective_cpus() == 1

    # Un cambio de la cuota mientras el Conversor está en marcha no cambia la cantidad de hilos
    cpu_max.write_text("max 100000\n", encoding="ascii")
    assert compute_effective_cpus(cpu_max=cpu_max) == affinity_cpus()
    assert effective_cpus() == 1
