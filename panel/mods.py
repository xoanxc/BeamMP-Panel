"""Mods del servidor (Resources/Client): listado, subida, borrado y detección de mapas."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import zipfile
from pathlib import Path
from urllib.parse import quote

from werkzeug.utils import secure_filename

from .previews import PreviewStore, level_folder
from .server import ServerConfig

_CHUNK = 1024 * 1024
_MAX_INFO_BYTES = 1024 * 1024
_MAX_PREVIEW_BYTES = 8 * 1024 * 1024
_PREVIEW_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
_PREVIEW_NAME_RE = re.compile(r"preview.*\.(jpe?g|png)$", re.IGNORECASE)


class ModError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------
# Lectura de info.json (BeamNG acepta JSON "relajado": comentarios y comas finales)
# ---------------------------------------------------------------------
def _parse_relaxed_json(text: str) -> dict:
    for candidate in (
        text,
        re.sub(r",\s*([}\]])", r"\1", re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)),
    ):
        try:
            data = json.loads(candidate)
            return data if isinstance(data, dict) else {}
        except ValueError:
            continue
    # Último recurso: sacar solo el título.
    match = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    return {"title": match.group(1)} if match else {}


def _pretty_name(folder: str) -> str:
    return re.sub(r"[_\-]+", " ", folder).strip().title() or folder


def _level_title(info: dict, folder: str) -> str:
    title = info.get("title")
    if not isinstance(title, str):
        return _pretty_name(folder)
    title = title.strip()
    # Muchos mapas usan una clave de traducción ("levels.foo.info.title") en vez del nombre.
    if not title or (" " not in title and "." in title):
        return _pretty_name(folder)
    return title[:80]


def inspect_zip(path: Path) -> dict:
    """Mira dentro de un .zip y devuelve su tipo y los mapas que contiene.

    Un mapa es una carpeta levels/<nombre>/ con info.json, un .mis o una
    subcarpeta main/. La ruta que espera BeamMP es /levels/<nombre>/info.json.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            levels: dict[str, dict] = {}
            has_vehicles = False
            for name in archive.namelist():
                parts = name.replace("\\", "/").strip("/").split("/")
                if len(parts) < 3:
                    continue
                top = parts[0].lower()
                if top == "vehicles":
                    has_vehicles = True
                elif top == "levels":
                    level = levels.setdefault(parts[1], {"root": {}, "main": False})
                    if len(parts) == 3:
                        level["root"][parts[2].lower()] = name
                    elif parts[2].lower() == "main":
                        level["main"] = True

            found = []
            for folder, level in sorted(levels.items()):
                root = level["root"]
                if not ("info.json" in root or level["main"] or any(f.endswith(".mis") for f in root)):
                    continue  # solo parchea archivos de un mapa existente
                info = {}
                if "info.json" in root:
                    member = archive.getinfo(root["info.json"])
                    if member.file_size <= _MAX_INFO_BYTES:
                        raw = archive.read(member).decode("utf-8-sig", errors="replace")
                        info = _parse_relaxed_json(raw)
                found.append(
                    {
                        "folder": folder,
                        "title": _level_title(info, folder),
                        "path": f"/levels/{folder}/info.json",
                        "preview_member": _find_preview(info, root),
                    }
                )
    except (zipfile.BadZipFile, OSError, KeyError, RuntimeError) as exc:
        return {"kind": "invalid", "levels": [], "error": str(exc)}

    kind = "map" if found else "vehicle" if has_vehicles else "other"
    return {"kind": kind, "levels": found}


def _find_preview(info: dict, root: dict) -> str | None:
    candidates = info.get("previews")
    if isinstance(candidates, list):
        for candidate in candidates:
            if isinstance(candidate, str):
                key = candidate.replace("\\", "/").split("/")[-1].lower()
                if key in root and Path(key).suffix in _PREVIEW_TYPES:
                    return root[key]
    for key in sorted(root):
        if _PREVIEW_NAME_RE.search(key):
            return root[key]
    return None


# ---------------------------------------------------------------------
# Biblioteca de mods
# ---------------------------------------------------------------------
class ModLibrary:
    def __init__(self, beammp_dir: Path, config: ServerConfig, max_upload_bytes: int):
        self.beammp_dir = Path(beammp_dir)
        self.config = config
        self.max_upload_bytes = max_upload_bytes
        self._cache: dict[str, tuple[tuple[int, int], dict]] = {}
        self._listing: tuple[tuple, list[dict]] = ((), [])
        self._lock = threading.Lock()

    @property
    def client_dir(self) -> Path:
        return self.config.resources_dir(self.beammp_dir) / "Client"

    @property
    def disabled_dir(self) -> Path:
        """Mods desactivados: BeamMP solo carga lo que hay en Client."""
        return self.config.resources_dir(self.beammp_dir) / "ClientDisabled"

    def path_of(self, mod: dict) -> Path:
        return (self.client_dir if mod["enabled"] else self.disabled_dir) / mod["name"]

    # -- listado ------------------------------------------------------
    @staticmethod
    def _scan(folder: Path, enabled: bool) -> list[tuple]:
        found = []
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    name = entry.name
                    if name.startswith(".") or not name.lower().endswith(".zip") or not entry.is_file():
                        continue
                    stat = entry.stat()
                    found.append((name, stat.st_size, stat.st_mtime_ns, enabled))
        except OSError:
            pass
        return found

    def snapshot(self) -> tuple[tuple, list[dict]]:
        """(firma, mods). La firma solo cambia si cambia algún .zip de las carpetas.

        Mientras la firma no cambie se devuelve la misma lista, sin volver a
        construirla: quien la reciba no debe modificarla.
        """
        found = self._scan(self.client_dir, True) + self._scan(self.disabled_dir, False)
        found.sort(key=lambda item: (item[0].lower(), not item[3]))
        signature = (str(self.client_dir), *found)
        with self._lock:
            if self._listing[0] == signature:
                return self._listing
        mods = []
        for name, size, mtime_ns, enabled in found:
            path = (self.client_dir if enabled else self.disabled_dir) / name
            mods.append(
                {
                    "name": name,
                    "size": size,
                    "modified": mtime_ns // 1_000_000_000,
                    "enabled": enabled,
                    **self._inspect_cached(path, (size, mtime_ns)),
                }
            )
        with self._lock:
            paths = {str((self.client_dir if item[3] else self.disabled_dir) / item[0]) for item in found}
            for stale in set(self._cache) - paths:
                del self._cache[stale]
            self._listing = (signature, mods)
            return self._listing

    def list(self) -> list[dict]:
        """Todos los .zip (activos y desactivados) con su contenido."""
        return self.snapshot()[1]

    def _inspect_cached(self, path: Path, signature: tuple[int, int]) -> dict:
        key = str(path)
        with self._lock:
            cached = self._cache.get(key)
        if cached and cached[0] == signature:
            return cached[1]
        details = inspect_zip(path)
        with self._lock:
            self._cache[key] = (signature, details)
        return details

    def rescan(self) -> list[dict]:
        with self._lock:
            self._cache.clear()
            self._listing = ((), [])
        return self.list()

    def disk(self) -> dict:
        try:
            target = self.client_dir if self.client_dir.exists() else self.beammp_dir
            usage = shutil.disk_usage(target)
            return {"free": usage.free, "total": usage.total}
        except OSError:
            return {"free": None, "total": None}

    # -- subida -------------------------------------------------------
    @staticmethod
    def clean_name(filename: str) -> str:
        name = secure_filename(filename or "")
        if not name or not name.lower().endswith(".zip") or len(name) <= 4:
            raise ModError("Solo se admiten archivos .zip.")
        return name

    def _existing(self, filename: str) -> Path:
        """Ruta del mod, esté activo o desactivado."""
        name = secure_filename(filename or "")
        if name and name == filename:
            for folder in (self.client_dir, self.disabled_dir):
                if (folder / name).is_file():
                    return folder / name
        raise ModError("Ese mod no existe.", 404)

    def set_enabled(self, filename: str, enabled: bool) -> None:
        """Mueve el mod entre Client y ClientDisabled."""
        source = self._existing(filename)
        target = (self.client_dir if enabled else self.disabled_dir) / source.name
        if source == target:
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        except OSError as exc:
            raise ModError(f"No se ha podido mover el mod: {exc}", 500) from exc

    def save_upload(self, filename: str, stream, content_length: int | None, overwrite: bool = False) -> str:
        """Guarda el cuerpo de la petición directamente en disco, sin pasar por memoria ni /tmp."""
        name = self.clean_name(filename)
        target = self.client_dir / name
        disabled_copy = self.disabled_dir / name
        if (target.exists() or disabled_copy.exists()) and not overwrite:
            raise ModError(f"Ya existe un mod llamado {name}.", 409)
        if content_length is not None and content_length > self.max_upload_bytes:
            raise ModError("El archivo supera el tamaño máximo permitido.", 413)

        self.client_dir.mkdir(parents=True, exist_ok=True)
        free = self.disk()["free"]
        if content_length and free is not None and content_length > free:
            raise ModError("No hay espacio suficiente en disco.", 507)

        partial = self.client_dir / f".{name}.{secrets.token_hex(4)}.part"
        try:
            written = 0
            with open(partial, "wb") as fh:
                while chunk := stream.read(_CHUNK):
                    written += len(chunk)
                    if written > self.max_upload_bytes:
                        raise ModError("El archivo supera el tamaño máximo permitido.", 413)
                    fh.write(chunk)
            if content_length is not None and written != content_length:
                raise ModError("La subida se ha interrumpido. Inténtalo de nuevo.")
            if inspect_zip(partial)["kind"] == "invalid":
                raise ModError("El archivo no es un .zip válido.")
            os.replace(partial, target)
            disabled_copy.unlink(missing_ok=True)  # al sustituirlo queda activo
        finally:
            if partial.exists():
                partial.unlink()
        return name

    # -- borrado y vistas previas -------------------------------------
    def delete(self, filename: str) -> None:
        self._existing(filename).unlink()

    def preview(self, filename: str, folder: str) -> tuple[bytes, str]:
        path = self._existing(filename)
        stat = path.stat()
        details = self._inspect_cached(path, (stat.st_size, stat.st_mtime_ns))
        for level in details["levels"]:
            if level["folder"] == folder and level["preview_member"]:
                member = level["preview_member"]
                with zipfile.ZipFile(path) as archive:
                    if archive.getinfo(member).file_size > _MAX_PREVIEW_BYTES:
                        break
                    return archive.read(member), _PREVIEW_TYPES[Path(member).suffix.lower()]
        raise ModError("Este mapa no tiene imagen de vista previa.", 404)


# ---------------------------------------------------------------------
# Catálogo de mapas
# ---------------------------------------------------------------------
def _read_map_file(path: Path) -> tuple[dict, dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}

    def section(name):
        entries = data.get(name) if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return {}
        return {str(k): v for k, v in entries.items() if isinstance(v, str)}

    return section("normal"), section("mods")


def _file_signature(path: Path) -> tuple[int, int]:
    try:
        stat = Path(path).stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return 0, 0


class MapCatalog:
    """Une tres fuentes: mapas oficiales, mapas añadidos a mano y mapas detectados en los mods.

    El catálogo se reconstruye solo cuando cambia alguna de sus fuentes (los dos
    JSON, los .zip de mods o las imágenes subidas); el resto de consultas usan
    el resultado guardado.
    """

    def __init__(self, official_file: Path, custom_file: Path, mods: ModLibrary, previews: PreviewStore, asset_version: str = ""):
        self.official_file = official_file
        self.custom_file = custom_file
        self.mods = mods
        self.previews = previews
        self.asset_version = asset_version
        self._lock = threading.Lock()
        self._cached: tuple[tuple, dict, dict] | None = None

    # -- consulta -----------------------------------------------------
    def build(self) -> dict:
        """{'official': [...], 'mods': [...]}. No modificar el resultado."""
        return self._current()[0]

    def find(self, path: str) -> dict | None:
        return self._current()[1].get(path)

    def find_folder(self, folder: str) -> dict | None:
        """Mapa del catálogo cuya carpeta de nivel es `folder`."""
        wanted = (folder or "").lower()
        for entry in self._current()[1].values():
            if (entry["folder"] or "").lower() == wanted:
                return entry
        return None

    def name_for(self, path: str) -> str:
        entry = self.find(path)
        return entry["name"] if entry else (path or "Desconocido")

    # -- construcción -------------------------------------------------
    def _current(self) -> tuple[dict, dict]:
        mods_signature, mods = self.mods.snapshot()
        signature = (
            _file_signature(self.official_file),
            _file_signature(self.custom_file),
            mods_signature,
            self.previews.signature(),
        )
        with self._lock:
            if self._cached and self._cached[0] == signature:
                return self._cached[1], self._cached[2]
        catalog = self._build(mods)
        index = {entry["path"]: entry for entry in catalog["mods"] + catalog["official"]}
        with self._lock:
            self._cached = (signature, catalog, index)
        return catalog, index

    def _preview(self, folder: str | None, mod: dict | None = None, in_zip: bool = False) -> dict:
        """Imagen de un mapa: la subida por el usuario, la del .zip o la ilustración incluida."""
        custom = self.previews.custom(folder) if folder else None
        if custom:
            url = f"/api/maps/image/{quote(folder)}?v={custom[1]}"
        elif mod and in_zip:
            url = f"/api/maps/preview?mod={quote(mod['name'])}&folder={quote(folder)}&v={mod['modified']}-{mod['size']}"
        elif folder and self.previews.has_builtin(folder):
            url = f"/static/maps/{folder.lower()}.svg?v={self.asset_version}"
        else:
            url = None
        return {"preview": url, "custom_preview": bool(custom)}

    def _build(self, mods: list[dict]) -> dict:
        official, bundled_mods = _read_map_file(self.official_file)
        custom_official, custom_mods = _read_map_file(self.custom_file)

        official_maps: dict[str, dict] = {}
        for name, path in {**official, **custom_official}.items():
            folder = level_folder(path)
            official_maps.setdefault(
                path.lower(), {"name": name, "path": path, "folder": folder, **self._preview(folder)}
            )

        manual = {**bundled_mods, **custom_mods}
        manual_names = {path.lower(): name for name, path in manual.items()}
        manual_paths = {path.lower(): path for path in manual.values()}

        mod_maps: dict[str, dict] = {}
        for mod in mods:
            if not mod["enabled"]:
                continue  # BeamMP no carga los mods desactivados
            for level in mod["levels"]:
                key = level["path"].lower()
                if key in official_maps or key in mod_maps:
                    continue  # el mod modifica un mapa oficial o está duplicado
                mod_maps[key] = {
                    "name": manual_names.get(key, level["title"]),
                    "path": level["path"],
                    "mod": mod["name"],
                    "folder": level["folder"],
                    "detected": True,
                    **self._preview(level["folder"], mod, bool(level["preview_member"])),
                }
        # Entradas manuales cuyo .zip no se ha encontrado: se muestran, pero avisando.
        for key, path in manual_paths.items():
            if key not in mod_maps and key not in official_maps:
                folder = level_folder(path)
                mod_maps[key] = {
                    "name": manual_names[key],
                    "path": path,
                    "mod": None,
                    "folder": folder,
                    "detected": False,
                    **self._preview(folder),
                }

        return {
            "official": list(official_maps.values()),
            "mods": sorted(mod_maps.values(), key=lambda m: m["name"].lower()),
        }
