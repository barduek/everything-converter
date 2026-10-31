"""Pruebas de docx.libreoffice: perfil, soffice, fuentes y PDF (Req 7.1, 7.4, 16.2, 16.3).

LibreOffice no se ejecuta: el doble escribe el PDF que produce la conversión.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import cast

import pytest

from converter.config import Config, load_config
from converter.converters.base import (
    ConversionContext,
    ConversionFailure,
    ConversionRequest,
    NoticeCollector,
)
from converter.converters.docx.fonts import FontReplacement
from converter.converters.docx.libreoffice import (
    PDF_FILTER,
    DocxLibreOfficeConverter,
    soffice_command,
)
from converter.detection.source_info import NoInfo
from converter.messages_es import FailureReason, NoticeCode
from converter.models import Category, LossClass
from converter.registry.model import ConversionPair
from converter.sandbox.runner import CompletedRun, SandboxRunner
from converter.storage.workspace import Workspace, WorkspaceManager
from converter.tasks.progress import ProgressReporter
from tests.pdf_samples import PageSpec, build_pdf

TASK_ID = "6f1c2c9e-8a0b-4d7e-9a35-2f0d4b8c1e77"


class FakeRunner:
    def __init__(self, returncode: int = 0, pdf: bytes | None = None) -> None:
        self.returncode = returncode
        self.pdf = b"" if pdf is None else pdf
        self.commands: list[list[str]] = []

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        limits: object,
        env: object,
        on_stdout_line: object = None,
        on_stderr_line: object = None,
    ) -> CompletedRun:
        command = list(argv)
        self.commands.append(command)
        if self.returncode == 0:
            outdir = Path(command[command.index("--outdir") + 1])
            (outdir / "document.pdf").write_bytes(self.pdf)
        return CompletedRun(self.returncode, ())


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    space = WorkspaceManager(tmp_path).create()
    with zipfile.ZipFile(space.source_file, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    return space


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    directory = tmp_path / "profile" / "user"
    directory.mkdir(parents=True)
    (directory / "registrymodifications.xcu").write_text("<oor:items/>", encoding="utf-8")
    return directory.parent


def _config() -> Config:
    config = load_config({})
    assert isinstance(config, Config)
    return config


def _context(workspace: Workspace, runner: FakeRunner) -> ConversionContext:
    return ConversionContext(
        task_id=TASK_ID,
        pair=ConversionPair(
            source="DOCX",
            target="PDF",
            category=Category.DOCUMENT,
            loss_class=LossClass.LOSSY,
            loss_description="nota",
            converter_id="docx.libreoffice",
        ),
        request=ConversionRequest("PDF"),
        source=NoInfo(),
        workspace=workspace,
        config=_config(),
        runner=cast(SandboxRunner, runner),
        progress=ProgressReporter(),
        notices=NoticeCollector(),
    )


def test_soffice_command_uses_the_headless_profile_and_the_pdf_filter(tmp_path: Path) -> None:
    command = soffice_command(tmp_path / "profile", tmp_path / "document.docx", tmp_path / "work")

    assert command[0] == "soffice"
    assert "--headless" in command and "--norestore" in command
    assert any(part.startswith("-env:UserInstallation=file:") for part in command)
    assert command[command.index("--convert-to") + 1] == PDF_FILTER
    assert "ExportNotes" in PDF_FILTER and '"value":"false"' in PDF_FILTER


@pytest.mark.req("7.1", "7.4")
@pytest.mark.asyncio
async def test_a_docx_becomes_a_pdf_and_reports_replaced_fonts(
    workspace: Workspace, profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "converter.converters.docx.libreoffice.find_replacements",
        lambda archive: (FontReplacement("Calibri", "Carlito"),),
    )
    runner = FakeRunner(pdf=build_pdf([PageSpec()]))
    ctx = _context(workspace, runner)

    outcome = await DocxLibreOfficeConverter(profile).run(ctx)

    assert outcome.result_path.name == "output.pdf"
    assert outcome.result_path.read_bytes().startswith(b"%PDF")
    assert ctx.notices.notices[0].code is NoticeCode.FONTS_REPLACED
    assert ctx.notices.notices[0].params["reemplazos"] == [("Calibri", "Carlito")]
    copied = workspace.tmp_dir / "lo-profile" / "user" / "registrymodifications.xcu"
    assert copied.is_file()


@pytest.mark.req("16.2")
@pytest.mark.asyncio
async def test_a_package_without_a_main_part_fails_before_libreoffice(
    workspace: Workspace, profile: Path
) -> None:
    workspace.source_file.write_bytes(b"")
    with zipfile.ZipFile(workspace.source_file, "w") as archive:
        archive.writestr("word/other.xml", "<x/>")
    runner = FakeRunner(pdf=build_pdf([PageSpec()]))

    with pytest.raises(ConversionFailure) as error:
        await DocxLibreOfficeConverter(profile).run(_context(workspace, runner))

    assert error.value.failure.reason is FailureReason.DAMAGED
    assert runner.commands == []


@pytest.mark.req("16.3")
@pytest.mark.asyncio
async def test_soffice_failing_fails_the_task(
    workspace: Workspace, profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "converter.converters.docx.libreoffice.find_replacements", lambda archive: ()
    )
    runner = FakeRunner(returncode=1)

    with pytest.raises(ConversionFailure) as error:
        await DocxLibreOfficeConverter(profile).run(_context(workspace, runner))

    assert error.value.failure.reason is FailureReason.INTERNAL_ERROR


@pytest.mark.req("16.3")
@pytest.mark.asyncio
async def test_a_pdf_that_does_not_open_fails(
    workspace: Workspace, profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "converter.converters.docx.libreoffice.find_replacements", lambda archive: ()
    )
    runner = FakeRunner(pdf=b"no es un pdf")

    with pytest.raises(ConversionFailure) as error:
        await DocxLibreOfficeConverter(profile).run(_context(workspace, runner))

    assert error.value.failure.reason is FailureReason.DAMAGED
