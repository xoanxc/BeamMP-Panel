"""API del servidor en marcha: estado, jugadores, chat, accesos, registro,
programación, actualización y copias de seguridad."""

from __future__ import annotations

import time
from datetime import datetime

from flask import Blueprint, jsonify, request, send_file

from .server import Service
from .web import ApiError, admin_required, current_user, json_body, login_required, services

bp = Blueprint("api_server", __name__, url_prefix="/api")


# ---------------------------------------------------------------------
# Estado y control
# ---------------------------------------------------------------------
@bp.get("/status")
@login_required
def status():
    s = services()
    general = s.config.read().get("General", {})
    state = s.service.status()
    current = general.get("Map", "")
    entry = s.catalog.find(current)
    players = s.players()
    return jsonify(
        service={"state": state["state"], "uptime": state["uptime"]},
        server_name=general.get("Name", ""),
        map={
            "path": current,
            "name": entry["name"] if entry else (current or "Desconocido"),
            "preview": entry["preview"] if entry else None,
        },
        players={"count": len(players), "max": general.get("MaxPlayers", 0), "list": players},
        bridge=s.bridge.alive() if state["state"] == "online" else None,
        metrics=s.metrics.sample(state["pid"]),
        config_found=s.settings.config_path.is_file(),
        default_credentials=s.auth.uses_default_credentials,
    )


@bp.post("/server/<action>")
@admin_required
def server_action(action):
    if action not in Service.ACTIONS:
        raise ApiError("Acción desconocida.", 404)
    services().service.run(action)
    return jsonify(ok=True)


# ---------------------------------------------------------------------
# Jugadores y chat
# ---------------------------------------------------------------------
def _connected(name) -> bool:
    return isinstance(name, str) and any(p["name"] == name for p in services().players())


@bp.post("/players/kick")
@login_required
def kick_player():
    s = services()
    body = json_body()
    name = body.get("name")
    if not _connected(name):
        raise ApiError("Ese jugador no está conectado.", 404)
    s.bridge.kick(name, str(body.get("reason") or ""))
    s.log.forget(name)
    return jsonify(ok=True)


@bp.post("/chat")
@login_required
def chat():
    s = services()
    if s.service.status()["state"] != "online":
        raise ApiError("El servidor no está en marcha.", 409)
    s.bridge.say(str(json_body().get("message") or ""))
    return jsonify(ok=True)


# ---------------------------------------------------------------------
# Bloqueos y lista blanca
# ---------------------------------------------------------------------
def _access_payload():
    s = services()
    return jsonify(**s.access.read(), bridge=s.bridge.alive(), online=s.service.status()["state"] == "online")


@bp.get("/access")
@admin_required
def access():
    return _access_payload()


@bp.post("/access/ban")
@admin_required
def access_ban():
    s = services()
    body = json_body()
    name, reason = body.get("name"), str(body.get("reason") or "")
    s.access.ban(name, reason)
    kicked = False
    if _connected(name):  # si está dentro, además se le expulsa
        try:
            s.bridge.kick(name, "Bloqueado: " + (reason.strip() or "por un administrador"))
            s.log.forget(name)
            kicked = True
        except Exception:  # noqa: BLE001 - el bloqueo ya está guardado
            pass
    return jsonify(ok=True, kicked=kicked)


@bp.post("/access/unban")
@admin_required
def access_unban():
    services().access.unban(json_body().get("name"))
    return jsonify(ok=True)


@bp.post("/access/whitelist")
@admin_required
def access_whitelist():
    body = json_body()
    services().access.set_whitelist(body.get("enabled"), body.get("names"))
    return _access_payload()


# ---------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------
@bp.get("/logs")
@admin_required
def logs():
    # Con ?cursor= solo se envían las líneas nuevas desde la consulta anterior.
    lines = min(max(request.args.get("lines", 400, type=int), 10), 2000)
    return jsonify(services().log.follow(request.args.get("cursor"), lines))


# ---------------------------------------------------------------------
# Reinicios programados y rotación de mapas
# ---------------------------------------------------------------------
def _schedule_payload():
    s = services()
    now = datetime.now()
    return jsonify(
        schedule=s.schedule.read(),
        upcoming=s.scheduler.upcoming(now),
        server_time=now.strftime("%H:%M"),
        bridge=s.bridge.alive(),
    )


@bp.get("/schedule")
@admin_required
def schedule():
    return _schedule_payload()


@bp.post("/schedule")
@admin_required
def schedule_save():
    s = services()
    catalog = s.catalog.build()
    valid = {entry["path"] for entry in catalog["official"] + catalog["mods"]}
    s.schedule.save(json_body(), valid, time.time())
    return _schedule_payload()


# ---------------------------------------------------------------------
# Actualización de BeamMP Server
# ---------------------------------------------------------------------
@bp.get("/update")
@admin_required
def update_check():
    return jsonify(services().updater.check(force=request.args.get("force") == "1"))


@bp.post("/update")
@admin_required
def update_apply():
    return jsonify(ok=True, version=services().updater.apply())


# ---------------------------------------------------------------------
# Copias de seguridad
# ---------------------------------------------------------------------
@bp.get("/backups")
@admin_required
def backups():
    return jsonify(backups=services().backups.list())


@bp.post("/backups")
@admin_required
def backup_create():
    name = services().backups.create(include_mods=json_body().get("include_mods") is True)
    return jsonify(ok=True, name=name), 201


@bp.get("/backups/<name>")
@admin_required
def backup_download(name):
    response = send_file(services().backups.path(name), as_attachment=True, download_name=name, max_age=0)
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.put("/backups/<name>")
@admin_required
def backup_upload(name):
    saved = services().backups.save_upload(name, request.stream, request.content_length)
    return jsonify(ok=True, name=saved), 201


@bp.delete("/backups/<name>")
@admin_required
def backup_delete(name):
    services().backups.delete(name)
    return jsonify(ok=True)


@bp.post("/backups/<name>/restore")
@admin_required
def backup_restore(name):
    s = services()
    summary = s.backups.restore(name)
    s.access.sync()  # los bloqueos restaurados se publican al servidor
    restarted = False
    if json_body().get("restart") and s.service.status()["state"] in ("online", "starting"):
        s.service.run("restart")
        restarted = True
    return jsonify(ok=True, restored=summary, restarted=restarted)


# ---------------------------------------------------------------------
# Cuenta propia y usuarios
# ---------------------------------------------------------------------
@bp.post("/account")
@login_required
def account():
    from flask import session

    s = services()
    body = json_body()
    me = current_user()["username"]
    if not s.auth.verify(me, str(body.get("current_password", ""))):
        raise ApiError("La contraseña actual no es correcta.", 403)
    # La sesión actual sigue siendo válida; las demás de este usuario quedan cerradas.
    session.update(s.auth.update_self(me, str(body.get("username", "")), str(body.get("new_password", ""))))
    return jsonify(ok=True, username=session["user"])


@bp.get("/users")
@admin_required
def users():
    return jsonify(users=services().auth.users(), me=current_user()["username"])


@bp.post("/users")
@admin_required
def user_create():
    body = json_body()
    services().auth.create_user(str(body.get("username", "")), str(body.get("password", "")), body.get("role"))
    return jsonify(ok=True), 201


@bp.patch("/users/<username>")
@admin_required
def user_update(username):
    s = services()
    body = json_body()
    if username == current_user()["username"]:
        raise ApiError("Tu propia cuenta se cambia en «Mi cuenta».", 409)
    if "role" in body:
        s.auth.set_role(username, body["role"])
    if "password" in body:
        s.auth.set_password(username, str(body["password"]))
    return jsonify(ok=True)


@bp.delete("/users/<username>")
@admin_required
def user_delete(username):
    if username == current_user()["username"]:
        raise ApiError("No puedes borrar tu propia cuenta.", 409)
    services().auth.delete_user(username)
    return jsonify(ok=True)
