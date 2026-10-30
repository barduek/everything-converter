"""Pruebas de src/converter/storage/sweeper.py con un reloj simulado y raíces temporales.

Los borrados corren de verdad sobre un `WorkspaceManager` en un directorio temporal. Los fallos
también son reales: un directorio sin permiso de escritura dentro del Espacio_de_Trabajo (que el
usuario root no respeta, por eso esas pruebas se omiten como root) o un Espacio_de_Trabajo fuera
del Almacenamiento_Temporal, que `WorkspaceManager` rechaza siempre.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from converter.eventlog import EventLog
from converter.storage.sweeper import (
    MAX_RETRIES,
    RETRY_DELAY_S,
    SWEEP_INTERVAL_S,
    CleanupKind,
    Sweeper,
)
from converter.storage.workspace import RESULT_DIR, Workspace, WorkspaceManager

FIXED_TS = datetime(2025, 1, 15, 10, 32, 7, 412000, tzinfo=UTC)

needs_permissions = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignora los permisos de los directorios"
)


class FakeClock:
    """Reloj monotónico que solo avanza cuando la prueba lo pide."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Env:
    def __init__(self, root: Path) -> None:
        self.manager = WorkspaceManager(root)
        self.clock = FakeClock()
        self.stream = io.StringIO()
        self.sweeper = Sweeper(
            self.manager, EventLog(self.stream, clock=lambda: FIXED_TS), clock=self.clock
        )

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines()]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    root = tmp_path / "storage"
    root.mkdir()
    return Env(root)


def _fill(ws: Workspace) -> None:
    ws.source_file.write_bytes(b"origen")
    (ws.work_dir / "parcial.bin").write_bytes(b"intermedio")
    ws.result_file.write_bytes(b"resultado")


@pytest.fixture
def locked(env: Env) -> Iterator[tuple[Workspace, Path]]:
    """Espacio_de_Trabajo con un directorio sin permiso de escritura que impide borrarlo."""
    ws = env.manager.create()
    _fill(ws)
    blocker = ws.work_dir / "bloqueado"
    blocker.mkdir()
    (blocker / "archivo").write_bytes(b"x")
    blocker.chmod(0o500)
    try:
        yield ws, blocker
    finally:
        if blocker.exists():
            blocker.chmod(0o700)


def _outside(tmp_path: Path) -> Workspace:
    """Espacio_de_Trabajo fuera del Almacenamiento_Temporal: borrarlo falla siempre."""
    path = tmp_path / "ajeno" / ("ws-" + "a" * 32)
    path.mkdir(parents=True)
    (path / "archivo").write_bytes(b"ajeno")
    return Workspace(path)


@pytest.mark.req("17.9")
def test_timing_constants_match_the_spec() -> None:
    assert SWEEP_INTERVAL_S == 5
    assert RETRY_DELAY_S == 60
    assert MAX_RETRIES == 3


@pytest.mark.asyncio
@pytest.mark.req("1.3", "1.4", "1.8", "1.10", "17.3", "17.4", "17.10")
async def test_enqueued_removal_runs_on_the_next_pass(env: Env) -> None:
    reception = env.manager.create()
    task = env.manager.create()
    _fill(reception)
    _fill(task)
    env.sweeper.enqueue_remove(reception, task_id=None)
    env.sweeper.enqueue_remove(task, task_id="t1")
    # Encolar no borra: el borrado lo hace la pasada
    assert reception.path.exists() and task.path.exists()

    assert await env.sweeper.run_due() == 2

    assert not reception.path.exists() and not task.path.exists()
    assert env.sweeper.pending == ()
    assert env.events() == []
    assert await env.sweeper.run_due() == 0


@pytest.mark.asyncio
@pytest.mark.req("17.2")
async def test_enqueued_prune_keeps_only_the_result(env: Env) -> None:
    ws = env.manager.create()
    _fill(ws)
    env.sweeper.enqueue_prune(ws, task_id="t1")

    assert await env.sweeper.run_due() == 1

    assert [entry.name for entry in ws.path.iterdir()] == [RESULT_DIR]
    assert ws.result_file.read_bytes() == b"resultado"
    assert env.events() == []


@pytest.mark.asyncio
@pytest.mark.req("17.9")
@needs_permissions
async def test_failed_removal_is_logged_and_retried_60_seconds_later(
    env: Env, locked: tuple[Workspace, Path]
) -> None:
    ws, blocker = locked
    env.sweeper.enqueue_remove(ws, task_id="t1")

    assert await env.sweeper.run_due() == 1
    assert ws.path.exists()
    (first,) = env.events()
    assert first["event"] == "cleanup_failed"
    assert first["level"] == "warning"
    assert (first["task_id"], first["workspace"]) == ("t1", ws.name)
    assert (first["attempt"], first["will_retry"]) == (1, True)
    assert (first["detail"]["exception"], first["detail"]["errno"]) == (
        "PermissionError",
        "EACCES",
    )

    # Antes de los 60 segundos no se reintenta
    env.clock.advance(RETRY_DELAY_S - 0.5)
    assert await env.sweeper.run_due() == 0
    env.clock.advance(0.5)
    assert await env.sweeper.run_due() == 1
    assert [event["attempt"] for event in env.events()] == [1, 2]

    blocker.chmod(0o700)  # el motivo del fallo desaparece
    env.clock.advance(RETRY_DELAY_S)
    assert await env.sweeper.run_due() == 1

    assert not ws.path.exists()
    assert len(env.events()) == 2
    assert env.sweeper.pending == ()


@pytest.mark.asyncio
@pytest.mark.req("17.9", "17.8")
async def test_gives_up_after_three_retries_and_logs_the_last_as_error(
    env: Env, tmp_path: Path
) -> None:
    ws = _outside(tmp_path)
    env.sweeper.enqueue_remove(ws, task_id="t2")

    assert await env.sweeper.run_due() == 1
    for _ in range(MAX_RETRIES):
        env.clock.advance(RETRY_DELAY_S)
        assert await env.sweeper.run_due() == 1

    events = env.events()
    assert [event["attempt"] for event in events] == [1, 2, 3, 4]
    assert [event["level"] for event in events] == ["warning", "warning", "warning", "error"]
    assert [event["will_retry"] for event in events] == [True, True, True, False]
    assert {event["task_id"] for event in events} == {"t2"}
    assert {event["detail"]["exception"] for event in events} == {
        "converter.storage.workspace.OutsideStorageError"
    }
    assert env.sweeper.pending == ()
    env.clock.advance(10 * RETRY_DELAY_S)
    assert await env.sweeper.run_due() == 0
    assert len(env.events()) == 4
    # El espacio está fuera del almacenamiento: no se tocó (Req 17.8)
    assert (ws.path / "archivo").read_bytes() == b"ajeno"


@pytest.mark.asyncio
@pytest.mark.req("17.9", "17.5")
@needs_permissions
async def test_startup_purge_failures_are_logged_without_task_and_retried(
    env: Env, locked: tuple[Workspace, Path]
) -> None:
    ws, blocker = locked
    failures = env.manager.purge_all_workspaces()
    assert [failure.workspace for failure in failures] == [ws]

    env.sweeper.enqueue_purge_failures(failures)

    (first,) = env.events()
    assert (first["task_id"], first["workspace"]) == (None, ws.name)
    assert (first["attempt"], first["will_retry"]) == (1, True)
    # El primer intento fue el de la purga: el siguiente espera 60 segundos
    assert await env.sweeper.run_due() == 0
    blocker.chmod(0o700)
    env.clock.advance(RETRY_DELAY_S)
    assert await env.sweeper.run_due() == 1
    assert not ws.path.exists()
    assert len(env.events()) == 1


@pytest.mark.asyncio
@pytest.mark.req("17.2", "17.3", "17.4")
async def test_one_pending_cleanup_per_workspace_and_removal_covers_pruning(env: Env) -> None:
    ws = env.manager.create()
    _fill(ws)
    env.sweeper.enqueue_prune(ws, task_id="t1")
    env.sweeper.enqueue_remove(ws, task_id="t1")
    env.sweeper.enqueue_prune(ws, task_id="t1")
    env.sweeper.enqueue_remove(ws, task_id="t1")

    assert [job.kind for job in env.sweeper.pending] == [CleanupKind.REMOVE]
    assert await env.sweeper.run_due() == 1
    assert not ws.path.exists()


@pytest.mark.asyncio
@pytest.mark.req("17.9")
async def test_repeated_request_does_not_restart_the_retry_count(env: Env, tmp_path: Path) -> None:
    ws = _outside(tmp_path)
    env.sweeper.enqueue_remove(ws, task_id="t3")
    assert await env.sweeper.run_due() == 1

    # Otro pedido del mismo borrado mientras espera el reintento no adelanta ni duplica nada
    env.sweeper.enqueue_remove(ws, task_id="t3")
    assert await env.sweeper.run_due() == 0
    (job,) = env.sweeper.pending
    assert job.attempt == 2
    assert len(env.events()) == 1


@pytest.mark.asyncio
@pytest.mark.req("17.4")
async def test_run_loop_sweeps_periodically_until_cancelled(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    manager = WorkspaceManager(root)
    sweeper = Sweeper(manager, EventLog(io.StringIO()), interval=0.01)
    loop = asyncio.create_task(sweeper.run())
    try:
        ws = manager.create()
        sweeper.enqueue_remove(ws, task_id="t1")
        async with asyncio.timeout(5):
            while ws.path.exists():
                await asyncio.sleep(0.01)
        assert not loop.done()
    finally:
        loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop


def test_interval_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="interval"):
        Sweeper(WorkspaceManager(tmp_path), EventLog(io.StringIO()), interval=0)
