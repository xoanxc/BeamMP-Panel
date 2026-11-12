"""Panel web de BeamMP: aplicación Flask, sesión y páginas.

Producción:  gunicorn wsgi:app
Desarrollo:  python app.py
Recuperar acceso (admin/admin):  python app.py --reset-credentials

Las rutas de la API están en panel/api_server.py y panel/api_content.py.
"""

from __future__ import annotations

import hmac
import secrets
import sys
from datetime import timedelta

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.exceptions import HTTPException

from panel import __version__, api_content, api_server
from panel.access import AccessError, AccessList
from panel.auth import ROLES, AuthError, AuthStore, LoginThrottle
from panel.backups import BackupError, BackupManager
from panel.bridge import BridgeError, PlayerBridge
from panel.metrics import Metrics
from panel.mods import MapCatalog, ModError, ModLibrary
from panel.previews import MAX_IMAGE_BYTES, PreviewStore
from panel.schedule import ScheduleError, Scheduler, ScheduleStore
from panel.server import LogTracker, ServerConfig, Service, ServiceError
from panel.settings import Settings, load_settings
from panel.updater import ServerUpdater, UpdateError
from panel.web import STATIC_MAX_AGE, ApiError, Services, current_user, login_required


def _asset_version(static_dir) -> str:
    """Cambia cuando cambia cualquier archivo de static/: sirve para invalidar la caché del navegador."""
    try:
        newest = max(p.stat().st_mtime_ns for p in static_dir.rglob("*") if p.is_file())
    except (OSError, ValueError):
        newest = 0
    return f"{__version__}-{newest // 1_000_000_000:x}"


def build_services(settings: Settings) -> Services:
    auth = AuthStore(settings.data_dir)
    config = ServerConfig(settings.config_path)
    service = Service(settings.service_name)
    log = LogTracker(settings.log_path)
    mods = ModLibrary(settings.beammp_dir, config, settings.max_upload_bytes)
    previews = PreviewStore(settings.previews_dir, settings.static_dir / "maps")
    catalog = MapCatalog(
        settings.official_maps_file, settings.custom_maps_file, mods, previews, _asset_version(settings.static_dir)
    )
    bridge = PlayerBridge(settings.beammp_dir, config)
    access = AccessList(settings.data_dir / "access.json", bridge)
    schedule = ScheduleStore(settings.data_dir / "schedule.json")
    panel = Services(
        settings=settings,
        auth=auth,
        throttle=LoginThrottle(),
        config=config,
        service=service,
        log=log,
        mods=mods,
        previews=previews,
        catalog=catalog,
        bridge=bridge,
        access=access,
        metrics=Metrics(settings.beammp_dir),
        schedule=schedule,
        scheduler=None,  # se asigna abajo: necesita panel.players
        updater=ServerUpdater(settings.beammp_dir, service, log),
        backups=BackupManager(settings.backups_dir, settings.data_dir, settings.config_path, mods, settings.max_upload_bytes),
    )
    panel.scheduler = Scheduler(schedule, config, service, catalog, bridge, lambda: len(panel.players()))
    bridge.install()
    access.sync()
    return panel


def create_app(settings: Settings | None = None, background: bool = False) -> Flask:
    """`background=True` arranca el hilo de reinicios programados (un solo proceso)."""
    settings = settings or load_settings()
    panel = build_services(settings)
    auth = panel.auth

    app = Flask(__name__)
    app.secret_key = auth.secret_key
    app.config.update(
        SESSION_COOKIE_NAME="beammp_panel",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        MAX_CONTENT_LENGTH=settings.max_upload_bytes,
        SEND_FILE_MAX_AGE_DEFAULT=STATIC_MAX_AGE,
    )
    app.extensions["panel"] = panel
    app.register_blueprint(api_server.bp)
    app.register_blueprint(api_content.bp)
    asset_version = panel.catalog.asset_version

    # -----------------------------------------------------------------
    # CSRF y cabeceras de seguridad
    # -----------------------------------------------------------------
    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def asset(filename: str) -> str:
        return url_for("static", filename=filename, v=asset_version)

    app.jinja_env.globals.update(csrf_token=csrf_token, asset=asset, version=__version__)

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
    @app.errorhandler(BridgeError)
    @app.errorhandler(AuthError)
    @app.errorhandler(AccessError)
    @app.errorhandler(BackupError)
    @app.errorhandler(UpdateError)
    def handle_api_error(exc):
        return jsonify(error=str(exc)), exc.status

    @app.errorhandler(ScheduleError)
    def handle_schedule_error(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(ServiceError)
    def handle_service_error(exc):
        return jsonify(error=str(exc)), 502

    @app.errorhandler(HTTPException)
    def handle_http_error(exc):
        if request.path.startswith("/api/"):
            return jsonify(error=exc.description or exc.name), exc.code
        return exc

    # -----------------------------------------------------------------
    # Páginas
    # -----------------------------------------------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user():
            return redirect(url_for("index"))
        error = None
        status = 200
        if request.method == "POST":
            ip = request.remote_addr or "?"
            wait = panel.throttle.seconds_blocked(ip)
            identity = None if wait else auth.verify(request.form.get("username", ""), request.form.get("password", ""))
            if wait:
                error, status = f"Demasiados intentos. Espera {wait} s y vuelve a probar.", 429
            elif identity:
                panel.throttle.succeeded(ip)
                session.clear()  # sesión nueva al autenticarse
                session.permanent = True
                session.update(identity)
                return redirect(url_for("index"))
            else:
                panel.throttle.failed(ip)
                error, status = "Usuario o contraseña incorrectos.", 401
        return render_template("login.html", error=error), status

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/favicon.ico")
    def favicon():
        return send_file(settings.static_dir / "favicon.ico", mimetype="image/x-icon", max_age=86400)

    @app.get("/")
    @login_required
    def index():
        user = current_user()
        return render_template(
            "index.html",
            username=user["username"],
            role=user["role"],
            roles=ROLES,
            max_upload=settings.max_upload_bytes,
            max_image=MAX_IMAGE_BYTES,
        )

    if background:
        panel.scheduler.start()
    return app


def main(argv: list[str]) -> int:
    if "--reset-credentials" in argv:
        AuthStore(load_settings().data_dir).reset()
        print("Acceso restablecido: admin / admin. Reinicia el panel para aplicarlo.")
        return 0
    create_app(background=True).run(host="0.0.0.0", port=5000, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
