"""Usuarios del panel (con roles) y protección contra fuerza bruta."""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"
MIN_PASSWORD_LENGTH = 8
ROLES = {"admin": "Administrador", "moderator": "Moderador"}
_HASH_METHOD = "pbkdf2:sha256:600000"


class AuthError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _user(password: str, role: str) -> dict:
    return {
        "password_hash": generate_password_hash(password, method=_HASH_METHOD),
        "role": role,
        "epoch": secrets.token_hex(8),  # cambia con la contraseña: invalida sesiones antiguas
    }


def _clean_username(username: str) -> str:
    username = (username or "").strip()
    if not 3 <= len(username) <= 32 or not username.isprintable():
        raise AuthError("El usuario debe tener entre 3 y 32 caracteres.")
    return username


def _check_password(password: str) -> str:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(f"La contraseña debe tener al menos {MIN_PASSWORD_LENGTH} caracteres.")
    return password


class AuthStore:
    """Guarda usuarios (hash, rol) y la clave de sesión en data/panel.json.

    Roles: 'admin' lo puede todo; 'moderator' solo ve el resumen, expulsa
    jugadores y envía mensajes al chat.
    """

    def __init__(self, data_dir: Path):
        self._file = Path(data_dir) / "panel.json"
        self._lock = threading.RLock()
        # Se comprueba contra este hash cuando el usuario no existe, para que la
        # respuesta tarde lo mismo y no revele qué usuarios hay.
        self._dummy_hash = generate_password_hash(secrets.token_hex(8), method=_HASH_METHOD)
        self._data = self._load()

    # -- persistencia -------------------------------------------------
    def _load(self) -> dict:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and "secret_key" in data:
            if isinstance(data.get("users"), dict) and data["users"]:
                return data
            if {"username", "password_hash"} <= data.keys():  # formato de un solo usuario (v2.0)
                migrated = {
                    "secret_key": data["secret_key"],
                    "default_credentials": bool(data.get("default_credentials")),
                    "users": {
                        data["username"]: {
                            "password_hash": data["password_hash"],
                            "role": "admin",
                            "epoch": data.get("epoch") or secrets.token_hex(8),
                        }
                    },
                }
                self._save(migrated)
                return migrated
        data = {
            "secret_key": secrets.token_hex(32),
            "default_credentials": True,
            "users": {DEFAULT_USERNAME: _user(DEFAULT_PASSWORD, "admin")},
        }
        self._save(data)
        return data

    def _save(self, data: dict) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self._file)

    def _change(self, mutate) -> None:
        """Aplica `mutate(users, data)` sobre una copia y la guarda."""
        with self._lock:
            data = json.loads(json.dumps(self._data))
            mutate(data["users"], data)
            self._save(data)
            self._data = data

    # -- consulta -----------------------------------------------------
    @property
    def secret_key(self) -> str:
        return self._data["secret_key"]

    @property
    def uses_default_credentials(self) -> bool:
        return bool(self._data.get("default_credentials"))

    def users(self) -> list[dict]:
        return [
            {"username": name, "role": user["role"]}
            for name, user in sorted(self._data["users"].items(), key=lambda item: item[0].lower())
        ]

    def verify(self, username: str, password: str) -> dict | None:
        """Devuelve los datos de sesión ({'user', 'epoch'}) si las credenciales son correctas."""
        user = self._data["users"].get(username or "")
        password_ok = check_password_hash(user["password_hash"] if user else self._dummy_hash, password or "")
        return {"user": username, "epoch": user["epoch"]} if user and password_ok else None

    def session_user(self, username, epoch) -> dict | None:
        """Usuario de una sesión, o None si ya no vale (borrado o con contraseña cambiada)."""
        user = self._data["users"].get(username) if isinstance(username, str) else None
        if not user or not secrets.compare_digest(str(epoch or ""), user["epoch"]):
            return None
        return {"username": username, "role": user["role"]}

    # -- cambios ------------------------------------------------------
    def _require(self, users: dict, username: str) -> dict:
        if username not in users:
            raise AuthError("Ese usuario no existe.", 404)
        return users[username]

    @staticmethod
    def _keep_an_admin(users: dict) -> None:
        if not any(user["role"] == "admin" for user in users.values()):
            raise AuthError("Tiene que quedar al menos un administrador.", 409)

    def update_self(self, current: str, new_username: str, password: str) -> dict:
        """Cambia el nombre y la contraseña de un usuario. Devuelve sus nuevos datos de sesión."""
        new_username = _clean_username(new_username)
        _check_password(password)

        def mutate(users, data):
            user = self._require(users, current)
            if new_username != current and new_username in users:
                raise AuthError("Ya existe un usuario con ese nombre.", 409)
            del users[current]
            users[new_username] = _user(password, user["role"])
            data["default_credentials"] = False

        self._change(mutate)
        return {"user": new_username, "epoch": self._data["users"][new_username]["epoch"]}

    def create_user(self, username: str, password: str, role: str) -> None:
        username = _clean_username(username)
        _check_password(password)
        if role not in ROLES:
            raise AuthError("Rol no válido.")

        def mutate(users, _data):
            if username in users:
                raise AuthError("Ya existe un usuario con ese nombre.", 409)
            users[username] = _user(password, role)

        self._change(mutate)

    def set_role(self, username: str, role: str) -> None:
        if role not in ROLES:
            raise AuthError("Rol no válido.")

        def mutate(users, _data):
            self._require(users, username)["role"] = role
            self._keep_an_admin(users)

        self._change(mutate)

    def set_password(self, username: str, password: str) -> None:
        _check_password(password)

        def mutate(users, _data):
            users[username] = _user(password, self._require(users, username)["role"])

        self._change(mutate)

    def delete_user(self, username: str) -> None:
        def mutate(users, _data):
            self._require(users, username)
            del users[username]
            self._keep_an_admin(users)

        self._change(mutate)

    def reset(self) -> None:
        """Deja un administrador admin/admin. Los demás usuarios y la clave de sesión se conservan."""

        def mutate(users, data):
            users[DEFAULT_USERNAME] = _user(DEFAULT_PASSWORD, "admin")
            data["default_credentials"] = True

        self._change(mutate)


class LoginThrottle:
    """Bloquea temporalmente una IP tras varios intentos fallidos."""

    def __init__(self, max_failures: int = 5, lock_seconds: int = 60):
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self._failures: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def seconds_blocked(self, ip: str) -> int:
        with self._lock:
            count, since = self._failures.get(ip, (0, 0.0))
            remaining = since + self.lock_seconds - time.monotonic()
            if count >= self.max_failures and remaining > 0:
                return int(remaining) + 1
            if remaining <= 0:
                self._failures.pop(ip, None)
            return 0

    def failed(self, ip: str) -> None:
        with self._lock:
            count, _ = self._failures.get(ip, (0, 0.0))
            self._failures[ip] = (count + 1, time.monotonic())

    def succeeded(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)
