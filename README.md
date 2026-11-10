# BeamMP Server & Web Control Panel

Instalador y panel web para desplegar y administrar un servidor dedicado de BeamMP en Linux (Debian/Ubuntu). Desde el navegador puedes arrancar, detener y reiniciar el servidor, cambiar de mapa, subir mods, editar los ajustes principales y leer el registro en vivo.

## Qué incluye

* **Acceso con usuario y contraseña.** Por defecto `admin` / `admin`; el panel avisa hasta que la cambies.
* **Resumen en vivo:** estado real del servicio, tiempo encendido, mapa en uso y nombres de los jugadores conectados.
* **Mapas:** oficiales y de mods en una sola pantalla, con buscador. Los mapas de mods se detectan solos.
* **Mods:** subida de `.zip` arrastrando, con barra de progreso, y borrado desde el panel.
* **Ajustes:** nombre, descripción, etiquetas, jugadores máximos, coches por jugador y privacidad, sin editar `ServerConfig.toml` a mano.
* **Registro:** las últimas líneas de `Server.log`, actualizadas cada 5 segundos.
* **Sin dependencias externas en el navegador:** CSS y JavaScript propios, servidos por el panel. Funciona en móvil.

## Instalación

```bash
git clone https://github.com/xoanxc/BeamMP-Panel.git
cd BeamMP-Panel
chmod +x install.sh
sudo ./install.sh
```

Durante la primera instalación se pide la Auth Key de BeamMP Server. Se genera en https://keymaster.beammp.com/

Al terminar:

| Servicio | Puerto | Unidad systemd |
| --- | --- | --- |
| Servidor BeamMP (TCP y UDP) | `30814` | `beammp` |
| Panel web (TCP) | `5000` | `beammp-web` |

Abre ambos puertos en el firewall y entra en `http://IP-del-servidor:5000`.

## Primer acceso y contraseña

1. Entra con `admin` / `admin`.
2. Ve a **Cuenta** y pon un usuario y una contraseña propios (mínimo 8 caracteres). Las demás sesiones abiertas se cierran.

Si olvidas la contraseña:

```bash
sudo ./install.sh reset-password
```

Esto devuelve el acceso a `admin` / `admin` sin tocar nada más.

Tras 5 intentos fallidos desde la misma IP, el acceso queda bloqueado 60 segundos.

> **Importante:** el panel sirve HTTP sin cifrar. Si lo expones a Internet, ponlo detrás de un proxy inverso con HTTPS (Caddy, nginx) y añade `Environment=PANEL_COOKIE_SECURE=1` al servicio, o limita el puerto 5000 a tu IP en el firewall.

## Mapas

### Detección automática de mapas de mods

No hace falta registrar los mapas a mano. El panel abre cada `.zip` de `Resources/Client` y busca carpetas `levels/<nombre>/` que sean un mapa completo (con `info.json`, un `.mis` o una subcarpeta `main/`). Por cada una:

* la ruta para BeamMP es `/levels/<nombre>/info.json`;
* el nombre visible sale del campo `title` de `info.json`; si falta o es una clave de traducción, se usa el nombre de la carpeta (`pikes_peak` → «Pikes Peak»);
* la imagen se toma de `previews` en `info.json` o de un archivo `*preview*.jpg/png`.

Los `.zip` que solo modifican archivos de un mapa oficial no se listan como mapa nuevo. El resultado se guarda en memoria y solo se vuelve a leer un `.zip` cuando cambia su tamaño o fecha. El botón **Volver a analizar** de la pestaña Mods fuerza la lectura de todos.

### Nombres personalizados y mapas añadidos a mano

Crea o edita `/opt/beammp-web/data/custom_maps.json`:

```json
{
    "normal": {
        "Mi mapa oficial extra": "/levels/nombre_carpeta/info.json"
    },
    "mods": {
        "Nürburgring": "/levels/ks_nord/info.json"
    }
}
```

* Si la ruta coincide con un mapa detectado, se usa tu nombre en lugar del detectado.
* Si no coincide con ningún `.zip`, el mapa aparece igualmente con un aviso.
* Los cambios se ven al recargar la página. Las actualizaciones del panel no tocan este archivo.

La lista de mapas oficiales está en `maps.json`, en la raíz del repositorio, y se actualiza con el panel.

## Mods

En la pestaña **Mods** arrastra uno o varios `.zip` o pulsa **Elegir archivos**. Cada archivo:

* se guarda directamente en `Resources/Client` (no pasa por memoria ni por `/tmp`);
* se comprueba que es un `.zip` válido antes de darlo por bueno; si la subida falla no deja restos;
* se clasifica como mapa, vehículo u otro.

El límite por archivo es de 4 GB (variable `PANEL_MAX_UPLOAD_MB`). BeamMP carga los mods al arrancar, así que después de subir o borrar hay que reiniciar el servidor; el panel ofrece el botón. No se puede borrar el mod que contiene el mapa en uso.

## Actualización

```bash
git pull
sudo ./install.sh update
```

Actualiza el código del panel, sus dependencias y la unidad systemd. No toca el servidor de juego, los mods, las credenciales ni `custom_maps.json`.

Si vienes de la versión 1, tu `maps.json` anterior se copia a `data/custom_maps.json` en la primera actualización, así que no pierdes los mapas que tuvieras añadidos.

## Configuración avanzada

Variables de entorno del servicio `beammp-web` (`/etc/systemd/system/beammp-web.service`):

| Variable | Por defecto | Para qué sirve |
| --- | --- | --- |
| `BEAMMP_DIR` | `/opt/beammp` | Carpeta de BeamMP Server |
| `BEAMMP_SERVICE` | `beammp` | Unidad systemd del servidor de juego |
| `PANEL_DATA_DIR` | `/opt/beammp-web/data` | Credenciales y mapas personalizados |
| `PANEL_MAX_UPLOAD_MB` | `4096` | Tamaño máximo por mod subido |
| `PANEL_COOKIE_SECURE` | `0` | Pon `1` si el panel se sirve por HTTPS |

Después de cambiar la unidad: `sudo systemctl daemon-reload && sudo systemctl restart beammp-web`.

## Estructura del proyecto

```
app.py              Aplicación Flask: rutas, sesión, CSRF
wsgi.py             Punto de entrada de gunicorn
panel/settings.py   Rutas y opciones (variables de entorno)
panel/auth.py       Credenciales y bloqueo de intentos
panel/server.py     ServerConfig.toml, systemd y lectura de Server.log
panel/mods.py       Mods, detección de mapas y catálogo
templates/, static/ Interfaz web
maps.json           Mapas oficiales
tests/              Pruebas automáticas
install.sh          Instalador y actualizador
```

Datos que el panel crea en `data/` (no se versionan): `panel.json` (usuario, hash de la contraseña y clave de sesión, con permisos `600`) y `custom_maps.json`.

## Desarrollo

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt pytest
python -m pytest tests                                         # pruebas
BEAMMP_DIR=/ruta/a/una/copia PANEL_DATA_DIR=./data python app.py   # servidor local en :5000
```

## Seguridad

* Contraseña guardada con hash PBKDF2-SHA256; nunca en claro.
* Todas las acciones exigen sesión y token CSRF.
* La Auth Key de BeamMP nunca se envía al navegador.
* Solo se puede seleccionar un mapa que esté en el catálogo, y solo se aceptan `.zip` con nombre saneado dentro de `Resources/Client`.
* El panel se ejecuta como `root` porque necesita `systemctl` y escribir en `/opt/beammp`. Trátalo como un acceso de administrador a la máquina.

## Licencia

Este proyecto se distribuye bajo la Licencia MIT. Consulta el archivo `LICENSE` para más detalles.
