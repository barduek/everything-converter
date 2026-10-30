"""Pruebas de src/converter/api/upload.py: la recepción de archivos en `POST /api/tasks`
(Req 1.1–1.10, 16.13, 17.1, 17.10, 19.3, 19.9).

La aplicación se arma con dependencias reales (`tests/api_support.py`) sobre un
Almacenamiento_Temporal en un directorio temporal, y se llama en el mismo proceso. El
Límite_de_Tamaño_Global es de 1 MB salvo donde la prueba necesita varias escrituras. Los
archivos que crean Tareas son texto, que el Detector_de_Tipo clasifica sin inspector.

Las fallas del disco se provocan de dos formas: un `WorkspaceManager` real con el espacio libre
fijo o con `source/original` ocupado, y una escritura que lanza `ENOSPC`. La desconexión del
cliente se envía llamando a la aplicación ASGI directamente, con un `http.disconnect` a mitad
del cuerpo.
"""

from __future__ import annotations

import asyncio
import dataclasses
import errno
import stat
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.types import Message, Scope

from converter.api import upload
from converter.api.app import AppDeps, create_app
from converter.api.errors import ApiError
from converter.api.security import is_task_id
from converter.messages_es import ReceptionError, render_error
from converter.storage.sweeper import CleanupKind
from converter.storage.workspace import Workspace, WorkspaceManager
from tests.api_support import MARKER, build_deps, client, encode_file_name, upload_headers

LIMIT = 1_000_000  # Límite_de_Tamaño_Global de 1 MB
ENV = {"CONVERTER_MAX_UPLOAD_MB": "1", "CONVERTER_MIN_FREE_SPACE_MB": "0"}
PAIRS = [("TXT", "HTML")]
NAME = "Vacaciones 2024.txt"
TEXT = "Línea de texto con eñes y acentos: áéíóú.\n".encode()


@pytest.fixture
def deps(tmp_path: Path) -> AppDeps:
    return build_deps(tmp_path, env=ENV, pairs=PAIRS)


@pytest.fixture
def app(deps: AppDeps) -> FastAPI:
    return create_app(deps)


@pytest_asyncio.fixture
async def api(app: FastAPI, deps: AppDeps) -> AsyncIterator[httpx.AsyncClient]:
    async with client(app) as http:
        yield http
    await deps.manager.aclose()


class Body:
    """Cuerpo enviado por partes, sin `Content-Length`, que cuenta las partes que se leyeron."""

    def __init__(self, chunk: bytes, count: int) -> None:
        self.chunk = chunk
        self.count = count
        self.read = 0  # partes que la aplicación pidió

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self.count):
            self.read += 1
            yield self.chunk


async def _chunks(data: bytes, size: int) -> AsyncIterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


def _error(code: str, params: Mapping[str, object] | None = None) -> dict[str, object]:
    message = render_error(code, params)
    return {
        "error": {
            "code": message.code,
            "stage": "recepción",
            "message": message.message,
            "action": message.action,
        }
    }


def _workspaces(deps: AppDeps) -> list[Path]:
    return sorted(deps.storage.root.iterdir())


def _assert_no_reception_left(deps: AppDeps) -> None:
    """Sin Tarea, sin Espacio_de_Trabajo y sin borrados pendientes."""
    assert len(deps.manager) == 0
    assert _workspaces(deps) == []
    assert deps.sweeper.pending == ()


async def _assert_workspace_removed(deps: AppDeps) -> None:
    """Sin Tarea; el único Espacio_de_Trabajo está encolado para borrar y el barrendero lo borra."""
    assert len(deps.manager) == 0
    [ws_path] = _workspaces(deps)
    [job] = deps.sweeper.pending
    assert (job.workspace.name, job.kind, job.task_id) == (ws_path.name, CleanupKind.REMOVE, None)
    assert await deps.sweeper.run_due() == 1
    assert _workspaces(deps) == []


# --- Recepción completa ---


@pytest.mark.req("1.1", "1.2", "1.6", "17.1", "19.3")
@pytest.mark.asyncio
async def test_an_upload_creates_a_received_task_with_an_identical_source(tmp_path: Path) -> None:
    # Límite de 5 MB: un archivo de más de 3,5 MB pasa por varias escrituras de 1 MiB
    deps = build_deps(tmp_path, env={**ENV, "CONVERTER_MAX_UPLOAD_MB": "5"}, pairs=PAIRS)
    data = b"".join(f"línea {n}: ñandú\n".encode() for n in range(180_000))
    assert 3_500_000 < len(data) < 5_000_000
    async with client(create_app(deps)) as api:
        response = await api.post(
            "/api/tasks", content=_chunks(data, 65_537), headers=upload_headers(NAME)
        )
    try:
        assert response.status_code == 201
        body = response.json()
        assert is_task_id(body["id"])
        assert (body["original_name"], body["state"], body["state_label"]) == (
            NAME,
            "recibida",
            "Recibida",
        )
        assert deps.manager.view(body["id"]).original_name == NAME

        [ws_path] = _workspaces(deps)
        ws = Workspace(ws_path)
        assert [entry.name for entry in ws.source_dir.iterdir()] == ["original"]
        assert ws.source_file.read_bytes() == data
        assert stat.S_IMODE(ws.source_file.stat().st_mode) == upload.SOURCE_FILE_MODE
        assert deps.sweeper.pending == ()
    finally:
        await deps.manager.aclose()


@pytest.mark.req("1.6", "1.7", "17.1")
@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd",
        "C:\\fakepath\\foto.png",
        "..",
        "",
        "con\x00nulo\n.txt",
        "Año ñandú 😀 (copia) ¡ya!.TXT",
        "%41.txt",  # no se decodifica dos veces
        "a" * 255,
        "ñ" * 255,
    ],
)
@pytest.mark.asyncio
async def test_the_original_name_is_kept_and_never_used_as_a_path(
    api: httpx.AsyncClient, deps: AppDeps, name: str
) -> None:
    response = await api.post("/api/tasks", content=TEXT, headers=upload_headers(name))
    assert response.status_code == 201
    assert response.json()["original_name"] == name
    [ws_path] = _workspaces(deps)
    root = deps.storage.root
    files = {path.relative_to(root).as_posix() for path in root.rglob("*")}
    ws_name = ws_path.name
    assert files == {
        ws_name,
        f"{ws_name}/source",
        f"{ws_name}/source/original",
        f"{ws_name}/work",
        f"{ws_name}/tmp",
        f"{ws_name}/result",
    }
    assert Workspace(ws_path).source_file.read_bytes() == TEXT


@pytest.mark.req("1.2", "1.6", "17.1", "19.3")
@pytest.mark.asyncio
async def test_uploads_with_the_same_name_get_their_own_task_and_workspace(
    api: httpx.AsyncClient, deps: AppDeps
) -> None:
    first = await api.post("/api/tasks", content=b"primero", headers=upload_headers(NAME))
    second = await api.post("/api/tasks", content=b"segundo", headers=upload_headers(NAME))
    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json()["id"] != second.json()["id"]
    sources = {Workspace(path).source_file.read_bytes() for path in _workspaces(deps)}
    assert sources == {b"primero", b"segundo"}


@pytest.mark.req("1.1", "1.3")
@pytest.mark.parametrize(
    ("size", "declared", "status"),
    [(LIMIT, True, 201), (LIMIT, False, 201), (LIMIT + 1, True, 413), (LIMIT + 1, False, 413)],
)
@pytest.mark.asyncio
async def test_the_limit_is_inclusive(
    api: httpx.AsyncClient, deps: AppDeps, size: int, declared: bool, status: int
) -> None:
    data = b"a" * size
    content = data if declared else _chunks(data, 300_000)
    response = await api.post("/api/tasks", content=content, headers=upload_headers(NAME))
    assert response.status_code == status
    if status == 201:
        [ws_path] = _workspaces(deps)
        assert Workspace(ws_path).source_file.stat().st_size == LIMIT
    else:
        assert response.json() == _error(ReceptionError.TOO_LARGE, {"limite_mb": 1})
        assert len(deps.manager) == 0


# --- Rechazos antes de leer el cuerpo ---


@pytest.mark.req("16.13")
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([], id="sin_nombre"),
        pytest.param([(b"X-File-Name", b"a.txt"), (b"X-File-Name", b"b.txt")], id="repetido"),
        pytest.param([(b"X-File-Name", "Año.txt".encode())], id="utf8_sin_codificar"),
        pytest.param([(b"X-File-Name", b"a b.txt")], id="espacio"),
        pytest.param([(b"X-File-Name", b"100%.txt")], id="porcentaje_suelto"),
        pytest.param([(b"X-File-Name", b"%zz.txt")], id="porcentaje_no_hexadecimal"),
        pytest.param([(b"X-File-Name", b"%C3.txt")], id="utf8_truncado"),
        pytest.param([(b"X-File-Name", b"%C0%AF")], id="forma_larga"),
        pytest.param([(b"X-File-Name", b"%ED%A0%80")], id="sustituto"),
        pytest.param([(b"X-File-Name", b"%FF")], id="byte_invalido"),
        pytest.param([(b"X-File-Name", b"a.txt"), (b"Content-Length", b"abc")], id="largo_letras"),
        pytest.param([(b"X-File-Name", b"a.txt"), (b"Content-Length", b"-1")], id="largo_negativo"),
    ],
)
@pytest.mark.asyncio
async def test_an_invalid_upload_request_is_rejected_without_reading(
    api: httpx.AsyncClient, deps: AppDeps, headers: list[tuple[bytes, bytes]]
) -> None:
    body = Body(TEXT, 1)
    marker = [(name.encode(), value.encode()) for name, value in MARKER.items()]
    response = await api.post("/api/tasks", content=body, headers=marker + headers)
    assert response.status_code == 400
    assert response.json() == _error(ReceptionError.INVALID_REQUEST)
    assert body.read == 0
    _assert_no_reception_left(deps)


@pytest.mark.req("1.10", "16.13", "17.10")
@pytest.mark.parametrize("name", ["a" * 256, "ñ" * 256, "😀" * 300])
@pytest.mark.asyncio
async def test_a_name_longer_than_255_characters_is_rejected_without_reading(
    api: httpx.AsyncClient, deps: AppDeps, name: str
) -> None:
    body = Body(TEXT, 1)
    response = await api.post("/api/tasks", content=body, headers=upload_headers(name))
    assert response.status_code == 400
    assert response.json() == _error(ReceptionError.NAME_TOO_LONG)
    assert body.read == 0
    _assert_no_reception_left(deps)


@pytest.mark.req("1.3", "16.13")
@pytest.mark.asyncio
async def test_a_declared_size_above_the_limit_is_rejected_before_reading(
    api: httpx.AsyncClient, deps: AppDeps
) -> None:
    body = Body(b"a" * 100_000, 11)
    headers = {**upload_headers(NAME), "Content-Length": str(LIMIT + 100_000)}
    response = await api.post("/api/tasks", content=body, headers=headers)
    assert response.status_code == 413
    error = response.json()
    assert error == _error(ReceptionError.TOO_LARGE, {"limite_mb": 1})
    assert error["error"]["message"] == "El archivo excede el límite de 1 MB."
    assert body.read == 0
    _assert_no_reception_left(deps)


class FixedFreeSpace(WorkspaceManager):
    """Almacenamiento_Temporal real que informa un espacio libre fijo."""

    __slots__ = ("_free",)

    def __init__(self, root: Path, free: int) -> None:
        super().__init__(root)
        self._free = free

    def free_bytes(self) -> int:
        return self._free


@pytest.mark.req("1.5", "16.13", "17.1")
@pytest.mark.parametrize(("free", "status"), [(4_999_999, 507), (5_000_000, 201)])
@pytest.mark.asyncio
async def test_the_minimum_free_space_is_checked_before_creating_the_workspace(
    tmp_path: Path, free: int, status: int
) -> None:
    deps = build_deps(tmp_path, env={**ENV, "CONVERTER_MIN_FREE_SPACE_MB": "5"}, pairs=PAIRS)
    deps = dataclasses.replace(deps, storage=FixedFreeSpace(deps.storage.root, free))
    body = Body(TEXT, 1)
    async with client(create_app(deps)) as api:
        response = await api.post("/api/tasks", content=body, headers=upload_headers(NAME))
    try:
        assert response.status_code == status
        if status == 507:
            assert response.json() == _error(ReceptionError.NO_SPACE)
            assert body.read == 0
            _assert_no_reception_left(deps)
        else:
            assert len(_workspaces(deps)) == 1
    finally:
        await deps.manager.aclose()


# --- Rechazos durante la recepción ---


@pytest.mark.req("1.3", "17.10")
@pytest.mark.asyncio
async def test_the_reception_stops_when_the_bytes_exceed_the_limit(
    api: httpx.AsyncClient, deps: AppDeps
) -> None:
    body = Body(b"a" * 300_000, 10)
    response = await api.post("/api/tasks", content=body, headers=upload_headers(NAME))
    assert response.status_code == 413
    assert response.json() == _error(ReceptionError.TOO_LARGE, {"limite_mb": 1})
    assert body.read == 4  # la cuarta parte llevó la cuenta a 1 200 000 bytes
    [ws_path] = _workspaces(deps)
    assert Workspace(ws_path).source_file.stat().st_size <= LIMIT
    await _assert_workspace_removed(deps)


@pytest.mark.req("1.4", "16.13", "17.10")
@pytest.mark.parametrize("declared", [True, False])
@pytest.mark.asyncio
async def test_an_empty_file_is_rejected_and_its_workspace_removed(
    api: httpx.AsyncClient, deps: AppDeps, declared: bool
) -> None:
    content = b"" if declared else _chunks(b"", 1)
    response = await api.post("/api/tasks", content=content, headers=upload_headers(NAME))
    assert response.status_code == 400
    assert response.json() == _error(ReceptionError.EMPTY)
    await _assert_workspace_removed(deps)


@pytest.mark.req("1.9", "16.13")
@pytest.mark.asyncio
async def test_an_unusable_storage_is_a_write_error(api: httpx.AsyncClient, deps: AppDeps) -> None:
    deps.storage.root.rmdir()
    response = await api.post("/api/tasks", content=TEXT, headers=upload_headers(NAME))
    assert response.status_code == 500
    assert response.json() == _error(ReceptionError.WRITE_ERROR)
    assert len(deps.manager) == 0
    assert deps.sweeper.pending == ()


class SourceTaken(WorkspaceManager):
    """Almacenamiento_Temporal real cuyos Espacios_de_Trabajo ya tienen `source/original`."""

    __slots__ = ()

    def create(self) -> Workspace:
        ws = super().create()
        ws.source_file.mkdir()
        return ws


@pytest.mark.req("1.8", "1.9", "16.13", "17.10")
@pytest.mark.asyncio
async def test_a_source_that_cannot_be_created_is_a_write_error(tmp_path: Path) -> None:
    deps = build_deps(tmp_path, env=ENV, pairs=PAIRS)
    deps = dataclasses.replace(deps, storage=SourceTaken(deps.storage.root))
    async with client(create_app(deps)) as api:
        response = await api.post("/api/tasks", content=TEXT, headers=upload_headers(NAME))
    assert response.status_code == 500
    assert response.json() == _error(ReceptionError.WRITE_ERROR)
    await _assert_workspace_removed(deps)


@pytest.mark.req("1.8", "1.9", "16.13", "17.10")
@pytest.mark.asyncio
async def test_a_write_error_stops_the_reception_and_removes_its_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_space(fd: int, data: bytes | bytearray) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(upload, "_write_all", no_space)
    # Límite de 5 MB: la primera escritura llega antes del final del cuerpo
    deps = build_deps(tmp_path, env={**ENV, "CONVERTER_MAX_UPLOAD_MB": "5"}, pairs=PAIRS)
    body = Body(b"a" * 300_000, 10)
    async with client(create_app(deps)) as api:
        response = await api.post("/api/tasks", content=body, headers=upload_headers(NAME))
    assert response.status_code == 500
    assert response.json() == _error(ReceptionError.WRITE_ERROR)
    assert body.read == 4  # la escritura de 1,2 MB falló y no se leyó más
    await _assert_workspace_removed(deps)


async def _post_until_disconnect(app: FastAPI, chunks: list[bytes]) -> list[Message]:
    """Envía `chunks` a `POST /api/tasks` y después un `http.disconnect`, como el servidor
    cuando el cliente pierde la conexión o cancela el envío. Devuelve lo que la aplicación
    envió."""
    headers = upload_headers(NAME)
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/tasks",
        "raw_path": b"/api/tasks",
        "query_string": b"",
        "root_path": "",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 50000),
    }
    incoming: list[Message] = [
        {"type": "http.request", "body": chunk, "more_body": True} for chunk in chunks
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


@pytest.mark.req("1.8", "17.10")
@pytest.mark.asyncio
async def test_a_client_disconnect_removes_the_partial_reception(
    app: FastAPI, deps: AppDeps
) -> None:
    sent = await _post_until_disconnect(app, [TEXT, TEXT])
    # No hay a quién responder: la respuesta no lleva mensaje y el servidor la descarta
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert body == b""
    await _assert_workspace_removed(deps)


# --- Recepciones simultáneas ---


@pytest.mark.req("19.9")
@pytest.mark.asyncio
async def test_simultaneous_receptions_do_not_wait_for_each_other(
    api: httpx.AsyncClient, deps: AppDeps
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_body() -> AsyncIterator[bytes]:
        yield b"primera parte, "
        started.set()  # la aplicación pidió la parte siguiente: la recepción está en curso
        await release.wait()
        yield b"segunda parte"

    slow = asyncio.create_task(
        api.post("/api/tasks", content=slow_body(), headers=upload_headers("lento.txt"))
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    quick = await asyncio.wait_for(
        api.post("/api/tasks", content=b"otro archivo", headers=upload_headers("rapido.txt")),
        timeout=5,
    )
    assert quick.status_code == 201
    assert not slow.done()

    release.set()
    first = await asyncio.wait_for(slow, timeout=5)
    assert first.status_code == 201
    assert first.json()["id"] != quick.json()["id"]
    sources = {Workspace(path).source_file.read_bytes() for path in _workspaces(deps)}
    assert sources == {b"primera parte, segunda parte", b"otro archivo"}


# --- Decodificación del nombre ---


@pytest.mark.parametrize(
    "name", ["notas.txt", "Año ñandú.md", "😀 a/b\\c..d", "!~*'()-_.", "%41", ""]
)
def test_decode_file_name_inverts_encode_uri_component(name: str) -> None:
    assert upload.decode_file_name([encode_file_name(name)]) == name


@pytest.mark.parametrize("values", [["1", "1"], ["1e6"], [" 1"], ["+1"], ["9" * 21]])
def test_declared_length_rejects_values_that_are_not_one_decimal_number(
    values: list[str],
) -> None:
    with pytest.raises(ApiError) as raised:
        upload.declared_length(values)
    assert raised.value.message.code == ReceptionError.INVALID_REQUEST


def test_declared_length_reads_a_decimal_value() -> None:
    assert upload.declared_length([]) is None
    assert upload.declared_length(["0"]) == 0
    assert upload.declared_length(["01000000"]) == LIMIT
