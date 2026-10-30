"""Pruebas de src/converter/sandbox/limits.py: validación de `Limits` y límites predeterminados
por herramienta (Req 13.7, 14.7)."""

from __future__ import annotations

from typing import Any

import pytest

from converter.sandbox.cpus import effective_cpus
from converter.sandbox.limits import (
    CPU_MARGIN_SECONDS,
    DEFAULT_FILE_SIZE_MB,
    Limits,
    SandboxTool,
    tool_limits,
)


@pytest.mark.req("13.7")
def test_limits_default_open_files_is_256() -> None:
    limits = Limits(address_space_mb=2000, cpu_seconds=60, file_size_mb=100)
    assert limits.open_files == 256


@pytest.mark.req("13.7")
@pytest.mark.parametrize("field", ["address_space_mb", "cpu_seconds", "file_size_mb", "open_files"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "10"])
def test_limits_rejects_values_that_are_not_positive_integers(field: str, value: Any) -> None:
    values: dict[str, Any] = {
        "address_space_mb": 2000,
        "cpu_seconds": 60,
        "file_size_mb": 100,
        "open_files": 256,
        field: value,
    }
    with pytest.raises(ValueError, match=field):
        Limits(**values)


@pytest.mark.req("13.7")
@pytest.mark.parametrize(
    ("tool", "address_space_mb"),
    [
        (SandboxTool.PYTHON, 2000),
        (SandboxTool.WEASYPRINT, 3000),
        (SandboxTool.LIBREOFFICE, 4000),
        (SandboxTool.FFMPEG, 3000),
        (SandboxTool.WHISPER_CLI, 3000),
    ],
)
def test_tool_limits_follow_the_design_table(tool: SandboxTool, address_space_mb: int) -> None:
    limits = tool_limits(tool, timeout_seconds=120, cpus=4)
    assert limits == Limits(
        address_space_mb=address_space_mb,
        cpu_seconds=120 * 4 + CPU_MARGIN_SECONDS,
        file_size_mb=DEFAULT_FILE_SIZE_MB,
        open_files=256,
    )


@pytest.mark.req("14.7")
def test_cpu_limit_never_cuts_a_step_before_its_timeout() -> None:
    # Con `cpus` CPU ocupadas durante todo el timeout, el proceso consume `timeout * cpus`
    # segundos de CPU, que quedan por debajo del límite
    for cpus in (1, 2, 64):
        for timeout in (8, 120, 86_400):
            limits = tool_limits(SandboxTool.FFMPEG, timeout_seconds=timeout, cpus=cpus)
            assert limits.cpu_seconds > timeout * cpus


@pytest.mark.req("14.7")
def test_tool_limits_default_to_the_effective_cpus() -> None:
    limits = tool_limits(SandboxTool.WHISPER_CLI, timeout_seconds=60)
    assert limits.cpu_seconds == 60 * effective_cpus() + CPU_MARGIN_SECONDS


@pytest.mark.req("14.7")
@pytest.mark.parametrize("kwargs", [{"timeout_seconds": 0}, {"timeout_seconds": 10, "cpus": 0}])
def test_tool_limits_rejects_non_positive_arguments(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        tool_limits(SandboxTool.PYTHON, **kwargs)
