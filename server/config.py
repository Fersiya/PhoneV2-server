"""Конфигурация сервера: пути, токен, список программ, логирование."""
import json
import os
import secrets
import threading
import time
from typing import List, Dict, Any

__all__ = [
    "PORT",
    "BASE_DIR",
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
BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
    if os.path.exists("D:\\"):
        return r"D:\Photos"
    return os.path.join(BASE_DIR, "photos")

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

BRAVE: str = r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"
BROWSER_PROCESS: str = "brave.exe"

DEFAULT_APPS: List[Dict[str, Any]] = [
    {
        "id": "brave",
        "title": "Brave",
        "launch": [BRAVE],
        "process": "brave.exe",
        "force": False,
    },
    {
        "id": "youtube",
        "title": "YouTube в Brave",
        "launch": [BRAVE, "https://www.youtube.com"],
        "process": None,
        "force": False,
    },
    {
        "id": "discord",
        "title": "Discord",
        "launch": [r"%LOCALAPPDATA%\Discord\Update.exe", "--processStart", "Discord.exe"],
        "process": "Discord.exe",
        "force": True,
    },
    {
        "id": "yandex-music",
        "title": "Яндекс Музыка",
        "launch": [r"%LOCALAPPDATA%\Programs\YandexMusic\Яндекс Музыка.exe"],
        "process": "Яндекс Музыка.exe",
        "force": True,
    },
    {
        "id": "premiere",
        "title": "Premiere Pro",
        "launch": [r"%ProgramFiles%\Adobe\Adobe Premiere Pro 2026\Adobe Premiere Pro.exe"],
        "process": "Adobe Premiere Pro.exe",
        "force": False,
    },
]

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
