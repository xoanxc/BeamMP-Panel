"""Imágenes de los mapas: ilustraciones incluidas con el panel e imágenes subidas por el usuario."""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

MAX_IMAGE_BYTES = 8 * 1024 * 1024
_FOLDER_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_LEVEL_PATH_RE = re.compile(r"^/levels/([^/]+)/", re.IGNORECASE)
IMAGE_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def level_folder(path: str) -> str | None:
    """'/levels/utah/info.json' -> 'utah'."""
    match = _LEVEL_PATH_RE.match(path or "")
    return match.group(1) if match and _FOLDER_RE.match(match.group(1)) else None


def sniff_image(head: bytes) -> str | None:
    """Extensión según los primeros bytes; no se confía en el nombre ni en Content-Type."""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


class PreviewStore:
    """`builtin_dir` contiene <carpeta>.svg (viene con el panel); `custom_dir`, lo subido."""

    def __init__(self, custom_dir: Path, builtin_dir: Path):
        self.custom_dir = Path(custom_dir)
        self.builtin_dir = Path(builtin_dir)
        self._lock = threading.Lock()
        self._custom: tuple[int, dict[str, tuple[Path, int]]] = (-1, {})
        try:
            self._builtin = {p.stem.lower() for p in self.builtin_dir.glob("*.svg")}
        except OSError:
            self._builtin = set()

    def signature(self) -> int:
        """Cambia cuando se añade o quita una imagen subida."""
        try:
            return self.custom_dir.stat().st_mtime_ns
        except OSError:
            return 0

    def _index(self) -> dict[str, tuple[Path, int]]:
        signature = self.signature()
        with self._lock:
            if self._custom[0] == signature:
                return self._custom[1]
        index = {}
        try:
            with os.scandir(self.custom_dir) as entries:
                for entry in entries:
                    stem, ext = os.path.splitext(entry.name)
                    if ext in IMAGE_TYPES and entry.is_file():
                        index[stem.lower()] = (Path(entry.path), entry.stat().st_mtime_ns)
        except OSError:
            pass
        with self._lock:
            self._custom = (signature, index)
        return index

    def custom(self, folder: str) -> tuple[Path, int] | None:
        """(ruta, versión) de la imagen subida para ese mapa, si existe."""
        return self._index().get((folder or "").lower())

    def has_builtin(self, folder: str) -> bool:
        return (folder or "").lower() in self._builtin

    def save(self, folder: str, data: bytes) -> None:
        if not _FOLDER_RE.match(folder or ""):
            raise ValueError("Mapa no válido.")
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("La imagen supera los 8 MB.")
        ext = sniff_image(data[:16])
        if not ext:
            raise ValueError("Solo se admiten imágenes JPG, PNG o WebP.")
        self.custom_dir.mkdir(parents=True, exist_ok=True)
        self.delete(folder)
        target = self.custom_dir / f"{folder.lower()}{ext}"
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)

    def delete(self, folder: str) -> bool:
        removed = False
        if _FOLDER_RE.match(folder or ""):
            for ext in IMAGE_TYPES:
                path = self.custom_dir / f"{folder.lower()}{ext}"
                if path.is_file():
                    path.unlink()
                    removed = True
        return removed
