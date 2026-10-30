"""Pruebas de src/converter/tasks/state_machine.py: tabla de transiciones, pares no admitidos y
Estados_Terminales (Req 14.3, 14.9, 14.11).

La tabla esperada se copia de "Gestor_de_Tareas" en design.md, y no se importa del módulo, para
que una fila mal escrita en state_machine.py haga fallar las pruebas. Como hay 6 estados y 9
eventos, las pruebas recorren los 54 pares.
"""

from __future__ import annotations

import itertools
from typing import Any, cast

import pytest

from converter.models import TERMINAL_STATES, Failure, Stage, TaskState
from converter.tasks.state_machine import (
    CancelRequested,
    Completed,
    ConfirmAccepted,
    Failed,
    InternalError,
    InvalidTransition,
    RetentionExpired,
    SourceRejected,
    Started,
    TaskEvent,
    TimedOut,
    transition,
)

_DAMAGED = Failure(reason=cast(Any, "archivo_danado"), stage=Stage.DETECTION, params={})
_INTERNAL = Failure(reason=cast(Any, "error_interno"), stage=Stage.CONVERSION, params={})
_TIMEOUT = Failure(reason=cast(Any, "tiempo_excedido"), stage=Stage.TRANSCRIPTION, params={})
_NO_SPACE = Failure(reason=cast(Any, "sin_espacio_proceso"), stage=Stage.CONVERSION, params={})

# Un ejemplo de cada uno de los 9 eventos
_EVENTS: list[TaskEvent] = [
    ConfirmAccepted(),
    SourceRejected(_DAMAGED),
    InternalError(_INTERNAL),
    CancelRequested(),
    RetentionExpired(),
    Started(),
    Completed(),
    Failed(_NO_SPACE),
    TimedOut(_TIMEOUT),
]

_R, _Q, _P = TaskState.RECEIVED, TaskState.QUEUED, TaskState.PROCESSING
_DONE, _FAIL, _CANCEL = TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED

_EXPECTED: dict[tuple[TaskState, type[TaskEvent]], TaskState] = {
    (_R, ConfirmAccepted): _Q,
    (_R, SourceRejected): _FAIL,
    (_R, InternalError): _FAIL,
    (_R, CancelRequested): _CANCEL,
    (_R, RetentionExpired): _CANCEL,
    (_Q, Started): _P,
    (_Q, CancelRequested): _CANCEL,
    (_P, Completed): _DONE,
    (_P, Failed): _FAIL,
    (_P, TimedOut): _FAIL,
    (_P, InternalError): _FAIL,
    (_P, CancelRequested): _CANCEL,
}

_ALL_PAIRS = list(itertools.product(TaskState, _EVENTS))


def _id(state: TaskState, event: TaskEvent) -> str:
    return f"{state.value}-{type(event).__name__}"


# Los 54 pares (estado, evento), separados según estén o no en la tabla
_LISTED = [pytest.param(s, e, id=_id(s, e)) for s, e in _ALL_PAIRS if (s, type(e)) in _EXPECTED]
_UNLISTED = [
    pytest.param(s, e, id=_id(s, e)) for s, e in _ALL_PAIRS if (s, type(e)) not in _EXPECTED
]


def test_parametrization_covers_the_54_pairs() -> None:
    assert len(_LISTED) == len(_EXPECTED) == 12
    assert len(_UNLISTED) == 6 * 9 - 12


@pytest.mark.req("14.3")
@pytest.mark.parametrize(("state", "event"), _LISTED)
def test_listed_pair_moves_to_the_new_state(state: TaskState, event: TaskEvent) -> None:
    assert transition(state, event) is _EXPECTED[(state, type(event))]


@pytest.mark.req("14.3", "14.9", "14.11")
@pytest.mark.parametrize(("state", "event"), _UNLISTED)
def test_unlisted_pair_is_invalid_and_keeps_the_state(state: TaskState, event: TaskEvent) -> None:
    assert transition(state, event) == InvalidTransition(state=state, event=event)


@pytest.mark.req("14.3")
def test_state_changes_are_exactly_the_eight_transitions_of_req_14_3() -> None:
    changes: set[tuple[TaskState, TaskState]] = set()
    for state, event in _ALL_PAIRS:
        result = transition(state, event)
        if isinstance(result, TaskState):
            changes.add((state, result))
    assert changes == {
        (_R, _Q),
        (_R, _FAIL),
        (_R, _CANCEL),
        (_Q, _P),
        (_Q, _CANCEL),
        (_P, _DONE),
        (_P, _FAIL),
        (_P, _CANCEL),
    }


@pytest.mark.req("14.9")
@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES), ids=str)
def test_terminal_state_never_changes_under_any_event_sequence(terminal: TaskState) -> None:
    # Dos pasadas por los 9 eventos, incluidos los que llegan tarde tras una cancelación
    state = terminal
    for event in _EVENTS * 2:
        result = transition(state, event)
        assert isinstance(result, InvalidTransition)
        state = result.state
    assert state is terminal


@pytest.mark.req("14.3", "14.9")
def test_full_lifecycle_then_late_events_are_ignored() -> None:
    steps: list[tuple[TaskEvent, TaskState]] = [
        (ConfirmAccepted(), _Q),
        (Started(), _P),
        (CancelRequested(), _CANCEL),
    ]
    state = TaskState.RECEIVED
    for event, expected in steps:
        result = transition(state, event)
        assert isinstance(result, TaskState)
        assert result is expected
        state = result
    # El subproceso termina o vence después de la cancelación: la Tarea sigue cancelada
    assert transition(state, Completed()) == InvalidTransition(_CANCEL, Completed())
    assert transition(state, TimedOut(_TIMEOUT)) == InvalidTransition(_CANCEL, TimedOut(_TIMEOUT))


@pytest.mark.req("14.11")
@pytest.mark.parametrize("state", [_Q, _P, _DONE, _FAIL, _CANCEL], ids=str)
def test_confirmation_outside_received_reports_the_current_state(state: TaskState) -> None:
    result = transition(state, ConfirmAccepted())
    assert isinstance(result, InvalidTransition)
    assert result.state is state


@pytest.mark.req("14.11")
@pytest.mark.parametrize("state", sorted(TERMINAL_STATES), ids=str)
def test_cancellation_of_a_terminal_task_reports_the_current_state(state: TaskState) -> None:
    result = transition(state, CancelRequested())
    assert isinstance(result, InvalidTransition)
    assert result.state is state


@pytest.mark.req("16.1")
def test_failing_events_carry_their_failure() -> None:
    assert SourceRejected(_DAMAGED).failure is _DAMAGED
    assert InternalError(_INTERNAL).failure is _INTERNAL
    assert Failed(_NO_SPACE).failure is _NO_SPACE
    assert TimedOut(_TIMEOUT).failure is _TIMEOUT
