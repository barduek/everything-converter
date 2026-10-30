"""Pruebas de src/converter/api/download.py: la descarga del Archivo_Resultado en
`GET /api/tasks/{id}/result` (Req 15.8, 15.9, 15.11, 16.6, 18.10, 19.4, 19.5).

La aplicación se arma con dependencias reales (`tests/api_support.py`) y se llama en el mismo
proceso. El convertidor de prueba copia el Archivo_de_Origen a `work/output`, así cada resultado
es reconocible por su contenido. El reloj del Gestor_de_Tareas es manual, para vencer el
Período_de_Retención sin esperar. El Archivo_Resultado llega a `result/output` por el camino
real: el Gestor_de_Tareas lo entrega y el barrendero poda el resto del Espacio_de_Trabajo.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

import httpx
import pytest

from converter.api.app import AppDeps, create_app
from converter.api.download import ascii_fallback, content_disposition
from converter.api.errors import TASK_UNAVAILABLE_BODY
from converter.api.security import SECURITY_HEADERS
from converter.config import Config
from converter.converters.base import ConversionContext, ConversionOutcome
from converter.messages_es import RequestError, render_error
from converter.models import TaskState
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
PAIRS: list[PairSpec] = [("TXT", "HTML"), ("TXT", "PDF"), ("TXT", "MD")]
NAME = "Vacaciones 2024.txt"
TEXT = "Línea de texto con eñes y acentos: áéíóú.\n".encode()
CLOCK_START = 1_000.0
UNKNOWN_ID = "6f1c2c9e-8a0b-4d7e-9a35-2f0d4b8c1e77"  # UUID v4 que no es de ninguna Tarea
# Un nombre de `Content-Disposition` válido: respaldo ASCII imprimible sin `"`, `\` ni `%`, y
# `filename*` con solo attr-char de RFC 8187
_HEADER = re.compile(
    r"""attachment; filename="([ !#$&-\[\]-~]*)"; filename\*=UTF-8''([A-Za-z0-9!#$&+.^_`|~%-]*)"""
)


class ManualClock:
    """Reloj monotónico que solo avanza cuando la prueba lo pide."""

    def __init__(self) -> None:
        self.now = CLOCK_START

    def __call__(self) -> float:
        return self.now


class CopyConverter(WritingConverter):
    """Convertidor `CONVERTER` cuyo resultado es una copia del Archivo_de_Origen."""

    def __init__(self, kind: Literal["single", "zip"] = "single") -> None:
        super().__init__()
        self.kind: Literal["single", "zip"] = kind

    async def run(self, ctx: ConversionContext) -> ConversionOutcome:
        self.runs.append(ctx.task_id)
        path = ctx.workspace.work_dir / "output"
        path.write_bytes(ctx.workspace.source_file.read_bytes())
        return ConversionOutcome(path, self.kind)


@dataclass(frozen=True)
class Env:
    deps: AppDeps
    api: httpx.AsyncClient
    clock: ManualClock

    @property
    def settings(self) -> Config:
        return self.deps.config


@asynccontextmanager
async def environment(
    tmp_path: Path,
    converter: WritingConverter | None = None,
    variables: Mapping[str, str] | None = None,
) -> AsyncIterator[Env]:
    clock = ManualClock()
    deps = build_deps(
        tmp_path,
        env={**ENV, **(variables or {})},
        pairs=PAIRS,
        converters={CONVERTER: converter or CopyConverter()},
        clock=clock,
    )
    async with client(create_app(deps)) as api:
        yield Env(deps, api, clock)
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
    task_id = await create_task(env.api, name, content)
    await wait_for(env.deps.manager, task_id, lambda view: view.detection == "completa")
    return task_id


async def complete(env: Env, name: str = NAME, content: bytes = TEXT, target: str = "HTML") -> str:
    """Identificador de una Tarea nueva ya completada."""
    task_id = await receive(env, name, content)
    response = await env.api.post(
        f"/api/tasks/{task_id}/confirm",
        json={"target": target, "acknowledge_loss": True},
        headers=MARKER,
    )
    assert response.status_code == 200, response.text
    await wait_for(env.deps.manager, task_id, lambda view: view.state is TaskState.COMPLETED)
    return task_id


def _headers(response: httpx.Response, name: str) -> list[str]:
    return response.headers.get_list(name)


# --- Descarga de una Tarea completada ---


@pytest.mark.req("15.9", "18.10")
@pytest.mark.asyncio
async def test_download_of_a_completed_task_is_the_result_with_the_security_headers(
    tmp_path: Path,
) -> None:
    async with environment(tmp_path) as env:
        task_id = await complete(env)
        await env.deps.sweeper.run_due()  # la poda de la entrega no toca el resultado
        response = await env.api.get(f"/api/tasks/{task_id}/result")
    assert response.status_code == 200
    assert response.content == TEXT
    assert _headers(response, "content-type") == ["text/html; charset=utf-8"]
    assert _headers(response, "content-disposition") == [
        "attachment; filename=\"Vacaciones 2024.html\"; filename*=UTF-8''Vacaciones%202024.html"
    ]
    assert _headers(response, "x-content-type-options") == ["nosniff"]
    assert _headers(response, "content-security-policy") == ["sandbox"]
    assert _headers(response, "cache-control") == ["no-store"]
    assert _headers(response, "content-length") == [str(len(TEXT))]
    # Las demás cabeceras de seguridad de la aplicación no se pierden en la descarga
    for name, value in SECURITY_HEADERS:
        if name not in {"content-security-policy", "x-content-type-options"}:
            assert _headers(response, name) == [value], name


@pytest.mark.req("15.9")
@pytest.mark.asyncio
async def test_every_download_of_the_same_result_is_byte_identical(tmp_path: Path) -> None:
    # Más de 64 KiB (el tamaño de las partes de FileResponse), con caracteres de varios bytes
    content = "".join(f"línea {n}: ñandú 😀\r\n" for n in range(12_000)).encode()
    assert len(content) > 3 * 65_536
    async with environment(tmp_path) as env:
        task_id = await complete(env, "datos.txt", content)
        downloads = [await env.api.get(f"/api/tasks/{task_id}/result") for _ in range(3)]
    for response in downloads:
        assert (response.status_code, response.content) == (200, content)
        assert dict(response.headers) == dict(downloads[0].headers)


@pytest.mark.req("15.9", "18.10")
@pytest.mark.asyncio
async def test_a_range_request_gets_the_requested_part_with_the_same_headers(
    tmp_path: Path,
) -> None:
    async with environment(tmp_path) as env:
        task_id = await complete(env)
        response = await env.api.get(f"/api/tasks/{task_id}/result", headers={"Range": "bytes=2-6"})
    assert response.status_code == 206
    assert response.content == TEXT[2:7]
    assert _headers(response, "content-security-policy") == ["sandbox"]
    assert _headers(response, "cache-control") == ["no-store"]
    assert "attachment" in _headers(response, "content-disposition")[0]


@pytest.mark.req("15.2", "18.10")
@pytest.mark.parametrize(
    ("original", "disposition"),
    [
        pytest.param(
            "Vacaciones 2024.TXT",
            'attachment; filename="Vacaciones 2024.html"; '
            "filename*=UTF-8''Vacaciones%202024.html",
            id="ascii",
        ),
        pytest.param(
            "Año ñandú.txt",
            'attachment; filename="A_o _and_.html"; '
            "filename*=UTF-8''A%C3%B1o%20%C3%B1and%C3%BA.html",
            id="acentos",
        ),
        pytest.param(
            "😀 emoji.txt",
            "attachment; filename=\"_ emoji.html\"; filename*=UTF-8''%F0%9F%98%80%20emoji.html",
            id="emoji",
        ),
        pytest.param(
            "100% \"real\" (copia) 'x'; y,z.txt",
            "attachment; filename=\"100_ _real_ (copia) 'x'; y,z.html\"; "
            "filename*=UTF-8''100%25%20_real_%20%28copia%29%20%27x%27%3B%20y%2Cz.html",
            id="signos",
        ),
        pytest.param(
            "línea\r\nSet-Cookie: x=1.txt",
            'attachment; filename="l_nea__Set-Cookie_ x=1.html"; '
            "filename*=UTF-8''l%C3%ADnea__Set-Cookie_%20x%3D1.html",
            id="saltos-de-línea",
        ),
        pytest.param(
            ".txt",
            "attachment; filename=\"resultado.html\"; filename*=UTF-8''resultado.html",
            id="sin-base",
        ),
    ],
)
@pytest.mark.asyncio
async def test_the_download_name_has_an_ascii_fallback_and_an_rfc_6266_filename(
    tmp_path: Path, original: str, disposition: str
) -> None:
    async with environment(tmp_path) as env:
        task_id = await complete(env, original)
        response = await env.api.get(f"/api/tasks/{task_id}/result")
    assert response.status_code == 200
    assert _headers(response, "content-disposition") == [disposition]
    assert "set-cookie" not in response.headers  # el nombre no puede inyectar otra cabecera


@pytest.mark.req("15.2", "15.10")
@pytest.mark.asyncio
async def test_a_zip_result_is_served_with_the_zip_content_type_and_name(tmp_path: Path) -> None:
    async with environment(tmp_path, CopyConverter("zip")) as env:
        task_id = await complete(env, "informe.txt", b"PK\x03\x04 contenido")
        response = await env.api.get(f"/api/tasks/{task_id}/result")
    assert response.status_code == 200
    assert _headers(response, "content-type") == ["application/zip"]
    assert _headers(response, "content-disposition") == [
        "attachment; filename=\"informe.zip\"; filename*=UTF-8''informe.zip"
    ]


@pytest.mark.req("18.10")
@pytest.mark.parametrize(
    ("target", "media_type"),
    [
        ("HTML", "text/html; charset=utf-8"),
        ("PDF", "application/pdf"),
        ("MD", "text/markdown; charset=utf-8"),
    ],
)
@pytest.mark.asyncio
async def test_the_content_type_is_the_one_of_the_target_format(
    tmp_path: Path, target: str, media_type: str
) -> None:
    async with environment(tmp_path) as env:
        task_id = await complete(env, target=target)
        response = await env.api.get(f"/api/tasks/{task_id}/result")
    assert _headers(response, "content-type") == [media_type]


@pytest.mark.req("19.4")
@pytest.mark.asyncio
async def test_each_task_downloads_its_own_result(tmp_path: Path) -> None:
    async with environment(tmp_path) as env:
        first = await complete(env, "primero.txt", b"contenido del primero")
        second = await complete(env, "segundo.txt", b"contenido del segundo")
        one = await env.api.get(f"/api/tasks/{first}/result")
        two = await env.api.get(f"/api/tasks/{second}/result")
    assert (one.content, two.content) == (b"contenido del primero", b"contenido del segundo")
    assert "primero.html" in one.headers["content-disposition"]
    assert "segundo.html" in two.headers["content-disposition"]


# --- Tarea que no está completada ---


async def _reach(env: Env, state: TaskState, gate: asyncio.Event) -> str:
    """Tarea nueva en el estado pedido, que no es completada."""
    task_id = await receive(env)
    if state is TaskState.RECEIVED:
        return task_id
    confirmed = await env.api.post(
        f"/api/tasks/{task_id}/confirm", json={"target": "HTML"}, headers=MARKER
    )
    assert confirmed.status_code == 200
    if state is TaskState.CANCELLED:
        cancelled = await env.api.post(f"/api/tasks/{task_id}/cancel", headers=MARKER)
        assert cancelled.status_code == 200
    elif state is TaskState.FAILED:
        gate.set()
        await wait_for(env.deps.manager, task_id, lambda view: view.state is state)
    return task_id


@pytest.mark.req("15.11", "16.6")
@pytest.mark.parametrize(
    "state",
    [
        TaskState.RECEIVED,
        TaskState.QUEUED,
        TaskState.PROCESSING,
        TaskState.FAILED,
        TaskState.CANCELLED,
    ],
    ids=lambda state: state.value,
)
@pytest.mark.asyncio
async def test_a_task_that_is_not_completed_has_no_download(
    tmp_path: Path, state: TaskState
) -> None:
    gate = asyncio.Event()
    # Un solo lugar de ejecución: la primera Tarea ocupa el lugar y la segunda queda en_cola.
    # El resultado parcial de una Tarea fallida es un archivo en `work/` que no se entrega
    partial = b"resultado parcial"
    converter = WritingConverter(partial, gate=gate, error=RuntimeError("falla del par"))
    async with environment(tmp_path, converter, {"CONVERTER_CONCURRENCY": "1"}) as env:
        if state is TaskState.QUEUED:
            await _reach(env, TaskState.PROCESSING, gate)  # ocupa el único lugar
            task_id = await _reach(env, TaskState.QUEUED, gate)
        else:
            task_id = await _reach(env, state, gate)
        assert env.deps.manager.view(task_id).state is state
        response = await env.api.get(f"/api/tasks/{task_id}/result")
        gate.set()  # el par falla recién ahora: el resultado parcial nunca se descarga
        await asyncio.sleep(0.02)
        later = await env.api.get(f"/api/tasks/{task_id}/result")
    assert response.status_code == 409
    assert response.json() == _error(RequestError.DOWNLOAD_UNAVAILABLE, {"estado": state.value})
    assert partial not in response.content
    assert "content-disposition" not in response.headers
    assert later.status_code == 409
    assert partial not in later.content


# --- Resultado vencido ---


@pytest.mark.req("15.8", "15.9")
@pytest.mark.asyncio
async def test_the_result_is_downloadable_until_the_retention_period_ends(tmp_path: Path) -> None:
    async with environment(tmp_path) as env:
        task_id = await complete(env)
        retention = env.settings.retention_seconds
        env.clock.now = CLOCK_START + retention - 0.001
        before = await env.api.get(f"/api/tasks/{task_id}/result")
        env.clock.now = CLOCK_START + retention
        after = await env.api.get(f"/api/tasks/{task_id}/result")
        view = (await env.api.get(f"/api/tasks/{task_id}")).json()
        await env.deps.sweeper.run_due()  # se borra el Espacio_de_Trabajo del resultado vencido
        removed = await env.api.get(f"/api/tasks/{task_id}/result")
    assert (before.status_code, before.content) == (200, TEXT)
    assert after.status_code == 410
    assert after.json() == _error(RequestError.RESULT_EXPIRED)
    assert "content-disposition" not in after.headers
    assert view["state"] == "completada"
    assert view["download"]["expired"] is True
    assert (removed.status_code, removed.json()) == (410, _error(RequestError.RESULT_EXPIRED))


@pytest.mark.req("15.8")
@pytest.mark.asyncio
async def test_the_retention_period_follows_the_configuration(tmp_path: Path) -> None:
    async with environment(tmp_path, variables={"CONVERTER_RETENTION_MINUTES": "1"}) as env:
        task_id = await complete(env)
        assert env.settings.retention_seconds == 60
        env.clock.now = CLOCK_START + 59.999
        assert (await env.api.get(f"/api/tasks/{task_id}/result")).status_code == 200
        env.clock.now = CLOCK_START + 60
        assert (await env.api.get(f"/api/tasks/{task_id}/result")).status_code == 410


# --- Identificador_de_Tarea ---


@pytest.mark.req("19.5")
@pytest.mark.parametrize(
    "task_id", [UNKNOWN_ID, UNKNOWN_ID.upper(), "no-es-un-id", "%20", "0" * 36]
)
@pytest.mark.asyncio
async def test_download_of_a_malformed_or_unknown_task_is_the_common_404(
    tmp_path: Path, task_id: str
) -> None:
    async with environment(tmp_path) as env:
        existing = await complete(env)
        response = await env.api.get(f"/api/tasks/{task_id}/result")
        uppercase = await env.api.get(f"/api/tasks/{existing.upper()}/result")
        missing = await env.api.get("/api/tasks//result")
    for answer in (response, uppercase, missing):
        assert answer.status_code == 404
        assert answer.content == TASK_UNAVAILABLE_BODY
        assert dict(answer.headers) == dict(missing.headers)
        assert "content-disposition" not in answer.headers


# --- Content-Disposition ---

NAMES = [
    "informe.pdf",
    "Año ñandú.txt",
    "😀 emoji.txt",
    "100% \"real\" (copia) 'x'; y,z.txt",
    'a\\b"c.txt',
    "línea\r\nnueva.txt",
    "con\x00nulo\x7f.txt",
    "\u202eRTL.txt",
    "ñ" * 100,
    "",
]


@pytest.mark.req("15.2", "18.10")
def test_the_fallback_name_is_printable_ascii_without_quotes_backslashes_or_percent() -> None:
    for code_point in [*range(0x500), 0x2028, 0x202E, 0xFEFF, 0x1F600, 0x10FFFF]:
        if 0xD800 <= code_point <= 0xDFFF:
            continue
        fallback = ascii_fallback(chr(code_point))
        assert len(fallback) == 1, hex(code_point)
        assert " " <= fallback <= "~", hex(code_point)
        assert fallback not in {'"', "\\", "%"}, hex(code_point)
    assert ascii_fallback('a"b\\c%d\r\n\x7f.txt') == "a_b_c_d___.txt"
    assert ascii_fallback("Vacaciones 2024 (1).pdf") == "Vacaciones 2024 (1).pdf"


@pytest.mark.req("15.2", "18.10")
@pytest.mark.parametrize("name", NAMES)
def test_content_disposition_is_one_attachment_with_both_names(name: str) -> None:
    header = content_disposition(name)
    match = _HEADER.fullmatch(header)
    assert match is not None, header
    fallback, encoded = match.groups()
    assert fallback == ascii_fallback(name)
    assert (
        unquote(encoded, encoding="utf-8", errors="strict") == name
    )  # filename* lo lleva completo
    assert header.isascii() and header.isprintable()  # sin saltos de línea ni controles


@pytest.mark.req("15.2")
def test_content_disposition_rejects_a_name_that_is_not_utf_8() -> None:
    with pytest.raises(UnicodeEncodeError):
        content_disposition("suelto\ud800.txt")
