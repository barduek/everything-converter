"""Pruebas de src/converter/api/tasks.py: vista, confirmación, rechazo de la confirmación y
cancelación de una Tarea (Req 3.3, 3.4, 3.7, 14.1, 14.2, 14.6, 14.11, 19.4, 19.5, 19.11).

La aplicación se arma con dependencias reales (`tests/api_support.py`) y se llama en el mismo
proceso, sin abrir puertos. Las Tareas se crean subiendo texto, que el Detector_de_Tipo clasifica
sin inspector, y se confirman contra tres pares de TXT: a HTML (Sin_Pérdida), a MD (Con_Pérdida)
y a PDF (Limitada). El convertidor de prueba espera a que la prueba active `gate`, así una Tarea
confirmada sigue en_proceso hasta que la prueba lo decide.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from converter.api.app import AppDeps, create_app
from converter.api.errors import TASK_UNAVAILABLE_BODY
from converter.messages_es import RequestError, render_error
from converter.models import LossClass, TaskState
from converter.storage.sweeper import CleanupKind
from tests.api_support import (
    CONVERTER,
    MARKER,
    PairSpec,
    WritingConverter,
    build_deps,
    client,
    create_task,
    wait_for,
)

ENV = {"CONVERTER_MIN_FREE_SPACE_MB": "0"}
PAIRS: list[PairSpec] = [
    ("TXT", "HTML"),
    ("TXT", "MD", LossClass.LOSSY),
    ("TXT", "PDF", LossClass.LIMITED),
]
TARGETS = ["HTML", "MD", "PDF"]  # en orden de registro
NAME = "Vacaciones 2024.txt"
TEXT = "Línea de texto con eñes y acentos: áéíóú.\n".encode()
# Firma de PNG: un Formato_de_Origen que ningún par registrado usa, así que se rechaza al detectar
UNSUPPORTED_SOURCE = b"\x89PNG\r\n\x1a\n" + bytes(32)
UNKNOWN_ID = "6f1c2c9e-8a0b-4d7e-9a35-2f0d4b8c1e77"  # UUID v4 que no es de ninguna Tarea
UUID1_ID = "6f1c2c9e-8a0b-1d7e-9a35-2f0d4b8c1e77"  # misma forma, pero versión 1


@dataclass(frozen=True)
class Env:
    deps: AppDeps
    api: httpx.AsyncClient
    converter: WritingConverter
    gate: asyncio.Event  # los pares no terminan hasta que la prueba lo active


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    gate = asyncio.Event()
    converter = WritingConverter(gate=gate)
    deps = build_deps(tmp_path, env=ENV, pairs=PAIRS, converters={CONVERTER: converter})
    async with client(create_app(deps)) as api:
        yield Env(deps, api, converter, gate)
    await deps.manager.aclose()


def _error(code: str, params: Mapping[str, object] | None = None) -> dict[str, object]:
    message = render_error(code, params)
    return {
        "error": {
            "code": message.code,
            "stage": None if message.stage is None else message.stage.value,
            "message": message.message,
            "action": message.action,
        }
    }


async def receive(env: Env, name: str = NAME, content: bytes = TEXT) -> str:
    """Sube un archivo y espera a que termine su detección."""
    task_id = await create_task(env.api, name, content)
    await wait_for(env.deps.manager, task_id, lambda view: view.detection == "completa")
    return task_id


async def post(
    env: Env, task_id: str, action: str, body: Mapping[str, object] | None = None
) -> httpx.Response:
    """`POST /api/tasks/{id}/{action}` con la cabecera de la Interfaz_de_Usuario."""
    return await env.api.post(f"/api/tasks/{task_id}/{action}", json=body, headers=MARKER)


async def confirm(env: Env, task_id: str, target: str, **fields: object) -> httpx.Response:
    return await post(env, task_id, "confirm", {"target": target, **fields})


async def reach_state(env: Env, state: TaskState) -> str:
    """Identificador de una Tarea nueva en el estado pedido."""
    if state is TaskState.FAILED:
        task_id = await create_task(env.api, "imagen.png", UNSUPPORTED_SOURCE)
        await wait_for(env.deps.manager, task_id, lambda view: view.state is TaskState.FAILED)
        return task_id
    task_id = await receive(env)
    if state is TaskState.RECEIVED:
        return task_id
    assert (await confirm(env, task_id, "HTML")).status_code == 200
    if state is TaskState.COMPLETED:
        env.gate.set()
        await wait_for(env.deps.manager, task_id, lambda view: view.state is state)
    elif state is TaskState.CANCELLED:
        assert (await post(env, task_id, "cancel")).status_code == 200
    else:
        assert state is TaskState.PROCESSING
    return task_id


# --- GET /api/tasks/{id} ---


@pytest.mark.req("14.2", "19.4")
@pytest.mark.asyncio
async def test_view_returns_the_task_with_its_offered_targets(env: Env) -> None:
    task_id = await receive(env)
    response = await env.api.get(f"/api/tasks/{task_id}")
    assert response.status_code == 200
    body = response.json()
    assert body == env.deps.manager.view(task_id).to_json()
    assert (body["id"], body["original_name"], body["state"], body["real_type"]) == (
        task_id,
        NAME,
        "recibida",
        "TXT",
    )
    assert [
        (target["format"], target["loss_class"], target["requires_confirmation"])
        for target in body["targets"]
    ] == [
        ("HTML", LossClass.LOSSLESS.value, False),
        ("MD", LossClass.LOSSY.value, True),
        ("PDF", LossClass.LIMITED.value, True),
    ]


@pytest.mark.req("14.2")
@pytest.mark.asyncio
async def test_view_of_a_rejected_source_has_the_failure_and_no_targets(env: Env) -> None:
    task_id = await reach_state(env, TaskState.FAILED)
    body = (await env.api.get(f"/api/tasks/{task_id}")).json()
    assert (body["state"], body["real_type"], body["targets"]) == ("fallida", "PNG", [])
    assert body["failure"]["message"] and body["failure"]["action"]


@pytest.mark.req("19.4")
@pytest.mark.asyncio
async def test_a_response_has_the_data_of_the_requested_task_only(env: Env) -> None:
    first = await receive(env, "primero.txt", b"uno")
    second = await receive(env, "segundo.txt", b"dos")
    for task_id, other, other_name in ((first, second, "segundo.txt"), (second, first, "primero")):
        response = await env.api.get(f"/api/tasks/{task_id}")
        assert response.json()["id"] == task_id
        assert other not in response.text and other_name not in response.text
    # Una solicitud sobre una Tarea no cambia las demás
    assert (await post(env, first, "cancel")).json()["state"] == "cancelada"
    assert (await env.api.get(f"/api/tasks/{second}")).json()["state"] == "recibida"


# --- Identificador_de_Tarea ---

ROUTES = [
    pytest.param("GET", "", id="vista"),
    pytest.param("POST", "/confirm", id="confirmar"),
    pytest.param("POST", "/decline", id="rechazar"),
    pytest.param("POST", "/cancel", id="cancelar"),
    pytest.param("GET", "/result", id="descargar"),
]
BAD_IDS = [
    UNKNOWN_ID,
    UNKNOWN_ID.upper(),
    UUID1_ID,
    UNKNOWN_ID.replace("-", ""),
    "no-es-un-id",
    "%20",
    "0" * 36,
]


@pytest.mark.req("19.5")
@pytest.mark.parametrize(("method", "suffix"), ROUTES)
@pytest.mark.asyncio
async def test_a_malformed_or_unknown_id_gets_the_same_404_in_every_route(
    env: Env, method: str, suffix: str
) -> None:
    existing = await receive(env)
    ids = [*BAD_IDS, existing.upper()]  # el identificador de una Tarea, en mayúsculas
    bodies: list[bytes | None] = [None, b'{"target": "HTML"}', b"no es json", b"[]"]
    responses = []
    for task_id in ids:
        for body in bodies:
            headers = {**MARKER, "Content-Type": "application/json"} if body else MARKER
            responses.append(
                await env.api.request(
                    method, f"/api/tasks/{task_id}{suffix}", content=body, headers=headers
                )
            )
    for response in responses:
        assert response.status_code == 404, response.request.url
        assert response.content == TASK_UNAVAILABLE_BODY, response.request.url
        assert dict(response.headers) == dict(responses[0].headers), response.request.url
    # Ninguna de esas solicitudes modificó la Tarea que sí existe
    assert (await env.api.get(f"/api/tasks/{existing}")).json()["state"] == "recibida"


@pytest.mark.req("19.5")
@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("GET", "/confirm"),
        ("GET", "/decline"),
        ("GET", "/cancel"),
        ("POST", ""),
        ("POST", "/result"),
    ],
)
@pytest.mark.asyncio
async def test_a_method_the_route_does_not_allow_gets_the_same_404(
    env: Env, method: str, suffix: str
) -> None:
    task_id = await receive(env)
    response = await env.api.request(method, f"/api/tasks/{task_id}{suffix}", headers=MARKER)
    assert response.status_code == 404
    assert response.content == TASK_UNAVAILABLE_BODY


# --- POST /api/tasks/{id}/confirm ---


@pytest.mark.req("14.1", "14.5")
@pytest.mark.asyncio
async def test_confirm_queues_the_task_and_it_runs_to_completion(env: Env) -> None:
    task_id = await receive(env)
    response = await confirm(env, task_id, "HTML")
    assert response.status_code == 200
    body = response.json()
    assert body == env.deps.manager.view(task_id).to_json()
    assert (body["state"], body["pair"], body["progress"], body["language"]) == (
        "en_proceso",  # hay lugar: el planificador la inició
        {"source": "TXT", "target": "HTML"},
        0,
        None,
    )
    env.gate.set()
    await wait_for(env.deps.manager, task_id, lambda view: view.state is TaskState.COMPLETED)
    body = (await env.api.get(f"/api/tasks/{task_id}")).json()
    assert body["state"] == "completada"
    assert body["download"] == {
        "name": "Vacaciones 2024.html",
        "url": f"/api/tasks/{task_id}/result",
        "expired": False,
    }


@pytest.mark.req("14.1")
@pytest.mark.asyncio
async def test_confirm_ignores_the_language_of_a_pair_that_is_not_a_transcription(
    env: Env,
) -> None:
    task_id = await receive(env)
    response = await confirm(env, task_id, "HTML", language="auto", acknowledge_loss=True)
    assert response.status_code == 200
    assert response.json()["language"] is None


@pytest.mark.req("3.3")
@pytest.mark.parametrize("target", ["MD", "PDF"])
@pytest.mark.parametrize("fields", [{}, {"acknowledge_loss": False}])
@pytest.mark.asyncio
async def test_confirm_of_a_lossy_pair_needs_the_loss_to_be_accepted(
    env: Env, target: str, fields: dict[str, object]
) -> None:
    task_id = await receive(env)
    before = (await env.api.get(f"/api/tasks/{task_id}")).json()
    response = await confirm(env, task_id, target, **fields)
    assert response.status_code == 409
    assert response.json() == _error(RequestError.CONFIRMATION_REQUIRED)
    assert (await env.api.get(f"/api/tasks/{task_id}")).json() == before  # sigue en recibida
    assert env.converter.runs == []

    accepted = await confirm(env, task_id, target, acknowledge_loss=True)
    assert accepted.status_code == 200
    assert accepted.json()["pair"] == {"source": "TXT", "target": target}


@pytest.mark.req("3.4")
@pytest.mark.parametrize("target", ["MP3", "html", "", "HTML "])
@pytest.mark.asyncio
async def test_confirm_of_a_target_that_is_not_offered_lists_the_offered_ones(
    env: Env, target: str
) -> None:
    task_id = await receive(env)
    before = (await env.api.get(f"/api/tasks/{task_id}")).json()
    response = await confirm(env, task_id, target, acknowledge_loss=True)
    assert response.status_code == 422
    params = {"destino": target, "tipo_real": "TXT", "destinos": TARGETS}
    expected = _error("destino_no_disponible", params)["error"]
    assert isinstance(expected, dict)
    assert response.json() == {"error": {**expected, "targets": TARGETS}}
    assert (await env.api.get(f"/api/tasks/{task_id}")).json() == before
    assert env.converter.runs == []


@pytest.mark.req("14.11")
@pytest.mark.parametrize(
    "state", [TaskState.PROCESSING, TaskState.COMPLETED, TaskState.CANCELLED, TaskState.FAILED]
)
@pytest.mark.asyncio
async def test_confirm_of_a_task_that_is_not_received_names_its_state(
    env: Env, state: TaskState
) -> None:
    task_id = await reach_state(env, state)
    before = (await env.api.get(f"/api/tasks/{task_id}")).json()
    response = await confirm(env, task_id, "HTML")
    assert response.status_code == 409
    assert response.json() == _error(RequestError.STATE_FORBIDS, {"estado": state.value})
    assert (await env.api.get(f"/api/tasks/{task_id}")).json() == before


INVALID_BODIES = [
    pytest.param(b"", id="vacío"),
    pytest.param(b"no es json", id="no-json"),
    pytest.param(b'{"target": "HTML"', id="truncado"),
    pytest.param(b'{"target": "\xff"}', id="no-utf8"),
    pytest.param(b"[]", id="arreglo"),
    pytest.param(b'"HTML"', id="texto"),
    pytest.param(b"{}", id="sin-target"),
    pytest.param(b'{"target": null}', id="target-nulo"),
    pytest.param(b'{"target": 5}', id="target-numérico"),
    pytest.param(b'{"target": ["HTML"]}', id="target-lista"),
    pytest.param(b'{"target": "HTML", "extra": 1}', id="clave-ajena"),
    pytest.param(b'{"target": "HTML", "acknowledge_loss": "true"}', id="loss-texto"),
    pytest.param(b'{"target": "HTML", "acknowledge_loss": 1}', id="loss-numérico"),
    pytest.param(b'{"target": "HTML", "acknowledge_loss": null}', id="loss-nulo"),
    pytest.param(b'{"target": "HTML", "language": 5}', id="idioma-numérico"),
    pytest.param(b'{"target": "' + b"x" * 20_000 + b'"}', id="demasiado-grande"),
]


@pytest.mark.req("3.3")
@pytest.mark.parametrize("body", INVALID_BODIES)
@pytest.mark.asyncio
async def test_confirm_with_an_invalid_body_is_a_400_and_changes_nothing(
    env: Env, body: bytes
) -> None:
    task_id = await receive(env)
    before = (await env.api.get(f"/api/tasks/{task_id}")).json()
    headers = {**MARKER, "Content-Type": "application/json"}
    response = await env.api.post(f"/api/tasks/{task_id}/confirm", content=body, headers=headers)
    assert response.status_code == 400
    assert response.json() == _error(RequestError.INVALID_REQUEST)
    assert (await env.api.get(f"/api/tasks/{task_id}")).json() == before
    assert env.converter.runs == []


@pytest.mark.req("3.3")
@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
@pytest.mark.asyncio
async def test_confirm_needs_a_json_content_type(env: Env, content_type: str | None) -> None:
    task_id = await receive(env)
    headers = dict(MARKER) if content_type is None else {**MARKER, "Content-Type": content_type}
    response = await env.api.post(
        f"/api/tasks/{task_id}/confirm", content=b'{"target": "HTML"}', headers=headers
    )
    assert response.status_code == 400
    assert response.json() == _error(RequestError.INVALID_REQUEST)
    assert (await env.api.get(f"/api/tasks/{task_id}")).json()["state"] == "recibida"


@pytest.mark.req("19.11")
@pytest.mark.asyncio
async def test_simultaneous_confirmations_are_served_one_at_a_time(env: Env) -> None:
    task_id = await receive(env)
    responses = await asyncio.gather(*(confirm(env, task_id, "HTML") for _ in range(5)))
    assert sorted(response.status_code for response in responses) == [200, 409, 409, 409, 409]
    rejected = [response for response in responses if response.status_code == 409]
    expected = _error(RequestError.STATE_FORBIDS, {"estado": "en_proceso"})
    assert all(response.json() == expected for response in rejected)
    await asyncio.sleep(0.01)
    assert env.converter.runs == [task_id]  # el par se ejecuta una sola vez


# --- POST /api/tasks/{id}/decline ---


@pytest.mark.req("3.7")
@pytest.mark.parametrize("target", ["MD", "PDF", "HTML"])
@pytest.mark.asyncio
async def test_decline_keeps_the_task_received_with_the_same_targets(env: Env, target: str) -> None:
    task_id = await receive(env)
    before = (await env.api.get(f"/api/tasks/{task_id}")).json()
    response = await post(env, task_id, "decline", {"target": target})
    assert response.status_code == 200
    body = response.json()
    assert body == before
    assert (body["state"], body["pair"]) == ("recibida", None)
    assert [item["format"] for item in body["targets"]] == TARGETS
    assert env.converter.runs == []
    # Después de rechazar, el usuario puede elegir otro destino
    assert (await confirm(env, task_id, "HTML")).status_code == 200


@pytest.mark.req("3.7")
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"", id="vacío"),
        pytest.param(b"no es json", id="no-json"),
        pytest.param(b"{}", id="sin-target"),
        pytest.param(b'{"target": 1}', id="target-numérico"),
        pytest.param(b'{"target": "MD", "acknowledge_loss": true}', id="clave-ajena"),
        pytest.param(b'{"target": "MD", "language": null}', id="clave-ajena-nula"),
    ],
)
@pytest.mark.asyncio
async def test_decline_with_an_invalid_body_is_a_400(env: Env, body: bytes) -> None:
    task_id = await receive(env)
    headers = {**MARKER, "Content-Type": "application/json"}
    response = await env.api.post(f"/api/tasks/{task_id}/decline", content=body, headers=headers)
    assert response.status_code == 400
    assert response.json() == _error(RequestError.INVALID_REQUEST)


@pytest.mark.req("3.7", "14.11")
@pytest.mark.parametrize("state", [TaskState.PROCESSING, TaskState.COMPLETED, TaskState.CANCELLED])
@pytest.mark.asyncio
async def test_decline_of_a_task_that_is_not_received_names_its_state(
    env: Env, state: TaskState
) -> None:
    task_id = await reach_state(env, state)
    response = await post(env, task_id, "decline", {"target": "MD"})
    assert response.status_code == 409
    assert response.json() == _error(RequestError.STATE_FORBIDS, {"estado": state.value})


# --- POST /api/tasks/{id}/cancel ---


@pytest.mark.req("14.6")
@pytest.mark.asyncio
async def test_cancel_of_a_received_task_cancels_it_and_removes_its_workspace(env: Env) -> None:
    task_id = await receive(env)
    response = await post(env, task_id, "cancel")  # sin cuerpo
    assert response.status_code == 200
    body = response.json()
    assert body == env.deps.manager.view(task_id).to_json()
    assert (body["state"], body["state_label"]) == ("cancelada", "Cancelada")
    assert [job.kind for job in env.deps.sweeper.pending] == [CleanupKind.REMOVE]
    assert (await env.api.get(f"/api/tasks/{task_id}")).json() == body


@pytest.mark.req("14.6")
@pytest.mark.asyncio
async def test_cancel_of_a_running_task_stops_its_conversion(env: Env) -> None:
    task_id = await reach_state(env, TaskState.PROCESSING)
    await asyncio.sleep(0.01)
    assert env.converter.runs == [task_id]
    response = await post(env, task_id, "cancel")
    assert response.status_code == 200
    assert response.json()["state"] == "cancelada"
    assert response.json()["progress"] is None
    env.gate.set()  # la conversión detenida no puede dejar un resultado
    await asyncio.sleep(0.01)
    assert (await env.api.get(f"/api/tasks/{task_id}")).json()["state"] == "cancelada"
    assert CleanupKind.REMOVE in [job.kind for job in env.deps.sweeper.pending]


@pytest.mark.req("14.6", "14.11")
@pytest.mark.parametrize(
    "state", [TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED], ids=lambda s: s.value
)
@pytest.mark.asyncio
async def test_cancel_of_a_task_in_a_terminal_state_names_its_state(
    env: Env, state: TaskState
) -> None:
    task_id = await reach_state(env, state)
    before = (await env.api.get(f"/api/tasks/{task_id}")).json()
    response = await post(env, task_id, "cancel")
    assert response.status_code == 409
    assert response.json() == _error(RequestError.STATE_FORBIDS, {"estado": state.value})
    assert (await env.api.get(f"/api/tasks/{task_id}")).json() == before


@pytest.mark.req("14.11", "19.11")
@pytest.mark.asyncio
async def test_simultaneous_cancellations_are_served_one_at_a_time(env: Env) -> None:
    task_id = await reach_state(env, TaskState.PROCESSING)
    responses = await asyncio.gather(*(post(env, task_id, "cancel") for _ in range(4)))
    assert sorted(response.status_code for response in responses) == [200, 409, 409, 409]
    expected = _error(RequestError.STATE_FORBIDS, {"estado": "cancelada"})
    assert all(r.json() == expected for r in responses if r.status_code == 409)


@pytest.mark.req("14.11", "19.11")
@pytest.mark.asyncio
async def test_a_confirmation_and_a_cancellation_at_the_same_time_leave_a_consistent_task(
    env: Env,
) -> None:
    task_id = await receive(env)
    confirmation, cancellation = await asyncio.gather(
        confirm(env, task_id, "HTML"), post(env, task_id, "cancel")
    )
    assert cancellation.status_code == 200  # cancelar se admite antes y después de confirmar
    assert confirmation.status_code in {200, 409}
    assert (await env.api.get(f"/api/tasks/{task_id}")).json()["state"] == "cancelada"
    if confirmation.status_code == 409:
        assert confirmation.json() == _error(RequestError.STATE_FORBIDS, {"estado": "cancelada"})
        assert env.converter.runs == []


@pytest.mark.req("19.5")
@pytest.mark.asyncio
async def test_requests_without_the_ui_header_do_not_reach_the_task(env: Env) -> None:
    task_id = await receive(env)
    for action in ("confirm", "decline", "cancel"):
        response = await env.api.post(f"/api/tasks/{task_id}/{action}", json={"target": "HTML"})
        assert response.status_code == 400, action
        assert response.json() == _error(RequestError.INVALID_REQUEST), action
    assert (await env.api.get(f"/api/tasks/{task_id}")).json()["state"] == "recibida"
