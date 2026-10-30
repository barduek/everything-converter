"""Pruebas de src/converter/models.py: valores en español de las enumeraciones, estados
terminales e inmutabilidad de fallos, rechazos y avisos (Req 4.1, 4.2, 14.3, 16.1, 16.12).

Los valores esperados se copian de "Data Models" en design.md para que un valor mal escrito en
models.py haga fallar las pruebas.
"""

from __future__ import annotations

import dataclasses
import json
from enum import StrEnum

import pytest

from converter.messages_es import FailureReason
from converter.models import (
    TERMINAL_STATES,
    Category,
    DestinationRejection,
    Failure,
    LossClass,
    Notice,
    Stage,
    TaskState,
)


@pytest.mark.req("4.1")
def test_category_values() -> None:
    assert [c.value for c in Category] == ["imagen", "documento", "audio", "video"]


@pytest.mark.req("4.2")
def test_loss_class_has_exactly_the_four_values() -> None:
    assert [c.value for c in LossClass] == ["Sin_Pérdida", "Con_Pérdida", "Limitada", "Aproximada"]


@pytest.mark.req("14.3")
def test_task_state_values() -> None:
    assert [s.value for s in TaskState] == [
        "recibida",
        "en_cola",
        "en_proceso",
        "completada",
        "fallida",
        "cancelada",
    ]


@pytest.mark.req("14.3")
def test_terminal_states_are_completed_failed_and_cancelled() -> None:
    assert isinstance(TERMINAL_STATES, frozenset)
    assert {s.value for s in TERMINAL_STATES} == {"completada", "fallida", "cancelada"}
    assert set(TaskState) - TERMINAL_STATES == {
        TaskState.RECEIVED,
        TaskState.QUEUED,
        TaskState.PROCESSING,
    }


@pytest.mark.req("16.1")
def test_stage_values() -> None:
    assert [s.value for s in Stage] == [
        "recepción",
        "detección",
        "conversión",
        "transcripción",
        "entrega",
    ]


@pytest.mark.parametrize("enum", [Category, LossClass, TaskState, Stage])
def test_enum_values_are_what_travels_in_the_api(enum: type[StrEnum]) -> None:
    for member in enum:
        assert str(member) == member.value
        assert json.loads(json.dumps(member)) == member.value
        assert enum(member.value) is member


@pytest.mark.req("16.1")
def test_failure_is_immutable() -> None:
    failure = Failure(reason=FailureReason.DAMAGED, stage=Stage.DETECTION, params={})
    assert failure.stage is Stage.DETECTION
    with pytest.raises(dataclasses.FrozenInstanceError):
        failure.stage = Stage.CONVERSION  # type: ignore[misc]


@pytest.mark.req("16.12")
def test_destination_rejection_is_immutable() -> None:
    rejection = DestinationRejection(code="video_sin_audio", params={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        rejection.code = "dimensiones_excedidas"  # type: ignore[misc]


def test_notice_is_immutable_and_compares_by_value() -> None:
    notice = Notice(code="bits_reducidos", params={"bits": 16})
    assert notice == Notice(code="bits_reducidos", params={"bits": 16})
    assert notice != Notice(code="bits_reducidos", params={"bits": 8})
    with pytest.raises(dataclasses.FrozenInstanceError):
        notice.code = "sin_voz"  # type: ignore[misc]
