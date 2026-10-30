"""Pruebas de src/converter/tasks/model.py: recepción completada y Tarea (Req 1.1, 1.7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from converter.models import TaskState
from converter.storage.workspace import Workspace
from converter.tasks.model import MAX_ORIGINAL_NAME_CHARS, CompletedUpload, Task

WORKSPACE = Workspace(Path("/var/lib/converter/storage/ws-" + "0a" * 16))


@pytest.mark.req("1.7")
def test_a_completed_upload_keeps_the_original_name_unchanged() -> None:
    name = "../Informe: final*?.Tar.GZ"
    upload = CompletedUpload(WORKSPACE, name, 1)

    assert upload.original_name == name
    assert name not in repr(upload)  # el Nombre_Original no llega a los registros por repr


@pytest.mark.req("1.1")
@pytest.mark.parametrize(
    ("name", "size", "error"),
    [
        ("a" * (MAX_ORIGINAL_NAME_CHARS + 1), 1, ValueError),
        ("a.txt", 0, ValueError),
        ("a.txt", True, TypeError),
        (b"a.txt", 1, TypeError),
    ],
)
def test_a_completed_upload_rejects_what_reception_never_accepts(
    name: object, size: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        CompletedUpload(WORKSPACE, name, size)  # type: ignore[arg-type]


def test_the_longest_original_name_is_accepted() -> None:
    name = "ñ" * MAX_ORIGINAL_NAME_CHARS
    assert CompletedUpload(WORKSPACE, name, 1).original_name == name


def test_a_new_task_is_received_with_its_own_mutable_state() -> None:
    first = Task(id="a", workspace=WORKSPACE, original_name="x.png", size_bytes=1, created_at=0.0)
    second = Task(id="a", workspace=WORKSPACE, original_name="x.png", size_bytes=1, created_at=0.0)

    assert first.state is TaskState.RECEIVED
    assert first.version == 0
    assert first.detection is None and first.failure is None and first.result is None
    assert not first.detection_done.is_set()
    assert first != second  # cada Tarea es una entidad distinta, aunque coincidan sus datos
    assert first.notices is not second.notices
    assert first.lock is not second.lock
    assert "x.png" not in repr(first)
