import os
import json
import subprocess
import threading
from flask import Flask, render_template, request, redirect, flash, jsonify

# -------------------------
# Inicializar Flask
# -------------------------
app = Flask(__name__)
app.secret_key = "default_dev_key"

# -------------------------
# Lock para evitar conflictos
# -------------------------
action_lock = threading.Lock()

# -------------------------
# Configuracion de rutas
# -------------------------
CONFIG_PATH = "/opt/beammp/ServerConfig.toml"
LOG_PATH = "/opt/beammp/Server.log"
SERVICE_NAME = "beammp"

# Detectamos la carpeta donde se ejecuta la app para encontrar el json de forma segura
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAPS_FILE = os.path.join(BASE_DIR, "maps.json")

# -------------------------
# Carga dinamica de mapas
# -------------------------
def get_maps():
    """Lee el archivo maps.json en vivo y devuelve los diccionarios de mapas."""
    if not os.path.exists(MAPS_FILE):
        return {}, {}
    try:
        with open(MAPS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("normal", {}), data.get("mods", {})
    except Exception as e:
        print(f"Error leyendo maps.json: {e}")
        return {}, {}

# -------------------------
# Funciones de control del servidor
# -------------------------
def restart_server_safe():
    if action_lock.locked():
        return
    with action_lock:
        subprocess.run(["systemctl", "restart", SERVICE_NAME])

def set_map_safe(map_path):
    if action_lock.locked():
        return
    with action_lock:
        try:
            with open(CONFIG_PATH, "r") as f:
                lines = f.readlines()

            with open(CONFIG_PATH, "w") as f:
                for line in lines:
                    if line.strip().startswith("Map ="):
                        f.write(f'Map = "{map_path}"\n')
                    else:
                        f.write(line)

            subprocess.run(["systemctl", "restart", SERVICE_NAME])
        except Exception as e:
            print(f"Error cambiando el mapa: {e}")

def get_current_map():
    try:
        with open(CONFIG_PATH, "r") as f:
            for line in f:
                if line.strip().startswith("Map ="):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return "Desconocido"

def map_name_friendly(map_path):
    maps_normal, maps_mods = get_maps()
    for name, path in {**maps_normal, **maps_mods}.items():
        if path == map_path:
            return name
    return map_path

# -------------------------
# Funciones de lectura de estado y jugadores
# -------------------------
def get_connected_players():
    if not os.path.exists(LOG_PATH):
        return 0
        
    connected = 0
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            
        start_index = 0
        for i in range(len(lines) - 1, -1, -1):
            if "Started" in lines[i] or "Starting" in lines[i]:
                start_index = i
                break
                
        for line in lines[start_index:]:
            line_lower = line.lower()
            if "connected" in line_lower and "player" in line_lower and "disconnected" not in line_lower:
                connected += 1
            elif "disconnected" in line_lower or "left" in line_lower or "timed out" in line_lower:
                connected -= 1
                
        return max(0, connected)
    except Exception:
        return 0

def get_max_players():
    try:
        with open(CONFIG_PATH, "r") as f:
            for line in f:
                if line.strip().startswith("MaxCars ="):
                    return int(line.split("=", 1)[1].strip())
    except Exception:
        pass
    return 10

# -------------------------
# Rutas de la aplicacion
# -------------------------
@app.route("/api/status")
def api_status():
    jugadores = get_connected_players()
    maximos = get_max_players()
    
    try:
        result = subprocess.run(["systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True)
        status = "online" if result.stdout.strip() == "active" else "offline"
    except Exception:
        status = "unknown"
        
    return jsonify({
        "status": status,
        "players": jugadores,
        "max_players": maximos
    })

@app.route("/", methods=["GET", "POST"])
def index():
    maps_normal, maps_mods = get_maps()

    if request.method == "POST":
        if "map_normal" in request.form:
            selected_map = request.form["map_normal"]
            if selected_map in maps_normal:
                threading.Thread(target=set_map_safe, args=(maps_normal[selected_map],), daemon=True).start()
                flash(f"Mapa cambiado a {selected_map} en segundo plano.")

        elif "map_mod" in request.form:
            selected_map = request.form["map_mod"]
            if selected_map in maps_mods:
                threading.Thread(target=set_map_safe, args=(maps_mods[selected_map],), daemon=True).start()
                flash(f"Mapa mod cambiado a {selected_map} en segundo plano.")

        elif "restart" in request.form:
            threading.Thread(target=restart_server_safe, daemon=True).start()
            flash("Servidor reiniciado en segundo plano.")

        return redirect("/")

    current_map_path = get_current_map()
    current_map_name = map_name_friendly(current_map_path)

    return render_template(
        "index.html",
        maps_normal=maps_normal,
        maps_mods=maps_mods,
        current_map=current_map_name
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)