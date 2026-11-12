"""Piezas comunes de las rutas: servicios del panel, sesión, permisos y errores."""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps

from flask import Response, current_app, jsonify, redirect, request, session, url_for

from .access import AccessList
from .auth import AuthStore, LoginThrottle
from .backups import BackupManager
from .bridge import PlayerBridge
from .metrics import Metrics
from .mods import MapCatalog, ModLibrary
from .previews import PreviewStore
from .schedule import Scheduler, ScheduleStore
from .server import LogTracker, ServerConfig, Service
from .settings import Settings
from .updater import ServerUpdater

STATIC_MAX_AGE = 365 * 24 * 3600  # los recursos llevan ?v=<versión>, así que pueden cachearse


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Services:
    """Todo lo que las rutas necesitan; se crea una vez en create_app()."""

    settings: Settings
    auth: AuthStore
    throttle: LoginThrottle
    config: ServerConfig
    service: Service
    log: LogTracker
    mods: ModLibrary
    previews: PreviewStore
    catalog: MapCatalog
    bridge: PlayerBridge
    access: AccessList
    metrics: Metrics
    schedule: ScheduleStore
    scheduler: Scheduler
    updater: ServerUpdater
    backups: BackupManager

    def players(self) -> list[dict]:
        """Jugadores conectados. Con el puente activo, la lista exacta del servidor;
        si no, la que se deduce de Server.log (sin ID ni marca de invitado)."""
        if self.service.status()["state"] != "online":
            return []
        exact = self.bridge.players()
        if exact is not None:
            return exact
        return [{"id": None, "name": name, "guest": None} for name in self.log.players()]

    def save_config(self, changes: dict) -> None:
        try:
            self.config.update(changes)
        except OSError as exc:
            raise ApiError(f"No se ha podido escribir ServerConfig.toml: {exc}", 500) from exc


def services() -> Services:
    return current_app.extensions["panel"]


def current_user() -> dict | None:
    """{'username', 'role'} de la sesión actual, o None."""
    return services().auth.session_user(session.get("user"), session.get("epoch"))


def _guard(view, admin_only: bool):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        is_api = request.path.startswith("/api/")
        if not user:
            if is_api:
                return jsonify(error="Sesión caducada. Vuelve a iniciar sesión."), 401
            return redirect(url_for("login"))
        if admin_only and user["role"] != "admin":
            return jsonify(error="Tu usuario no tiene permiso para esto."), 403
        return view(*args, **kwargs)

    return wrapper


def login_required(view):
    """Cualquier usuario con sesión (administrador o moderador)."""
    return _guard(view, admin_only=False)


def admin_required(view):
    return _guard(view, admin_only=True)


def json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("Petición no válida.")
    return data


def versioned(response: Response) -> Response:
    """Las URL con ?v= cambian cuando cambia el contenido: caché larga en el navegador."""
    if request.args.get("v"):
        response.headers["Cache-Control"] = f"private, max-age={STATIC_MAX_AGE}, immutable"
    else:
        response.headers["Cache-Control"] = "private, no-cache"
    return response
