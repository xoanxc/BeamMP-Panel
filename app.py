"""Panel web de BeamMP: aplicación Flask y rutas.

Producción:  gunicorn wsgi:app
Desarrollo:  python app.py
Recuperar acceso (vuelve a admin/admin):  python app.py --reset-credentials
"""

from __future__ import annotations

import hmac
import secrets
import sys
from datetime import timedelta
from functools import wraps

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.exceptions import HTTPException

from panel import __version__
from panel.auth import AuthStore, LoginThrottle
from panel.mods import MapCatalog, ModError, ModLibrary
from panel.server import LogTracker, ServerConfig, Service, ServiceError
from panel.settings import Settings, load_settings

# Ajustes de ServerConfig.toml editables desde el panel: clave -> (tipo, mínimo, máximo).
# Para cadenas, el máximo es la longitud. Las claves opcionales solo se muestran
# si ya existen en el archivo (dependen de la versión de BeamMP).
CONFIG_FIELDS = {
    "Name": (str, 1, 250),
    "Description": (str, 0, 1000),
    "Tags": (str, 0, 200),
    "MaxPlayers": (int, 1, 200),
    "MaxCars": (int, 1, 50),
    "Private": (bool, None, None),
    "AllowGuests": (bool, None, None),
    "LogChat": (bool, None, None),
    "Debug": (bool, None, None),
}
OPTIONAL_CONFIG_FIELDS = {"Tags", "AllowGuests", "LogChat", "Debug"}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or load_settings()
    auth = AuthStore(settings.data_dir)
    throttle = LoginThrottle()
    config = ServerConfig(settings.config_path)
    service = Service(settings.service_name)
    log = LogTracker(settings.log_path)
    mods = ModLibrary(settings.beammp_dir, config, settings.max_upload_bytes)
    catalog = MapCatalog(settings.official_maps_file, settings.custom_maps_file, mods)

    app = Flask(__name__)
    app.secret_key = auth.secret_key
    app.config.update(
        SESSION_COOKIE_NAME="beammp_panel",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        MAX_CONTENT_LENGTH=settings.max_upload_bytes,
    )
    app.extensions["panel_auth"] = auth

    # -----------------------------------------------------------------
    # Sesión, CSRF y cabeceras de seguridad
    # -----------------------------------------------------------------
    def logged_in() -> bool:
        return session.get("user") == auth.username and session.get("epoch") == auth.epoch

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    def login_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if logged_in():
                return view(*args, **kwargs)
            if request.path.startswith("/api/"):
                return jsonify(error="Sesión caducada. Vuelve a iniciar sesión."), 401
            return redirect(url_for("login"))

        return wrapper

    @app.before_request
    def check_csrf():
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        if not hmac.compare_digest(sent, session.get("csrf", "")) or not sent:
            if request.path.startswith("/api/"):
                return jsonify(error="Token de seguridad no válido. Recarga la página."), 400
            return redirect(url_for("login"))
        return None

    @app.after_request
    def security_headers(response):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.path.startswith("/api/") and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    # -----------------------------------------------------------------
    # Errores de la API siempre en JSON
    # -----------------------------------------------------------------
    @app.errorhandler(ApiError)
    @app.errorhandler(ModError)
    def handle_api_error(exc):
        return jsonify(error=str(exc)), exc.status

    @app.errorhandler(ServiceError)
    def handle_service_error(exc):
        return jsonify(error=str(exc)), 502

    @app.errorhandler(HTTPException)
    def handle_http_error(exc):
        if request.path.startswith("/api/"):
            return jsonify(error=exc.description or exc.name), exc.code
        return exc

    def json_body() -> dict:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ApiError("Petición no válida.")
        return data

    # -----------------------------------------------------------------
    # Páginas
    # -----------------------------------------------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if logged_in():
            return redirect(url_for("index"))
        error = None
        status = 200
        if request.method == "POST":
            ip = request.remote_addr or "?"
            wait = throttle.seconds_blocked(ip)
            if wait:
                error, status = f"Demasiados intentos. Espera {wait} s y vuelve a probar.", 429
            elif auth.verify(request.form.get("username", ""), request.form.get("password", "")):
                throttle.succeeded(ip)
                session.clear()  # sesión nueva al autenticarse
                session.permanent = True
                session["user"] = auth.username
                session["epoch"] = auth.epoch
                return redirect(url_for("index"))
            else:
                throttle.failed(ip)
                error, status = "Usuario o contraseña incorrectos.", 401
        return render_template("login.html", error=error), status

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/favicon.ico")
    def favicon():
        return "", 204

    @app.get("/")
    @login_required
    def index():
        return render_template(
            "index.html",
            username=auth.username,
            version=__version__,
            max_upload=settings.max_upload_bytes,
        )

    # -----------------------------------------------------------------
    # API: estado y control del servidor
    # -----------------------------------------------------------------
    @app.get("/api/status")
    @login_required
    def api_status():
        general = config.read().get("General", {})
        current = general.get("Map", "")
        players = log.players() if service.status()["state"] == "online" else []
        return jsonify(
            service=service.status(),
            server_name=general.get("Name", ""),
            map={"path": current, "name": catalog.name_for(current)},
            players={"count": len(players), "max": general.get("MaxPlayers", 0), "names": players},
            config_found=settings.config_path.is_file(),
            default_credentials=auth.uses_default_credentials,
        )

    @app.post("/api/server/<action>")
    @login_required
    def api_server_action(action):
        if action not in Service.ACTIONS:
            raise ApiError("Acción desconocida.", 404)
        service.run(action)
        return jsonify(ok=True)

    # -----------------------------------------------------------------
    # API: mapas
    # -----------------------------------------------------------------
    @app.get("/api/maps")
    @login_required
    def api_maps():
        data = catalog.build()
        data["current"] = config.get("Map", "")
        return jsonify(data)

    @app.post("/api/maps/select")
    @login_required
    def api_select_map():
        path = json_body().get("path")
        entry = catalog.find(path) if isinstance(path, str) else None
        if not entry:
            raise ApiError("Ese mapa no está en el catálogo.", 404)
        try:
            config.update({"Map": entry["path"]})
        except OSError as exc:
            raise ApiError(f"No se ha podido escribir ServerConfig.toml: {exc}", 500) from exc
        try:
            service.run("restart")
        except ServiceError as exc:
            raise ApiError(
                f"Mapa guardado, pero el servidor no se ha reiniciado: {exc}", 502
            ) from exc
        return jsonify(ok=True, name=entry["name"])

    @app.get("/api/maps/preview")
    @login_required
    def api_map_preview():
        image, mimetype = mods.preview(request.args.get("mod", ""), request.args.get("folder", ""))
        response = Response(image, mimetype=mimetype)
        response.headers["Cache-Control"] = "private, max-age=3600"
        return response

    # -----------------------------------------------------------------
    # API: mods
    # -----------------------------------------------------------------
    def public_mod(mod: dict) -> dict:
        return {
            "name": mod["name"],
            "size": mod["size"],
            "modified": mod["modified"],
            "kind": mod["kind"],
            "levels": [level["title"] for level in mod["levels"]],
        }

    def mods_payload(items: list[dict]) -> dict:
        return {
            "mods": [public_mod(mod) for mod in items],
            "disk": mods.disk(),
            "folder": str(mods.client_dir),
        }

    @app.get("/api/mods")
    @login_required
    def api_mods():
        return jsonify(mods_payload(mods.list()))

    @app.post("/api/mods/rescan")
    @login_required
    def api_mods_rescan():
        return jsonify(mods_payload(mods.rescan()))

    @app.put("/api/mods/<filename>")
    @login_required
    def api_mod_upload(filename):
        name = mods.save_upload(
            filename,
            request.stream,
            request.content_length,
            overwrite=request.args.get("overwrite") == "1",
        )
        return jsonify(ok=True, name=name), 201

    @app.delete("/api/mods/<filename>")
    @login_required
    def api_mod_delete(filename):
        current = config.get("Map", "")
        in_use = any(
            m["name"] == filename and any(lv["path"] == current for lv in m["levels"])
            for m in mods.list()
        )
        if in_use:
            raise ApiError("Ese mod contiene el mapa en uso. Cambia de mapa antes de borrarlo.", 409)
        mods.delete(filename)
        return jsonify(ok=True)

    # -----------------------------------------------------------------
    # API: ajustes del servidor
    # -----------------------------------------------------------------
    @app.get("/api/config")
    @login_required
    def api_config():
        general = config.read().get("General", {})
        values = {
            key: general.get(key, kind())
            for key, (kind, _, _) in CONFIG_FIELDS.items()
            if key in general or key not in OPTIONAL_CONFIG_FIELDS
        }
        return jsonify(values=values)

    @app.post("/api/config")
    @login_required
    def api_config_save():
        body = json_body()
        submitted = body.get("values")
        if not isinstance(submitted, dict) or not submitted:
            raise ApiError("No hay cambios que guardar.")
        changes = {}
        for key, value in submitted.items():
            if key not in CONFIG_FIELDS:
                raise ApiError(f"Ajuste no permitido: {key}")
            kind, low, high = CONFIG_FIELDS[key]
            if kind is bool:
                if not isinstance(value, bool):
                    raise ApiError(f"{key} debe ser verdadero o falso.")
            elif kind is int:
                if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                    raise ApiError(f"{key} debe ser un número entre {low} y {high}.")
            else:
                if not isinstance(value, str):
                    raise ApiError(f"{key} debe ser un texto.")
                value = "".join(ch for ch in value if ch.isprintable()).strip()
                if not low <= len(value) <= high:
                    raise ApiError(f"{key} debe tener entre {low} y {high} caracteres.")
            changes[key] = value
        try:
            config.update(changes)
        except OSError as exc:
            raise ApiError(f"No se ha podido escribir ServerConfig.toml: {exc}", 500) from exc
        if body.get("restart"):
            service.run("restart")
        return jsonify(ok=True)

    # -----------------------------------------------------------------
    # API: registro y cuenta
    # -----------------------------------------------------------------
    @app.get("/api/logs")
    @login_required
    def api_logs():
        lines = min(max(request.args.get("lines", 300, type=int), 10), 2000)
        return jsonify(lines=log.tail(lines), found=settings.log_path.is_file())

    @app.post("/api/account")
    @login_required
    def api_account():
        body = json_body()
        if not auth.verify(auth.username, str(body.get("current_password", ""))):
            raise ApiError("La contraseña actual no es correcta.", 403)
        try:
            auth.set_credentials(str(body.get("username", "")), str(body.get("new_password", "")))
        except ValueError as exc:
            raise ApiError(str(exc)) from exc
        # La sesión actual sigue siendo válida; las demás quedan cerradas.
        session["user"] = auth.username
        session["epoch"] = auth.epoch
        return jsonify(ok=True, username=auth.username)

    return app


def main(argv: list[str]) -> int:
    if "--reset-credentials" in argv:
        AuthStore(load_settings().data_dir).reset()
        print("Credenciales restablecidas: admin / admin. Reinicia el panel para aplicarlas.")
        return 0
    create_app().run(host="0.0.0.0", port=5000, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
