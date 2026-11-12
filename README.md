# BeamMP Server & Web Control Panel

Instalador y panel web para desplegar y administrar un servidor dedicado de BeamMP en Linux (Debian/Ubuntu). Desde el navegador puedes arrancar, detener y reiniciar el servidor, cambiar de mapa, subir mods, editar los ajustes principales y leer el registro en vivo.

## Qué incluye

* **Acceso con usuario y contraseña.** Por defecto `admin` / `admin`; el panel avisa hasta que la cambies.
* **Resumen en vivo:** estado real del servicio, tiempo encendido, mapa en uso y nombres de los jugadores conectados.
* **Jugadores:** lista exacta (ID e invitados), expulsar, bloquear de forma permanente, lista blanca y mensajes al chat del juego.
* **Recursos:** uso de CPU, memoria y disco de la máquina, y lo que consume BeamMP Server.
* **Reinicios programados y rotación automática de mapas**, con aviso previo por el chat.
* **Activar y desactivar mods** sin borrarlos.
* **Actualizar BeamMP Server** desde el panel.
* **Copias de seguridad** de la configuración y, si quieres, de los mods.
* **Varios usuarios con roles:** administrador y moderador.
* **Mapas:** oficiales y de mods en una sola pantalla, con imagen y buscador. Los mapas de mods se detectan solos.
* **Mods:** subida de `.zip` arrastrando, con barra de progreso, y borrado desde el panel.
* **Ajustes:** nombre, descripción, etiquetas, jugadores máximos, coches por jugador y privacidad, sin editar `ServerConfig.toml` a mano.
* **Registro:** las últimas líneas de `Server.log`; cada 5 segundos se piden solo las líneas nuevas.
* **Interfaz al estilo de los menús de BeamNG.drive**, sin dependencias externas en el navegador: CSS y JavaScript propios, servidos por el panel. Funciona en móvil.

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
2. Ve a **Cuenta** y pon un usuario y una contraseña propios (mínimo 8 caracteres). Tus demás sesiones abiertas se cierran.

Si olvidas la contraseña:

```bash
sudo ./install.sh reset-password
```

Esto crea o restablece el usuario `admin` / `admin` sin tocar nada más.

Tras 5 intentos fallidos desde la misma IP, el acceso queda bloqueado 60 segundos.

> **Importante:** el panel sirve HTTP sin cifrar. Si lo expones a Internet, ponlo detrás de un proxy inverso con HTTPS (Caddy, nginx) y añade `Environment=PANEL_COOKIE_SECURE=1` al servicio, o limita el puerto 5000 a tu IP en el firewall.

## Usuarios y roles

En **Cuenta**, un administrador puede crear más usuarios, cambiarles el rol o la contraseña y borrarlos.

| Rol | Qué puede hacer |
| --- | --- |
| Administrador | Todo |
| Moderador | Ver el resumen, expulsar jugadores y escribir en el chat. No ve mapas, mods, ajustes, registro ni copias |

Siempre tiene que quedar al menos un administrador. Cambiar la contraseña de un usuario cierra sus sesiones. `sudo ./install.sh reset-password` crea (o restablece) el usuario `admin` / `admin` sin borrar a los demás.

## El puente con el servidor (PanelBridge)

BeamMP no ofrece una API remota, así que el panel instala solo un plugin mínimo en `Resources/Server/PanelBridge/main.lua`. Usa únicamente la API oficial de BeamMP y se comunica con el panel mediante archivos en `/opt/beammp/panel-bridge/`:

* ejecuta las órdenes del panel: expulsar (`MP.DropPlayer`) y mensajes al chat (`MP.SendChatMessage`);
* informa de quién está conectado, con su ID y si es invitado;
* cuando un jugador intenta entrar (`onPlayerAuth`), le deniega el acceso si está bloqueado o si la lista blanca está activa y no figura en ella.

> **Después de instalar o actualizar el panel, reinicia el servidor de juego una vez** para que BeamMP cargue el plugin. Mientras el puente no esté activo, el panel lo avisa en la parte superior; la lista de jugadores se deduce entonces de `Server.log`, y expulsar, chat, bloqueos y lista blanca no funcionan.

## Jugadores

* **Expulsar** (Resumen): desconecta al jugador, con un motivo opcional. Puede volver a entrar.
* **Bloquear** (Resumen o pestaña Jugadores): no puede volver a entrar hasta que lo desbloquees. Si está conectado, además se le expulsa.
* **Lista blanca** (pestaña Jugadores): al activarla solo entran los jugadores de la lista; los invitados y el resto son rechazados.
* **Mensaje al chat** (Resumen): lo ven todos los jugadores conectados.

Los nombres son los de la cuenta de BeamMP y no distinguen mayúsculas. Se guardan en `data/access.json`.

## Reinicios programados y rotación de mapas

En **Ajustes → Programación**:

* **Reinicios:** una o varias horas al día (hora de la máquina, que el panel muestra). Minutos antes se avisa por el chat.
* **Rotación:** una lista ordenada de mapas y un intervalo en horas. Al cumplirse, el panel pone el siguiente mapa y reinicia. Por defecto espera a que no haya jugadores; si desactivas esa opción, avisa por el chat y cambia con gente dentro.

Un servidor que hayas detenido a mano no se arranca por la programación. El planificador vive dentro del panel, así que el servicio `beammp-web` tiene que estar en marcha (y con un solo proceso, como lo deja el instalador).

## Actualizar BeamMP Server

**Ajustes → Actualización** compara la versión instalada (la que aparece en `Server.log` al arrancar) con la última publicada en GitHub. Al actualizar se descarga el binario oficial para tu distribución, se comprueba que es un ejecutable, se guarda el anterior como `BeamMP-Server.old` y se reinicia el servidor. La máquina necesita salida a Internet hacia `github.com`.

## Copias de seguridad

**Ajustes → Copias de seguridad** crea un `.zip` en `data/backups/` con `ServerConfig.toml`, los mapas personalizados, las imágenes subidas, la programación, los bloqueos y la lista blanca. Opcionalmente incluye los mods (puede ocupar varios GB). Las copias se pueden descargar, subir, restaurar y borrar.

* **La copia contiene la Auth Key** del servidor: guárdala en un sitio seguro.
* Las cuentas del panel no se copian ni se restauran.
* Al restaurar se sobrescribe lo que haya en la copia; los mods que no estén en ella no se tocan.

## Mapas

### Imágenes

* Los mapas de mods usan la vista previa que trae su `.zip`.
* Los mapas oficiales llevan una ilustración propia del panel (`static/maps/<carpeta>.svg`). Las imágenes del juego son de BeamNG y no se distribuyen con el panel.
* En cualquier mapa, el botón de imagen (esquina superior izquierda del mosaico) permite subir la vista previa que quieras: JPG, PNG o WebP de hasta 8 MB. Por ejemplo, la del juego: está dentro de `BeamNG.drive/content/levels/<mapa>.zip`, en `levels/<mapa>/`. Se guardan en `data/previews/` y las actualizaciones no las tocan.

### Detección automática de mapas de mods

No hace falta registrar los mapas a mano. El panel abre cada `.zip` de `Resources/Client` y busca carpetas `levels/<nombre>/` que sean un mapa completo (con `info.json`, un `.mis` o una subcarpeta `main/`). Por cada una:

* la ruta para BeamMP es `/levels/<nombre>/info.json`;
* el nombre visible sale del campo `title` de `info.json`; si falta o es una clave de traducción, se usa el nombre de la carpeta (`pikes_peak` → «Pikes Peak»);
* la imagen se toma de `previews` en `info.json` o de un archivo `*preview*.jpg/png`.

Los `.zip` que solo modifican archivos de un mapa oficial no se listan como mapa nuevo. El resultado se guarda en memoria y solo se vuelve a leer un `.zip` cuando cambia su tamaño o fecha; el catálogo completo solo se reconstruye cuando cambia alguna de sus fuentes. El botón **Volver a analizar** de la pestaña Mods fuerza la lectura de todos.

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

El interruptor **Activo** de cada mod lo mueve entre `Resources/Client` y `Resources/ClientDisabled`: un mod desactivado no se envía a los jugadores y sus mapas dejan de ofrecerse, pero el archivo se conserva.

El límite por archivo es de 4 GB (variable `PANEL_MAX_UPLOAD_MB`). BeamMP carga los mods al arrancar, así que después de subir o borrar hay que reiniciar el servidor; el panel ofrece el botón. No se puede borrar ni desactivar el mod que contiene el mapa en uso.

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
app.py              Aplicación Flask: sesión, CSRF y páginas
wsgi.py             Punto de entrada de gunicorn
panel/web.py        Servicios compartidos, permisos y errores de las rutas
panel/api_server.py API: estado, jugadores, chat, accesos, registro, programación, actualización, copias, usuarios
panel/api_content.py API: mapas, imágenes, mods y ajustes
panel/settings.py   Rutas y opciones (variables de entorno)
panel/auth.py       Usuarios, roles y bloqueo de intentos
panel/server.py     ServerConfig.toml, systemd y lectura de Server.log
panel/mods.py       Mods, detección de mapas y catálogo
panel/previews.py   Imágenes de mapas (incluidas y subidas)
panel/bridge.py     Plugin PanelBridge: órdenes, lista de jugadores y control de acceso
panel/access.py     Bloqueos y lista blanca
panel/schedule.py   Reinicios programados y rotación de mapas
panel/metrics.py    CPU, memoria y disco
panel/updater.py    Actualización de BeamMP Server
panel/backups.py    Copias de seguridad
templates/, static/ Interfaz web
static/maps/        Ilustraciones de los mapas oficiales
maps.json           Mapas oficiales
tests/              Pruebas automáticas
install.sh          Instalador y actualizador
```

Datos que el panel crea en `data/` (no se versionan): `panel.json` (usuarios, hashes de las contraseñas y clave de sesión, con permisos `600`), `access.json`, `schedule.json`, `backups/` `custom_maps.json` y `previews/` (imágenes de mapas subidas).

## Desarrollo

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt pytest
python -m pytest tests                                         # pruebas
BEAMMP_DIR=/ruta/a/una/copia PANEL_DATA_DIR=./data python app.py   # servidor local en :5000
```

## Seguridad

* Contraseña guardada con hash PBKDF2-SHA256; nunca en claro.
* Todas las acciones exigen sesión y token CSRF, y cada ruta comprueba el rol del usuario.
* Al restaurar una copia solo se extraen las rutas del formato de copia; cualquier otro archivo del `.zip` se ignora.
* La actualización del servidor solo descarga desde las publicaciones oficiales de BeamMP en GitHub.
* La Auth Key de BeamMP nunca se envía al navegador.
* Solo se puede expulsar a un jugador que figure como conectado, y el motivo se limpia antes de pasarlo al servidor.
* Las imágenes de mapas se identifican por su contenido (no por el nombre) y solo se aceptan JPG, PNG y WebP.
* Solo se puede seleccionar un mapa que esté en el catálogo, y solo se aceptan `.zip` con nombre saneado dentro de `Resources/Client`.
* El panel se ejecuta como `root` porque necesita `systemctl` y escribir en `/opt/beammp`. Trátalo como un acceso de administrador a la máquina.

## Licencia

Este proyecto se distribuye bajo la Licencia MIT. Consulta el archivo `LICENSE` para más detalles.
