"""Ejecutor aislado: cada paso de procesamiento corre en un subproceso con límites.

Cubre Req 13.3, 13.6, 13.7, 14.6 y 14.7. `SandboxRunner.run` ejecuta `argv` a través del
lanzador (`python -I -m converter.sandbox.launch`), que fija los límites de `Limits` con
`setrlimit` y reemplaza su proceso por el programa con `exec` (ver `launch.py`). El intérprete
corre con `-I` para que el directorio de trabajo, que está dentro del Espacio_de_Trabajo, no
entre en `sys.path`.

- **Grupo de procesos propio.** El subproceso se crea con `start_new_session=True`: lidera una
  sesión y un grupo de procesos cuyo identificador es su PID, y sus hijos (por ejemplo,
  `soffice.bin`) heredan ese grupo. Las señales del ejecutor van siempre al grupo completo.
- **Cancelación y timeout.** Si se cancela la corrutina que espera `run` (con `Task.cancel()` o
  al vencer un `asyncio.timeout`, que es como el Gestor_de_Tareas aplica el
  Tiempo_Máximo_de_Procesamiento), el ejecutor deja de entregar líneas, envía SIGTERM al grupo,
  espera hasta 2 segundos a que el programa termine y envía SIGKILL al grupo, que alcanza
  también a los procesos que ignoran SIGTERM. Luego espera a que el programa termine y vuelve a
  lanzar la cancelación. Si la corrutina se cancela otra vez durante la espera, el SIGKILL sale
  de inmediato. Un callback de línea que lanza una excepción produce la misma secuencia, y `run`
  propaga esa excepción.
- **Procesos que quedan.** Cuando el programa termina por su cuenta, el ejecutor envía SIGKILL
  al grupo para que ningún hijo siga usando CPU o archivos del Espacio_de_Trabajo, y lee lo que
  queda en las tuberías durante 2 segundos como máximo (un proceso que salió del grupo con
  `setsid` podría mantenerlas abiertas).
- **Entorno mínimo.** El subproceso recibe solo `env` y no hereda el entorno del Conversor.
  `minimal_env` arma el entorno base: `PATH`, `LANG=C.UTF-8`, `HOME` y `TMPDIR` en `tmp/` del
  Espacio_de_Trabajo y `FONTCONFIG_FILE` cuando corresponde. `run` reemplaza siempre
  `http_proxy` y `https_proxy` por `http://127.0.0.1:9` (puerto discard, donde nada escucha) y
  deja `no_proxy` vacío, de modo que un pedido HTTP o HTTPS que una herramienta intente hacer
  falla sin salir del Equipo_de_Ejecución.
- **Salida.** stdout y stderr se leen línea por línea, sin el salto de línea ni un "\\r" final,
  decodificadas como UTF-8 con reemplazo, y cada línea se entrega al callback correspondiente.
  Una línea de más de `MAX_LINE_BYTES` bytes se entrega cortada a esa longitud. Las últimas
  líneas de stderr (como máximo `STDERR_TAIL_LINES` líneas y `STDERR_TAIL_CHARS` caracteres)
  vuelven en `CompletedRun.stderr_lines`, para que quien llama las reduzca con
  `converter.eventlog.sanitize_tool_output`. El ejecutor nunca registra la salida.

El subproceso se crea con `loop.subprocess_exec` y un protocolo propio.
`asyncio.create_subprocess_exec` usa ese mismo método con un protocolo de streams, pero en
Python 3.12 su `Process.wait()` no termina mientras alguna tubería siga abierta. Con el
protocolo propio, el ejecutor sabe cuándo terminó el programa aunque un hijo mantenga abiertas
las tuberías.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from subprocess import DEVNULL, PIPE
from types import MappingProxyType

from converter.sandbox.launch import MODULE as LAUNCHER_MODULE
from converter.sandbox.launch import LaunchSpec, format_args
from converter.sandbox.limits import Limits

LineCallback = Callable[[str], None]

KILL_GRACE_SECONDS = 2.0  # espera entre SIGTERM y SIGKILL (Req 14.6)
DRAIN_SECONDS = 2.0  # lectura de las tuberías después de que el programa termina
MAX_LINE_BYTES = 65_536
STDERR_TAIL_LINES = 200
STDERR_TAIL_CHARS = 262_144  # mayor que MAX_LINE_BYTES: la última línea siempre se conserva

LANG = "C.UTF-8"
NEUTRAL_PROXY = "http://127.0.0.1:9"
PROXY_ENV: Mapping[str, str] = MappingProxyType(
    {"http_proxy": NEUTRAL_PROXY, "https_proxy": NEUTRAL_PROXY, "no_proxy": ""}
)

_STDOUT = 1
_STDERR = 2


@dataclass(frozen=True, slots=True)
class CompletedRun:
    """Resultado de un subproceso que terminó sin cancelarse."""

    returncode: int  # convención de asyncio: -N si terminó por la señal N
    # Últimas líneas de stderr; solo pueden ir al Log_de_Eventos a través de
    # `sanitize_tool_output`
    stderr_lines: tuple[str, ...]


def python_module_command(module: str, *args: str, python: str = sys.executable) -> list[str]:
    """`python -I -m module args`: el intérprete ignora las variables `PYTHON*`, el sitio del
    usuario y el directorio de trabajo."""
    return [python, "-I", "-m", module, *args]


def launcher_command(
    argv: Sequence[str], limits: Limits, *, python: str = sys.executable
) -> list[str]:
    """Comando que ejecuta `argv` a través del lanzador con `limits`."""
    spec = LaunchSpec(
        address_space_mb=limits.address_space_mb,
        cpu_seconds=limits.cpu_seconds,
        file_size_mb=limits.file_size_mb,
        open_files=limits.open_files,
        argv=tuple(argv),
    )
    return python_module_command(LAUNCHER_MODULE, *format_args(spec), python=python)


def minimal_env(
    tmp_dir: Path, *, fontconfig_file: Path | None = None, path: str | None = None
) -> dict[str, str]:
    """Entorno base de un subproceso cuyo `HOME` y `TMPDIR` son `tmp_dir`.

    `path` es el `PATH` del Conversor si se omite. Se descartan sus entradas relativas, incluida
    la vacía, que equivale al directorio de trabajo: los programas nunca se buscan dentro del
    Espacio_de_Trabajo. Incluye las variables de proxy neutralizadas, que `run` vuelve a fijar
    aunque `env` no las tenga.
    """
    if path is None:
        path = os.environ.get("PATH", os.defpath)
    home = str(tmp_dir.absolute())
    env = {
        "PATH": os.pathsep.join(entry for entry in path.split(os.pathsep) if os.path.isabs(entry)),
        "LANG": LANG,
        "HOME": home,
        "TMPDIR": home,
        **PROXY_ENV,
    }
    if fontconfig_file is not None:
        env["FONTCONFIG_FILE"] = str(fontconfig_file.absolute())
    return env


class _StderrTail:
    """Últimas líneas de stderr, acotadas por cantidad de líneas y de caracteres."""

    def __init__(self) -> None:
        self._lines: deque[str] = deque()
        self._chars = 0

    def append(self, line: str) -> None:
        self._lines.append(line)
        self._chars += len(line)
        while len(self._lines) > STDERR_TAIL_LINES or self._chars > STDERR_TAIL_CHARS:
            self._chars -= len(self._lines.popleft())

    def lines(self) -> tuple[str, ...]:
        return tuple(self._lines)


def _decode(line: bytes | bytearray) -> str:
    return line.removesuffix(b"\r").decode("utf-8", errors="replace")


class _LineSplitter:
    """Divide en líneas los bytes de una tubería y entrega cada una a `on_line`."""

    def __init__(self, on_line: LineCallback) -> None:
        self._on_line = on_line
        self._buffer = bytearray()
        self._overflow = False  # la línea en curso superó MAX_LINE_BYTES y ya se entregó

    def feed(self, data: bytes) -> None:
        buffer = self._buffer
        buffer += data
        start = 0
        while (end := buffer.find(b"\n", start)) != -1:
            if not self._overflow:
                self._on_line(_decode(buffer[start : min(end, start + MAX_LINE_BYTES)]))
            self._overflow = False
            start = end + 1
        del buffer[:start]
        if len(buffer) > MAX_LINE_BYTES:
            if not self._overflow:
                self._on_line(_decode(buffer[:MAX_LINE_BYTES]))
                self._overflow = True
            buffer.clear()

    def finish(self) -> None:
        """Entrega la última línea si no terminaba en un salto de línea."""
        if self._buffer and not self._overflow:
            self._on_line(_decode(self._buffer))
        self._buffer.clear()


class _RunProtocol(asyncio.SubprocessProtocol):
    """Recibe la salida y el fin de un subproceso.

    Los callbacks corren dentro del bucle de eventos: si uno lanza una excepción, el protocolo
    deja de entregar líneas y la guarda en `failed` para que `run` la propague.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        on_stdout_line: LineCallback | None,
        on_stderr_line: LineCallback,
    ) -> None:
        self._splitters: dict[int, _LineSplitter] = {_STDERR: _LineSplitter(on_stderr_line)}
        if on_stdout_line is not None:
            self._splitters[_STDOUT] = _LineSplitter(on_stdout_line)
        self._open_pipes = {_STDOUT, _STDERR}
        self._delivering = True
        self.exited: asyncio.Future[None] = loop.create_future()  # terminó el programa
        self.pipes_closed: asyncio.Future[None] = loop.create_future()  # EOF en ambas tuberías
        self.failed: asyncio.Future[None] = loop.create_future()  # falló un callback

    def stop_delivering(self) -> None:
        self._delivering = False

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        splitter = self._splitters.get(fd)
        if splitter is not None:
            self._deliver(lambda: splitter.feed(data))

    def pipe_connection_lost(self, fd: int, exc: Exception | None) -> None:
        splitter = self._splitters.get(fd)
        if splitter is not None:
            self._deliver(splitter.finish)
        self._open_pipes.discard(fd)
        if not self._open_pipes and not self.pipes_closed.done():
            self.pipes_closed.set_result(None)

    def process_exited(self) -> None:
        if not self.exited.done():
            self.exited.set_result(None)

    def _deliver(self, action: Callable[[], None]) -> None:
        if not self._delivering:
            return
        try:
            action()
        except Exception as exc:
            self._delivering = False
            if not self.failed.done():
                self.failed.set_exception(exc)

    def raise_if_failed(self) -> None:
        if self.failed.done():
            self.failed.result()

    def forget_failure(self) -> None:
        # Evita el aviso "exception was never retrieved" si `run` termina con otra excepción
        if self.failed.done():
            self.failed.exception()


def _signal_group(pgid: int, signum: int) -> None:
    # ESRCH: el grupo ya no tiene procesos. EPERM: en macOS, el grupo solo tiene zombis
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signum)


class SandboxRunner:
    """Lanza subprocesos con límites, entorno mínimo y cancelación por grupo de procesos."""

    def __init__(
        self, *, python: str = sys.executable, kill_grace_seconds: float = KILL_GRACE_SECONDS
    ) -> None:
        self._python = python
        self._kill_grace = kill_grace_seconds

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        limits: Limits,
        env: Mapping[str, str],
        on_stdout_line: LineCallback | None = None,
        on_stderr_line: LineCallback | None = None,
    ) -> CompletedRun:
        """Ejecuta `argv` en `cwd` y espera a que termine.

        `argv[0]` se busca en el `PATH` de `env`. `env` es el entorno completo del subproceso,
        normalmente `minimal_env(...)` más las variables propias de la herramienta. Lanza
        `OSError` si el lanzador no puede iniciarse (por ejemplo, si `cwd` no existe). Los
        errores del programa no lanzan excepciones: quedan en `CompletedRun.returncode`, con los
        códigos 125, 126 y 127 del lanzador.
        """
        if not argv:
            raise ValueError("argv no puede estar vacío")
        command = launcher_command(argv, limits, python=self._python)
        tail = _StderrTail()

        def stderr_line(line: str) -> None:
            tail.append(line)
            if on_stderr_line is not None:
                on_stderr_line(line)

        loop = asyncio.get_running_loop()
        transport, protocol = await loop.subprocess_exec(
            lambda: _RunProtocol(loop, on_stdout_line, stderr_line),
            *command,
            stdin=DEVNULL,
            stdout=PIPE,
            stderr=PIPE,
            cwd=cwd,
            env={**env, **PROXY_ENV},
            start_new_session=True,
        )
        pgid = transport.get_pid()  # con start_new_session, el PID es también el del grupo
        try:
            await asyncio.wait(
                (protocol.exited, protocol.failed), return_when=asyncio.FIRST_COMPLETED
            )
            protocol.raise_if_failed()
            # El programa terminó: no deben quedar procesos de su grupo
            _signal_group(pgid, signal.SIGKILL)
            await asyncio.wait(
                (protocol.pipes_closed, protocol.failed),
                timeout=DRAIN_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            protocol.raise_if_failed()
        except BaseException:
            protocol.stop_delivering()  # después de cancelar no se entregan más líneas
            await self._terminate(pgid, protocol)
            raise
        finally:
            protocol.forget_failure()
            transport.close()  # cierra las tuberías aunque un proceso fuera del grupo las use
        returncode = transport.get_returncode()
        assert returncode is not None
        return CompletedRun(returncode=returncode, stderr_lines=tail.lines())

    async def _terminate(self, pgid: int, protocol: _RunProtocol) -> None:
        """SIGTERM al grupo, hasta `kill_grace_seconds` de espera y SIGKILL al grupo."""
        try:
            if not protocol.exited.done():
                _signal_group(pgid, signal.SIGTERM)
                await asyncio.wait((protocol.exited,), timeout=self._kill_grace)
        finally:
            # También si el programa ya terminó: alcanza a los hijos que ignoraron SIGTERM
            _signal_group(pgid, signal.SIGKILL)
        await asyncio.wait((protocol.exited,))
