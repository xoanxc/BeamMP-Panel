#!/bin/bash
# set -e asegura que el script se detenga si ocurre un error crítico
set -e

# ==========================================
# VARIABLES DE ENTORNO
# ==========================================
APP_DIR="/opt/beammp-web"
BEAMMP_DIR="/opt/beammp"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_PANEL="/etc/systemd/system/beammp-web.service"
SERVICE_BEAMMP="/etc/systemd/system/beammp.service"

# ==========================================
# FUNCIONES DE LOGGING
# ==========================================
log_info() { echo "[INFO] $1"; }
log_warn() { echo "[WARN] $1"; }
log_err()  { echo "[ERROR] $1"; exit 1; }

# ==========================================
# FUNCIÓN: ACTUALIZAR PANEL WEB
# ==========================================
update_panel() {
    log_info "Iniciando actualizacion del panel web..."
    
    systemctl stop beammp-web 2>/dev/null || true
    
    log_info "Copiando nuevos archivos a $APP_DIR..."
    cp "$SCRIPT_DIR/app.py" "$APP_DIR/"
    cp "$SCRIPT_DIR/maps.json" "$APP_DIR/"
    mkdir -p "$APP_DIR/templates"
    cp -r "$SCRIPT_DIR/templates/"* "$APP_DIR/templates/"
    
    log_info "Reiniciando el servicio del panel web..."
    systemctl daemon-reload
    systemctl start beammp-web
    
    log_info "Panel web actualizado correctamente. Operacion finalizada."
    exit 0
}

# 1. Comprobar si se ha pasado el parámetro "update"
if [ "$1" == "update" ]; then
    update_panel
fi

# 2. Comprobar si ya existe una instalación y preguntar al usuario
if [ -d "$APP_DIR" ] && [ -f "$SERVICE_PANEL" ]; then
    log_warn "Se ha detectado una instalacion existente del panel web."
    read -p "¿Que deseas hacer? [a]ctualizar panel / [r]einstalar servidor completo (a/r): " RESPUESTA
    if [[ "$RESPUESTA" == "a" || "$RESPUESTA" == "A" ]]; then
        update_panel
    fi
fi

# ==========================================
# INSTALACIÓN COMPLETA
# ==========================================
log_info "Iniciando instalacion o reinstalacion completa..."

log_info "Deteniendo servicios en ejecucion..."
systemctl stop beammp beammp-web 2>/dev/null || true

log_info "Limpiando instalacion previa del panel web..."
rm -rf "$APP_DIR"

# ------------------------------
# Detectar distro y arquitectura
# ------------------------------
if command -v lsb_release >/dev/null 2>&1; then
    DISTRO=$(lsb_release -is | tr '[:upper:]' '[:lower:]')
    DISTROVERSION=$(lsb_release -rs | cut -d. -f1)
else
    source /etc/os-release
    DISTRO=$(echo "$ID" | tr '[:upper:]' '[:lower:]')
    DISTROVERSION=$(echo "$VERSION_ID" | cut -d. -f1)
fi

if [ "$DISTRO" = "debian" ] && [ -z "$DISTROVERSION" ]; then
    DISTROVERSION="12"
elif [ "$DISTRO" = "ubuntu" ] && [ -z "$DISTROVERSION" ]; then
    DISTROVERSION="22.04"
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
# Instalar dependencias
# ------------------------------
log_info "Instalando dependencias necesarias (esto puede tardar unos segundos)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl unzip wget liblua5.3-dev cmake make g++ tar zip openssl jq > /dev/null

# ------------------------------
# Descargar e instalar BeamMP Server
# ------------------------------
log_info "Preparando directorio de BeamMP Server en $BEAMMP_DIR..."
mkdir -p "$BEAMMP_DIR"
cd "$BEAMMP_DIR"

log_info "Consultando API de GitHub para la ultima version..."
API_RESPONSE=$(curl -s https://api.github.com/repos/BeamMP/BeamMP-Server/releases/latest)

# Usamos jq para extraer la URL exacta evitando versiones debuginfo
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
wget -q "$URL" -O BeamMP-Server
chmod +x BeamMP-Server

# ------------------------------
# Configuración inicial (Solo si es nuevo)
# ------------------------------
if [ ! -f "$BEAMMP_DIR/ServerConfig.toml" ]; then
    log_info "Generando archivos de configuracion iniciales..."
    ./BeamMP-Server &> /dev/null &
    SERVER_PID=$!
    sleep 3
    kill $SERVER_PID || true
    
    read -rep "Introduce tu Auth Key de BeamMP Server: " AUTH_KEY
    sed -i "s|^AuthKey\s*=.*|AuthKey = \"$AUTH_KEY\"|" "$BEAMMP_DIR/ServerConfig.toml"
else
    log_info "Archivo de configuracion existente detectado. Se conservan Auth Key y configuraciones previas."
fi

# ------------------------------
# Configurar servicio systemd BeamMP
# ------------------------------
log_info "Creando servicio systemd para BeamMP Server..."
cat > "$SERVICE_BEAMMP" <<EOF
[Unit]
Description=BeamMP Dedicated Server
After=network.target

[Service]
WorkingDirectory=$BEAMMP_DIR
ExecStart=$BEAMMP_DIR/BeamMP-Server
Restart=always
User=root

[Install]
WantedBy=multi-user.target
EOF

# ------------------------------
# Instalar panel web
# ------------------------------
log_info "Copiando panel web a $APP_DIR..."
mkdir -p "$APP_DIR/templates"
cp "$SCRIPT_DIR/app.py" "$APP_DIR/"
cp "$SCRIPT_DIR/maps.json" "$APP_DIR/"
cp -r "$SCRIPT_DIR/templates/"* "$APP_DIR/templates/"

log_info "Generando secret key para la aplicacion Flask..."
SECRET_KEY=$(openssl rand -hex 32)
sed -i "s|^app.secret_key = .*|app.secret_key = \"$SECRET_KEY\"|" "$APP_DIR/app.py"
chmod 600 "$APP_DIR/app.py"

log_info "Creando entorno virtual de Python..."
cd "$APP_DIR"
python3 -m venv venv
source venv/bin/activate

log_info "Instalando dependencias de Python (Flask, gunicorn)..."
pip install --upgrade pip -q
pip install Flask gunicorn -q

# ------------------------------
# Configurar servicio systemd para panel
# ------------------------------
log_info "Creando servicio systemd para el panel web..."
cat > "$SERVICE_PANEL" <<EOF
[Unit]
Description=BeamMP Web Control Panel
After=network.target beammp.service
Requires=beammp.service

[Service]
User=root
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/gunicorn --workers 3 --bind 0.0.0.0:5000 app:app
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# ------------------------------
# Activar y arrancar servicios
# ------------------------------
log_info "Activando y reiniciando servicios de sistema..."
systemctl daemon-reload
systemctl enable beammp >/dev/null 2>&1
systemctl enable beammp-web >/dev/null 2>&1
systemctl restart beammp
systemctl restart beammp-web

echo "--------------------------------------------------------"
log_info "Instalacion/Reinstalacion completada con exito."
log_info "Servidor BeamMP escuchando en el puerto 30814"
log_info "Panel web de control disponible en el puerto 5000"
echo "--------------------------------------------------------"