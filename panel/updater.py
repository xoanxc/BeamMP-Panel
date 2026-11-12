"""Comprobación y actualización de BeamMP Server desde sus publicaciones en GitHub."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import threading
import time
import urllib.request
from pathlib import Path

RELEASE_API = "https://api.github.com/repos/BeamMP/BeamMP-Server/releases/latest"
DOWNLOAD_PREFIX = "https://github.com/BeamMP/BeamMP-Server/releases/download/"
MAX_BINARY_BYTES = 300 * 1024 * 1024
_CHUNK = 1024 * 1024


class UpdateError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def version_tuple(text: str | None) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text or ""))


def detect_platform() -> tuple[str, str, str]:
    """(distro, versión mayor, arquitectura) con los nombres que usan los binarios de BeamMP."""
    info = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            info[key] = value.strip().strip('"')
    except OSError:
        pass
    machine = platform.machine().lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)
    return info.get("ID", "").lower(), info.get("VERSION_ID", "").split(".")[0], arch


def pick_asset(assets: list[dict], distro: str, version: str, arch: str) -> dict | None:
    """El binario exacto para esta distro; si no existe, el más reciente de la misma distro."""
    usable = [
        a for a in assets
        if isinstance(a.get("name"), str) and "debuginfo" not in a["name"]
        and str(a.get("browser_download_url", "")).startswith(DOWNLOAD_PREFIX)
    ]
    exact = f"BeamMP-Server.{distro}.{version}.{arch}"
    for asset in usable:
        if exact in asset["name"]:
            return asset
    same_distro = [a for a in usable if distro and f".{distro}." in a["name"] and a["name"].endswith(arch)]
    return max(same_distro, key=lambda a: version_tuple(a["name"]), default=None)


def _open_url(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "BeamMP-Panel", "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(request, timeout=30)  # noqa: S310 - solo URL https fijas de GitHub


class ServerUpdater:
    def __init__(self, beammp_dir: Path, service, log, open_url=_open_url, cache_seconds: float = 6 * 3600):
        self.binary = Path(beammp_dir) / "BeamMP-Server"
        self.service = service
        self.log = log
        self._open_url = open_url
        self._cache_seconds = cache_seconds
        self._cached: tuple[float, dict] | None = None
        self._lock = threading.Lock()

    def _latest(self, force: bool) -> dict:
        if not force and self._cached and time.monotonic() - self._cached[0] < self._cache_seconds:
            return self._cached[1]
        try:
            with self._open_url(RELEASE_API) as response:
                release = json.loads(response.read(4 * 1024 * 1024))
            asset = pick_asset(release.get("assets", []), *detect_platform())
            latest = {
                "version": ".".join(map(str, version_tuple(release.get("tag_name")))) or None,
                "asset": asset["name"] if asset else None,
                "url": asset["browser_download_url"] if asset else None,
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - red caída, límite de GitHub, JSON inesperado…
            return {"version": None, "asset": None, "url": None, "error": f"No se ha podido consultar GitHub: {exc}"}
        self._cached = (time.monotonic(), latest)
        return latest

    def check(self, force: bool = False) -> dict:
        latest = self._latest(force)
        current = self.log.server_version()
        newer = bool(latest["version"]) and version_tuple(latest["version"]) > version_tuple(current)
        return {
            "current": current,
            "latest": latest["version"],
            "asset": latest["asset"],
            "error": latest["error"],
            # Si no se conoce la versión instalada, se deja actualizar igualmente.
            "update_available": bool(latest["url"]) and (newer or current is None),
            "installed": self.binary.is_file(),
        }

    def apply(self) -> str:
        """Descarga el binario nuevo, guarda el anterior como BeamMP-Server.old y reinicia."""
        with self._lock:
            latest = self._latest(force=True)
            if latest["error"]:
                raise UpdateError(latest["error"])
            if not latest["url"]:
                raise UpdateError("No hay un binario de BeamMP Server para este sistema.", 404)
            if not self.binary.parent.is_dir():
                raise UpdateError("No se encuentra la carpeta de BeamMP Server.", 404)
            partial = self.binary.with_name("BeamMP-Server.new")
            try:
                written = 0
                with self._open_url(latest["url"]) as response, open(partial, "wb") as fh:
                    while chunk := response.read(_CHUNK):
                        written += len(chunk)
                        if written > MAX_BINARY_BYTES:
                            raise UpdateError("La descarga es demasiado grande para ser el servidor.")
                        fh.write(chunk)
                with open(partial, "rb") as fh:
                    if fh.read(4) != b"\x7fELF":
                        raise UpdateError("El archivo descargado no es un ejecutable de Linux.")
                os.chmod(partial, 0o755)
                if self.binary.is_file():
                    shutil.copy2(self.binary, self.binary.with_name("BeamMP-Server.old"))
                os.replace(partial, self.binary)
            except UpdateError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise UpdateError(f"La actualización ha fallado: {exc}") from exc
            finally:
                partial.unlink(missing_ok=True)
            self._cached = None
        if self.service.status()["state"] in ("online", "starting"):
            self.service.run("restart")
        return latest["version"]
