"""Pruebas de src/converter/tasks/scheduler.py: cola FIFO global, Límite_de_Concurrencia y
salida de Tareas de la cola o de en_proceso (Req 14.4, 14.10, 19.8)."""

from __future__ import annotations

import pytest

from converter.tasks.scheduler import SchedulerCore


def _scheduler(limit: int, *queued: str) -> SchedulerCore:
    scheduler = SchedulerCore(limit=limit)
    for task_id in queued:
        scheduler.enqueue(task_id)
    return scheduler


@pytest.mark.req("14.13")
def test_starts_without_tasks() -> None:
    scheduler = SchedulerCore(limit=2)
    assert list(scheduler.queue) == []
    assert scheduler.running == set()
    assert scheduler.dispatch() == []


@pytest.mark.parametrize("limit", [0, -1])
def test_rejects_limit_below_one(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        SchedulerCore(limit=limit)


@pytest.mark.req("14.10")
def test_enqueue_does_not_start_tasks_until_dispatch() -> None:
    scheduler = _scheduler(2, "a")
    assert list(scheduler.queue) == ["a"]
    assert scheduler.running == set()


@pytest.mark.req("14.4", "14.10", "19.8")
def test_dispatch_starts_oldest_tasks_up_to_the_limit() -> None:
    scheduler = _scheduler(2, "a", "b", "c", "d")
    assert scheduler.dispatch() == ["a", "b"]
    assert scheduler.running == {"a", "b"}
    assert list(scheduler.queue) == ["c", "d"]


@pytest.mark.req("14.4")
def test_full_scheduler_keeps_queued_tasks_waiting() -> None:
    scheduler = _scheduler(1, "a", "b")
    scheduler.dispatch()
    scheduler.enqueue("c")
    assert scheduler.dispatch() == []
    assert scheduler.running == {"a"}
    assert list(scheduler.queue) == ["b", "c"]


@pytest.mark.req("14.10", "19.8")
def test_finished_task_frees_its_place_for_the_oldest_queued_task() -> None:
    scheduler = _scheduler(2, "a", "b", "c", "d")
    scheduler.dispatch()
    scheduler.remove("b")  # se completó, falló o se canceló en_proceso
    assert scheduler.running == {"a"}
    assert scheduler.dispatch() == ["c"]
    assert scheduler.running == {"a", "c"}
    assert list(scheduler.queue) == ["d"]


@pytest.mark.req("19.8")
def test_cancelled_queued_task_leaves_the_others_in_order() -> None:
    scheduler = _scheduler(1, "a", "b", "c", "d")
    scheduler.dispatch()
    scheduler.remove("c")  # cancelación en_cola
    assert list(scheduler.queue) == ["b", "d"]
    assert scheduler.dispatch() == []  # sacarla de la cola no libera un lugar en_proceso
    scheduler.remove("a")
    assert scheduler.dispatch() == ["b"]


@pytest.mark.req("19.8")
def test_tasks_enqueued_after_dispatch_wait_behind_the_older_ones() -> None:
    scheduler = _scheduler(1, "a", "b")
    scheduler.dispatch()
    scheduler.enqueue("c")
    scheduler.remove("a")
    assert scheduler.dispatch() == ["b"]
    scheduler.remove("b")
    assert scheduler.dispatch() == ["c"]


def test_remove_of_unknown_or_already_removed_task_changes_nothing() -> None:
    scheduler = _scheduler(1, "a", "b")
    scheduler.dispatch()
    scheduler.remove("a")
    scheduler.remove("a")
    scheduler.remove("x")
    assert scheduler.running == set()
    assert list(scheduler.queue) == ["b"]


_RUNNING_ID = "6f1c2a4e-8b3d-4c5f-9a7e-1d2b3c4d5e6f"
_QUEUED_ID = "0a9b8c7d-6e5f-4a3b-8c2d-1e0f9a8b7c6d"


@pytest.mark.parametrize("task_id", [_RUNNING_ID, _QUEUED_ID], ids=["en_proceso", "en_cola"])
def test_enqueue_rejects_a_task_already_in_the_scheduler(task_id: str) -> None:
    scheduler = _scheduler(1, _RUNNING_ID, _QUEUED_ID)
    scheduler.dispatch()
    with pytest.raises(ValueError) as excinfo:
        scheduler.enqueue(task_id)
    assert task_id not in str(excinfo.value)  # el Identificador no queda en el mensaje
    assert scheduler.running == {_RUNNING_ID}
    assert list(scheduler.queue) == [_QUEUED_ID]
