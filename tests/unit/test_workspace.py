"""Pruebas de src/converter/storage/workspace.py con raíces en directorios temporales.

Cubren el contrato básico de cada operación. Los casos hostiles (enlaces simbólicos que apuntan
afuera, almacenamiento de solo lectura) y los reintentos del barrendero quedan para las pruebas
del almacenamiento y del barrendero.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from converter.storage.workspace import (
    STORAGE_ROOT,
    SUBDIRS,
    WORKSPACE_NAME,
    OutsideStorageError,
    Workspace,
    WorkspaceManager,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _fill(ws: Workspace) -> None:
    # Contenido de una Tarea completada: origen, intermedios, perfil temporal y resultado
    ws.source_file.write_bytes(b"origen")
    (ws.work_dir / "audio16k.wav").write_bytes(b"intermedio")
    (ws.work_dir / "pages").mkdir()
    (ws.work_dir / "pages" / "1.png").write_bytes(b"png")
    (ws.tmp_dir / "lo-profile").mkdir()
    ws.result_file.write_bytes(b"resultado")


@pytest.fixture
def manager(tmp_path: Path) -> WorkspaceManager:
    root = tmp_path / "storage"
    root.mkdir()
    return WorkspaceManager(root)


@pytest.mark.req("20.7")
def test_default_root_is_the_fixed_storage_path() -> None:
    assert str(STORAGE_ROOT) == "/var/lib/converter/storage"
    assert WorkspaceManager().root == STORAGE_ROOT


@pytest.mark.req("17.1", "1.6")
def test_create_makes_private_workspace_with_fixed_layout(manager: WorkspaceManager) -> None:
    ws = manager.create()

    assert WORKSPACE_NAME.fullmatch(ws.name)
    assert ws.path.parent == Path(os.path.realpath(manager.root))
    assert sorted(entry.name for entry in ws.path.iterdir()) == sorted(SUBDIRS)
    assert _mode(ws.path) == 0o700
    for subdir in SUBDIRS:
        assert stat.S_ISDIR(os.lstat(ws.path / subdir).st_mode)
        assert _mode(ws.path / subdir) == 0o700
    # Nombres internos fijos, independientes del Nombre_Original
    assert ws.source_file == ws.path / "source" / "original"
    assert ws.result_file == ws.path / "result" / "output"
    assert ws.work_dir == ws.path / "work"
    assert ws.tmp_dir == ws.path / "tmp"


@pytest.mark.req("17.1")
def test_create_mode_does_not_depend_on_umask(manager: WorkspaceManager) -> None:
    previous = os.umask(0o277)  # quitaría la escritura y la ejecución al dueño
    try:
        ws = manager.create()
    finally:
        os.umask(previous)
    assert _mode(ws.path) == 0o700
    assert all(_mode(ws.path / subdir) == 0o700 for subdir in SUBDIRS)


@pytest.mark.req("17.1")
def test_each_created_workspace_is_distinct(manager: WorkspaceManager) -> None:
    workspaces = [manager.create() for _ in range(50)]
    assert len({ws.name for ws in workspaces}) == 50
    assert sorted(p.name for p in manager.root.iterdir()) == sorted(ws.name for ws in workspaces)


@pytest.mark.req("1.5")
def test_free_bytes_reports_statvfs_of_the_storage(manager: WorkspaceManager) -> None:
    info = os.statvfs(manager.root)
    expected = info.f_bavail * info.f_frsize
    # Otros procesos pueden escribir en el mismo volumen entre las dos lecturas
    assert abs(manager.free_bytes() - expected) <= 64 * 1024 * 1024
    assert manager.free_bytes() > 0


@pytest.mark.req("17.6")
def test_remove_deletes_the_whole_workspace_and_is_idempotent(
    manager: WorkspaceManager,
) -> None:
    ws = manager.create()
    other = manager.create()
    _fill(ws)
    _fill(other)

    manager.remove(ws)
    assert not os.path.lexists(ws.path)
    manager.remove(ws)  # un reintento sobre un espacio ya borrado no falla

    assert [p.name for p in manager.root.iterdir()] == [other.name]
    assert other.result_file.read_bytes() == b"resultado"


@pytest.mark.req("17.2")
def test_prune_for_completed_keeps_only_the_result(manager: WorkspaceManager) -> None:
    ws = manager.create()
    _fill(ws)
    (ws.path / "suelto.txt").write_bytes(b"fuera de los subdirectorios")

    manager.prune_for_completed(ws)

    assert [p.name for p in ws.path.iterdir()] == ["result"]
    assert [p.name for p in ws.result_dir.iterdir()] == ["output"]
    assert ws.result_file.read_bytes() == b"resultado"
    manager.prune_for_completed(ws)  # repetir no cambia nada
    assert ws.result_file.read_bytes() == b"resultado"


@pytest.mark.req("17.2")
def test_prune_for_completed_of_missing_workspace_does_nothing(
    manager: WorkspaceManager,
) -> None:
    ws = manager.create()
    manager.remove(ws)
    manager.prune_for_completed(ws)
    assert list(manager.root.iterdir()) == []


@pytest.mark.req("17.5", "20.8")
def test_purge_removes_workspaces_and_keeps_everything_else(
    manager: WorkspaceManager,
) -> None:
    old = [manager.create() for _ in range(3)]
    for ws in old:
        _fill(ws)
    root = manager.root
    (root / "notas.txt").write_bytes(b"ajeno")
    (root / "fotos").mkdir()
    (root / "fotos" / "a.jpg").write_bytes(b"ajeno")
    # Nombres que se parecen a un Espacio_de_Trabajo pero no coinciden con ^ws-[0-9a-f]{32}$
    look_alikes = ["ws-" + "A" * 32, "ws-" + "0" * 31, "ws-" + "0" * 33, "xws-" + "0" * 32]
    for name in look_alikes:
        (root / name).mkdir()
        (root / name / "dato").write_bytes(b"ajeno")
    # Coincide con el patrón pero es un archivo, no un directorio
    (root / ("ws-" + "f" * 32)).write_bytes(b"ajeno")

    assert manager.purge_all_workspaces() == []

    remaining = sorted(p.name for p in root.iterdir())
    assert remaining == sorted(["notas.txt", "fotos", *look_alikes, "ws-" + "f" * 32])
    assert (root / "fotos" / "a.jpg").read_bytes() == b"ajeno"
    assert all((root / name / "dato").read_bytes() == b"ajeno" for name in look_alikes)
    assert (root / ("ws-" + "f" * 32)).read_bytes() == b"ajeno"


@pytest.mark.req("17.8")
def test_remove_rejects_a_workspace_outside_the_storage(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    elsewhere = WorkspaceManager(tmp_path).create()  # padre: tmp_path, no el almacenamiento
    _fill(elsewhere)

    with pytest.raises(OutsideStorageError):
        manager.remove(elsewhere)
    with pytest.raises(OutsideStorageError):
        manager.prune_for_completed(elsewhere)

    assert elsewhere.source_file.read_bytes() == b"origen"
    assert elsewhere.result_file.read_bytes() == b"resultado"


@pytest.mark.parametrize(
    "name", ["original", "ws-" + "0" * 31, "ws-" + "G" * 32, "..", "ws-" + "0" * 32 + "\n"]
)
def test_workspace_only_accepts_workspace_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="Espacio_de_Trabajo"):
        Workspace(tmp_path / name)


@pytest.mark.req("20.13")
def test_check_writable_leaves_the_storage_as_it_was(manager: WorkspaceManager) -> None:
    (manager.root / "notas.txt").write_bytes(b"ajeno")
    manager.check_writable()
    assert [p.name for p in manager.root.iterdir()] == ["notas.txt"]


@pytest.mark.req("20.13")
def test_check_writable_fails_when_the_storage_does_not_exist(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        WorkspaceManager(tmp_path / "no-existe").check_writable()
