#!/bin/bash
# Instalador de BeamMP Server + panel web.
#
#   ./install.sh                  Instalación completa (o menú si ya está instalado)
#   ./install.sh update           Actualiza solo el panel web
#   ./install.sh reset-password   Devuelve el acceso del panel a admin / admin
set -euo pipefail

# ==========================================
# VARIABLES
# ==========================================
APP_DIR="/opt/beammp-web"
DATA_DIR="$APP_DIR/data"
BEAMMP_DIR="/opt/beammp"
PANEL_PORT="5000"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_PANEL="/etc/systemd/system/beammp-web.service"
SERVICE_BEAMMP="/etc/systemd/system/beammp.service"

log_info() { echo "[INFO] $1"; }
log_warn() { echo "[WARN] $1"; }
log_err()  { echo "[ERROR] $1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || log_err "Ejecuta el instalador como root (sudo ./install.sh)."

# ==========================================
# PANEL WEB
# ==========================================
install_panel_files() {
    log_info "Copiando el panel a $APP_DIR..."
    mkdir -p "$APP_DIR" "$DATA_DIR"
    chmod 700 "$DATA_DIR"

    # Instalaciones antiguas: el maps.json editado a mano pasa a ser el catálogo
    # personalizado, que las actualizaciones ya no sobrescriben.
    if [ -f "$APP_DIR/maps.json" ] && [ ! -f "$DATA_DIR/custom_maps.json" ]; then
        log_info "Conservando tus mapas en $DATA_DIR/custom_maps.json"
        cp "$APP_DIR/maps.json" "$DATA_DIR/custom_maps.json"
    fi

    # Se sustituye el código por completo; data/ y venv/ no se tocan.
    rm -rf "$APP_DIR/panel" "$APP_DIR/templates" "$APP_DIR/static"
    cp -r "$SCRIPT_DIR/panel" "$SCRIPT_DIR/templates" "$SCRIPT_DIR/static" "$APP_DIR/"
    cp "$SCRIPT_DIR/app.py" "$SCRIPT_DIR/wsgi.py" "$SCRIPT_DIR/maps.json" "$SCRIPT_DIR/requirements.txt" "$APP_DIR/"
    find "$APP_DIR" -name "__pycache__" -type d -prune -exec rm -rf {} +
    chmod 644 "$APP_DIR/app.py"

    if [ ! -x "$APP_DIR/venv/bin/python" ]; then
        log_info "Creando entorno virtual de Python..."
        python3 -m venv "$APP_DIR/venv"
    fi
    log_info "Instalando dependencias de Python..."
    "$APP_DIR/venv/bin/pip" install --upgrade pip -q
    "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
}

write_panel_service() {
    log_info "Creando servicio systemd para el panel web..."
    # Wants (no Requires): si fuese Requires, reiniciar o detener BeamMP desde el
    # panel reiniciaría o detendría también el propio panel.
    # Un solo proceso con hilos: el estado en memoria (jugadores, caché de mods,
    # bloqueo de intentos de acceso) es único y las subidas largas no bloquean.
    cat > "$SERVICE_PANEL" <<EOF
[Unit]
Description=BeamMP Web Control Panel
After=network.target beammp.service
Wants=beammp.service

[Service]
User=root
WorkingDirectory=$APP_DIR
Environment=BEAMMP_DIR=$BEAMMP_DIR
Environment=PANEL_DATA_DIR=$DATA_DIR
ExecStart=$APP_DIR/venv/bin/gunicorn --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PANEL_PORT wsgi:app
Restart=always

[Install]
WantedBy=multi-user.target
EOF
}

update_panel() {
    log_info "Actualizando el panel web (el servidor de juego no se toca)..."
    systemctl stop beammp-web 2>/dev/null || true
    install_panel_files
    write_panel_service
    systemctl daemon-reload
    systemctl enable beammp-web >/dev/null 2>&1
    systemctl start beammp-web
    log_info "Panel web actualizado."
    exit 0
}

reset_password() {
    [ -x "$APP_DIR/venv/bin/python" ] || log_err "El panel no está instalado en $APP_DIR."
    PANEL_DATA_DIR="$DATA_DIR" "$APP_DIR/venv/bin/python" "$APP_DIR/app.py" --reset-credentials
    systemctl restart beammp-web
    log_info "Ya puedes entrar con admin / admin. Cambia la contraseña desde el panel."
    exit 0
}

# ==========================================
# MODO DE EJECUCIÓN
# ==========================================
case "${1:-}" in
    update) update_panel ;;
    reset-password) reset_password ;;
    "") ;;
    *) log_err "Opción desconocida: $1 (usa: update | reset-password)" ;;
esac

if [ -d "$APP_DIR" ] && [ -f "$SERVICE_PANEL" ]; then
    log_warn "Se ha detectado una instalacion existente del panel web."
    read -rp "¿Que deseas hacer? [a]ctualizar panel / [r]einstalar servidor completo (a/r): " RESPUESTA
    if [[ "$RESPUESTA" == "a" || "$RESPUESTA" == "A" ]]; then
        update_panel
    fi
fi

# ==========================================
# INSTALACIÓN COMPLETA
# ==========================================
log_info "Iniciando instalacion o reinstalacion completa..."
systemctl stop beammp beammp-web 2>/dev/null || true

# ------------------------------
# Detectar distro y arquitectura
# ------------------------------
if command -v lsb_release >/dev/null 2>&1; then
    DISTRO=$(lsb_release -is | tr '[:upper:]' '[:lower:]')
    DISTROVERSION=$(lsb_release -rs | cut -d. -f1)
else
    # shellcheck disable=SC1091
    source /etc/os-release
    DISTRO=$(echo "${ID:-}" | tr '[:upper:]' '[:lower:]')
    DISTROVERSION=$(echo "${VERSION_ID:-}" | cut -d. -f1)
fi

if [ "$DISTRO" = "debian" ] && [ -z "$DISTROVERSION" ]; then
    DISTROVERSION="12"
elif [ "$DISTRO" = "ubuntu" ] && [ -z "$DISTROVERSION" ]; then
    DISTROVERSION="22"
fi

ARCH=$(uname -m)
case $ARCH in
    x86_64) ARCH="x86_64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *) log_err "Arquitectura $ARCH no soportada." ;;
esac

ASSET_EXACT="BeamMP-Server.${DISTRO}.${DISTROVERSION}.${ARCH}"
log_info "Sistema detectado: $DISTRO $DISTROVERSION $ARCH"

# ------------------------------
# Dependencias del sistema
# ------------------------------
log_info "Instalando dependencias necesarias (esto puede tardar unos segundos)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl wget unzip liblua5.3-dev openssl jq > /dev/null

# ------------------------------
# Descargar BeamMP Server
# ------------------------------
log_info "Preparando directorio de BeamMP Server en $BEAMMP_DIR..."
mkdir -p "$BEAMMP_DIR/Resources/Client" "$BEAMMP_DIR/Resources/Server"
cd "$BEAMMP_DIR"

log_info "Consultando API de GitHub para la ultima version..."
API_RESPONSE=$(curl -fsSL https://api.github.com/repos/BeamMP/BeamMP-Server/releases/latest) \
    || log_err "No se ha podido consultar GitHub. Comprueba la conexion a Internet."

# jq extrae la URL exacta evitando los binarios debuginfo
URL=$(echo "$API_RESPONSE" | jq -r ".assets[] | select(.name | contains(\"$ASSET_EXACT\") and (contains(\"debuginfo\") | not)) | .browser_download_url" | head -n 1)

if [ -z "$URL" ] || [ "$URL" == "null" ]; then
    log_warn "No se encontro binario exacto para ${DISTRO} ${DISTROVERSION} ${ARCH}."
    log_info "Buscando la version mas reciente compatible..."
    URL=$(echo "$API_RESPONSE" | jq -r ".assets[] | select(.name | contains(\"$DISTRO\") and contains(\"$ARCH\") and (contains(\"debuginfo\") | not)) | .browser_download_url" | sort -V | tail -n 1)
    if [ -z "$URL" ] || [ "$URL" == "null" ]; then
        log_err "No se encontro ningun binario compatible para $DISTRO $ARCH. Abortando."
    fi
fi

log_info "Descargando BeamMP Server desde: $URL"
# Se descarga a un archivo temporal para no dejar un binario a medias si falla.
wget -q "$URL" -O BeamMP-Server.new
chmod +x BeamMP-Server.new
mv -f BeamMP-Server.new BeamMP-Server

# ------------------------------
# Configuración inicial (solo si es nueva)
# ------------------------------
if [ ! -f "$BEAMMP_DIR/ServerConfig.toml" ]; then
    log_info "Generando archivos de configuracion iniciales..."
    ./BeamMP-Server &> /dev/null &
    SERVER_PID=$!
    for _ in $(seq 1 20); do
        [ -f "$BEAMMP_DIR/ServerConfig.toml" ] && break
        sleep 0.5
    done
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    [ -f "$BEAMMP_DIR/ServerConfig.toml" ] || log_err "BeamMP Server no ha generado ServerConfig.toml. Ejecutalo a mano en $BEAMMP_DIR para ver el error."

    read -rp "Introduce tu Auth Key de BeamMP Server: " AUTH_KEY
    # Se escapan los caracteres especiales de sed por si la clave los trae.
    AUTH_KEY_ESCAPED=$(printf '%s' "$AUTH_KEY" | sed 's/[\\&|"]/\\&/g')
    sed -i "s|^AuthKey\s*=.*|AuthKey = \"$AUTH_KEY_ESCAPED\"|" "$BEAMMP_DIR/ServerConfig.toml"
    chmod 600 "$BEAMMP_DIR/ServerConfig.toml"
else
    log_info "Archivo de configuracion existente detectado. Se conservan Auth Key y configuraciones previas."
fi

# ------------------------------
# Servicio systemd de BeamMP
# ------------------------------
log_info "Creando servicio systemd para BeamMP Server..."
cat > "$SERVICE_BEAMMP" <<EOF
[Unit]
Description=BeamMP Dedicated Server
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$BEAMMP_DIR
ExecStart=$BEAMMP_DIR/BeamMP-Server
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

# ------------------------------
# Panel web
# ------------------------------
install_panel_files
write_panel_service

# ------------------------------
# Activar y arrancar servicios
# ------------------------------
log_info "Activando y reiniciando servicios de sistema..."
systemctl daemon-reload
systemctl enable beammp beammp-web >/dev/null 2>&1
systemctl restart beammp
systemctl restart beammp-web

echo "--------------------------------------------------------"
log_info "Instalacion/Reinstalacion completada con exito."
log_info "Servidor BeamMP escuchando en el puerto 30814"
log_info "Panel web disponible en http://<IP-del-servidor>:$PANEL_PORT"
if [ ! -f "$DATA_DIR/panel.json" ] || grep -q '"default_credentials": true' "$DATA_DIR/panel.json" 2>/dev/null; then
    log_warn "Acceso inicial: usuario admin, contraseña admin. Cambiala nada mas entrar."
fi
echo "--------------------------------------------------------"
