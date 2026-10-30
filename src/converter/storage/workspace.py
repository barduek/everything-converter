"""Gestor_de_Archivos_Temporales: Espacios_de_Trabajo dentro del Almacenamiento_Temporal.

El Almacenamiento_Temporal es la ruta fija `/var/lib/converter/storage` (Req 20.7). La raíz se
recibe como parámetro, con ese valor predeterminado, solo para poder probar con directorios
temporales; el arranque no la toma de ninguna Variable_de_Configuración.

Cada Espacio_de_Trabajo es un directorio `ws-<32 hex aleatorios>` con modo 0700 (Req 17.1):

    ws-3b9f.../
      source/original     Archivo_de_Origen, con nombre interno fijo (Req 1.6)
      work/               Archivos_Intermedios y salidas parciales
      tmp/                HOME y TMPDIR de los subprocesos, perfil de LibreOffice
      result/output       Archivo_Resultado, solo después de la entrega

Los archivos de una Tarea se guardan solo en las rutas de su `Workspace` (Req 13.2, 17.7). El
nombre del directorio sale de `secrets`, nunca del Nombre_Original.

Borrado seguro (Req 17.6, 17.8, 20.8):

- `remove` y `prune_for_completed` resuelven la ruta real del Espacio_de_Trabajo y comprueban
  que su padre sea el Almacenamiento_Temporal. Si no lo es, lanzan `OutsideStorageError` sin
  tocar nada.
- Los borrados usan descriptores de directorio (`dir_fd`) y `shutil.rmtree`, que en Linux y
  macOS recorre el árbol con operaciones relativas a descriptores y no sigue enlaces
  simbólicos. Un enlace simbólico se borra como entrada, nunca su destino.
- `purge_all_workspaces` borra solo los directorios reales cuyo nombre coincide con
  `^ws-[0-9a-f]{32}$`. Todo otro archivo, carpeta o enlace del volumen queda intacto.

El Conversor no inspecciona el tipo de sistema de archivos del Almacenamiento_Temporal ni
rechaza volúmenes de red: el volumen es responsabilidad de quien lo monta (D17, Req 20.8).

Las operaciones son síncronas y no guardan estado mutable, así que pueden correr en hilos
(`anyio.to_thread`). No registran nada: un fallo se lanza (o, en la purga, se devuelve) para
que el barrendero lo registre en el Log_de_Eventos y reintente (Req 17.9).
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

STORAGE_ROOT = Path("/var/lib/converter/storage")  # Req 20.7

WORKSPACE_PREFIX = "ws-"
WORKSPACE_NAME = re.compile(r"ws-[0-9a-f]{32}")  # se usa con `fullmatch`
WORKSPACE_MODE = 0o700
_WORKSPACE_ID_BYTES = 16  # 32 dígitos hexadecimales

SOURCE_DIR = "source"
WORK_DIR = "work"
TMP_DIR = "tmp"
RESULT_DIR = "result"
SUBDIRS = (SOURCE_DIR, WORK_DIR, TMP_DIR, RESULT_DIR)

SOURCE_NAME = "original"  # Archivo_de_Origen dentro de source/ (Req 1.6)
RESULT_NAME = "output"  # Archivo_Resultado dentro de result/

_WRITE_CHECK_NAME = "write-check"

# Abre un directorio sin seguir un enlace simbólico en el último componente
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class OutsideStorageError(OSError):
    """El Espacio_de_Trabajo no está directamente dentro del Almacenamiento_Temporal (Req 17.8).

    Hereda de `OSError` para que quien borra trate este rechazo como cualquier otro fallo de
    eliminación.
    """


@dataclass(frozen=True, slots=True)
class Workspace:
    """Espacio_de_Trabajo de una Tarea o de una recepción en curso."""

    path: Path  # <Almacenamiento_Temporal>/ws-<32 hex>

    def __post_init__(self) -> None:
        if WORKSPACE_NAME.fullmatch(self.path.name) is None:
            raise ValueError(f"nombre de Espacio_de_Trabajo inválido: {self.path.name!r}")

    @property
    def name(self) -> str:
        """Nombre del directorio, `ws-<32 hex>`; es lo que identifica al espacio en el log."""
        return self.path.name

    @property
    def source_dir(self) -> Path:
        return self.path / SOURCE_DIR

    @property
    def source_file(self) -> Path:
        """Archivo_de_Origen: `source/original`."""
        return self.path / SOURCE_DIR / SOURCE_NAME

    @property
    def work_dir(self) -> Path:
        return self.path / WORK_DIR

    @property
    def tmp_dir(self) -> Path:
        return self.path / TMP_DIR

    @property
    def result_dir(self) -> Path:
        return self.path / RESULT_DIR

    @property
    def result_file(self) -> Path:
        """Archivo_Resultado: `result/output`."""
        return self.path / RESULT_DIR / RESULT_NAME


@dataclass(frozen=True, slots=True)
class RemovalFailure:
    """Espacio_de_Trabajo que la purga no pudo eliminar, con el error."""

    workspace: Workspace
    error: OSError


@contextlib.contextmanager
def _open_dir(path: str, *, dir_fd: int | None = None) -> Iterator[int]:
    fd = os.open(path, _DIR_FLAGS, dir_fd=dir_fd)
    try:
        yield fd
    finally:
        os.close(fd)


def _make_private_dir(name: str, *, dir_fd: int) -> None:
    os.mkdir(name, WORKSPACE_MODE, dir_fd=dir_fd)  # FileExistsError si ya existe: no reutiliza
    with _open_dir(name, dir_fd=dir_fd) as fd:
        # `mkdir` aplica la umask; `fchmod` deja exactamente 0700 sin seguir enlaces
        os.fchmod(fd, WORKSPACE_MODE)


def _delete_entry(name: str, *, dir_fd: int) -> None:
    """Borra la entrada `name` del directorio `dir_fd` sin seguir enlaces simbólicos.

    Un directorio real se borra con su contenido; un enlace simbólico o un archivo, como
    entrada. Si la entrada no existe, no hace nada.
    """
    try:
        info = os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        # Si entre `lstat` y `rmtree` la entrada pasa a ser un enlace, `rmtree` lo detecta
        # comparando `lstat` con `fstat` y lanza OSError sin seguirlo
        shutil.rmtree(name, dir_fd=dir_fd)
    else:
        os.unlink(name, dir_fd=dir_fd)


class WorkspaceManager:
    """Crea y elimina Espacios_de_Trabajo dentro del Almacenamiento_Temporal."""

    __slots__ = ("_root",)

    def __init__(self, root: str | os.PathLike[str] = STORAGE_ROOT) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """Ruta del Almacenamiento_Temporal tal como se configuró."""
        return self._root

    def _real_root(self) -> str:
        return os.path.realpath(self._root)

    def _open_storage_for(self, ws: Workspace) -> contextlib.AbstractContextManager[int]:
        # Solo se resuelve el padre: el Espacio_de_Trabajo se abre después sin seguir enlaces
        real_root = self._real_root()
        if os.path.realpath(ws.path.parent) != real_root:
            raise OutsideStorageError(f"{ws.name} no está en el Almacenamiento_Temporal")
        return _open_dir(real_root)

    def free_bytes(self) -> int:
        """Bytes libres del Almacenamiento_Temporal para un proceso sin privilegios (Req 1.5)."""
        info = os.statvfs(self._root)
        return info.f_bavail * info.f_frsize

    def create(self) -> Workspace:
        """Crea un Espacio_de_Trabajo nuevo con modo 0700 y sus subdirectorios (Req 17.1).

        El nombre lleva 128 bits de `secrets`; `mkdir` falla en vez de reutilizar un directorio
        existente, así que nunca coincide con el de otra Tarea o recepción. Si falla a mitad de
        camino, borra lo creado y relanza el `OSError`.
        """
        real_root = self._real_root()
        name = WORKSPACE_PREFIX + secrets.token_hex(_WORKSPACE_ID_BYTES)
        with _open_dir(real_root) as root_fd:
            # Fuera del `try`: si el nombre ya existiera, el directorio ajeno no se borra
            os.mkdir(name, WORKSPACE_MODE, dir_fd=root_fd)
            try:
                with _open_dir(name, dir_fd=root_fd) as ws_fd:
                    os.fchmod(ws_fd, WORKSPACE_MODE)
                    for subdir in SUBDIRS:
                        _make_private_dir(subdir, dir_fd=ws_fd)
            except BaseException:
                with contextlib.suppress(OSError):
                    _delete_entry(name, dir_fd=root_fd)
                raise
        return Workspace(Path(real_root, name))

    def remove(self, ws: Workspace) -> None:
        """Elimina el Espacio_de_Trabajo con todo su contenido (Req 17.3, 17.4, 17.6, 17.10).

        Si ya no existe, no hace nada, así un reintento es seguro. Lanza `OutsideStorageError`
        si el espacio no está en el Almacenamiento_Temporal y `OSError` si el borrado falla.
        """
        with self._open_storage_for(ws) as root_fd:
            _delete_entry(ws.name, dir_fd=root_fd)

    def prune_for_completed(self, ws: Workspace) -> None:
        """Deja en el Espacio_de_Trabajo solo `result/` (Req 17.2).

        Borra `source/`, `work/`, `tmp/` y cualquier otra entrada, salvo `result/` si es un
        directorio real. Si el espacio ya no existe, no hace nada. Lanza `OutsideStorageError`
        si no está en el Almacenamiento_Temporal y `OSError` si el borrado falla o si la ruta
        del espacio dejó de ser un directorio real (por ejemplo, si es un enlace simbólico).
        """
        with self._open_storage_for(ws) as root_fd:
            try:
                # Con O_NOFOLLOW y O_DIRECTORY, un enlace o un archivo en su lugar da OSError
                ws_fd = os.open(ws.name, _DIR_FLAGS, dir_fd=root_fd)
            except FileNotFoundError:
                return
            try:
                with os.scandir(ws_fd) as entries:
                    doomed = [
                        entry.name
                        for entry in entries
                        if not (entry.name == RESULT_DIR and entry.is_dir(follow_symlinks=False))
                    ]
                for name in doomed:
                    _delete_entry(name, dir_fd=ws_fd)
            finally:
                os.close(ws_fd)

    def purge_all_workspaces(self) -> list[RemovalFailure]:
        """Elimina todos los Espacios_de_Trabajo del Almacenamiento_Temporal (Req 17.5, 20.8).

        Borra solo los directorios reales cuyo nombre coincide con `^ws-[0-9a-f]{32}$`; no
        sigue enlaces simbólicos ni toca otros archivos o carpetas. Intenta todos aunque alguno
        falle y devuelve los que no pudo eliminar, para registrarlos y reintentar. Lanza
        `OSError` si no puede leer el Almacenamiento_Temporal.
        """
        real_root = self._real_root()
        failures: list[RemovalFailure] = []
        with _open_dir(real_root) as root_fd:
            with os.scandir(root_fd) as entries:
                names = sorted(
                    entry.name
                    for entry in entries
                    if WORKSPACE_NAME.fullmatch(entry.name) is not None
                    and entry.is_dir(follow_symlinks=False)
                )
            for name in names:
                try:
                    shutil.rmtree(name, dir_fd=root_fd)
                except OSError as exc:
                    failures.append(RemovalFailure(Workspace(Path(real_root, name)), exc))
        return failures

    def check_writable(self) -> None:
        """Comprueba al arrancar que se pueden crear archivos en el almacenamiento (Req 20.13).

        Crea un Espacio_de_Trabajo de prueba, crea un archivo vacío en su `tmp/` y borra todo.
        Si el proceso termina antes de borrarlo, la purga del arranque lo elimina, porque es un
        Espacio_de_Trabajo. Lanza `OSError` con el motivo si algún paso falla.
        """
        ws = self.create()
        try:
            fd = os.open(
                ws.tmp_dir / _WRITE_CHECK_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            os.close(fd)
        finally:
            self.remove(ws)
