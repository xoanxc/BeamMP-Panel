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

    def read(self) -> dict:
        sections: dict[str, dict] = {}
        current = ""
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return sections
        for line in lines:
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
                "--property=LoadState,ActiveState,ActiveEnterTimestampMonotonic",
            )
        except ServiceError:
            return {"state": "unknown", "uptime": None}
        props = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        if props.get("LoadState") == "not-found":
            return {"state": "missing", "uptime": None}
        state = _STATE_NAMES.get(props.get("ActiveState", ""), "unknown")
        uptime = None
        if state == "online":
            try:
                # systemd y time.monotonic() usan el mismo reloj (CLOCK_MONOTONIC).
                started = int(props["ActiveEnterTimestampMonotonic"]) / 1_000_000
                uptime = max(0, int(time.monotonic() - started))
            except (KeyError, ValueError):
                pass
        return {"state": state, "uptime": uptime}

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

    def players(self) -> list[str]:
        with self._lock:
            self._catch_up()
            return list(self._players)

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
            return
        joined = _ASSIGNED_RE.match(message) or _CONNECTED_RE.match(message)
        if joined:
            self._players[joined.group(1)] = None
            return
        gone = _GONE_RE.match(message)
        if gone:
            self._players.pop(gone.group(1), None)

    def tail(self, max_lines: int = 200, max_bytes: int = 256 * 1024) -> list[str]:
        try:
            with open(self.path, "rb") as fh:
                size = os.fstat(fh.fileno()).st_size
                fh.seek(max(0, size - max_bytes))
                data = fh.read()
        except OSError:
            return []
        lines = data.decode("utf-8", errors="replace").splitlines()
        if size > max_bytes and lines:
            lines = lines[1:]  # la primera puede estar cortada
        return lines[-max_lines:]
