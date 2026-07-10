from flask import Flask, render_template, request, redirect, flash
import subprocess
import threading

# -------------------------
# Inicializar Flask
# -------------------------
app = Flask(__name__)
app.secret_key = "default_dev_key"  # será reemplazada en install.sh

# -------------------------
# Lock para evitar reinicios simultáneos o cambios de mapa simultáneos
# -------------------------
action_lock = threading.Lock()

# -------------------------
# Mapas normales y de mods
# -------------------------
maps_normal = {
    "Gridmap": "/levels/gridmap/info.json",
    "Automation Test Track": "/levels/automation_test_track/info.json",
    "East Coast USA": "/levels/east_coast_usa/info.json",
    "Hirochi Raceway": "/levels/hirochi_raceway/info.json",
    "Italy": "/levels/italy/info.json",
    "Jungle Rock Island": "/levels/jungle_rock_island/info.json",
    "Industrial": "/levels/industrial/info.json",
    "Small Island": "/levels/small_island/info.json",
    "Smallgrid": "/levels/smallgrid/info.json",
    "Utah": "/levels/utah/info.json",
    "West Coast USA": "/levels/west_coast_usa/info.json",
    "Driver Training": "/levels/driver_training/info.json",
    "Derby": "/levels/derby/info.json"
}

maps_mods = {
    "Nürburgring": "/levels/ks_nord/info.json",
}

# -------------------------
# Configuración del servidor
# -------------------------
CONFIG_PATH = "/opt/beammp/ServerConfig.toml"
SERVICE_NAME = "beammp"

# -------------------------
# Función para reiniciar servidor de forma segura
# -------------------------
def restart_server_safe():
    """
    Reinicia BeamMP solo si no hay otra acción en curso.
    """
    if action_lock.locked():
        print("Otra acción en curso, ignorando petición de reinicio.")
        return
    with action_lock:
        print("Reinicio iniciado en segundo plano...")
        subprocess.run(["systemctl", "restart", SERVICE_NAME])
        print("Reinicio completado.")

# -------------------------
# Función para cambiar el mapa de forma segura
# -------------------------
def set_map_safe(map_path):
    """
    Cambia el mapa y reinicia el servidor en segundo plano,
    usando el mismo lock para evitar conflictos.
    """
    if action_lock.locked():
        print("Otra acción en curso, ignorando cambio de mapa.")
        return
    with action_lock:
        # Leer todas las líneas del archivo
        with open(CONFIG_PATH, "r") as f:
            lines = f.readlines()

        # Reescribir líneas cambiando solo la que empieza con "Map ="
        with open(CONFIG_PATH, "w") as f:
            for line in lines:
                if line.strip().startswith("Map ="):
                    f.write(f'Map = "{map_path}"\n')
                else:
                    f.write(line)

        # Reiniciar servidor en background
        subprocess.run(["systemctl", "restart", SERVICE_NAME])
        print(f"Mapa cambiado a {map_path} y servidor reiniciado.")

# -------------------------
# Función para obtener el mapa actual
# -------------------------
def get_current_map():
    with open(CONFIG_PATH, "r") as f:
        for line in f:
            if line.strip().startswith("Map ="):
                return line.split("=", 1)[1].strip().strip('"')
    return None

# -------------------------
# Función para mostrar nombre del mapa
# -------------------------
def map_name_friendly(map_path):
    for name, path in {**maps_normal, **maps_mods}.items():
        if path == map_path:
            return name
    return map_path

import os
from flask import jsonify

# -------------------------
# Función para obtener jugadores leyendo el archivo Log
# -------------------------
def get_connected_players():
    log_path = "/opt/beammp/Server.log"
    # Si el servidor acaba de instalarse y no hay log, devolvemos 0
    if not os.path.exists(log_path):
        return 0
        
    connected_players = 0
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            
        # Analizamos las últimas 500 líneas para ser más rápidos
        for line in lines[-500:]:
            # Cuando un jugador entra al servidor
            if "has joined the server" in line:
                connected_players += 1
            # Cuando un jugador sale del servidor
            elif "has left the server" in line or "has timed out" in line:
                connected_players -= 1
                
        # Asegurarnos de que no baje de 0 por errores de lectura
        return max(0, connected_players)
    except Exception as e:
        print(f"Error leyendo logs: {e}")
        return 0

def get_max_players():
    # Leemos los jugadores máximos configurados en el archivo TOML
    try:
        with open(CONFIG_PATH, "r") as f:
            for line in f:
                if line.strip().startswith("MaxCars ="):
                    # MaxCars en BeamMP determina el número de jugadores/vehículos
                    return int(line.split("=", 1)[1].strip())
    except:
        pass
    return 10 # Valor por defecto si no lo encuentra

# -------------------------
# Ruta API para el frontend
# -------------------------
@app.route("/api/status")
def api_status():
    jugadores = get_connected_players()
    maximos = get_max_players()
    
    return jsonify({
        "status": "online",
        "players": jugadores,
        "max_players": maximos
    })
    
# -------------------------
# Ruta principal del panel
# -------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        # Cambiar mapa normal
        if "map_normal" in request.form:
            selected_map = request.form["map_normal"]
            threading.Thread(target=set_map_safe, args=(maps_normal[selected_map],), daemon=True).start()
            flash(f"¡Mapa cambiado a {selected_map} en segundo plano!")

        # Cambiar mapa de mod
        elif "map_mod" in request.form:
            selected_map = request.form["map_mod"]
            threading.Thread(target=set_map_safe, args=(maps_mods[selected_map],), daemon=True).start()
            flash(f"¡Mapa mod cambiado a {selected_map} en segundo plano!")

        # Reiniciar servidor
        elif "restart" in request.form:
            threading.Thread(target=restart_server_safe, daemon=True).start()
            flash("¡Servidor reiniciado en segundo plano!")

        return redirect("/")

    current_map_path = get_current_map()
    current_map_name = map_name_friendly(current_map_path)

    return render_template(
        "index.html",
        maps_normal=maps_normal,
        maps_mods=maps_mods,
        current_map=current_map_name
    )

# -------------------------
# Ejecutar en modo desarrollo
# -------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
