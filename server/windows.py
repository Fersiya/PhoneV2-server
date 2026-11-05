"""Управление окнами Windows: перечисление, фокус, сворачивание, закрытие, поиск браузера."""
import ctypes
import json
import os
import time
from typing import List, Tuple, Dict, Any, Optional

from .config import IS_WINDOWS, BROWSER_PROCESS, log
from .winapi import _user32, _kernel32, WM_CLOSE
from .input import _send_inputs, _vk_input

if IS_WINDOWS:
    from ctypes import wintypes
    from .winapi import _EnumProc

__all__ = [
    "get_active_window_title",
    "list_open_windows",
    "control_window",
    "focus_browser",
    "_window_process_info",
    "_window_process_name",
    "_window_title",
]

_IGNORED_WINDOW_TITLES = {
    "program manager",
    "microsoft text input application",
    "settings",
    "параметры",
    "windows input experience",
}


def _window_process_info(hwnd) -> Tuple[str, str]:
    if not IS_WINDOWS or not hwnd:
        return "", ""
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = _kernel32.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return "", ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            full_path = buf.value
            return os.path.basename(full_path).lower(), full_path
        return "", ""
    finally:
        _kernel32.CloseHandle(handle)


def _window_process_name(hwnd) -> str:
    return _window_process_info(hwnd)[0]


def _window_title(hwnd) -> str:
    if not IS_WINDOWS or not hwnd:
        return ""
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def get_active_window_title() -> str:
    if not IS_WINDOWS:
        return ""
    try:
        return _window_title(_user32.GetForegroundWindow())
    except Exception:
        return ""


def list_open_windows() -> List[Dict[str, Any]]:
    if not IS_WINDOWS:
        return []
    fg_hwnd = _user32.GetForegroundWindow()
    items = []

    def callback(hwnd, _lparam):
        try:
            if _user32.IsWindowVisible(hwnd):
                title = _window_title(hwnd).strip()
                if title and title.lower() not in _IGNORED_WINDOW_TITLES:
                    proc, exe_path = _window_process_info(hwnd)
                    if proc and proc not in ("searchhost.exe", "shellexperiencehost.exe", "textinputhost.exe"):
                        items.append(
                            {
                                "hwnd": int(hwnd),
                                "title": title,
                                "process": proc,
                                "path": exe_path,
                                "active": bool(hwnd == fg_hwnd),
                                "minimized": bool(_user32.IsIconic(hwnd)),
                            }
                        )
        except Exception:
            pass
        return True

    _user32.EnumWindows(_EnumProc(callback), 0)
    return items[:25]


def control_window(body: str) -> Tuple[int, str]:
    if not IS_WINDOWS:
        return 400, "только для Windows"
    try:
        data = json.loads(body) if body.startswith("{") else {}
        hwnd = int(data.get("hwnd", 0))
        action = str(data.get("action", "focus"))
    except Exception:
        return 400, "некорректные параметры окна"

    if not hwnd:
        return 400, "не указан hwnd"
    try:
        if action == "focus":
            if _user32.IsIconic(hwnd):
                _user32.ShowWindow(hwnd, 9)
            _send_inputs([_vk_input(0x12, False), _vk_input(0x12, True)])
            _user32.SetForegroundWindow(hwnd)
            return 200, "ok"
        if action == "minimize":
            _user32.ShowWindow(hwnd, 6)
            return 200, "ok"
        if action == "maximize":
            if _user32.IsZoomed(hwnd):
                _user32.ShowWindow(hwnd, 9)
            else:
                _user32.ShowWindow(hwnd, 3)
            return 200, "ok"
        if action == "close":
            _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            return 200, "ok"
        return 404, "неизвестное действие"
    except Exception as e:
        return 500, f"ошибка управления окном: {e}"


def _foreground_is_browser() -> bool:
    if not IS_WINDOWS:
        return False
    hwnd = _user32.GetForegroundWindow()
    return bool(hwnd) and _window_process_name(hwnd) == BROWSER_PROCESS


def _find_browser_window() -> Optional[int]:
    if not IS_WINDOWS:
        return None
    candidates = []

    def callback(hwnd, _lparam):
        try:
            if _user32.IsWindowVisible(hwnd):
                title = _window_title(hwnd)
                if title and _window_process_name(hwnd) == BROWSER_PROCESS:
                    candidates.append((hwnd, title.lower()))
        except Exception:
            pass
        return True

    _user32.EnumWindows(_EnumProc(callback), 0)
    if not candidates:
        return None
    for hwnd, title in candidates:
        if "youtube" in title:
            return hwnd
    return candidates[0][0]


def focus_browser() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        if _foreground_is_browser():
            return True
        hwnd = _find_browser_window()
        if not hwnd:
            return False
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, 9)
        _send_inputs([_vk_input(0x12, False), _vk_input(0x12, True)])
        _user32.SetForegroundWindow(hwnd)
        for _ in range(10):
            time.sleep(0.05)
            if _foreground_is_browser():
                return True
        return False
    except Exception as e:
        log(f"[!] focus_browser: {e}")
        return False
