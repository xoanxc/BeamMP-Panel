"""Copias de seguridad: configuración del servidor, datos del panel y, si se pide, los mods."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
import zipfile
from pathlib import Path

from werkzeug.utils import secure_filename

from . import __version__

_CHUNK = 1024 * 1024
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,96}\.zip$")
_PANEL_FILES = ("custom_maps.json", "schedule.json", "access.json")
_IMAGE_RE = re.compile(r"^[a-z0-9_.\-]{1,64}\.(jpg|png|webp)$")
_MAX_SMALL_FILE = 16 * 1024 * 1024


class BackupError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class BackupManager:
    """Formato del .zip:

    manifest.json            versión del panel, fecha y si incluye mods
    ServerConfig.toml        configuración de BeamMP (¡contiene la Auth Key!)
    panel/<archivo>.json     mapas personalizados, programación, bloqueos y lista blanca
    panel/previews/<imagen>  imágenes de mapas subidas
    mods/<mod>.zip           mods activos        (solo con include_mods)
    mods-disabled/<mod>.zip  mods desactivados   (solo con include_mods)

    Las cuentas del panel (panel.json) no se copian ni se restauran.
    """

    def __init__(self, backups_dir: Path, data_dir: Path, config_path: Path, mods, max_upload_bytes: int):
        self.dir = Path(backups_dir)
        self.data_dir = Path(data_dir)
        self.config_path = Path(config_path)
        self.mods = mods
        self.max_upload_bytes = max_upload_bytes
        self._lock = threading.Lock()  # una copia o restauración cada vez
        self._manifests: dict[str, tuple[tuple[int, int], dict]] = {}

    # -- consulta -----------------------------------------------------
    def path(self, name: str) -> Path:
        path = self.dir / (name or "")
        if not _NAME_RE.match(name or "") or secure_filename(name) != name or not path.is_file():
            raise BackupError("Esa copia no existe.", 404)
        return path

    def _manifest(self, path: Path, stat: os.stat_result) -> dict:
        signature = (stat.st_size, stat.st_mtime_ns)
        cached = self._manifests.get(path.name)
        if cached and cached[0] == signature:
            return cached[1]
        try:
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            manifest = manifest if isinstance(manifest, dict) else {}
        except (zipfile.BadZipFile, KeyError, ValueError, OSError):
            manifest = {}
        self._manifests[path.name] = (signature, manifest)
        return manifest

    def list(self) -> list[dict]:
        items = []
        try:
            entries = [e for e in os.scandir(self.dir) if _NAME_RE.match(e.name) and e.is_file()]
        except OSError:
            entries = []
        for entry in entries:
            stat = entry.stat()
            manifest = self._manifest(Path(entry.path), stat)
            items.append(
                {
                    "name": entry.name,
                    "size": stat.st_size,
                    "created": int(manifest.get("created") or stat.st_mtime),
                    "includes_mods": manifest.get("includes_mods") is True,
                    "valid": bool(manifest),
                }
            )
        return sorted(items, key=lambda item: item["created"], reverse=True)

    def _free_space(self) -> int | None:
        try:
            return shutil.disk_usage(self.dir if self.dir.exists() else self.data_dir).free
        except OSError:
            return None

    # -- crear --------------------------------------------------------
    def create(self, include_mods: bool = False) -> str:
        with self._lock:
            mods = self.mods.list() if include_mods else []
            free = self._free_space()
            if free is not None and sum(m["size"] for m in mods) + _MAX_SMALL_FILE > free:
                raise BackupError("No hay espacio suficiente en disco para la copia.", 507)
            self.dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.dir, 0o700)
            name = time.strftime("copia-%Y%m%d-%H%M%S") + ("-mods" if include_mods else "") + ".zip"
            partial = self.dir / f".{name}.{secrets.token_hex(4)}.part"
            manifest = {"panel_version": __version__, "created": int(time.time()), "includes_mods": include_mods}
            try:
                with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("manifest.json", json.dumps(manifest))
                    if self.config_path.is_file():
                        archive.write(self.config_path, "ServerConfig.toml")
                    for filename in _PANEL_FILES:
                        if (self.data_dir / filename).is_file():
                            archive.write(self.data_dir / filename, f"panel/{filename}")
                    previews = self.data_dir / "previews"
                    if previews.is_dir():
                        for image in sorted(previews.iterdir()):
                            if _IMAGE_RE.match(image.name) and image.is_file():
                                archive.write(image, f"panel/previews/{image.name}")
                    for mod in mods:  # ya están comprimidos: se guardan tal cual
                        folder = "mods" if mod["enabled"] else "mods-disabled"
                        archive.write(self.mods.path_of(mod), f"{folder}/{mod['name']}", zipfile.ZIP_STORED)
                os.replace(partial, self.dir / name)
            except OSError as exc:
                raise BackupError(f"No se ha podido crear la copia: {exc}", 500) from exc
            finally:
                partial.unlink(missing_ok=True)
            return name

    # -- subir y borrar -----------------------------------------------
    def save_upload(self, filename: str, stream, content_length: int | None) -> str:
        name = secure_filename(filename or "")
        if not _NAME_RE.match(name):
            raise BackupError("La copia debe ser un archivo .zip.")
        if (self.dir / name).exists():
            raise BackupError(f"Ya existe una copia llamada {name}.", 409)
        if content_length is not None and content_length > self.max_upload_bytes:
            raise BackupError("El archivo supera el tamaño máximo permitido.", 413)
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        free = self._free_space()
        if content_length and free is not None and content_length > free:
            raise BackupError("No hay espacio suficiente en disco.", 507)
        partial = self.dir / f".{name}.{secrets.token_hex(4)}.part"
        try:
            written = 0
            with open(partial, "wb") as fh:
                while chunk := stream.read(_CHUNK):
                    written += len(chunk)
                    if written > self.max_upload_bytes:
                        raise BackupError("El archivo supera el tamaño máximo permitido.", 413)
                    fh.write(chunk)
            if content_length is not None and written != content_length:
                raise BackupError("La subida se ha interrumpido. Inténtalo de nuevo.")
            if not self._manifest(partial, partial.stat()):
                raise BackupError("El archivo no es una copia de seguridad de este panel.")
            os.replace(partial, self.dir / name)
        finally:
            self._manifests.pop(partial.name, None)
            partial.unlink(missing_ok=True)
        return name

    def delete(self, name: str) -> None:
        self.path(name).unlink()
        self._manifests.pop(name, None)

    # -- restaurar ----------------------------------------------------
    def _target(self, member: str) -> tuple[str, Path] | None:
        """Destino de un archivo de la copia. Solo se aceptan las rutas del formato: nada más se extrae."""
        parts = member.split("/")
        leaf = parts[-1]
        if member == "ServerConfig.toml":
            return "config", self.config_path
        if len(parts) == 2 and parts[0] == "panel" and leaf in _PANEL_FILES:
            return "panel", self.data_dir / leaf
        if len(parts) == 3 and parts[:2] == ["panel", "previews"] and _IMAGE_RE.match(leaf):
            return "images", self.data_dir / "previews" / leaf
        if len(parts) == 2 and parts[0] in ("mods", "mods-disabled") and leaf.lower().endswith(".zip") and secure_filename(leaf) == leaf:
            enabled = parts[0] == "mods"
            return "mods", (self.mods.client_dir if enabled else self.mods.disabled_dir) / leaf
        return None

    def restore(self, name: str) -> dict:
        """Escribe el contenido de la copia sobre lo actual. Los mods que no estén en la copia no se tocan."""
        path = self.path(name)
        with self._lock:
            try:
                archive = zipfile.ZipFile(path)
            except (zipfile.BadZipFile, OSError) as exc:
                raise BackupError("La copia está dañada.") from exc
            with archive:
                if not self._manifest(path, path.stat()):
                    raise BackupError("El archivo no es una copia de seguridad de este panel.")
                plan = []
                for info in archive.infolist():
                    target = None if info.is_dir() else self._target(info.filename)
                    if target:
                        if target[0] != "mods" and info.file_size > _MAX_SMALL_FILE:
                            raise BackupError(f"{info.filename} es demasiado grande para ser legítimo.")
                        plan.append((info, *target))
                free = self._free_space()
                if free is not None and sum(info.file_size for info, _, _ in plan) > free:
                    raise BackupError("No hay espacio suficiente en disco para restaurar.", 507)
                summary = {"config": False, "panel": 0, "images": 0, "mods": 0}
                try:
                    for info, kind, target in plan:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        partial = target.with_name(f".{target.name}.{secrets.token_hex(4)}.part")
                        try:
                            with archive.open(info) as source, open(partial, "wb") as fh:
                                shutil.copyfileobj(source, fh, _CHUNK)
                            if kind == "config" and target.is_file():
                                shutil.copymode(target, partial)
                            if kind == "mods":  # un mod no puede quedar activo y desactivado a la vez
                                for folder in (self.mods.client_dir, self.mods.disabled_dir):
                                    if folder != target.parent:
                                        (folder / target.name).unlink(missing_ok=True)
                            os.replace(partial, target)
                        finally:
                            partial.unlink(missing_ok=True)
                        if kind == "config":
                            summary["config"] = True
                        else:
                            summary[kind] += 1
                except (OSError, zipfile.BadZipFile) as exc:
                    raise BackupError(f"La restauración ha fallado a medias: {exc}", 500) from exc
        return summary
