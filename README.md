# BeamMP Server & Web Control Panel

Este repositorio contiene una solución completa y automatizada para desplegar y gestionar un servidor dedicado de BeamMP en entornos Linux. Incluye un instalador robusto y un panel web de control moderno que permite la gestión de mapas y la monitorización del estado del servidor en tiempo real.

## Características Principales

* **Despliegue Automatizado:** El script `install.sh` detecta la distribución (Debian/Ubuntu), instala dependencias, configura servicios `systemd` y despliega la aplicación web.
* **Sistema de Actualización Inteligente:** Permite actualizar el código del panel web sin interrumpir el servidor de juego ni perder las configuraciones.
* **Interfaz Moderna:** Frontend construido con Tailwind CSS, totalmente responsivo y sin dependencias estáticas locales.
* **Monitorización en Tiempo Real:** El panel consulta los registros del servidor (`Server.log`) para mostrar el estado en vivo y el número de jugadores conectados.
* **Gestión de Mapas Externa:** Los mapas oficiales y los mods se configuran a través de un archivo `maps.json` independiente, evitando la necesidad de modificar el código fuente de la aplicación.
* **Concurrencia Segura:** Uso de bloqueos (`threading.Lock`) en el backend para evitar corrupciones si múltiples usuarios intentan reiniciar el servidor simultáneamente.

## Instalación

### 1. Clonar el repositorio
Descarga el código fuente en tu servidor:

```bash
git clone [https://github.com/xoanxc/BeamMP-Panel.git](https://github.com/xoanxc/BeamMP-Panel.git)
cd BeamMP-Panel
chmod +x install.sh

```

### 2. Ejecutar el instalador

Ejecuta el script de instalación con privilegios. El script preparará el entorno y te guiará durante el proceso:

```bash
./install.sh

```

*Nota: Durante la instalación inicial, se te solicitará tu Auth Key de BeamMP Server. Puedes generar una desde el portal oficial: https://keymaster.beammp.com/*

## Actualización y Mantenimiento

Si realizas cambios en el código del panel (por ejemplo, modificando `app.py` o `index.html`) o descargas una nueva versión del repositorio, puedes aplicar los cambios sin reinstalar el servidor de juego ejecutando:

```bash
./install.sh update

```

Esto actualizará únicamente la interfaz web y reiniciará el servicio del panel en menos de un segundo, manteniendo intactos tus mods y configuraciones.

## Configuración de Mapas

La lista de mapas disponibles en el panel web se gestiona exclusivamente a través del archivo `maps.json` ubicado en la raíz de la instalación.

Para añadir un nuevo mapa (oficial o mod), edita el archivo siguiendo esta estructura:

```json
{
    "normal": {
        "Nombre del Mapa": "/levels/nombre_carpeta/info.json"
    },
    "mods": {
        "Nombre del Mod": "/levels/nombre_mod/info.json"
    }
}

```

Los cambios en este archivo se reflejarán en el panel web inmediatamente tras recargar la página, sin necesidad de reiniciar ningún servicio.

## Puertos y Accesos

Una vez finalizada la instalación, los servicios estarán disponibles en los siguientes puertos por defecto:

* **Servidor BeamMP (UDP/TCP):** `30814`
* **Panel de Control Web (TCP):** `5000`

Asegúrate de tener estos puertos abiertos en tu firewall (iptables, ufw, o el panel de control de tu proveedor de alojamiento) para permitir conexiones entrantes.

## Licencia

Este proyecto se distribuye bajo la Licencia MIT. Consulta el archivo `LICENSE` para más detalles.
