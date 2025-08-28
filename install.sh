#!/bin/bash
set -e

APP_DIR="/opt/beammp-web"
BEAMMP_DIR="/opt/beammp-server"
SERVICE_PANEL="/etc/systemd/system/beammp-web.service"
SERVICE_BEAMMP="/etc/systemd/system/beammp.service"

# ------------------------------
# Detectar distro, versión y arquitectura
# ------------------------------
echo "Detectando sistema..."
if command -v lsb_release >/dev/null 2>&1; then
    DISTRO=$(lsb_release -is | tr '[:upper:]' '[:lower:]')
    DISTROVERSION=$(lsb_release -rs | cut -d. -f1)
else
    source /etc/os-release
    DISTRO=$(echo "$ID" | tr '[:upper:]' '[:lower:]')
    DISTROVERSION=$(echo "$VERSION_ID" | cut -d. -f1)
fi

ARCH=$(uname -m)
case $ARCH in
    x86_64) ARCH="x86_64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *) echo "Arquitectura $ARCH no soportada."; exit 1 ;;
esac

echo "Detectado: $DISTRO $DISTROVERSION $ARCH"

# ------------------------------
# Instalar dependencias
# ------------------------------
echo "Instalando dependencias del panel y del servidor (se mostrará progreso)..."
apt update -y
apt install -y python3 python3-venv python3-pip git curl unzip liblua5.3-dev cmake make g++ tar zip

# ------------------------------
# Descargar e instalar BeamMP Server
# ------------------------------
echo "Preparando directorio de BeamMP Server en $BEAMMP_DIR..."
mkdir -p $BEAMMP_DIR
cd $BEAMMP_DIR

ASSET_EXACT="BeamMP-Server.${DISTRO}.${DISTROVERSION}.${ARCH}"

echo "Buscando binario de BeamMP Server..."
URL=$(curl -s https://api.github.com/repos/BeamMP/BeamMP-Server/releases/latest \
    | grep "browser_download_url" \
    | grep "$ASSET_EXACT" \
    | cut -d '"' -f 4)

if [ -z "$URL" ]; then
    echo "No se encontró binario exacto para ${DISTRO} ${DISTROVERSION} ${ARCH}."
    echo "Buscando la versión más reciente compatible..."
    URL=$(curl -s https://api.github.com/repos/BeamMP/BeamMP-Server/releases/latest \
        | grep "browser_download_url" \
        | grep "$DISTRO" \
        | grep "$ARCH" \
        | cut -d '"' -f 4 \
        | sort -V \
        | tail -n1)
    if [ -n "$URL" ]; then
        echo "Se usará la versión más reciente disponible: $URL"
    else
        echo "No se encontró ningún binario compatible para ${DISTRO} ${ARCH}. Abortando."
        exit 1
    fi
else
    echo "Se encontró binario exacto: $URL"
fi

echo "Descargando BeamMP Server..."
wget -q "$URL" -O BeamMP-Server
chmod +x BeamMP-Server

echo "Ejecutando BeamMP por primera vez para generar configuración..."
./BeamMP-Server || true
sleep 2

# Crear ServerConfig.toml básico si no existe
if [ ! -f "$BEAMMP_DIR/ServerConfig.toml" ]; then
    echo "Creando configuración inicial del servidor..."
    cat > "$BEAMMP_DIR/ServerConfig.toml" <<EOF
[General]
Name = "Servidor BeamMP"
Port = 30814
MaxPlayers = 8
Private = false
EOF
fi

# ------------------------------
# Configurar servicio systemd BeamMP
# ------------------------------
echo "Creando servicio systemd para BeamMP Server..."
cat > $SERVICE_BEAMMP <<EOF
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
# Instalar panel web desde archivos locales
# ------------------------------
echo "Copiando panel web a $APP_DIR..."
rm -rf $APP_DIR
mkdir -p $APP_DIR
cp app.py $APP_DIR/
mkdir -p $APP_DIR/templates $APP_DIR/static
cp -r templates/* $APP_DIR/templates/
cp -r static/* $APP_DIR/static/

echo "Creando entorno virtual Python..."
cd $APP_DIR
python3 -m venv venv
source venv/bin/activate

echo "Instalando dependencias Python del panel..."
pip install --upgrade pip
pip install -r requirements.txt

# ------------------------------
# Configurar servicio systemd para panel
# ------------------------------
echo "Creando servicio systemd para el panel web..."
cat > $SERVICE_PANEL <<EOF
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
echo "Activando servicios..."
systemctl daemon-reload
systemctl enable beammp
systemctl enable beammp-web
systemctl restart beammp
systemctl restart beammp-web

echo "------------------------------------------"
echo "Instalación completada."
echo "Servidor BeamMP corriendo en el puerto 30814"
echo "Panel web disponible en el puerto 5000"
echo "------------------------------------------"
