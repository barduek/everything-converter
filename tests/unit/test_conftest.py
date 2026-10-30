"""Pruebas de tests/conftest.py: perfiles de Hypothesis y marcas del proyecto."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    # Proyecto temporal con el mismo conftest que usa la suite
    pytester.makeconftest(CONFTEST.read_text(encoding="utf-8"))
    # Evita el aviso de pytest-asyncio por la opción sin definir
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\n")
    return pytester


def test_requires_tool_skips_only_when_tool_is_missing(
    project: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Una herramienta falsa en un directorio propio del PATH
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "herramienta-presente"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    project.makepyfile(
        """
        import pytest

        @pytest.mark.requires_tool("herramienta-presente")
        def test_present():
            pass

        @pytest.mark.requires_tool("herramienta-presente", "herramienta-ausente-xyz")
        def test_missing():
            pass
        """
    )
    result = project.runpytest("-rs")
    result.assert_outcomes(passed=1, skipped=1)
    result.stdout.fnmatch_lines(["*herramienta no disponible en el PATH: herramienta-ausente-xyz*"])


@pytest.mark.parametrize(
    ("decorator", "message"),
    [
        ('req("20")', "*criterio inválido en req(): '20'*"),
        ("req()", "*req() necesita al menos un criterio*"),
        ("requires_tool()", "*requires_tool() necesita al menos un nombre*"),
    ],
)
def test_malformed_marks_stop_collection(
    project: pytest.Pytester, decorator: str, message: str
) -> None:
    project.makepyfile(
        f"""
        import pytest

        @pytest.mark.{decorator}
        def test_x():
            pass
        """
    )
    result = project.runpytest()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines([message])


def test_req_accepts_valid_ids(project: pytest.Pytester) -> None:
    project.makepyfile(
        """
        import pytest

        @pytest.mark.req("20.1", "20.13")
        def test_x():
            pass
        """
    )
    project.runpytest().assert_outcomes(passed=1)


@pytest.mark.parametrize(
    ("profile", "expected_examples"),
    [(None, 100), ("", 100), ("ci", 100), ("nightly", 1000)],
)
def test_hypothesis_profile_from_env(
    project: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    profile: str | None,
    expected_examples: int,
) -> None:
    if profile is None:
        monkeypatch.delenv("HYPOTHESIS_PROFILE", raising=False)
    else:
        monkeypatch.setenv("HYPOTHESIS_PROFILE", profile)
    project.makepyfile(
        f"""
        from hypothesis import settings

        def test_profile():
            assert settings.default.max_examples == {expected_examples}
            assert settings.default.deadline is None
        """
    )
    # Subproceso: cargar un perfil cambia el estado global de Hypothesis
    project.runpytest_subprocess().assert_outcomes(passed=1)
