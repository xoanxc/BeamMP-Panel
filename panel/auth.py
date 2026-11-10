"""Credenciales del panel y protección contra fuerza bruta."""

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
_HASH_METHOD = "pbkdf2:sha256:600000"


class AuthStore:
    """Guarda usuario, hash de la contraseña y clave de sesión en data/panel.json."""

    def __init__(self, data_dir: Path):
        self._file = Path(data_dir) / "panel.json"
        self._lock = threading.Lock()
        self._data = self._load()

    # -- persistencia -------------------------------------------------
    def _load(self) -> dict:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            if {"username", "password_hash", "secret_key"} <= data.keys():
                return data
        except (OSError, ValueError):
            pass
        data = self._defaults(secret_key=secrets.token_hex(32))
        self._save(data)
        return data

    @staticmethod
    def _defaults(secret_key: str) -> dict:
        return {
            "username": DEFAULT_USERNAME,
            "password_hash": generate_password_hash(DEFAULT_PASSWORD, method=_HASH_METHOD),
            "secret_key": secret_key,
            "epoch": secrets.token_hex(8),
            "default_credentials": True,
        }

    def _save(self, data: dict) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self._file)

    # -- consulta -----------------------------------------------------
    @property
    def secret_key(self) -> str:
        return self._data["secret_key"]

    @property
    def username(self) -> str:
        return self._data["username"]

    @property
    def epoch(self) -> str:
        """Cambia cada vez que cambian las credenciales: invalida sesiones antiguas."""
        return self._data.get("epoch", "")

    @property
    def uses_default_credentials(self) -> bool:
        return bool(self._data.get("default_credentials"))

    def verify(self, username: str, password: str) -> bool:
        # Se comprueba siempre el hash para no revelar por tiempo si el usuario existe.
        password_ok = check_password_hash(self._data["password_hash"], password or "")
        username_ok = secrets.compare_digest(
            (username or "").encode(), self._data["username"].encode()
        )
        return password_ok and username_ok

    # -- cambios ------------------------------------------------------
    def set_credentials(self, username: str, password: str) -> None:
        username = (username or "").strip()
        if not 3 <= len(username) <= 32 or not username.isprintable():
            raise ValueError("El usuario debe tener entre 3 y 32 caracteres.")
        if len(password or "") < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"La contraseña debe tener al menos {MIN_PASSWORD_LENGTH} caracteres."
            )
        with self._lock:
            data = dict(self._data)
            data.update(
                username=username,
                password_hash=generate_password_hash(password, method=_HASH_METHOD),
                epoch=secrets.token_hex(8),
                default_credentials=False,
            )
            self._save(data)
            self._data = data

    def reset(self) -> None:
        """Vuelve a admin/admin conservando la clave de sesión."""
        with self._lock:
            data = self._defaults(secret_key=self._data["secret_key"])
            self._save(data)
            self._data = data


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
