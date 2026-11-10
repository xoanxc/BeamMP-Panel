"""Configuración del panel. Todo se puede cambiar con variables de entorno."""

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    beammp_dir: Path          # Carpeta donde vive BeamMP-Server
    data_dir: Path            # Datos propios del panel (credenciales, mapas personalizados)
    official_maps_file: Path  # Catálogo de mapas oficiales incluido en el repositorio
    service_name: str         # Unidad systemd del servidor de juego
    max_upload_bytes: int     # Tamaño máximo por mod subido
    cookie_secure: bool       # Marcar la cookie de sesión como "Secure" (solo con HTTPS)

    @property
    def config_path(self) -> Path:
        return self.beammp_dir / "ServerConfig.toml"

    @property
    def log_path(self) -> Path:
        return self.beammp_dir / "Server.log"

    @property
    def custom_maps_file(self) -> Path:
        return self.data_dir / "custom_maps.json"


def load_settings() -> Settings:
    env = os.environ.get
    return Settings(
        beammp_dir=Path(env("BEAMMP_DIR", "/opt/beammp")),
        data_dir=Path(env("PANEL_DATA_DIR", str(BASE_DIR / "data"))),
        official_maps_file=BASE_DIR / "maps.json",
        service_name=env("BEAMMP_SERVICE", "beammp"),
        max_upload_bytes=int(env("PANEL_MAX_UPLOAD_MB", "4096")) * 1024 * 1024,
        cookie_secure=env("PANEL_COOKIE_SECURE", "0") == "1",
    )
