"""Конфигурация сервера: пути, токен, список программ, логирование."""
import json
import os
import secrets
import sys
import threading
import time
from typing import List, Dict, Any

__all__ = [
    "PORT",
    "BASE_DIR",
    "APP_DIR",
    "FROZEN",
    "restart_command",
    "CFG_PATH",
    "LOG_PATH",
    "UPDATES_DIR",
    "MEDIA_DIR",
    "LOG_MAX_BYTES",
    "IS_WINDOWS",
    "CREATE_NO_WINDOW",
    "DETACHED_FLAGS",
    "BRAVE",
    "BROWSER_PROCESS",
    "BROWSER_NAME",
    "detect_default_apps",
    "DEFAULT_APPS",
    "CFG",
    "TOKEN",
    "APPS",
    "APP_LIST",
    "log",
    "save_config",
    "reload_config_if_needed",
]

PORT: int = 8765
# FROZEN — собранный установщиком PCControl.exe: программа лежит в Program Files,
# а настройки, логи и обновления — в %LOCALAPPDATA%\\PCControl (туда можно писать без прав администратора)
FROZEN: bool = bool(getattr(sys, "frozen", False))
APP_DIR: str = (
    os.path.dirname(os.path.abspath(sys.executable)) if FROZEN
    else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if FROZEN:
    BASE_DIR: str = os.path.join(os.environ.get("LOCALAPPDATA", APP_DIR), "PCControl")
    os.makedirs(BASE_DIR, exist_ok=True)
else:
    BASE_DIR = APP_DIR


def restart_command() -> list:
    """Команда для запуска нового экземпляра сервера (из исходников или из установленного exe)."""
    if FROZEN:
        return [sys.executable] + sys.argv[1:]
    return [sys.executable, os.path.join(BASE_DIR, "server.py")] + sys.argv[1:]

CFG_PATH: str = os.path.join(BASE_DIR, "config.json")
LOG_PATH: str = os.path.join(BASE_DIR, "server.log")
UPDATES_DIR: str = os.path.join(BASE_DIR, "updates")

# Папка для сохранения фото/видео со смартфона
# Если диск D: существует на ПК — автоматически сохраняем в D:\Photos, либо по пути из config.json
def _get_media_dir() -> str:
    try:
        if os.path.exists(CFG_PATH):
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                custom = cfg.get("photos_dir")
                if custom:
                    return custom
    except Exception:
        pass
    if not FROZEN and os.path.exists("D:\\"):
        return r"D:\Photos"
    pictures = os.path.join(os.path.expanduser("~"), "Pictures", "PCControl")
    return pictures if FROZEN else os.path.join(BASE_DIR, "photos")

MEDIA_DIR: str = _get_media_dir()
try:
    os.makedirs(MEDIA_DIR, exist_ok=True)
except Exception:
    pass

LOG_MAX_BYTES: int = 200_000
IS_WINDOWS: bool = os.name == "nt"

# Флаги процессов Windows
CREATE_NO_WINDOW: int = 0x08000000 if IS_WINDOWS else 0
DETACHED_FLAGS: int = 0x00000208 if IS_WINDOWS else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

import glob as _glob


def _exp(path: str) -> str:
    return os.path.expandvars(path)


def _first_existing(patterns: List[str]) -> str:
    """Первый существующий путь из списка (поддерживает * для версий)."""
    for pat in patterns:
        p = _exp(pat)
        if "*" in p:
            hits = sorted(_glob.glob(p))
            if hits:
                return hits[-1]
        elif os.path.exists(p):
            return p
    return ""


# Браузеры по приоритету: (id, название, процесс, возможные пути)
_BROWSERS = [
    ("brave", "Brave", "brave.exe", [r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
                                     r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"]),
    ("chrome", "Chrome", "chrome.exe", [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                                        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                                        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"]),
    ("edge", "Edge", "msedge.exe", [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                                    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"]),
    ("firefox", "Firefox", "firefox.exe", [r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
                                           r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe"]),
]


def _detect_browser() -> tuple:
    """(id, название, процесс, путь) первого найденного браузера; без Windows — Brave по умолчанию."""
    if os.name == "nt":
        for bid, name, proc, paths in _BROWSERS:
            found = _first_existing(paths)
            if found:
                return bid, name, proc, found
    return "brave", "Brave", "brave.exe", _BROWSERS[0][3][0]


_BROWSER_ID, BROWSER_NAME, BROWSER_PROCESS, BRAVE = _detect_browser()  # BRAVE — исторически «путь к браузеру»

# Остальные известные программы: (id, название, процесс, force, команда запуска или список путей)
_KNOWN_APPS = [
    ("discord", "Discord", "Discord.exe", True, [r"%LOCALAPPDATA%\Discord\Update.exe", "--processStart", "Discord.exe"], [r"%LOCALAPPDATA%\Discord\Update.exe"]),
    ("telegram", "Telegram", "Telegram.exe", True, None, [r"%APPDATA%\Telegram Desktop\Telegram.exe", r"%ProgramFiles%\Telegram Desktop\Telegram.exe"]),
    ("spotify", "Spotify", "Spotify.exe", True, None, [r"%APPDATA%\Spotify\Spotify.exe"]),
    ("yandex-music", "Яндекс Музыка", "Яндекс Музыка.exe", True, None, [r"%LOCALAPPDATA%\Programs\YandexMusic\Яндекс Музыка.exe"]),
    ("steam", "Steam", "steam.exe", False, None, [r"%ProgramFiles(x86)%\Steam\steam.exe", r"%ProgramFiles%\Steam\steam.exe"]),
    ("vscode", "VS Code", "Code.exe", False, None, [r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe", r"%ProgramFiles%\Microsoft VS Code\Code.exe"]),
    ("obs", "OBS Studio", "obs64.exe", False, None, [r"%ProgramFiles%\obs-studio\bin\64bit\obs64.exe"]),
    ("premiere", "Premiere Pro", "Adobe Premiere Pro.exe", False, None, [r"%ProgramFiles%\Adobe\Adobe Premiere Pro *\Adobe Premiere Pro.exe"]),
    ("aftereffects", "After Effects", "AfterFX.exe", False, None, [r"%ProgramFiles%\Adobe\Adobe After Effects *\Support Files\AfterFX.exe"]),
    ("davinci", "DaVinci Resolve", "Resolve.exe", False, None, [r"%ProgramFiles%\Blackmagic Design\DaVinci Resolve\Resolve.exe"]),
    ("notepad", "Блокнот", "notepad.exe", False, [r"%SystemRoot%\System32\notepad.exe"], [r"%SystemRoot%\System32\notepad.exe"]),
]


def detect_default_apps() -> List[Dict[str, Any]]:
    """Список программ для первого запуска: только то, что реально установлено на этом ПК."""
    apps: List[Dict[str, Any]] = [
        {"id": _BROWSER_ID, "title": BROWSER_NAME, "launch": [BRAVE], "process": BROWSER_PROCESS, "force": False},
        {"id": "youtube", "title": f"YouTube в {BROWSER_NAME}", "launch": [BRAVE, "https://www.youtube.com"], "process": None, "force": False},
    ]
    if os.name != "nt":
        return apps
    for aid, title, proc, force, launch, paths in _KNOWN_APPS:
        found = _first_existing(paths)
        if not found:
            continue
        if launch is None:
            launch_cmd = [found]
        elif len(launch) == 1:
            launch_cmd = [found]
        else:
            launch_cmd = [launch[0], *launch[1:]]
        apps.append({"id": aid, "title": title, "launch": launch_cmd, "process": proc, "force": force})
    return apps


DEFAULT_APPS: List[Dict[str, Any]] = detect_default_apps()

_cfg_lock = threading.Lock()
_cfg_mtime: float = -1.0
CFG: dict = {}
TOKEN: str = ""
APPS: dict = {}
APP_LIST: list = []


def log(msg: str, to_file: bool = True) -> None:
    """Вывод в консоль и в server.log. Не падает при запуске через pythonw.exe."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    if not to_file:
        return
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def save_config(cfg: dict) -> None:
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def reload_config_if_needed() -> None:
    """Подхватывает изменения config.json на лету без перезапуска сервера."""
    global _cfg_mtime, CFG, TOKEN, APPS, APP_LIST
    with _cfg_lock:
        try:
            mtime = os.path.getmtime(CFG_PATH) if os.path.exists(CFG_PATH) else 0.0
        except OSError:
            mtime = 0.0

        if _cfg_mtime == mtime and CFG:
            return

        cfg: dict = {}
        if os.path.exists(CFG_PATH):
            try:
                with open(CFG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                if not isinstance(cfg, dict):
                    raise ValueError("в файле должен быть JSON-объект")
            except Exception as e:
                log(f"[!] Ошибка чтения config.json: {e}")
                _cfg_mtime = mtime
                if CFG:
                    return
                raise SystemExit(
                    f"config.json повреждён ({e}). Исправь его или удали, чтобы создать заново."
                )

        changed = False
        if "token" not in cfg:
            cfg["token"] = secrets.token_urlsafe(24)
            changed = True
        if "apps" not in cfg:
            cfg["apps"] = DEFAULT_APPS
            changed = True
        if changed:
            save_config(cfg)
            try:
                mtime = os.path.getmtime(CFG_PATH)
            except OSError:
                pass

        apps_ok: list = []
        for a in cfg["apps"]:
            if isinstance(a, dict) and a.get("id") and a.get("title") and a.get("launch"):
                apps_ok.append(a)
            else:
                log(f"[!] Пропускаю некорректную запись в apps: {a!r}")

        CFG = cfg
        TOKEN = str(cfg["token"])
        APP_LIST = apps_ok
        APPS = {a["id"]: a for a in apps_ok}
        _cfg_mtime = mtime


reload_config_if_needed()
