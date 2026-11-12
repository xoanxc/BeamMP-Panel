"""Reinicios programados y rotación automática de mapas (data/schedule.json)."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_FIRE_WINDOW = timedelta(seconds=90)  # margen para no perder una hora si el panel va justo
TICK_SECONDS = 20


class ScheduleError(ValueError):
    pass


def _defaults() -> dict:
    return {
        "restart": {"enabled": False, "times": [], "warn_minutes": 5},
        "rotation": {"enabled": False, "maps": [], "every_hours": 6, "only_when_empty": True, "last": 0},
    }


class ScheduleStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def read(self) -> dict:
        data = _defaults()
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return data
        for section in data:
            if isinstance(stored, dict) and isinstance(stored.get(section), dict):
                for key, default in data[section].items():
                    value = stored[section].get(key)
                    if type(value) is type(default):
                        data[section][key] = value
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def save(self, submitted, valid_map_paths: set[str], now: float) -> dict:
        """Valida lo enviado desde el panel y lo guarda."""
        try:
            restart, rotation = submitted["restart"], submitted["rotation"]
            times = sorted(set(restart["times"]))
            warn, hours, maps = restart["warn_minutes"], rotation["every_hours"], rotation["maps"]
            flags = (restart["enabled"], rotation["enabled"], rotation["only_when_empty"])
        except (KeyError, TypeError) as exc:
            raise ScheduleError("Programación no válida.") from exc
        if not all(isinstance(flag, bool) for flag in flags):
            raise ScheduleError("Programación no válida.")
        if len(times) > 24 or not all(isinstance(t, str) and _TIME_RE.match(t) for t in times):
            raise ScheduleError("Las horas de reinicio deben tener el formato HH:MM.")
        if type(warn) is not int or not 0 <= warn <= 60:
            raise ScheduleError("El aviso previo debe estar entre 0 y 60 minutos.")
        if type(hours) is not int or not 1 <= hours <= 720:
            raise ScheduleError("El intervalo de rotación debe estar entre 1 y 720 horas.")
        if not isinstance(maps, list) or len(maps) > 100 or len(set(map(str, maps))) != len(maps):
            raise ScheduleError("Lista de mapas no válida.")
        if any(path not in valid_map_paths for path in maps):
            raise ScheduleError("La rotación incluye un mapa que no está en el catálogo.")
        if flags[0] and not times:
            raise ScheduleError("Añade al menos una hora de reinicio.")
        if flags[1] and len(maps) < 2:
            raise ScheduleError("La rotación necesita al menos dos mapas.")
        with self._lock:
            previous = self.read()["rotation"]
            # El intervalo empieza a contar al activar la rotación.
            last = previous["last"] if previous["enabled"] and flags[1] else int(now)
            data = {
                "restart": {"enabled": flags[0], "times": times, "warn_minutes": warn},
                "rotation": {"enabled": flags[1], "maps": maps, "every_hours": hours, "only_when_empty": flags[2], "last": last},
            }
            self._write(data)
        return data

    def mark_rotated(self, now: float) -> None:
        with self._lock:
            data = self.read()
            data["rotation"]["last"] = int(now)
            self._write(data)


class Scheduler:
    """Comprueba la programación cada pocos segundos en un hilo del panel."""

    def __init__(self, store: ScheduleStore, config, service, catalog, bridge, player_count):
        self.store = store
        self.config = config
        self.service = service
        self.catalog = catalog
        self.bridge = bridge
        self.player_count = player_count  # función: jugadores conectados ahora
        self._done: set[str] = set()  # avisos y reinicios ya hechos (clave con fecha y hora)
        self._rotation_at: datetime | None = None  # rotación anunciada, pendiente de ejecutar
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="panel-scheduler", daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick(datetime.now())
            except Exception:  # noqa: BLE001 - un fallo puntual no debe parar el planificador
                pass

    def _announce(self, message: str) -> None:
        try:
            self.bridge.say(message)
        except Exception:  # noqa: BLE001 - el aviso es opcional
            pass

    def tick(self, now: datetime) -> list[str]:
        """Hace lo que toque a la hora `now`. Devuelve las acciones realizadas."""
        schedule = self.store.read()
        if self.service.status()["state"] != "online":
            self._rotation_at = None
            return []  # no se arranca un servidor que alguien ha detenido
        actions = self._tick_restart(schedule["restart"], now)
        if "restart" not in actions:
            actions += self._tick_rotation(schedule["rotation"], schedule["restart"]["warn_minutes"], now)
        return actions

    def _tick_restart(self, restart: dict, now: datetime) -> list[str]:
        if not restart["enabled"]:
            return []
        actions = []
        warn = timedelta(minutes=restart["warn_minutes"])
        for day in (now.date(), now.date() + timedelta(days=1)):
            for text in restart["times"]:
                fire_at = datetime.combine(day, datetime.strptime(text, "%H:%M").time())
                key = fire_at.isoformat()
                if warn and fire_at - warn <= now < fire_at and "warn " + key not in self._done:
                    self._done.add("warn " + key)
                    minutes = max(1, round((fire_at - now).total_seconds() / 60))
                    self._announce(f"El servidor se reiniciará en {minutes} min.")
                    actions.append("warn")
                if fire_at <= now < fire_at + _FIRE_WINDOW and key not in self._done:
                    self._done.add(key)
                    self.service.run("restart")
                    actions.append("restart")
        if len(self._done) > 200:
            self._done = {k for k in self._done if k.split(" ")[-1] >= (now - timedelta(days=1)).isoformat()}
        return actions

    def _next_map(self, maps: list[str]) -> str | None:
        available = [path for path in maps if self.catalog.find(path)]
        if len(available) < 2:
            return None
        current = self.config.get("Map", "")
        index = available.index(current) + 1 if current in available else 0
        return available[index % len(available)]

    def _tick_rotation(self, rotation: dict, warn_minutes: int, now: datetime) -> list[str]:
        if not rotation["enabled"] or now.timestamp() - rotation["last"] < rotation["every_hours"] * 3600:
            self._rotation_at = None
            return []
        target = self._next_map(rotation["maps"])
        if not target:
            return []
        players = self.player_count()
        if players and rotation["only_when_empty"]:
            self._rotation_at = None
            return []  # se espera a que el servidor quede vacío
        if players and warn_minutes and self._rotation_at is None:
            self._rotation_at = now + timedelta(minutes=warn_minutes)
            name = self.catalog.name_for(target)
            self._announce(f"Cambio de mapa a {name} en {warn_minutes} min. El servidor se reiniciará.")
            return ["warn"]
        if self._rotation_at and now < self._rotation_at:
            return []
        self._rotation_at = None
        self.config.update({"Map": target})
        self.store.mark_rotated(now.timestamp())
        self.service.run("restart")
        return ["rotate"]

    def upcoming(self, now: datetime) -> dict:
        """Próximo reinicio y próxima rotación, para mostrarlos en el panel."""
        schedule = self.store.read()
        next_restart = None
        if schedule["restart"]["enabled"]:
            candidates = [
                datetime.combine(day, datetime.strptime(text, "%H:%M").time())
                for day in (now.date(), now.date() + timedelta(days=1))
                for text in schedule["restart"]["times"]
            ]
            future = [c for c in candidates if c > now]
            next_restart = min(future).strftime("%d/%m %H:%M") if future else None
        rotation = schedule["rotation"]
        next_rotation = next_map = None
        if rotation["enabled"]:
            due = datetime.fromtimestamp(rotation["last"] + rotation["every_hours"] * 3600)
            next_rotation = max(due, now).strftime("%d/%m %H:%M")
            target = self._next_map(rotation["maps"])
            next_map = self.catalog.name_for(target) if target else None
        return {"restart": next_restart, "rotation": next_rotation, "rotation_map": next_map}
