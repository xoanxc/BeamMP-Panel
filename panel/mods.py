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

from werkzeug.utils import secure_filename

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
        self._lock = threading.Lock()

    @property
    def client_dir(self) -> Path:
        resources = self.config.get("ResourceFolder", "Resources") or "Resources"
        return self.beammp_dir / str(resources) / "Client"

    # -- listado ------------------------------------------------------
    def list(self) -> list[dict]:
        """Todos los .zip de Resources/Client con su contenido (resultado cacheado)."""
        mods = []
        seen = set()
        try:
            entries = sorted(self.client_dir.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            entries = []
        for entry in entries:
            if entry.suffix.lower() != ".zip" or entry.name.startswith(".") or not entry.is_file():
                continue
            stat = entry.stat()
            seen.add(entry.name)
            details = self._inspect_cached(entry, (stat.st_size, stat.st_mtime_ns))
            mods.append(
                {"name": entry.name, "size": stat.st_size, "modified": int(stat.st_mtime), **details}
            )
        with self._lock:
            for name in set(self._cache) - seen:
                del self._cache[name]
        return mods

    def _inspect_cached(self, path: Path, signature: tuple[int, int]) -> dict:
        with self._lock:
            cached = self._cache.get(path.name)
        if cached and cached[0] == signature:
            return cached[1]
        details = inspect_zip(path)
        with self._lock:
            self._cache[path.name] = (signature, details)
        return details

    def rescan(self) -> list[dict]:
        with self._lock:
            self._cache.clear()
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
        name = secure_filename(filename or "")
        path = self.client_dir / name
        if not name or name != filename or not path.is_file():
            raise ModError("Ese mod no existe.", 404)
        return path

    def save_upload(self, filename: str, stream, content_length: int | None, overwrite: bool = False) -> str:
        """Guarda el cuerpo de la petición directamente en disco, sin pasar por memoria ni /tmp."""
        name = self.clean_name(filename)
        target = self.client_dir / name
        if target.exists() and not overwrite:
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
        finally:
            if partial.exists():
                partial.unlink()
        return name

    # -- borrado y vistas previas -------------------------------------
    def delete(self, filename: str) -> None:
        self._existing(filename).unlink()
        with self._lock:
            self._cache.pop(filename, None)

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


class MapCatalog:
    """Une tres fuentes: mapas oficiales, mapas añadidos a mano y mapas detectados en los mods."""

    def __init__(self, official_file: Path, custom_file: Path, mods: ModLibrary):
        self.official_file = official_file
        self.custom_file = custom_file
        self.mods = mods

    def build(self) -> dict:
        official, bundled_mods = _read_map_file(self.official_file)
        custom_official, custom_mods = _read_map_file(self.custom_file)

        official_maps: dict[str, dict] = {}
        for name, path in {**official, **custom_official}.items():
            official_maps.setdefault(path.lower(), {"name": name, "path": path})

        manual_names = {path.lower(): name for name, path in {**bundled_mods, **custom_mods}.items()}
        manual_paths = {path.lower(): path for path in {**bundled_mods, **custom_mods}.values()}

        mod_maps: dict[str, dict] = {}
        for mod in self.mods.list():
            for level in mod["levels"]:
                key = level["path"].lower()
                if key in official_maps or key in mod_maps:
                    continue  # el mod modifica un mapa oficial o está duplicado
                mod_maps[key] = {
                    "name": manual_names.get(key, level["title"]),
                    "path": level["path"],
                    "mod": mod["name"],
                    "folder": level["folder"],
                    "has_preview": bool(level["preview_member"]),
                    "detected": True,
                }
        # Entradas manuales cuyo .zip no se ha encontrado: se muestran, pero avisando.
        for key, path in manual_paths.items():
            if key not in mod_maps and key not in official_maps:
                mod_maps[key] = {
                    "name": manual_names[key],
                    "path": path,
                    "mod": None,
                    "folder": None,
                    "has_preview": False,
                    "detected": False,
                }

        return {
            "official": list(official_maps.values()),
            "mods": sorted(mod_maps.values(), key=lambda m: m["name"].lower()),
        }

    def find(self, path: str, catalog: dict | None = None) -> dict | None:
        catalog = catalog or self.build()
        for entry in catalog["official"] + catalog["mods"]:
            if entry["path"] == path:
                return entry
        return None

    def name_for(self, path: str, catalog: dict | None = None) -> str:
        entry = self.find(path, catalog)
        return entry["name"] if entry else (path or "Desconocido")
