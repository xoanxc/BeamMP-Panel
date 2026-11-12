"""Bloqueos permanentes y lista blanca de jugadores (data/access.json)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .bridge import PlayerBridge, clean_text, valid_player_name

DEFAULT_BAN_REASON = "Bloqueado por un administrador"
MAX_ENTRIES = 2000


class AccessError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class AccessList:
    """Los nombres se comparan sin distinguir mayúsculas. Cada cambio se publica al plugin."""

    def __init__(self, path: Path, bridge: PlayerBridge):
        self.path = Path(path)
        self.bridge = bridge
        self._lock = threading.Lock()

    def read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        bans = [
            {"name": b["name"], "reason": str(b.get("reason") or DEFAULT_BAN_REASON), "date": int(b.get("date") or 0)}
            for b in data.get("bans", [])
            if isinstance(b, dict) and valid_player_name(b.get("name"))
        ]
        whitelist = [n for n in data.get("whitelist", []) if valid_player_name(n)]
        return {"bans": bans, "whitelist_enabled": data.get("whitelist_enabled") is True, "whitelist": whitelist}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
        self._publish(data)

    def _publish(self, data: dict) -> None:
        self.bridge.write_access(
            {b["name"]: f"Bloqueado: {b['reason']}" for b in data["bans"]},
            data["whitelist_enabled"],
            data["whitelist"],
        )

    def sync(self) -> None:
        """Vuelve a publicar el estado guardado (al arrancar o tras restaurar una copia)."""
        self._publish(self.read())

    def ban(self, name: str, reason: str = "") -> None:
        if not valid_player_name(name):
            raise AccessError("Nombre de jugador no válido.")
        with self._lock:
            data = self.read()
            data["bans"] = [b for b in data["bans"] if b["name"].lower() != name.lower()]
            if len(data["bans"]) >= MAX_ENTRIES:
                raise AccessError("La lista de bloqueados está llena.")
            data["bans"].append(
                {"name": name, "reason": clean_text(reason, DEFAULT_BAN_REASON), "date": int(time.time())}
            )
            data["bans"].sort(key=lambda b: b["name"].lower())
            self._save(data)

    def unban(self, name: str) -> None:
        with self._lock:
            data = self.read()
            kept = [b for b in data["bans"] if b["name"].lower() != str(name).lower()]
            if len(kept) == len(data["bans"]):
                raise AccessError("Ese jugador no está bloqueado.", 404)
            data["bans"] = kept
            self._save(data)

    def set_whitelist(self, enabled, names) -> None:
        if not isinstance(enabled, bool) or not isinstance(names, list) or len(names) > MAX_ENTRIES:
            raise AccessError("Lista blanca no válida.")
        unique: dict[str, str] = {}
        for name in names:
            if not valid_player_name(name):
                raise AccessError(f"Nombre de jugador no válido: {name!s:.40}")
            unique.setdefault(name.lower(), name)
        if enabled and not unique:
            raise AccessError("Añade al menos un jugador antes de activar la lista blanca.")
        with self._lock:
            data = self.read()
            data["whitelist_enabled"] = enabled
            data["whitelist"] = sorted(unique.values(), key=str.lower)
            self._save(data)
