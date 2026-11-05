"""Управление программами: запуск, закрытие, кеш процессов и пользовательский софт."""
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from typing import Set, Tuple, Dict, Any
from urllib.parse import quote_plus

from .config import (
    IS_WINDOWS,
    CREATE_NO_WINDOW,
    DETACHED_FLAGS,
    BRAVE,
    CFG,
    save_config,
    reload_config_if_needed,
    log,
)
from .winapi import (
    _kernel32,
    TH32CS_SNAPPROCESS,
)
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes
    from .winapi import PROCESSENTRY32W

__all__ = [
    "invalidate_process_cache",
    "running_processes",
    "launch_app",
    "close_app",
    "add_custom_app",
    "delete_custom_app",
    "open_url_on_pc",
]

_proc_lock = threading.Lock()
_proc_cache: Set[str] = set()
_proc_cache_ts: float = 0.0
PROCESS_CACHE_TTL: float = 1.5


def invalidate_process_cache() -> None:
    global _proc_cache_ts
    with _proc_lock:
        _proc_cache_ts = 0.0


def running_processes() -> Set[str]:
    global _proc_cache, _proc_cache_ts
    if not IS_WINDOWS:
        return set()

    now = time.monotonic()
    with _proc_lock:
        if now - _proc_cache_ts < PROCESS_CACHE_TTL:
            return set(_proc_cache)

    names: Set[str] = set()
    try:
        h_snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if h_snap and h_snap != wintypes.HANDLE(-1).value:
            try:
                pe = PROCESSENTRY32W()
                pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                if _kernel32.Process32FirstW(h_snap, ctypes.byref(pe)):
                    while True:
                        names.add(pe.szExeFile.lower())
                        if not _kernel32.Process32NextW(h_snap, ctypes.byref(pe)):
                            break
            finally:
                _kernel32.CloseHandle(h_snap)
    except Exception:
        pass

    if names:
        with _proc_lock:
            _proc_cache = names
            _proc_cache_ts = time.monotonic()
    return set(names)


def launch_app(app: dict) -> Tuple[int, str]:
    args = [os.path.expandvars(a) for a in app["launch"]]
    exe = args[0]
    if not os.path.exists(exe):
        found = shutil.which(exe)
        if not found:
            return 404, f"не найден файл: {exe}"
        exe = found
        args[0] = found
    workdir = os.path.dirname(exe) or None
    try:
        subprocess.Popen(args, cwd=workdir, creationflags=DETACHED_FLAGS)
        invalidate_process_cache()
        return 200, "ok"
    except OSError as e:
        return 500, f"не удалось запустить: {e}"


def close_app(app: dict) -> Tuple[int, str]:
    proc = app.get("process")
    if not proc:
        return 400, "это приложение нельзя закрыть"
    cmd = ["taskkill", "/IM", proc]
    if app.get("force"):
        cmd.append("/F")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as e:
        return 500, f"не удалось выполнить taskkill: {e}"
    invalidate_process_cache()
    if result.returncode != 0:
        return 409, "программа не запущена или не закрылась"
    return 200, "ok"


def add_custom_app(body: str) -> Tuple[int, str]:
    """Добавляет новую программу в config.json прямо с телефона (автоматически извлекая имя процесса)."""
    global CFG
    try:
        data = json.loads(body)
    except Exception:
        return 400, "некорректный JSON"

    title = str(data.get("title", "")).strip()
    launch_path = str(data.get("launch", "")).strip()
    process = str(data.get("process", "")).strip()
    force = bool(data.get("force", False))

    if not launch_path and not title:
        return 400, "укажи название или путь к программе"

    # Если указан только путь, название берём из имени файла
    if not title and launch_path:
        base = os.path.basename(launch_path)
        title = os.path.splitext(base)[0].capitalize()

    # Если процесс не указан, автоматически берем exe-файл из пути
    if not process and launch_path:
        base = os.path.basename(launch_path)
        if base.lower().endswith(".exe"):
            process = base

    # Если указано только название (например, Telegram), формируем запуск
    if not launch_path and title:
        launch_path = title if title.lower().endswith(".exe") else f"{title}.exe"
        if not process:
            process = launch_path

    app_id = str(data.get("id", "")).strip()
    if not app_id:
        app_id = "app_" + secrets.token_hex(4)

    new_app = {
        "id": app_id,
        "title": title,
        "launch": [launch_path],
        "process": process or None,
        "force": force,
    }

    from .config import _cfg_lock
    with _cfg_lock:
        existing = [a for a in CFG.get("apps", []) if a.get("id") != app_id]
        existing.append(new_app)
        CFG["apps"] = existing
        save_config(CFG)

    reload_config_if_needed()
    return 200, "ok"


def delete_custom_app(app_id: str) -> Tuple[int, str]:
    """Удаляет программу из config.json."""
    global CFG
    from .config import _cfg_lock
    with _cfg_lock:
        existing = [a for a in CFG.get("apps", []) if a.get("id") != app_id]
        if len(existing) == len(CFG.get("apps", [])):
            return 404, "программа не найдена"
        CFG["apps"] = existing
        save_config(CFG)
    reload_config_if_needed()
    return 200, "ok"


def open_url_on_pc(raw_input: str) -> Tuple[int, str]:
    clean = raw_input.strip()
    if not clean:
        return 400, "пустой запрос"

    if clean.startswith("http://") or clean.startswith("https://"):
        target_url = clean
    elif "." in clean and " " not in clean:
        target_url = "https://" + clean
    else:
        target_url = f"https://www.youtube.com/results?search_query={quote_plus(clean)}"

    brave_exe = os.path.expandvars(BRAVE)
    try:
        if os.path.exists(brave_exe):
            workdir = os.path.dirname(brave_exe) or None
            subprocess.Popen([brave_exe, target_url], cwd=workdir, creationflags=DETACHED_FLAGS)
        elif IS_WINDOWS:
            os.startfile(target_url)  # type: ignore[attr-defined]
        else:
            return 404, "браузер не найден"
        invalidate_process_cache()
        if IS_WINDOWS:
            from .windows import focus_browser
            timer = threading.Timer(2.0, focus_browser)
            timer.daemon = True
            timer.start()
        return 200, "ok"
    except Exception as e:
        return 500, f"не удалось открыть ссылку: {e}"
