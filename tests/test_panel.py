"""Pruebas del panel. Ejecutar con:  python -m pytest tests"""

import io
import json
import os
import re
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from panel.server import LogTracker, ServerConfig  # noqa: E402
from panel.settings import Settings  # noqa: E402

CONFIG = """# This is the BeamMP-Server config file.
[General]
Name = "Servidor de prueba"
Port = 30814
AuthKey = "secreto"
Private = true
MaxCars = 2
MaxPlayers = 8 # jugadores
Map="/levels/gridmap_v2/info.json"
Description = "Hola \\"mundo\\""
ResourceFolder = "Resources"

[Misc]
ImScaredOfUpdates = false
"""


def make_zip(files: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


MAP_ZIP = make_zip(
    {
        "levels/ks_nord/info.json": '{\n  "title": "Nordschleife", // comentario\n  "previews": ["ks_preview.jpg"],\n}',
        "levels/ks_nord/ks_preview.jpg": b"\xff\xd8fake",
        "levels/ks_nord/main/items.level.json": "{}",
    }
)
CAR_ZIP = make_zip({"vehicles/coche/coche.jbeam": "{}"})


@pytest.fixture
def env(tmp_path):
    beammp = tmp_path / "beammp"
    (beammp / "Resources" / "Client").mkdir(parents=True)
    (beammp / "ServerConfig.toml").write_text(CONFIG, encoding="utf-8")
    official = tmp_path / "maps.json"
    official.write_text(
        json.dumps({"normal": {"Gridmap V2": "/levels/gridmap_v2/info.json"}, "mods": {}})
    )
    settings = Settings(
        beammp_dir=beammp,
        data_dir=tmp_path / "data",
        official_maps_file=official,
        service_name="beammp-test-inexistente",
        max_upload_bytes=1024 * 1024,
        cookie_secure=False,
    )
    app = create_app(settings)
    app.config["TESTING"] = True
    return app, settings


def set_online(app, state="online"):
    """En las pruebas no hay systemd: se simula el estado del servicio."""
    app.extensions["panel"].service.status = lambda: {"state": state, "uptime": 60, "pid": None}


def csrf(client) -> str:
    html = client.get("/login").get_data(as_text=True)
    found = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if found:
        return found.group(1)
    html = client.get("/").get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


@pytest.fixture
def client(env):
    """Cliente con sesión iniciada como admin/admin."""
    app, _ = env
    client = app.test_client()
    response = client.post(
        "/login", data={"username": "admin", "password": "admin", "csrf_token": csrf(client)}
    )
    assert response.status_code == 302
    client.headers = {"X-CSRF-Token": csrf(client)}
    return client


# -- acceso -----------------------------------------------------------
def test_requires_login(env):
    client = env[0].test_client()
    assert client.get("/").status_code == 302
    assert client.get("/api/status").status_code == 401


def test_wrong_password_and_lockout(env):
    client = env[0].test_client()
    token = csrf(client)
    for _ in range(5):
        r = client.post("/login", data={"username": "admin", "password": "x", "csrf_token": token})
        assert r.status_code == 401
    r = client.post("/login", data={"username": "admin", "password": "admin", "csrf_token": token})
    assert r.status_code == 429


def test_csrf_is_enforced(client):
    assert client.post("/api/server/restart").status_code == 400


def test_change_password(env, client):
    app, _ = env
    bad = client.post(
        "/api/account",
        json={"current_password": "mal", "username": "xoan", "new_password": "contraseña-larga"},
        headers=client.headers,
    )
    assert bad.status_code == 403
    assert client.get("/api/status").get_json()["default_credentials"] is True
    ok = client.post(
        "/api/account",
        json={"current_password": "admin", "username": "xoan", "new_password": "contraseña-larga"},
        headers=client.headers,
    )
    assert ok.status_code == 200
    assert client.get("/api/status").get_json()["default_credentials"] is False

    other = app.test_client()
    token = csrf(other)
    old = other.post("/login", data={"username": "admin", "password": "admin", "csrf_token": token})
    assert old.status_code == 401
    new = other.post(
        "/login", data={"username": "xoan", "password": "contraseña-larga", "csrf_token": token}
    )
    assert new.status_code == 302


# -- configuración ----------------------------------------------------
def test_config_roundtrip(env, client):
    _, settings = env
    values = client.get("/api/config").get_json()["values"]
    assert values["Name"] == "Servidor de prueba"
    assert values["MaxPlayers"] == 8
    assert values["Description"] == 'Hola "mundo"'
    assert "AuthKey" not in values and "Debug" not in values

    r = client.post(
        "/api/config",
        json={"values": {"Name": 'Mi "server" # 1', "MaxPlayers": 12, "Private": False}},
        headers=client.headers,
    )
    assert r.status_code == 200
    text = settings.config_path.read_text(encoding="utf-8")
    assert 'AuthKey = "secreto"' in text and "[Misc]" in text and text.startswith("# This is")
    general = ServerConfig(settings.config_path).read()["General"]
    assert general["Name"] == 'Mi "server" # 1'
    assert general["MaxPlayers"] == 12 and general["Private"] is False

    for bad in ({"AuthKey": "x"}, {"MaxPlayers": 0}, {"MaxPlayers": "8"}, {"Private": 1}):
        assert client.post("/api/config", json={"values": bad}, headers=client.headers).status_code == 400


def test_config_adds_missing_key_inside_section(tmp_path):
    path = tmp_path / "ServerConfig.toml"
    path.write_text('[General]\nName = "a"\n\n[Misc]\nX = 1\n')
    ServerConfig(path).update({"Tags": "Freeroam"})
    sections = ServerConfig(path).read()
    assert sections["General"]["Tags"] == "Freeroam" and "Tags" not in sections["Misc"]


# -- mods y mapas -----------------------------------------------------
def test_upload_detects_map(env, client):
    _, settings = env
    r = client.put("/api/mods/Nürburgring mod.zip", data=MAP_ZIP, headers=client.headers)
    assert r.status_code == 201
    name = r.get_json()["name"]
    assert (settings.beammp_dir / "Resources" / "Client" / name).is_file()
    client.put("/api/mods/coche.zip", data=CAR_ZIP, headers=client.headers)

    kinds = {m["name"]: m["kind"] for m in client.get("/api/mods").get_json()["mods"]}
    assert all(m["enabled"] for m in client.get("/api/mods").get_json()["mods"])
    assert kinds == {name: "map", "coche.zip": "vehicle"}

    maps = client.get("/api/maps").get_json()
    assert maps["current"] == "/levels/gridmap_v2/info.json"
    assert [m["name"] for m in maps["mods"]] == ["Nordschleife"]
    assert maps["mods"][0]["path"] == "/levels/ks_nord/info.json"

    preview = client.get(f"/api/maps/preview?mod={name}&folder=ks_nord")
    assert preview.status_code == 200 and preview.mimetype == "image/jpeg"


def test_upload_rejections(env, client):
    _, settings = env
    folder = settings.beammp_dir / "Resources" / "Client"
    assert client.put("/api/mods/malo.zip", data=b"no soy un zip", headers=client.headers).status_code == 400
    assert client.put("/api/mods/script.lua", data=MAP_ZIP, headers=client.headers).status_code == 400
    assert client.put("/api/mods/grande.zip", data=b"0" * (1024 * 1024 + 1), headers=client.headers).status_code == 413
    assert list(folder.iterdir()) == []  # no quedan restos de subidas fallidas

    assert client.put("/api/mods/a.zip", data=MAP_ZIP, headers=client.headers).status_code == 201
    assert client.put("/api/mods/a.zip", data=MAP_ZIP, headers=client.headers).status_code == 409
    assert client.put("/api/mods/a.zip?overwrite=1", data=MAP_ZIP, headers=client.headers).status_code == 201


def test_custom_name_overrides_detected(env, client):
    _, settings = env
    client.put("/api/mods/nord.zip", data=MAP_ZIP, headers=client.headers)
    settings.custom_maps_file.write_text(
        json.dumps({"mods": {"Nürburgring": "/levels/ks_nord/info.json", "Sin zip": "/levels/x/info.json"}}),
        encoding="utf-8",
    )
    mods = {m["name"]: m for m in client.get("/api/maps").get_json()["mods"]}
    assert mods["Nürburgring"]["detected"] is True
    assert mods["Sin zip"]["detected"] is False


def test_select_map_and_delete_guard(env, client):
    _, settings = env
    client.put("/api/mods/nord.zip", data=MAP_ZIP, headers=client.headers)
    r = client.post("/api/maps/select", json={"path": "/levels/inventado/info.json"}, headers=client.headers)
    assert r.status_code == 404

    # En el entorno de pruebas no existe el servicio: el mapa se guarda y el reinicio falla (502).
    r = client.post("/api/maps/select", json={"path": "/levels/ks_nord/info.json"}, headers=client.headers)
    assert r.status_code in (200, 502)
    assert ServerConfig(settings.config_path).get("Map") == "/levels/ks_nord/info.json"

    assert client.delete("/api/mods/nord.zip", headers=client.headers).status_code == 409
    ServerConfig(settings.config_path).update({"Map": "/levels/gridmap_v2/info.json"})
    assert client.delete("/api/mods/nord.zip", headers=client.headers).status_code == 200
    assert client.delete("/api/mods/nord.zip", headers=client.headers).status_code == 404
    assert client.delete("/api/mods/..%2F..%2FServerConfig.toml", headers=client.headers).status_code == 404


# -- registro ---------------------------------------------------------
def test_log_tracker_follows_players(tmp_path):
    path = tmp_path / "Server.log"
    path.write_text(
        "[30/09/22 18:06:25] [INFO] BeamMP Server v3.0.2\n"
        "[30/09/22 18:06:37] [INFO] Assigned ID 0 to Fantasma\n"
        "[30/09/22 18:06:38] [INFO] Fantasma : Connected\n"
        "[30/09/22 18:06:40] [INFO] BeamMP Server v3.0.2\n"
        "[30/09/22 18:06:37] [INFO] Assigned ID 0 to Brolafsky\n"
        "[30/09/22 18:07:13] [INFO] Assigned ID 1 to Wrendre\n"
        "[30/09/22 18:07:14] [INFO] Wrendre : Connected\n"
        "[30/09/22 18:07:17] [INFO] Brolafsky is now synced!\n"
    )
    tracker = LogTracker(path)
    assert tracker.players() == ["Brolafsky", "Wrendre"]
    with open(path, "a") as fh:
        fh.write('[01/10/22 05:00:58.135] (1) "Wrendre" [INFO] Wrendre Connection Terminated\n')
        fh.write("[01/10/22 05:01:00] [INFO] Assigned ID 1 to A medi")  # línea incompleta
    assert tracker.players() == ["Brolafsky"]
    with open(path, "a") as fh:
        fh.write("as\n")
    assert tracker.players() == ["Brolafsky", "A medias"]
    path.write_text("[01/10/22 06:00:00] [INFO] BeamMP Server v3.0.2\n")  # reinicio: log recortado
    assert tracker.players() == []
    assert tracker.tail(5) == ["[01/10/22 06:00:00] [INFO] BeamMP Server v3.0.2"]


def test_log_follow_returns_only_new_lines(tmp_path):
    path = tmp_path / "Server.log"
    tracker = LogTracker(path)
    assert tracker.follow()["found"] is False
    path.write_text("uno\ndos\n")
    first = tracker.follow()
    assert first["lines"] == ["uno", "dos"] and first["reset"] is True
    assert tracker.follow(first["cursor"])["lines"] == []
    with open(path, "a") as fh:
        fh.write("tres\ncuatro a med")
    second = tracker.follow(first["cursor"])
    assert second["lines"] == ["tres"] and second["reset"] is False
    with open(path, "a") as fh:
        fh.write("ias\n")
    assert tracker.follow(second["cursor"])["lines"] == ["cuatro a medias"]
    path.write_text("nuevo\n")  # log recortado: se empieza de cero
    again = tracker.follow(second["cursor"])
    assert again["lines"] == ["nuevo"] and again["reset"] is True
    assert tracker.follow("basura")["reset"] is True


def test_logs_api_uses_cursor(env, client):
    _, settings = env
    settings.log_path.write_text("[INFO] a\n[INFO] b\n")
    first = client.get("/api/logs").get_json()
    assert first["lines"] == ["[INFO] a", "[INFO] b"]
    with open(settings.log_path, "a") as fh:
        fh.write("[INFO] c\n")
    assert client.get(f"/api/logs?cursor={first['cursor']}").get_json()["lines"] == ["[INFO] c"]


# -- caché ------------------------------------------------------------
def test_config_cache_sees_external_edits(tmp_path):
    path = tmp_path / "ServerConfig.toml"
    path.write_text('[General]\nName = "a"\n')
    config = ServerConfig(path)
    assert config.get("Name") == "a"
    assert config.read() is config.read()  # sin cambios no se vuelve a leer
    path.write_text('[General]\nName = "editado a mano"\n')
    assert config.get("Name") == "editado a mano"


def test_catalog_is_reused_until_something_changes(env, client):
    first = client.get("/api/maps").get_json()
    assert client.get("/api/maps").get_json() == first
    client.put("/api/mods/nord.zip", data=MAP_ZIP, headers=client.headers)
    assert [m["name"] for m in client.get("/api/maps").get_json()["mods"]] == ["Nordschleife"]
    client.delete("/api/mods/nord.zip", headers=client.headers)
    assert client.get("/api/maps").get_json()["mods"] == []


# -- imágenes de mapas ------------------------------------------------
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_static_assets_and_favicon(env, client):
    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 200 and favicon.mimetype == "image/x-icon"
    html = client.get("/").get_data(as_text=True)
    for url in re.findall(r'(?:href|src)="(/static/[^"]+)"', html):
        response = client.get(url)
        assert response.status_code == 200, url
        assert "max-age=31536000" in response.headers["Cache-Control"]


def test_official_maps_have_builtin_preview(env, client):
    official = client.get("/api/maps").get_json()["official"]
    assert official[0]["folder"] == "gridmap_v2"
    assert official[0]["preview"].startswith("/static/maps/gridmap_v2.svg")
    assert client.get(official[0]["preview"]).mimetype == "image/svg+xml"
    assert client.get("/api/status").get_json()["map"]["preview"] == official[0]["preview"]


def test_every_bundled_official_map_has_an_illustration():
    root = Path(__file__).resolve().parent.parent
    paths = json.loads((root / "maps.json").read_text())["normal"].values()
    missing = [p for p in paths if not (root / "static" / "maps" / f"{p.split('/')[2]}.svg").is_file()]
    assert missing == []


def test_custom_map_image(env, client):
    url = "/api/maps/image/gridmap_v2"
    assert client.get(url).status_code == 404
    assert client.put(url, data=b"<svg onload=alert(1)>", headers=client.headers).status_code == 400
    assert client.put("/api/maps/image/inventado", data=PNG, headers=client.headers).status_code == 404
    assert client.put("/api/maps/image/..%2Fpanel", data=PNG, headers=client.headers).status_code == 404
    assert client.put(url, data=PNG, headers=client.headers).status_code == 201

    entry = client.get("/api/maps").get_json()["official"][0]
    assert entry["custom_preview"] is True and entry["preview"].startswith(url + "?v=")
    image = client.get(entry["preview"])
    assert image.mimetype == "image/png" and image.data == PNG
    assert "immutable" in image.headers["Cache-Control"]

    # Otra imagen sustituye a la anterior aunque cambie de formato.
    assert client.put(url, data=b"\xff\xd8\xff\xe0jpeg", headers=client.headers).status_code == 201
    assert client.get(url).mimetype == "image/jpeg"

    assert client.delete(url, headers=client.headers).status_code == 200
    assert client.delete(url, headers=client.headers).status_code == 404
    entry = client.get("/api/maps").get_json()["official"][0]
    assert entry["custom_preview"] is False and entry["preview"].startswith("/static/maps/")


def test_mod_map_preview_url(env, client):
    client.put("/api/mods/nord.zip", data=MAP_ZIP, headers=client.headers)
    entry = client.get("/api/maps").get_json()["mods"][0]
    preview = client.get(entry["preview"])
    assert preview.status_code == 200 and preview.mimetype == "image/jpeg"
    assert "immutable" in preview.headers["Cache-Control"]


# -- expulsar jugadores -----------------------------------------------
def fake_server(bridge, outcome):
    """Hace de plugin PanelBridge: contesta a la primera orden que llegue."""
    seen = []

    def run():
        command = bridge.exchange_dir / "command.txt"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if command.is_file():
                token, *fields = command.read_text(encoding="utf-8").rstrip("\n").split("\t")
                command.unlink()
                seen.append(fields)
                (bridge.exchange_dir / "result.txt").write_text(f"{token}\t{outcome}\n")
                return
            time.sleep(0.02)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, seen


def test_bridge_plugin_is_installed(env):
    app, settings = env
    plugin = settings.beammp_dir / "Resources" / "Server" / "PanelBridge" / "main.lua"
    source = plugin.read_text(encoding="utf-8")
    assert "MP.DropPlayer" in source and str(settings.beammp_dir / "panel-bridge") in source
    assert app.extensions["panel"].bridge.install() is False  # ya está al día


def test_kick_player(env, client):
    app, settings = env
    set_online(app)
    bridge = app.extensions["panel"].bridge
    settings.log_path.write_text(
        "[01/01/26 10:00:00] [INFO] BeamMP Server v3.9.3\n"
        "[01/01/26 10:00:05] [INFO] Assigned ID 0 to Xoan\n"
        "[01/01/26 10:00:06] [INFO] Assigned ID 1 to Molesto\n"
    )
    kick = lambda body: client.post("/api/players/kick", json=body, headers=client.headers)  # noqa: E731

    assert kick({"name": "Nadie"}).status_code == 404
    assert kick({"name": 7}).status_code == 404

    thread, seen = fake_server(bridge, "ok")
    assert kick({"name": "Molesto", "reason": "  Deja de\tchocar\n a todos "}).status_code == 200
    thread.join()
    assert seen == [["kick", "Molesto", "Deja de chocar a todos"]]
    assert kick({"name": "Molesto"}).status_code == 404  # ya no figura como conectado

    thread, seen = fake_server(bridge, "notfound")
    assert kick({"name": "Xoan"}).status_code == 404
    thread.join()
    assert seen == [["kick", "Xoan", "Expulsado por un administrador"]]


def test_kick_without_bridge_answer(env, client):
    app, settings = env
    bridge = app.extensions["panel"].bridge
    bridge.timeout = 0.3
    set_online(app)
    settings.log_path.write_text("[01/01/26 10:00:05] [INFO] Assigned ID 0 to Xoan\n")
    r = client.post("/api/players/kick", json={"name": "Xoan"}, headers=client.headers)
    assert r.status_code == 504 and "puente" in r.get_json()["error"]
    assert not (bridge.exchange_dir / "command.txt").exists()  # la orden no queda pendiente


# -- lista exacta de jugadores, chat, bloqueos y lista blanca ---------
def write_bridge_players(bridge, text):
    bridge.exchange_dir.mkdir(parents=True, exist_ok=True)
    (bridge.exchange_dir / "players.txt").write_text(text, encoding="utf-8")


def test_players_come_from_bridge_when_alive(env, client):
    app, settings = env
    set_online(app)
    bridge = app.extensions["panel"].bridge
    settings.log_path.write_text("[01/01/26 10:00:05] [INFO] Assigned ID 0 to DelRegistro\n")
    status = client.get("/api/status").get_json()
    assert status["bridge"] is False
    assert status["players"]["list"] == [{"id": None, "name": "DelRegistro", "guest": None}]

    write_bridge_players(bridge, "3\tguest123\t1\n0\tXoan\t0\n")
    status = client.get("/api/status").get_json()
    assert status["bridge"] is True and status["players"]["count"] == 2
    assert status["players"]["list"] == [
        {"id": 0, "name": "Xoan", "guest": False},
        {"id": 3, "name": "guest123", "guest": True},
    ]
    old = time.time() - 60  # si el plugin deja de informar, se vuelve al registro
    os.utime(bridge.exchange_dir / "players.txt", (old, old))
    assert client.get("/api/status").get_json()["bridge"] is False

    set_online(app, "offline")
    status = client.get("/api/status").get_json()
    assert status["players"]["list"] == [] and status["bridge"] is None
    assert set(status["metrics"]) >= {"cpu", "mem_used", "mem_total", "disk_used", "disk_total"}


def test_chat_message(env, client):
    app, _ = env
    bridge = app.extensions["panel"].bridge
    say = lambda text: client.post("/api/chat", json={"message": text}, headers=client.headers)  # noqa: E731
    assert say("hola").status_code == 409  # servidor parado
    set_online(app)
    assert say("  \n ").status_code == 400
    thread, seen = fake_server(bridge, "ok")
    assert say("Reinicio en\t5 minutos\n").status_code == 200
    thread.join()
    assert seen == [["say", "Reinicio en 5 minutos"]]


def test_bans_and_whitelist(env, client):
    app, settings = env
    bridge = app.extensions["panel"].bridge
    access_file = bridge.exchange_dir / "access.txt"
    post = lambda url, body: client.post(url, json=body, headers=client.headers)  # noqa: E731

    assert post("/api/access/ban", {"name": "con\ttab"}).status_code == 400
    assert post("/api/access/ban", {"name": "Tramposo", "reason": "Chocar\na propósito"}).status_code == 200
    assert post("/api/access/ban", {"name": "TRAMPOSO", "reason": "otra vez"}).status_code == 200  # no se duplica
    data = client.get("/api/access").get_json()
    assert [(b["name"], b["reason"]) for b in data["bans"]] == [("TRAMPOSO", "otra vez")]
    assert access_file.read_text(encoding="utf-8") == "whitelist\t0\nban\tTRAMPOSO\tBloqueado: otra vez\n"

    assert post("/api/access/whitelist", {"enabled": True, "names": []}).status_code == 400
    r = post("/api/access/whitelist", {"enabled": True, "names": ["Xoan", "xoan", "Alumno2"]})
    assert r.status_code == 200 and r.get_json()["whitelist"] == ["Alumno2", "Xoan"]
    assert access_file.read_text(encoding="utf-8").startswith("whitelist\t1\nallow\tAlumno2\nallow\tXoan\nban\t")

    assert post("/api/access/unban", {"name": "tramposo"}).status_code == 200
    assert post("/api/access/unban", {"name": "tramposo"}).status_code == 404
    assert json.loads((settings.data_dir / "access.json").read_text(encoding="utf-8"))["bans"] == []


def test_ban_kicks_connected_player(env, client):
    app, _ = env
    set_online(app)
    bridge = app.extensions["panel"].bridge
    write_bridge_players(bridge, "0\tMolesto\t0\n")
    thread, seen = fake_server(bridge, "ok")
    r = client.post("/api/access/ban", json={"name": "Molesto", "reason": "spam"}, headers=client.headers)
    thread.join()
    assert r.get_json() == {"ok": True, "kicked": True}
    assert seen == [["kick", "Molesto", "Bloqueado: spam"]]


def test_plugin_handles_auth_and_chat(env):
    app, _ = env
    source = app.extensions["panel"].bridge.plugin_path.read_text(encoding="utf-8")
    for needle in ('MP.RegisterEvent("onPlayerAuth"', "MP.SendChatMessage(-1", "MP.IsPlayerGuest", "players.txt", "access.txt"):
        assert needle in source


# -- usuarios y roles -------------------------------------------------
def login(app, username, password):
    client = app.test_client()
    r = client.post("/login", data={"username": username, "password": password, "csrf_token": csrf(client)})
    if r.status_code != 302:
        return None
    client.headers = {"X-CSRF-Token": csrf(client)}
    return client


def test_users_and_roles(env, client):
    app, _ = env
    set_online(app)
    post = lambda body: client.post("/api/users", json=body, headers=client.headers)  # noqa: E731
    assert post({"username": "mod", "password": "corta", "role": "moderator"}).status_code == 400
    assert post({"username": "mod", "password": "contraseña-mod", "role": "jefe"}).status_code == 400
    assert post({"username": "mod", "password": "contraseña-mod", "role": "moderator"}).status_code == 201
    assert post({"username": "mod", "password": "contraseña-mod", "role": "moderator"}).status_code == 409
    assert [u["role"] for u in client.get("/api/users").get_json()["users"]] == ["admin", "moderator"]

    mod = login(app, "mod", "contraseña-mod")
    assert 'data-role="moderator"' in mod.get("/").get_data(as_text=True)
    assert mod.get("/api/status").status_code == 200
    assert mod.post("/api/players/kick", json={"name": "Nadie"}, headers=mod.headers).status_code == 404  # permitido
    forbidden = [
        ("get", "/api/maps"), ("get", "/api/mods"), ("get", "/api/config"), ("get", "/api/logs"), ("get", "/api/users"),
        ("get", "/api/access"), ("get", "/api/schedule"), ("get", "/api/backups"), ("get", "/api/update"),
        ("post", "/api/server/stop"), ("post", "/api/access/ban"), ("post", "/api/backups"), ("post", "/api/update"),
        ("post", "/api/users"), ("delete", "/api/users/admin"), ("delete", "/api/mods/x.zip"),
    ]
    for method, url in forbidden:
        assert getattr(mod, method)(url, json={}, headers=mod.headers).status_code == 403, url

    # El administrador no puede quedarse sin administradores ni tocarse a sí mismo aquí.
    assert client.delete("/api/users/admin", headers=client.headers).status_code == 409
    assert client.patch("/api/users/admin", json={"role": "moderator"}, headers=client.headers).status_code == 409
    assert client.patch("/api/users/nadie", json={"role": "admin"}, headers=client.headers).status_code == 404

    # Cambiar la contraseña de otro usuario cierra sus sesiones.
    assert client.patch("/api/users/mod", json={"password": "otra-contraseña"}, headers=client.headers).status_code == 200
    assert mod.get("/api/status").status_code == 401
    assert login(app, "mod", "contraseña-mod") is None
    mod = login(app, "mod", "otra-contraseña")

    assert client.patch("/api/users/mod", json={"role": "admin"}, headers=client.headers).status_code == 200
    assert mod.get("/api/maps").status_code == 200
    assert client.delete("/api/users/mod", headers=client.headers).status_code == 200
    assert mod.get("/api/status").status_code == 401


def test_old_single_user_file_is_migrated(tmp_path):
    from werkzeug.security import generate_password_hash

    from panel.auth import AuthStore

    (tmp_path / "panel.json").write_text(json.dumps({
        "username": "xoan", "password_hash": generate_password_hash("mi-contraseña"),
        "secret_key": "clave", "epoch": "abc", "default_credentials": False,
    }))
    store = AuthStore(tmp_path)
    assert store.secret_key == "clave" and store.users() == [{"username": "xoan", "role": "admin"}]
    assert store.verify("xoan", "mi-contraseña") == {"user": "xoan", "epoch": "abc"}
    assert store.verify("xoan", "mal") is None and store.verify("nadie", "mi-contraseña") is None
    store.reset()
    assert store.verify("admin", "admin") and store.verify("xoan", "mi-contraseña")


# -- activar y desactivar mods ----------------------------------------
def test_disable_and_enable_mod(env, client):
    _, settings = env
    resources = settings.beammp_dir / "Resources"
    client.put("/api/mods/nord.zip", data=MAP_ZIP, headers=client.headers)
    toggle = lambda name, on: client.post(f"/api/mods/{name}/enabled", json={"enabled": on}, headers=client.headers)  # noqa: E731

    assert toggle("nord.zip", "no").status_code == 400
    assert toggle("otro.zip", False).status_code == 404
    assert toggle("nord.zip", False).status_code == 200
    assert (resources / "ClientDisabled" / "nord.zip").is_file() and not (resources / "Client" / "nord.zip").exists()
    assert [(m["name"], m["enabled"], m["kind"]) for m in client.get("/api/mods").get_json()["mods"]] == [("nord.zip", False, "map")]
    assert client.get("/api/maps").get_json()["mods"] == []  # sus mapas dejan de ofrecerse
    assert client.put("/api/mods/nord.zip", data=MAP_ZIP, headers=client.headers).status_code == 409

    assert toggle("nord.zip", True).status_code == 200
    assert len(client.get("/api/maps").get_json()["mods"]) == 1
    ServerConfig(settings.config_path).update({"Map": "/levels/ks_nord/info.json"})
    assert toggle("nord.zip", False).status_code == 409  # contiene el mapa en uso

    ServerConfig(settings.config_path).update({"Map": "/levels/gridmap_v2/info.json"})
    toggle("nord.zip", False)
    assert client.put("/api/mods/nord.zip?overwrite=1", data=MAP_ZIP, headers=client.headers).status_code == 201
    assert not (resources / "ClientDisabled" / "nord.zip").exists()  # al sustituirlo queda activo
    toggle("nord.zip", False)
    assert client.delete("/api/mods/nord.zip", headers=client.headers).status_code == 200


# -- reinicios programados y rotación ---------------------------------
class FakeService:
    def __init__(self):
        self.state, self.calls = "online", []

    def status(self):
        return {"state": self.state, "uptime": 1, "pid": None}

    def run(self, action):
        self.calls.append(action)


class FakeBridge:
    def __init__(self):
        self.said = []

    def say(self, message):
        self.said.append(message)


def make_scheduler(env, players=0):
    from panel.schedule import Scheduler

    panel = env[0].extensions["panel"]
    service, bridge, count = FakeService(), FakeBridge(), [players]
    scheduler = Scheduler(panel.schedule, panel.config, service, panel.catalog, bridge, lambda: count[0])
    return scheduler, service, bridge, count


def save_schedule(client, restart=None, rotation=None):
    body = {
        "restart": {"enabled": False, "times": [], "warn_minutes": 5, **(restart or {})},
        "rotation": {"enabled": False, "maps": [], "every_hours": 6, "only_when_empty": True, **(rotation or {})},
    }
    return client.post("/api/schedule", json=body, headers=client.headers)


def test_schedule_validation(env, client):
    assert client.get("/api/schedule").get_json()["schedule"]["restart"] == {"enabled": False, "times": [], "warn_minutes": 5}
    assert save_schedule(client, {"enabled": True}).status_code == 400  # sin horas
    assert save_schedule(client, {"times": ["25:00"]}).status_code == 400
    assert save_schedule(client, {"warn_minutes": 90}).status_code == 400
    assert save_schedule(client, rotation={"enabled": True, "maps": ["/levels/gridmap_v2/info.json"]}).status_code == 400
    assert save_schedule(client, rotation={"maps": ["/levels/inventado/info.json"]}).status_code == 400
    assert client.post("/api/schedule", json={"restart": 1}, headers=client.headers).status_code == 400
    r = save_schedule(client, {"enabled": True, "times": ["05:00", "05:00", "17:30"]})
    assert r.status_code == 200 and r.get_json()["schedule"]["restart"]["times"] == ["05:00", "17:30"]
    assert r.get_json()["upcoming"]["restart"] is not None


def test_scheduled_restart_warns_then_restarts_once(env, client):
    from datetime import datetime

    save_schedule(client, {"enabled": True, "times": ["05:00"], "warn_minutes": 5})
    scheduler, service, bridge, _ = make_scheduler(env)
    at = lambda h, m, s=0: datetime(2026, 11, 12, h, m, s)  # noqa: E731
    assert scheduler.tick(at(4, 50)) == []
    assert scheduler.tick(at(4, 55, 10)) == ["warn"] and "5 min" in bridge.said[0]
    assert scheduler.tick(at(4, 57)) == []  # un solo aviso
    assert scheduler.tick(at(5, 0, 15)) == ["restart"] and service.calls == ["restart"]
    assert scheduler.tick(at(5, 0, 40)) == [] and service.calls == ["restart"]
    assert scheduler.tick(datetime(2026, 11, 13, 5, 0, 5)) == ["restart"]  # al día siguiente, otra vez

    service.state = "offline"  # un servidor detenido a mano no se arranca
    assert scheduler.tick(datetime(2026, 11, 14, 5, 0, 5)) == []


def test_map_rotation(env, client):
    from datetime import datetime, timedelta

    app, settings = env
    client.put("/api/mods/nord.zip", data=MAP_ZIP, headers=client.headers)
    maps = ["/levels/gridmap_v2/info.json", "/levels/ks_nord/info.json"]
    assert save_schedule(client, {"warn_minutes": 2}, {"enabled": True, "maps": maps, "every_hours": 6}).status_code == 200
    scheduler, service, bridge, players = make_scheduler(env, players=2)
    now = datetime.now()
    current = lambda: ServerConfig(settings.config_path).get("Map")  # noqa: E731

    assert scheduler.tick(now + timedelta(hours=5)) == []  # aún no toca
    assert scheduler.tick(now + timedelta(hours=7)) == [] and current() == maps[0]  # espera a que se vacíe
    players[0] = 0
    assert scheduler.tick(now + timedelta(hours=7, minutes=1)) == ["rotate"]
    assert current() == maps[1] and service.calls == ["restart"]
    assert scheduler.tick(now + timedelta(hours=8)) == []  # el intervalo vuelve a contar

    # Con gente dentro y sin esperar: avisa y cambia pasados los minutos de aviso.
    save_schedule(client, {"warn_minutes": 2}, {"enabled": True, "maps": maps, "every_hours": 6, "only_when_empty": False})
    players[0] = 3
    later = now + timedelta(hours=14)
    assert scheduler.tick(later) == ["warn"] and "Gridmap V2" in bridge.said[-1]
    assert scheduler.tick(later + timedelta(minutes=1)) == []
    assert scheduler.tick(later + timedelta(minutes=2, seconds=5)) == ["rotate"] and current() == maps[0]


# -- actualización de BeamMP Server -----------------------------------
ASSETS = [
    {"name": n, "browser_download_url": "https://github.com/BeamMP/BeamMP-Server/releases/download/v3.9.9/" + n}
    for n in ("BeamMP-Server.debian.12.x86_64", "BeamMP-Server.debian.12.x86_64.debuginfo", "BeamMP-Server.debian.11.x86_64",
              "BeamMP-Server.ubuntu.22.04.x86_64", "BeamMP-Server.ubuntu.24.04.x86_64", "BeamMP-Server.debian.12.arm64", "BeamMP-Server.exe")
]


def test_pick_asset():
    from panel.updater import pick_asset, version_tuple

    assert pick_asset(ASSETS, "debian", "12", "x86_64")["name"] == "BeamMP-Server.debian.12.x86_64"
    assert pick_asset(ASSETS, "debian", "13", "x86_64")["name"] == "BeamMP-Server.debian.12.x86_64"  # la más reciente
    assert pick_asset(ASSETS, "ubuntu", "24", "x86_64")["name"] == "BeamMP-Server.ubuntu.24.04.x86_64"
    assert pick_asset(ASSETS, "debian", "12", "arm64")["name"] == "BeamMP-Server.debian.12.arm64"
    assert pick_asset(ASSETS, "fedora", "40", "x86_64") is None
    evil = [{"name": "BeamMP-Server.debian.12.x86_64", "browser_download_url": "https://ejemplo.com/x"}]
    assert pick_asset(evil, "debian", "12", "x86_64") is None
    assert version_tuple("v3.10.0") > version_tuple("3.9.3")


def test_update_downloads_and_replaces_binary(env, client, monkeypatch):
    import panel.updater as updater

    app, settings = env
    panel = app.extensions["panel"]
    monkeypatch.setattr(updater, "detect_platform", lambda: ("debian", "12", "x86_64"))
    payload = {"binary": b"\x7fELF" + b"nuevo"}
    opened = []

    def open_url(url):
        opened.append(url)
        if url == updater.RELEASE_API:
            return io.BytesIO(json.dumps({"tag_name": "v3.9.9", "assets": ASSETS}).encode())
        return io.BytesIO(payload["binary"])

    panel.updater._open_url = open_url
    binary = settings.beammp_dir / "BeamMP-Server"
    binary.write_bytes(b"\x7fELFviejo")
    settings.log_path.write_text("[01/01/26 10:00:00] [INFO] BeamMP Server v3.9.3\n")

    info = client.get("/api/update").get_json()
    assert info["current"] == "3.9.3" and info["latest"] == "3.9.9" and info["update_available"] is True
    assert info["asset"] == "BeamMP-Server.debian.12.x86_64"

    payload["binary"] = b"<html>no es un binario</html>"
    assert client.post("/api/update", headers=client.headers).status_code == 502
    assert binary.read_bytes() == b"\x7fELFviejo" and not binary.with_name("BeamMP-Server.new").exists()

    payload["binary"] = b"\x7fELF" + b"nuevo"
    r = client.post("/api/update", headers=client.headers)
    assert r.status_code == 200 and r.get_json()["version"] == "3.9.9"
    assert binary.read_bytes() == b"\x7fELFnuevo" and os.access(binary, os.X_OK)
    assert binary.with_name("BeamMP-Server.old").read_bytes() == b"\x7fELFviejo"
    assert opened[-1].endswith("/BeamMP-Server.debian.12.x86_64")

    with open(settings.log_path, "a") as fh:  # el servidor arranca con la versión nueva
        fh.write("[01/01/26 11:00:00] [INFO] BeamMP Server v3.9.9\n")
    assert client.get("/api/update").get_json()["update_available"] is False

    def broken(url):
        raise OSError("sin red")

    panel.updater._open_url = broken
    info = client.get("/api/update?force=1").get_json()
    assert "GitHub" in info["error"] and info["update_available"] is False


# -- copias de seguridad ----------------------------------------------
def test_backup_and_restore(env, client):
    app, settings = env
    resources = settings.beammp_dir / "Resources"
    client.put("/api/mods/nord.zip", data=MAP_ZIP, headers=client.headers)
    client.put("/api/mods/coche.zip", data=CAR_ZIP, headers=client.headers)
    client.post("/api/mods/coche.zip/enabled", json={"enabled": False}, headers=client.headers)
    client.put("/api/maps/image/gridmap_v2", data=PNG, headers=client.headers)
    client.post("/api/access/ban", json={"name": "Tramposo"}, headers=client.headers)
    original_config = settings.config_path.read_text(encoding="utf-8")

    small = client.post("/api/backups", json={}, headers=client.headers)
    assert small.status_code == 201
    time.sleep(1.1)  # el nombre lleva la hora al segundo
    full = client.post("/api/backups", json={"include_mods": True}, headers=client.headers).get_json()["name"]
    listed = client.get("/api/backups").get_json()["backups"]
    assert [b["includes_mods"] for b in listed] == [True, False] and listed[0]["name"] == full

    download = client.get(f"/api/backups/{full}")
    names = set(zipfile.ZipFile(io.BytesIO(download.data)).namelist())
    assert names == {"manifest.json", "ServerConfig.toml", "panel/access.json", "panel/previews/gridmap_v2.png",
                     "mods/nord.zip", "mods-disabled/coche.zip"}
    assert "panel.json" not in str(names)  # las cuentas no salen del servidor

    # Se estropea todo y se restaura.
    client.post("/api/config", json={"values": {"Name": "Roto"}}, headers=client.headers)
    client.post("/api/access/unban", json={"name": "Tramposo"}, headers=client.headers)
    client.delete("/api/maps/image/gridmap_v2", headers=client.headers)
    client.delete("/api/mods/nord.zip", headers=client.headers)
    client.post("/api/mods/coche.zip/enabled", json={"enabled": True}, headers=client.headers)

    r = client.post(f"/api/backups/{full}/restore", json={"restart": True}, headers=client.headers)
    assert r.status_code == 200
    assert r.get_json()["restored"] == {"config": True, "panel": 1, "images": 1, "mods": 2}
    assert settings.config_path.read_text(encoding="utf-8") == original_config
    assert [b["name"] for b in client.get("/api/access").get_json()["bans"]] == ["Tramposo"]
    assert "ban\tTramposo" in (settings.beammp_dir / "panel-bridge" / "access.txt").read_text(encoding="utf-8")
    assert client.get("/api/maps").get_json()["official"][0]["custom_preview"] is True
    assert (resources / "Client" / "nord.zip").is_file()
    assert (resources / "ClientDisabled" / "coche.zip").is_file() and not (resources / "Client" / "coche.zip").exists()

    assert client.delete(f"/api/backups/{full}", headers=client.headers).status_code == 200
    assert client.get(f"/api/backups/{full}").status_code == 404
    assert client.get("/api/backups/..%2Fpanel.json").status_code == 404


def test_backup_upload_and_hostile_archive(env, client):
    _, settings = env
    put = lambda name, data: client.put(f"/api/backups/{name}", data=data, headers=client.headers)  # noqa: E731
    assert put("notas.txt", b"x").status_code == 400
    assert put("otra.zip", MAP_ZIP).status_code == 400  # un zip cualquiera no es una copia
    assert not (settings.backups_dir / "otra.zip").exists()

    hostile = make_zip({
        "manifest.json": json.dumps({"created": 1, "includes_mods": True}),
        "../../etc/cron.d/malo": "x",
        "panel/panel.json": '{"users": {}}',
        "panel/previews/../../panel.json": "x",
        "mods/../../fuera.zip": "x",
        "mods/script.sh": "x",
        "panel/schedule.json": '{"restart": {"enabled": false, "times": ["03:00"], "warn_minutes": 1}}',
    })
    assert put("hostil.zip", hostile).status_code == 201
    assert put("hostil.zip", hostile).status_code == 409
    before = (settings.data_dir / "panel.json").read_text()
    r = client.post("/api/backups/hostil.zip/restore", json={}, headers=client.headers)
    assert r.get_json()["restored"] == {"config": False, "panel": 1, "images": 0, "mods": 0}
    assert (settings.data_dir / "panel.json").read_text() == before
    assert not (settings.beammp_dir.parent / "fuera.zip").exists() and not (settings.beammp_dir / "fuera.zip").exists()
    assert client.get("/api/schedule").get_json()["schedule"]["restart"]["times"] == ["03:00"]
