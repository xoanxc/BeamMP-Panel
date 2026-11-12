"""Acceso al servidor BeamMP: ServerConfig.toml, servicio systemd y Server.log."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------
# ServerConfig.toml
# ---------------------------------------------------------------------
_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
_KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_-]+)\s*=\s*(.*)$")
_BASIC_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_LITERAL_STRING_RE = re.compile(r"'([^']*)'")


def _parse_value(raw: str):
    raw = raw.strip()
    if raw.startswith('"'):
        match = _BASIC_STRING_RE.match(raw)
        if match:
            try:
                return json.loads(match.group(0))
            except ValueError:
                return match.group(1)
    if raw.startswith("'"):
        match = _LITERAL_STRING_RE.match(raw)
        if match:
            return match.group(1)
    raw = raw.split("#", 1)[0].strip()
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw)
    except ValueError:
        return raw


def _format_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    # Las cadenas JSON son cadenas básicas TOML válidas.
    return json.dumps(str(value), ensure_ascii=False)


class ServerConfig:
    """Lee y modifica ServerConfig.toml conservando comentarios y el resto de claves.

    Se edita línea a línea a propósito: así no hace falta ninguna librería TOML
    y el archivo queda igual que lo dejó BeamMP salvo en las claves cambiadas.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cached: tuple[tuple[int, int], dict] | None = None

    def read(self) -> dict:
        """Secciones del archivo. El resultado se reutiliza mientras el archivo no
        cambie (tamaño y fecha), así que no debe modificarse."""
        try:
            stat = self.path.stat()
        except OSError:
            return {}
        signature = (stat.st_mtime_ns, stat.st_size)
        cached = self._cached
        if cached and cached[0] == signature:
            return cached[1]
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}
        sections = self._parse(text)
        self._cached = (signature, sections)
        return sections

    @staticmethod
    def _parse(text: str) -> dict:
        sections: dict[str, dict] = {}
        current = ""
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            section = _SECTION_RE.match(line)
            if section:
                current = section.group(1).strip()
                continue
            key = _KEY_RE.match(line)
            if key:
                sections.setdefault(current, {})[key.group(2)] = _parse_value(key.group(3))
        return sections

    def get(self, key: str, default=None, section: str = "General"):
        return self.read().get(section, {}).get(key, default)

    def resources_dir(self, beammp_dir: Path) -> Path:
        """Carpeta Resources del servidor (clave ResourceFolder)."""
        return Path(beammp_dir) / str(self.get("ResourceFolder", "Resources") or "Resources")

    def update(self, values: dict, section: str = "General") -> None:
        """Cambia (o añade) claves dentro de una sección. Escritura atómica."""
        with self._lock:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            pending = dict(values)
            current = ""
            section_end = None  # índice donde insertar claves nuevas de la sección
            for i, line in enumerate(lines):
                if line.lstrip().startswith("#"):
                    continue
                header = _SECTION_RE.match(line)
                if header:
                    current = header.group(1).strip()
                    if current == section:
                        section_end = i + 1
                    continue
                key = _KEY_RE.match(line)
                if key and current == section:
                    section_end = i + 1
                    if key.group(2) in pending:
                        value = _format_value(pending.pop(key.group(2)))
                        lines[i] = f"{key.group(1)}{key.group(2)} = {value}"
            if pending:
                new_lines = [f"{k} = {_format_value(v)}" for k, v in pending.items()]
                if section_end is None:
                    lines += ["", f"[{section}]"] + new_lines
                else:
                    lines[section_end:section_end] = new_lines
            self._write("\n".join(lines) + "\n")

    def _write(self, content: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".ServerConfig.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            shutil.copymode(self.path, tmp)
            os.replace(tmp, self.path)
            self._cached = None
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


# ---------------------------------------------------------------------
# Servicio systemd
# ---------------------------------------------------------------------
class ServiceError(RuntimeError):
    pass


_STATE_NAMES = {
    "active": "online",
    "activating": "starting",
    "reloading": "starting",
    "deactivating": "stopping",
    "inactive": "offline",
    "failed": "failed",
}


class Service:
    ACTIONS = ("start", "stop", "restart")

    def __init__(self, name: str, cache_seconds: float = 2.0):
        self.name = name
        self._cache_seconds = cache_seconds
        self._cached: tuple[float, dict] | None = None
        self._lock = threading.Lock()

    def _systemctl(self, *args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["systemctl", *args], capture_output=True, text=True, timeout=15
            )
        except FileNotFoundError as exc:
            raise ServiceError("systemctl no está disponible en este sistema.") from exc
        except subprocess.TimeoutExpired as exc:
            raise ServiceError("systemctl no ha respondido a tiempo.") from exc

    def status(self) -> dict:
        """Estado del servicio: online, starting, stopping, offline, failed, missing o unknown."""
        with self._lock:
            now = time.monotonic()
            if self._cached and now - self._cached[0] < self._cache_seconds:
                return self._cached[1]
            status = self._query()
            self._cached = (now, status)
            return status

    def _query(self) -> dict:
        try:
            result = self._systemctl(
                "show",
                self.name,
                "--property=LoadState,ActiveState,ActiveEnterTimestampMonotonic,MainPID",
            )
        except ServiceError:
            return {"state": "unknown", "uptime": None, "pid": None}
        props = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        if props.get("LoadState") == "not-found":
            return {"state": "missing", "uptime": None, "pid": None}
        state = _STATE_NAMES.get(props.get("ActiveState", ""), "unknown")
        uptime = None
        main_pid = props.get("MainPID", "")
        pid = int(main_pid) if main_pid.isdigit() else 0
        if state == "online":
            try:
                # systemd y time.monotonic() usan el mismo reloj (CLOCK_MONOTONIC).
                started = int(props["ActiveEnterTimestampMonotonic"]) / 1_000_000
                uptime = max(0, int(time.monotonic() - started))
            except (KeyError, ValueError):
                pass
        return {"state": state, "uptime": uptime, "pid": pid or None}

    def run(self, action: str) -> None:
        if action not in self.ACTIONS:
            raise ServiceError(f"Acción no permitida: {action}")
        # --no-block: la orden se encola y la petición web responde al momento.
        result = self._systemctl(action, "--no-block", self.name)
        with self._lock:
            self._cached = None
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or f"código {result.returncode}"
            raise ServiceError(f"systemctl {action} ha fallado: {detail}")


# ---------------------------------------------------------------------
# Server.log
# ---------------------------------------------------------------------
_INFO_RE = re.compile(r"\[INFO\]\s+(.*?)\s*$")
_ASSIGNED_RE = re.compile(r"^Assigned ID \d+ to (.+)$")
_CONNECTED_RE = re.compile(r"^(.+) : Connected$")
_GONE_RE = re.compile(r"^(.+) Connection Terminated$")
_BOOT_PREFIX = "BeamMP Server v"
_VERSION_RE = re.compile(r"^BeamMP Server v(\d+(?:\.\d+)*)")


class LogTracker:
    """Sigue Server.log de forma incremental para saber quién está conectado.

    Solo se leen los bytes nuevos desde la última consulta. Si el archivo se
    recorta o se sustituye (reinicio del servidor), se empieza de cero.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._inode = None
        self._offset = 0
        self._players: dict[str, None] = {}  # dict para conservar el orden de llegada
        self._version: str | None = None

    def players(self) -> list[str]:
        with self._lock:
            self._catch_up()
            return list(self._players)

    def server_version(self) -> str | None:
        """Versión de BeamMP Server según su último arranque registrado."""
        with self._lock:
            self._catch_up()
            return self._version

    def _catch_up(self) -> None:
        try:
            with open(self.path, "rb") as fh:
                stat = os.fstat(fh.fileno())
                if stat.st_ino != self._inode or stat.st_size < self._offset:
                    self._inode, self._offset = stat.st_ino, 0
                    self._players.clear()
                fh.seek(self._offset)
                for raw in fh:
                    if not raw.endswith(b"\n"):
                        break  # línea a medio escribir: se leerá en la próxima consulta
                    self._offset += len(raw)
                    self._handle(raw.decode("utf-8", errors="replace"))
        except OSError:
            self._inode, self._offset = None, 0
            self._players.clear()

    def _handle(self, line: str) -> None:
        match = _INFO_RE.search(line)
        if not match:
            return
        message = match.group(1)
        if message.startswith(_BOOT_PREFIX):
            self._players.clear()
            version = _VERSION_RE.match(message)
            if version:
                self._version = version.group(1)
            return
        joined = _ASSIGNED_RE.match(message) or _CONNECTED_RE.match(message)
        if joined:
            self._players[joined.group(1)] = None
            return
        gone = _GONE_RE.match(message)
        if gone:
            self._players.pop(gone.group(1), None)

    def forget(self, name: str) -> None:
        """Quita a un jugador de la lista sin esperar a que el registro lo confirme."""
        with self._lock:
            self._players.pop(name, None)

    def follow(self, cursor: str | None = None, max_lines: int = 400, max_bytes: int = 256 * 1024) -> dict:
        """Líneas completas escritas desde `cursor`.

        Sin cursor (o si el archivo se ha sustituido o recortado) devuelve el
        final del registro con reset=True. El cursor devuelto se pasa en la
        siguiente consulta para recibir solo lo nuevo.
        """
        try:
            with open(self.path, "rb") as fh:
                stat = os.fstat(fh.fileno())
                start = self._cursor_offset(cursor, stat)
                reset = start is None or stat.st_size - start > max_bytes
                if reset:
                    start = max(0, stat.st_size - max_bytes)
                fh.seek(start)
                data = fh.read(stat.st_size - start)
        except OSError:
            return {"lines": [], "cursor": None, "reset": True, "found": False}
        if reset and start > 0:
            cut = data.find(b"\n") + 1  # la primera línea puede estar cortada
            data, start = data[cut:], start + cut
        end = data.rfind(b"\n") + 1  # una línea a medio escribir se deja para la próxima
        lines = [
            line.rstrip("\r")
            for line in data[:end].decode("utf-8", errors="replace").split("\n")[:-1]
        ]
        if reset:
            lines = lines[-max_lines:]
        return {
            "lines": lines,
            "cursor": f"{stat.st_ino}:{start + end}",
            "reset": reset,
            "found": True,
        }

    @staticmethod
    def _cursor_offset(cursor: str | None, stat: os.stat_result) -> int | None:
        try:
            inode, offset = (int(part) for part in (cursor or "").split(":"))
        except ValueError:
            return None
        return offset if inode == stat.st_ino and 0 <= offset <= stat.st_size else None

    def tail(self, max_lines: int = 200) -> list[str]:
        return self.follow(None, max_lines)["lines"]
