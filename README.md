# Instalador de BeamMP Server y Panel Web

Este repositorio contiene un script `install.sh` que instala y configura automáticamente el servidor dedicado de BeamMP junto con un sencillo panel web para poder cambiar de mapa cómodamente.

---

## Instalación

1. Clona el repositorio:

```bash
git clone https://github.com/xoanxc/BeamMP-Panel.git
cd ./BeamMP-Panel
chmod +x install.sh
./install.sh
```
2. AuthKey

Durante la instalación se te pedirá tu AuthKey de BeamMP Server.
Puedes obtenerla en: https://keymaster.beammp.com/

3. Listo!

Una vez terminado, el servidor y el panel web estarán corriendo:

- Servidor BeamMP: puerto 30814

- Panel web: puerto 5000

### Configuración de mapas

##### Mapas Vanilla

Ya vienen incluidos en `app.py`:

```python
maps_normal = {
    "Gridmap": "/levels/gridmap/info.json",
    "East Coast USA": "/levels/east_coast_usa/info.json",
    # etc...
}
```

##### Mapas de Mods

Se pueden añadir fácilmente editando `app.py`

```python
maps_mods = {
    "Nürburgring": "/levels/ks_nord/info.json",
    # Añadir más mapas con el mismo formato
}
```

#### Notas

- Durante la instalación, se genera una `secret_key``` única para el panel web, usada por Flask para las sesiones y flash.

- Si ejecutas app.py directamente sin instalar con install.sh, se usa una `secret_key` por defecto `default_dev_key`.