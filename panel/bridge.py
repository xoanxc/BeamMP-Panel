"""Puente con el servidor en marcha: un plugin Lua mínimo que coopera con el panel.

BeamMP no tiene API remota y, bajo systemd, su consola no es accesible. El panel
instala un plugin en Resources/Server/PanelBridge que, usando solo la API
oficial de BeamMP:

* cada segundo mira si hay una orden en <BeamMP>/panel-bridge/command.txt
  (expulsar, mensaje al chat), la ejecuta y deja el resultado en result.txt;
* escribe en players.txt quién está conectado (ID, nombre, invitado);
* al entrar un jugador (onPlayerAuth) consulta access.txt, que escribe el panel,
  y le deniega la entrada si está bloqueado o no figura en la lista blanca.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

from .server import ServerConfig

PLUGIN_NAME = "PanelBridge"
DEFAULT_KICK_REASON = "Expulsado por un administrador"
MAX_TEXT_LENGTH = 200
PLAYERS_MAX_AGE = 15.0  # el plugin reescribe players.txt cada 5 s como mucho

_PLUGIN_TEMPLATE = """\
-- PanelBridge: puente entre el Panel BeamMP y el servidor.
-- Archivo generado por el panel; se reescribe solo. No lo edites.
local EXCHANGE = __EXCHANGE__
local COMMAND = EXCHANGE .. "/command.txt"
local RESULT = EXCHANGE .. "/result.txt"
local PLAYERS = EXCHANGE .. "/players.txt"
local ACCESS = EXCHANGE .. "/access.txt"
local NOT_LISTED = "Este servidor solo admite a los jugadores de su lista."

local function readAll(path)
    local file = io.open(path, "r")
    if not file then return nil end
    local text = file:read("*a")
    file:close()
    return text
end

local function writeAtomic(path, text)
    local file = io.open(path .. ".tmp", "w")
    if not file then return end
    file:write(text)
    file:close()
    os.rename(path .. ".tmp", path)
end

-- Órdenes del panel ---------------------------------------------------
local function kick(name, reason)
    local found = false
    for id, playerName in pairs(MP.GetPlayers() or {}) do
        if playerName == name then
            MP.DropPlayer(id, reason)
            found = true
        end
    end
    return found
end

local function runCommand()
    local text = readAll(COMMAND)
    if not text then return end
    os.remove(COMMAND)
    local token, action, first, second = text:match("^([^\\t\\n]*)\\t([^\\t\\n]*)\\t([^\\t\\n]*)\\t?([^\\n]*)")
    if not token then return end
    local outcome = "unknown"
    if action == "kick" then
        outcome = kick(first, second) and "ok" or "notfound"
    elseif action == "say" then
        MP.SendChatMessage(-1, first)
        outcome = "ok"
    end
    writeAtomic(RESULT, token .. "\\t" .. outcome .. "\\n")
end

-- Lista de jugadores --------------------------------------------------
local lastPlayers = nil
local ticksSinceWrite = 0

local function reportPlayers()
    local lines = {}
    for id, name in pairs(MP.GetPlayers() or {}) do
        local ok, guest = pcall(MP.IsPlayerGuest, id)
        lines[#lines + 1] = tostring(id) .. "\\t" .. tostring(name) .. "\\t" .. ((ok and guest) and "1" or "0")
    end
    table.sort(lines)
    local text = table.concat(lines, "\\n") .. "\\n"
    ticksSinceWrite = ticksSinceWrite + 1
    -- Se reescribe al cambiar y, si no, cada 5 s: así el panel sabe que el puente sigue vivo.
    if text ~= lastPlayers or ticksSinceWrite >= 5 then
        writeAtomic(PLAYERS, text)
        lastPlayers = text
        ticksSinceWrite = 0
    end
end

function PanelBridgeTick()
    runCommand()
    reportPlayers()
end

-- Bloqueos y lista blanca ---------------------------------------------
function PanelBridgeAuth(name, role, isGuest, identifiers)
    local text = readAll(ACCESS)
    if not text then return end
    local key = tostring(name):lower()
    local whitelist, allowed = false, false
    for line in text:gmatch("[^\\n]+") do
        local kind, value, extra = line:match("^([^\\t]*)\\t([^\\t]*)\\t?(.*)$")
        if kind == "whitelist" then
            whitelist = (value == "1")
        elseif kind == "allow" and value:lower() == key then
            allowed = true
        elseif kind == "ban" and value:lower() == key then
            return extra
        end
    end
    if whitelist and not allowed then return NOT_LISTED end
end

MP.RegisterEvent("PanelBridgeTick", "PanelBridgeTick")
MP.CreateEventTimer("PanelBridgeTick", 1000)
MP.RegisterEvent("onPlayerAuth", "PanelBridgeAuth")
print("PanelBridge listo")
"""


class BridgeError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def clean_text(text: str, default: str = "") -> str:
    """Texto en una sola línea, sin caracteres de control y con longitud acotada."""
    cleaned = "".join(ch for ch in " ".join(str(text or "").split()) if ch.isprintable())
    return cleaned[:MAX_TEXT_LENGTH] or default


def valid_player_name(name) -> bool:
    return isinstance(name, str) and 0 < len(name) <= 64 and name.isprintable() and name == name.strip()


class PlayerBridge:
    def __init__(self, beammp_dir: Path, config: ServerConfig, timeout: float = 5.0):
        self.beammp_dir = Path(beammp_dir)
        self.config = config
        self.timeout = timeout
        self.exchange_dir = self.beammp_dir / "panel-bridge"
        self._lock = threading.Lock()

    @property
    def plugin_path(self) -> Path:
        return self.config.resources_dir(self.beammp_dir) / "Server" / PLUGIN_NAME / "main.lua"

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".new")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def install(self) -> bool:
        """Escribe el plugin si falta o está desactualizado. Devuelve True si lo ha escrito."""
        if not self.config.path.is_file():
            return False  # BeamMP no está instalado aquí: no se crea nada
        exchange = str(self.exchange_dir)
        if any(ord(ch) < 32 for ch in exchange):
            return False
        # Una cadena JSON sin escapes \u es también una cadena Lua válida.
        source = _PLUGIN_TEMPLATE.replace("__EXCHANGE__", json.dumps(exchange, ensure_ascii=False))
        target = self.plugin_path
        try:
            if target.is_file() and target.read_text(encoding="utf-8") == source:
                return False
            self.exchange_dir.mkdir(parents=True, exist_ok=True)
            self._write(target, source)
        except OSError:
            return False
        return True

    # -- estado -------------------------------------------------------
    def players(self) -> list[dict] | None:
        """Jugadores según el servidor, o None si el plugin no está informando."""
        path = self.exchange_dir / "players.txt"
        try:
            if time.time() - path.stat().st_mtime > PLAYERS_MAX_AGE:
                return None
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        players = []
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].isdigit():
                players.append({"id": int(parts[0]), "name": parts[1], "guest": parts[2] == "1"})
        return sorted(players, key=lambda p: p["id"])

    def alive(self) -> bool:
        return self.players() is not None

    def write_access(self, bans: dict[str, str], whitelist_enabled: bool, allowed: list[str]) -> None:
        """Publica bloqueos y lista blanca para el plugin (los lee cada vez que entra alguien)."""
        if not self.config.path.is_file():
            return
        lines = [f"whitelist\t{'1' if whitelist_enabled else '0'}"]
        lines += [f"allow\t{name}" for name in allowed]
        lines += [f"ban\t{name}\t{reason}" for name, reason in bans.items()]
        try:
            self._write(self.exchange_dir / "access.txt", "\n".join(lines) + "\n")
        except OSError:
            pass

    # -- órdenes ------------------------------------------------------
    def kick(self, name: str, reason: str = "") -> None:
        """Expulsa a un jugador por nombre. Espera la confirmación del plugin."""
        if not valid_player_name(name):
            raise BridgeError("Nombre de jugador no válido.", 400)
        outcome = self._send("kick", name, clean_text(reason, DEFAULT_KICK_REASON))
        if outcome == "notfound":
            raise BridgeError(f"{name} ya no está conectado.", 404)
        self._expect_ok(outcome)

    def say(self, message: str) -> None:
        """Envía un mensaje a todos los jugadores por el chat del juego."""
        message = clean_text(message)
        if not message:
            raise BridgeError("Escribe un mensaje.", 400)
        self._expect_ok(self._send("say", message))

    @staticmethod
    def _expect_ok(outcome: str) -> None:
        if outcome != "ok":
            raise BridgeError("El servidor no ha entendido la orden. Reinícialo para actualizar el puente.")

    def _send(self, action: str, *fields: str) -> str:
        command = self.exchange_dir / "command.txt"
        result = self.exchange_dir / "result.txt"
        token = secrets.token_hex(8)
        with self._lock:  # una orden cada vez
            just_installed = self.install()
            try:
                result.unlink(missing_ok=True)
                self._write(command, "\t".join((token, action, *fields)) + "\n")
            except OSError as exc:
                raise BridgeError(f"No se puede escribir la orden para el servidor: {exc}", 500) from exc

            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                time.sleep(0.1)
                try:
                    answer = result.read_text(encoding="utf-8").strip().split("\t")
                except OSError:
                    continue
                if len(answer) == 2 and answer[0] == token:
                    result.unlink(missing_ok=True)
                    return answer[1]
            command.unlink(missing_ok=True)  # que no se ejecute más tarde por sorpresa
        if just_installed:
            raise BridgeError(
                "El puente con el servidor se acaba de instalar. Reinicia el servidor una vez "
                "para activarlo y vuelve a intentarlo.",
                503,
            )
        raise BridgeError(
            "El servidor no responde a la orden. Comprueba que está en marcha; si acabas de "
            "actualizar el panel, reinícialo una vez para cargar el puente.",
            504,
        )
