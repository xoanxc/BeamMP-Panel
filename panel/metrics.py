"""Uso de CPU, memoria y disco, leído de /proc (sin dependencias). Solo Linux."""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _cpu_times() -> tuple[int, int] | None:
    """(total, inactivo) acumulados del sistema, en ticks."""
    text = _read("/proc/stat")
    if not text or not text.startswith("cpu "):
        return None
    values = [int(v) for v in text.split("\n", 1)[0].split()[1:9]]
    return sum(values), values[3] + (values[4] if len(values) > 4 else 0)


def _process_ticks(pid: int) -> int | None:
    text = _read(f"/proc/{pid}/stat")
    if not text:
        return None
    try:
        fields = text[text.rindex(")") + 2:].split()  # el nombre puede contener espacios
        return int(fields[11]) + int(fields[12])  # utime + stime
    except (ValueError, IndexError):
        return None


def _status_kb(text: str | None, key: str) -> int | None:
    for line in (text or "").splitlines():
        if line.startswith(key + ":"):
            try:
                return int(line.split()[1]) * 1024
            except (ValueError, IndexError):
                return None
    return None


def _percent(part: float, whole: float) -> float | None:
    return round(max(0.0, min(100.0, 100.0 * part / whole)), 1) if whole > 0 else None


class Metrics:
    """Los porcentajes de CPU son la media entre dos consultas (como mínimo `min_interval` s)."""

    def __init__(self, disk_path: Path, min_interval: float = 2.0):
        self.disk_path = Path(disk_path)
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._previous: dict | None = None
        self._result: dict | None = None
        try:
            self._ticks_per_second = os.sysconf("SC_CLK_TCK")
        except (ValueError, OSError, AttributeError):
            self._ticks_per_second = 100

    def sample(self, pid: int | None = None) -> dict:
        with self._lock:
            now = time.monotonic()
            if self._result and self._previous and now - self._previous["time"] < self.min_interval:
                return self._result
            current = {"time": now, "cpu": _cpu_times(), "pid": pid, "ticks": _process_ticks(pid) if pid else None}
            previous, self._previous = self._previous, current

            cpu = process_cpu = None
            if previous and previous["cpu"] and current["cpu"]:
                total = current["cpu"][0] - previous["cpu"][0]
                idle = current["cpu"][1] - previous["cpu"][1]
                cpu = _percent(total - idle, total)
            if previous and pid and previous["pid"] == pid and previous["ticks"] is not None and current["ticks"] is not None:
                seconds = (current["ticks"] - previous["ticks"]) / self._ticks_per_second
                process_cpu = _percent(seconds, (now - previous["time"]) * (os.cpu_count() or 1))

            meminfo = _read("/proc/meminfo")
            mem_total = _status_kb(meminfo, "MemTotal")
            mem_available = _status_kb(meminfo, "MemAvailable")
            try:
                disk = shutil.disk_usage(self.disk_path if self.disk_path.exists() else "/")
                disk_total, disk_used = disk.total, disk.total - disk.free
            except OSError:
                disk_total = disk_used = None

            self._result = {
                "cpu": cpu,
                "mem_total": mem_total,
                "mem_used": mem_total - mem_available if mem_total and mem_available is not None else None,
                "disk_total": disk_total,
                "disk_used": disk_used,
                "process_cpu": process_cpu,
                "process_mem": _status_kb(_read(f"/proc/{pid}/status"), "VmRSS") if pid else None,
            }
            return self._result
