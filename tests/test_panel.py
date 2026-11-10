"""Pruebas del panel. Ejecutar con:  python -m pytest tests"""

import io
import json
import re
import sys
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
