"""Pruebas de src/converter/__main__.py: la secuencia de arranque del Conversor (Req 4.9, 14.13,
17.5, 20.5, 20.6, 20.9, 20.13).

`main` corre de verdad, con `Locations` apuntando a directorios temporales (el
Almacenamiento_Temporal real es una ruta fija del Contenedor). Los arranques que terminan antes
de abrir el puerto se prueban llamando a `main` y leyendo el Log_de_Eventos de la salida
estándar. El arranque completo corre en un hilo, con Uvicorn en un puerto libre de `127.0.0.1`,
y se detiene desde la prueba con `Server.should_exit`. `on_server` da la ocasión de mirar el
almacenamiento y el puerto después de la purga y antes de abrirlo.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NoReturn

import pytest
import uvicorn
from fastapi import FastAPI

from converter.__main__ import (
    BACKGROUND_TASK_FAILED,
    CATALOG_INVALID,
    CONFIG_INVALID,
    EXIT_CANTCREAT,
    EXIT_CONFIG,
    EXIT_SOFTWARE,
    GRACEFUL_SHUTDOWN_SECONDS,
    SERVICE_READY,
    STORAGE_NOT_WRITABLE,
    Background,
    Locations,
    Server,
    _prepare_storage,
    main,
    server_config,
)
from converter.eventlog import EventLog
from converter.storage.sweeper import CleanupKind, Sweeper
from converter.storage.workspace import WORKSPACE_NAME, WorkspaceManager
from tests.api_support import build_deps, config

needs_permissions = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignora los permisos de los directorios"
)

START_TIMEOUT_S = 30.0  # lo que puede tardar el arranque completo en una máquina lenta
EXIT_BUDGET_S = 10.0  # Req 20.6 y 20.13: el proceso termina dentro de este plazo


def events(text: str) -> list[dict[str, Any]]:
    """Eventos del Log_de_Eventos: un objeto JSON por línea."""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def accepts_connections(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return False
    return True


def locations(tmp_path: Path, **override: Path) -> Locations:
    """Rutas de una prueba: un Almacenamiento_Temporal vacío y los catálogos del paquete."""
    storage = tmp_path / "storage"
    storage.mkdir(exist_ok=True)
    return Locations(**{"storage": storage, "languages": tmp_path / "languages.es.json"} | override)


def stale_workspace(storage: Path) -> Path:
    """Espacio_de_Trabajo que dejó un inicio anterior, con un resultado adentro."""
    ws = WorkspaceManager(storage).create()
    ws.result_file.write_bytes(b"resultado de otro inicio")
    return ws.path


def never_called(server: Server) -> NoReturn:
    """`on_server` de los arranques que deben terminar antes de crear el servidor."""
    pytest.fail("el arranque no debía llegar a crear el servidor")


# --------------------------------------------------------------------------------------------
# Paso 1: Variables_de_Configuración


@pytest.mark.req("20.6")
def test_invalid_variables_are_logged_one_by_one_and_exit_78(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    where = locations(tmp_path)
    stale = stale_workspace(where.storage)
    env = {
        "CONVERTER_PORT": "70000",
        "CONVERTER_CONCURRENCY": "",
        "CONVERTER_RASTER_DPI": "abc",
        "CONVERTER_RETENTION_MINUTES": "60",  # válida: no se informa
    }

    code = main(env, locations=where, on_server=never_called)

    assert code == EXIT_CONFIG == 78
    logged = events(capsys.readouterr().out)
    assert [(e["event"], e["level"], e["variable"]) for e in logged] == [
        (CONFIG_INVALID, "error", "CONVERTER_PORT"),
        (CONFIG_INVALID, "error", "CONVERTER_CONCURRENCY"),
        (CONFIG_INVALID, "error", "CONVERTER_RASTER_DPI"),
    ]
    assert all(e["reason"] for e in logged)
    # La configuración se valida antes de tocar el almacenamiento
    assert stale.exists()


@pytest.mark.req("20.6")
def test_the_configuration_is_checked_before_the_storage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    where = Locations(storage=tmp_path / "no-existe", languages=tmp_path / "languages.es.json")

    code = main({"CONVERTER_PORT": "0"}, locations=where)

    assert code == EXIT_CONFIG
    assert [e["event"] for e in events(capsys.readouterr().out)] == [CONFIG_INVALID]


@pytest.mark.req("20.6")
def test_python_m_converter_exits_78_with_invalid_variables_within_10_seconds(
    tmp_path: Path,
) -> None:
    env = {"PATH": os.environ.get("PATH", ""), "CONVERTER_PORT": "abc"}
    started = time.monotonic()

    done = subprocess.run(
        [sys.executable, "-m", "converter"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert time.monotonic() - started < EXIT_BUDGET_S
    assert done.returncode == 78
    (event,) = events(done.stdout)
    assert event["event"] == CONFIG_INVALID
    assert event["variable"] == "CONVERTER_PORT"


# --------------------------------------------------------------------------------------------
# Paso 2: Almacenamiento_Temporal


@pytest.mark.req("20.13")
def test_a_missing_storage_logs_the_reason_and_exits_73(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "no-existe"

    code = main({}, locations=Locations(storage=missing), on_server=never_called)

    assert code == EXIT_CANTCREAT == 73
    (event,) = events(capsys.readouterr().out)
    assert event["event"] == STORAGE_NOT_WRITABLE
    assert event["level"] == "error"
    assert event["path"] == str(missing)
    assert event["reason"]
    assert event["detail"]["errno"] == "ENOENT"


@needs_permissions
@pytest.mark.req("20.13")
def test_a_read_only_storage_exits_73_without_serving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    where = locations(tmp_path)
    where.storage.chmod(0o500)
    try:
        code = main({}, locations=where, on_server=never_called)
    finally:
        where.storage.chmod(0o700)

    assert code == EXIT_CANTCREAT
    (event,) = events(capsys.readouterr().out)
    assert event["event"] == STORAGE_NOT_WRITABLE
    assert event["detail"]["errno"] == "EACCES"


@needs_permissions
@pytest.mark.req("17.5", "17.9")
def test_a_workspace_the_purge_cannot_remove_is_left_to_the_sweeper(tmp_path: Path) -> None:
    storage = WorkspaceManager(locations(tmp_path).storage)
    stuck = storage.create()
    (stuck.work_dir / "parcial").write_bytes(b"x")
    stuck.work_dir.chmod(0o500)  # sus archivos no se pueden borrar
    out = io.StringIO()
    sweeper = Sweeper(storage, EventLog(out))
    try:
        assert _prepare_storage(storage, sweeper, EventLog(out)) is True
    finally:
        stuck.work_dir.chmod(0o700)

    (job,) = sweeper.pending
    assert (job.workspace, job.kind, job.task_id, job.attempt) == (
        stuck,
        CleanupKind.REMOVE,
        None,
        2,
    )
    (event,) = events(out.getvalue())
    assert (event["event"], event["workspace"], event["attempt"]) == (
        "cleanup_failed",
        stuck.name,
        1,
    )


# --------------------------------------------------------------------------------------------
# Pasos 3 y 5: catálogo, convertidores e idiomas


@pytest.mark.req("4.9")
@pytest.mark.parametrize("broken", ["formats", "pairs"])
def test_an_invalid_initial_catalog_exits_70(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], broken: str
) -> None:
    bad = tmp_path / f"{broken}.toml"
    if broken == "formats":
        bad.write_text("esto no es [TOML", encoding="utf-8")
    else:
        bad.write_text(
            '[[pair]]\nsource = "PNG"\ntarget = "JPG"\nloss_class = "Sin_Pérdida"\n'
            'loss_description = "x"\nconverter = "no.existe"\n',
            encoding="utf-8",
        )
    where = locations(tmp_path, **{broken: bad})

    code = main({}, locations=where, on_server=never_called)

    assert code == EXIT_SOFTWARE == 70
    (event,) = [e for e in events(capsys.readouterr().out) if e["event"] == CATALOG_INVALID]
    assert event["level"] == "error"
    assert event["file"] == str(bad)
    assert event["reason"]


def test_a_languages_file_that_breaks_its_format_exits_70(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    languages = tmp_path / "languages.es.json"
    languages.write_text('[{"code": "en", "name": "inglés"}]', encoding="utf-8")  # sin español

    code = main({}, locations=locations(tmp_path), on_server=never_called)

    assert code == EXIT_SOFTWARE
    (event,) = [e for e in events(capsys.readouterr().out) if e["event"] == CATALOG_INVALID]
    assert event["file"] == str(languages)


# --------------------------------------------------------------------------------------------
# Arranque completo


@pytest.mark.req("14.13", "17.5", "20.5", "20.9")
def test_a_normal_start_purges_before_opening_the_port_and_then_reports_ready(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    where = locations(tmp_path)
    stale = stale_workspace(where.storage)
    foreign_file = where.storage / "datos.txt"
    foreign_file.write_text("no es un Espacio_de_Trabajo", encoding="utf-8")
    foreign_dir = where.storage / "ws-no-es-un-espacio"
    foreign_dir.mkdir()
    port = free_port()
    servers: list[Server] = []
    before_open: dict[str, bool] = {}

    def on_server(server: Server) -> None:
        before_open["stale_removed"] = not stale.exists()
        before_open["foreign_kept"] = foreign_file.exists() and foreign_dir.exists()
        before_open["port_closed"] = not accepts_connections(port)
        servers.append(server)

    exit_codes: list[int] = []
    crashes: list[BaseException] = []

    def run() -> None:
        try:
            exit_codes.append(
                main({"CONVERTER_PORT": str(port)}, locations=where, on_server=on_server)
            )
        except BaseException as error:  # se vuelve a lanzar desde el hilo de la prueba
            crashes.append(error)

    thread = threading.Thread(target=run, name="converter-main")
    thread.start()
    try:
        health = wait_until_ready(port, thread, crashes)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as index:
            assert index.status == 200
    finally:
        for server in servers:
            server.should_exit = True
        thread.join(START_TIMEOUT_S)

    assert not thread.is_alive()
    assert crashes == []
    assert exit_codes == [0]
    assert health == {"ready": True}
    # Req 17.5: la purga ocurrió antes de abrir el puerto, y solo borró Espacios_de_Trabajo
    assert before_open == {"stale_removed": True, "foreign_kept": True, "port_closed": True}
    assert foreign_file.exists() and foreign_dir.exists()
    ready = [e for e in events(capsys.readouterr().out) if e["event"] == SERVICE_READY]
    assert [(e["level"], e["port"]) for e in ready] == [("info", port)]
    # Nadie quedó con un Espacio_de_Trabajo: ni el de prueba de escritura ni el del inicio anterior
    leftovers = [p for p in where.storage.iterdir() if WORKSPACE_NAME.fullmatch(p.name)]
    assert leftovers == []


def wait_until_ready(port: int, thread: threading.Thread, crashes: list[BaseException]) -> Any:
    """Cuerpo de `/api/health` en cuanto responde 200; falla si el arranque no termina."""
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        assert not crashes, crashes
        assert thread.is_alive(), "el arranque terminó sin informar listo"
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as ok:
                return json.load(ok)
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.05)
    pytest.fail("el Conversor no informó listo a tiempo")


# --------------------------------------------------------------------------------------------
# Servidor y tareas de fondo


@pytest.mark.req("20.5")
def test_uvicorn_serves_the_configured_port_with_one_worker_and_no_access_log() -> None:
    settings = server_config(FastAPI(), config({"CONVERTER_PORT": "9123"}))

    assert settings.port == 9123
    assert settings.host == "0.0.0.0"
    assert settings.workers == 1
    assert settings.access_log is False
    # `docker stop` espera 10 segundos antes de enviar SIGKILL
    assert settings.timeout_graceful_shutdown == GRACEFUL_SHUTDOWN_SECONDS < 10


@pytest.mark.asyncio
@pytest.mark.req("20.9")
async def test_the_server_reports_started_with_the_port_open_and_stopped_after_shutdown() -> None:
    port = free_port()
    calls: list[str] = []

    def on_started() -> None:
        calls.append(f"started:{server.started}:{accepts_connections(port)}")

    async def on_stopped() -> None:
        calls.append("stopped")

    server = Server(
        uvicorn.Config(FastAPI(), host="127.0.0.1", port=port, log_level="warning"),
        on_started=on_started,
        on_stopped=on_stopped,
    )
    serving = asyncio.create_task(server.serve())
    async with asyncio.timeout(START_TIMEOUT_S):
        while not server.started:
            await asyncio.sleep(0.01)
    assert calls == ["started:True:True"]

    server.should_exit = True
    await asyncio.wait_for(serving, START_TIMEOUT_S)

    assert calls == ["started:True:True", "stopped"]


BACKGROUND_NAMES = {"sweeper", "retention"}


def running_names() -> set[str]:
    return {task.get_name() for task in asyncio.all_tasks()}


@pytest.mark.asyncio
async def test_the_sweeper_and_the_retention_run_until_stopped(tmp_path: Path) -> None:
    deps = build_deps(tmp_path)
    background = Background(deps.sweeper, deps.manager, EventLog(io.StringIO()))

    background.start()
    await asyncio.sleep(0)
    assert running_names() >= BACKGROUND_NAMES

    await background.stop()
    assert not BACKGROUND_NAMES & running_names()
    await background.stop()  # el apagado puede pedirlo dos veces


class BrokenSweeper(Sweeper):
    async def run(self) -> NoReturn:
        raise RuntimeError("contenido del archivo")


@pytest.mark.asyncio
@pytest.mark.req("16.8")
async def test_a_background_task_that_fails_is_logged_without_its_message(tmp_path: Path) -> None:
    deps = build_deps(tmp_path)
    out = io.StringIO()
    log = EventLog(out)
    background = Background(BrokenSweeper(deps.storage, log), deps.manager, log)

    background.start()
    for _ in range(3):
        await asyncio.sleep(0)
    await background.stop()

    (event,) = events(out.getvalue())
    assert event["event"] == BACKGROUND_TASK_FAILED
    assert event["level"] == "error"
    assert (event["task"], event["exception"]) == ("sweeper", "RuntimeError")
    assert "contenido del archivo" not in out.getvalue()
