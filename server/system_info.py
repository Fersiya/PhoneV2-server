"""Телеметрия системы: процессор, память, аптайм, диски и уровень звука."""
import ctypes
import os
import shutil
import threading
import time
from typing import Dict, List, Any, Tuple
from .config import IS_WINDOWS
from .winapi import (
    _kernel32,
    FILETIME,
    MEMORYSTATUSEX,
)
from .input import _send_inputs, _vk_input

__all__ = [
    "get_cpu_usage_percent",
    "get_ram_info",
    "get_system_uptime_seconds",
    "get_system_disks",
    "get_real_windows_volume",
    "set_exact_volume",
]

_last_idle: int = 0
_last_kernel: int = 0
_last_user: int = 0
_last_cpu_percent: int = 0
_last_cpu_time: float = 0.0
_cpu_lock = threading.Lock()


def get_cpu_usage_percent() -> int:
    global _last_idle, _last_kernel, _last_user, _last_cpu_percent, _last_cpu_time
    if not IS_WINDOWS:
        return 12
    with _cpu_lock:
        now = time.monotonic()
        if now - _last_cpu_time < 0.8:
            return _last_cpu_percent

        try:
            idle = FILETIME()
            kernel = FILETIME()
            user = FILETIME()
            if not _kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                return _last_cpu_percent

            i = (idle.dwHighDateTime << 32) | idle.dwLowDateTime
            k = (kernel.dwHighDateTime << 32) | kernel.dwLowDateTime
            u = (user.dwHighDateTime << 32) | user.dwLowDateTime

            if _last_idle > 0:
                idle_diff = i - _last_idle
                kernel_diff = k - _last_kernel
                user_diff = u - _last_user
                total = kernel_diff + user_diff
                if total > 0:
                    _last_cpu_percent = max(0, min(100, round((1.0 - (idle_diff / float(total))) * 100)))

            _last_idle, _last_kernel, _last_user = i, k, u
            _last_cpu_time = now
        except Exception:
            pass
        return _last_cpu_percent


def get_ram_info() -> Dict[str, Any]:
    if not IS_WINDOWS:
        return {"percent": 40, "totalGb": 16.0, "usedGb": 6.4}
    try:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if _kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            total_gb = round(stat.ullTotalPhys / (1024**3), 1)
            used_gb = round((stat.ullTotalPhys - stat.ullAvailPhys) / (1024**3), 1)
            return {
                "percent": int(stat.dwMemoryLoad),
                "totalGb": total_gb,
                "usedGb": used_gb,
            }
    except Exception:
        pass
    return {"percent": 0, "totalGb": 0.0, "usedGb": 0.0}


def get_system_uptime_seconds() -> int:
    if not IS_WINDOWS:
        return 7200
    try:
        return int(_kernel32.GetTickCount64() // 1000)
    except Exception:
        return 0


def get_system_disks() -> List[Dict[str, Any]]:
    """Возвращает список смонтированных дисков (исключая флоппи A: и B:)."""
    disks: List[Dict[str, Any]] = []
    if IS_WINDOWS:
        drives = []
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            if os.path.exists(f"{letter}:\\"):
                drives.append(f"{letter}:")
    else:
        drives = ["/"]

    for d in drives:
        path = f"{d}\\" if IS_WINDOWS else d
        try:
            total, used, free = shutil.disk_usage(path)
            total_gb = round(total / (1024**3), 1)
            free_gb = round(free / (1024**3), 1)
            pct = round((used / float(total)) * 100) if total > 0 else 0
            disks.append({
                "drive": d,
                "totalGb": total_gb,
                "freeGb": free_gb,
                "usedPercent": pct,
            })
        except Exception:
            pass
    return disks


_current_volume: int = 50


def get_real_windows_volume() -> int:
    global _current_volume
    if not IS_WINDOWS:
        return _current_volume
    try:
        from ctypes import wintypes
        vol = wintypes.DWORD()
        res = ctypes.windll.winmm.waveOutGetVolume(0, ctypes.byref(vol))
        if res == 0 and vol.value > 0:
            left = vol.value & 0xFFFF
            return max(0, min(100, round((left / 65535.0) * 100)))
    except Exception:
        pass
    return _current_volume


def set_exact_volume(body: str) -> Tuple[int, str]:
    global _current_volume
    if not IS_WINDOWS:
        return 400, "только для Windows"
    try:
        level = max(0, min(100, int(body.strip())))
    except ValueError:
        return 400, "уровень громкости должен быть числом 0..100"
    try:
        events = []
        for _ in range(50):
            events.append(_vk_input(0xAE, False))
            events.append(_vk_input(0xAE, True))
        steps_up = round(level / 2.0)
        for _ in range(steps_up):
            events.append(_vk_input(0xAF, False))
            events.append(_vk_input(0xAF, True))
        _send_inputs(events)
        _current_volume = level
        return 200, "ok"
    except Exception as e:
        return 500, f"не удалось выставить громкость: {e}"
