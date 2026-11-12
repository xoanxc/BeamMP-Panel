"""API de contenido: mapas, imágenes, mods y ajustes de ServerConfig.toml."""

from __future__ import annotations

from flask import Blueprint, Response, jsonify, request, send_file

from .previews import IMAGE_TYPES, MAX_IMAGE_BYTES
from .server import ServiceError
from .web import ApiError, admin_required, json_body, login_required, services, versioned

bp = Blueprint("api_content", __name__, url_prefix="/api")

# Ajustes de ServerConfig.toml editables desde el panel: clave -> (tipo, mínimo, máximo).
# Para cadenas, el máximo es la longitud. Las claves opcionales solo se muestran
# si ya existen en el archivo (dependen de la versión de BeamMP).
CONFIG_FIELDS = {
    "Name": (str, 1, 250),
    "Description": (str, 0, 1000),
    "Tags": (str, 0, 200),
    "MaxPlayers": (int, 1, 200),
    "MaxCars": (int, 1, 50),
    "Private": (bool, None, None),
    "AllowGuests": (bool, None, None),
    "LogChat": (bool, None, None),
    "Debug": (bool, None, None),
}
OPTIONAL_CONFIG_FIELDS = {"Tags", "AllowGuests", "LogChat", "Debug"}


def validate_config(submitted) -> dict:
    """Comprueba los ajustes enviados y devuelve los valores listos para escribir."""
    if not isinstance(submitted, dict) or not submitted:
        raise ApiError("No hay cambios que guardar.")
    changes = {}
    for key, value in submitted.items():
        if key not in CONFIG_FIELDS:
            raise ApiError(f"Ajuste no permitido: {key}")
        kind, low, high = CONFIG_FIELDS[key]
        if kind is bool:
            if not isinstance(value, bool):
                raise ApiError(f"{key} debe ser verdadero o falso.")
        elif kind is int:
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ApiError(f"{key} debe ser un número entre {low} y {high}.")
        else:
            if not isinstance(value, str):
                raise ApiError(f"{key} debe ser un texto.")
            value = "".join(ch for ch in value if ch.isprintable()).strip()
            if not low <= len(value) <= high:
                raise ApiError(f"{key} debe tener entre {low} y {high} caracteres.")
        changes[key] = value
    return changes


# ---------------------------------------------------------------------
# Mapas
# ---------------------------------------------------------------------
@bp.get("/maps")
@admin_required
def maps():
    s = services()
    return jsonify(**s.catalog.build(), current=s.config.get("Map", ""))


@bp.post("/maps/select")
@admin_required
def select_map():
    s = services()
    path = json_body().get("path")
    entry = s.catalog.find(path) if isinstance(path, str) else None
    if not entry:
        raise ApiError("Ese mapa no está en el catálogo.", 404)
    s.save_config({"Map": entry["path"]})
    try:
        s.service.run("restart")
    except ServiceError as exc:
        raise ApiError(f"Mapa guardado, pero el servidor no se ha reiniciado: {exc}", 502) from exc
    return jsonify(ok=True, name=entry["name"])


@bp.get("/maps/preview")
@login_required
def map_preview():
    image, mimetype = services().mods.preview(request.args.get("mod", ""), request.args.get("folder", ""))
    return versioned(Response(image, mimetype=mimetype))


@bp.get("/maps/image/<folder>")
@login_required
def map_image(folder):
    found = services().previews.custom(folder)
    if not found:
        raise ApiError("Ese mapa no tiene imagen propia.", 404)
    path = found[0]
    return versioned(send_file(path, mimetype=IMAGE_TYPES[path.suffix], conditional=True))


@bp.put("/maps/image/<folder>")
@admin_required
def map_image_upload(folder):
    s = services()
    entry = s.catalog.find_folder(folder)
    if not entry:
        raise ApiError("Ese mapa no está en el catálogo.", 404)
    if (request.content_length or 0) > MAX_IMAGE_BYTES:
        raise ApiError("La imagen supera los 8 MB.", 413)
    try:
        s.previews.save(entry["folder"], request.stream.read(MAX_IMAGE_BYTES + 1))
    except ValueError as exc:
        raise ApiError(str(exc)) from exc
    except OSError as exc:
        raise ApiError(f"No se ha podido guardar la imagen: {exc}", 500) from exc
    return jsonify(ok=True), 201


@bp.delete("/maps/image/<folder>")
@admin_required
def map_image_delete(folder):
    if not services().previews.delete(folder):
        raise ApiError("Ese mapa no tiene imagen propia.", 404)
    return jsonify(ok=True)


# ---------------------------------------------------------------------
# Mods
# ---------------------------------------------------------------------
def _mods_payload(items: list[dict]):
    mods = services().mods
    return jsonify(
        mods=[
            {
                "name": mod["name"],
                "size": mod["size"],
                "modified": mod["modified"],
                "kind": mod["kind"],
                "enabled": mod["enabled"],
                "levels": [level["title"] for level in mod["levels"]],
            }
            for mod in items
        ],
        disk=mods.disk(),
        folder=str(mods.client_dir),
    )


def _guard_map_in_use(filename: str, action: str) -> None:
    s = services()
    current = s.config.get("Map", "")
    for mod in s.mods.list():
        if mod["name"] == filename and mod["enabled"] and any(lv["path"] == current for lv in mod["levels"]):
            raise ApiError(f"Ese mod contiene el mapa en uso. Cambia de mapa antes de {action}.", 409)


@bp.get("/mods")
@admin_required
def mods_list():
    return _mods_payload(services().mods.list())


@bp.post("/mods/rescan")
@admin_required
def mods_rescan():
    return _mods_payload(services().mods.rescan())


@bp.put("/mods/<filename>")
@admin_required
def mod_upload(filename):
    name = services().mods.save_upload(
        filename, request.stream, request.content_length, overwrite=request.args.get("overwrite") == "1"
    )
    return jsonify(ok=True, name=name), 201


@bp.post("/mods/<filename>/enabled")
@admin_required
def mod_enable(filename):
    enabled = json_body().get("enabled")
    if not isinstance(enabled, bool):
        raise ApiError("Petición no válida.")
    if not enabled:
        _guard_map_in_use(filename, "desactivarlo")
    services().mods.set_enabled(filename, enabled)
    return jsonify(ok=True)


@bp.delete("/mods/<filename>")
@admin_required
def mod_delete(filename):
    _guard_map_in_use(filename, "borrarlo")
    services().mods.delete(filename)
    return jsonify(ok=True)


# ---------------------------------------------------------------------
# Ajustes del servidor
# ---------------------------------------------------------------------
@bp.get("/config")
@admin_required
def config_get():
    general = services().config.read().get("General", {})
    values = {
        key: general.get(key, kind())
        for key, (kind, _, _) in CONFIG_FIELDS.items()
        if key in general or key not in OPTIONAL_CONFIG_FIELDS
    }
    return jsonify(values=values)


@bp.post("/config")
@admin_required
def config_save():
    s = services()
    body = json_body()
    s.save_config(validate_config(body.get("values")))
    if body.get("restart"):
        s.service.run("restart")
    return jsonify(ok=True)
