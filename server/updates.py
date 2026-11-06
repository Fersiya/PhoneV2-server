"""Обновления из GitHub Releases.

1) APK приложения: сервер сам скачивает свежий PCControl.apk из релиза репозитория приложения в папку updates/,
   а телефон обновляется через привычное OTA («Проверить обновления на ПК»).
2) Сам сервер: проверяет релиз PhoneV2-server и по кнопке запускает новый установщик.
Репозитории публичные, поэтому токены GitHub не нужны.
"""
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
from typing import Optional, Tuple

from .config import CFG, UPDATES_DIR, FROZEN, DETACHED_FLAGS, log
from .ota import get_apk_manifest_version
from .version import VERSION

__all__ = [
    "APP_REPO",
    "SERVER_REPO",
    "start_github_updates",
    "fetch_latest_apk",
    "check_server_update",
    "install_server_update",
]

APP_REPO = "Fersiya/PhoneV2"
SERVER_REPO = "Fersiya/PhoneV2-server"
CHECK_EVERY_SEC = 6 * 3600

_status = {"app": "", "server": "", "server_latest": "", "server_url": ""}


def _api(repo: str) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"User-Agent": "pccontrol-server", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _download(url: str, dest: str) -> None:
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "pccontrol-server"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
    os.replace(tmp, dest)


def fetch_latest_apk() -> str:
    """Скачивает APK из последнего релиза приложения, если он новее лежащего в updates/. Возвращает сообщение."""
    try:
        rel = _api(APP_REPO)
        asset = next((a for a in rel.get("assets", []) if a["name"].lower().endswith(".apk")), None)
        if not asset:
            return "В релизе приложения нет APK"
        os.makedirs(UPDATES_DIR, exist_ok=True)
        dest = os.path.join(UPDATES_DIR, "app-release.apk")
        tag = rel.get("tag_name", "")
        marker = os.path.join(UPDATES_DIR, "github_release.txt")
        try:
            with open(marker, "r", encoding="utf-8") as f:
                if f.read().strip() == tag and os.path.exists(dest):
                    return f"Приложение {tag} уже скачано"
        except OSError:
            pass
        _download(asset["browser_download_url"], dest)
        # Проверяем, что это настоящий APK, и убираем версию из version.json, чтобы сервер взял её из самого файла
        code, name = get_apk_manifest_version(dest)
        if not code:
            os.remove(dest)
            return "Скачанный файл не похож на APK"
        try:
            vj = os.path.join(UPDATES_DIR, "version.json")
            if os.path.exists(vj):
                os.remove(vj)
        except OSError:
            pass
        with open(marker, "w", encoding="utf-8") as f:
            f.write(tag)
        log(f"[Updates] Скачан APK приложения {tag} (версия {name}, код {code})")
        return f"Скачано обновление приложения {tag}"
    except Exception as e:
        return f"Не удалось проверить обновления приложения: {e}"


def _parse_version(v: str) -> Tuple[int, ...]:
    nums = re.findall(r"\d+", v)
    return tuple(int(x) for x in nums[:3]) or (0,)


def check_server_update() -> Tuple[bool, str]:
    """Возвращает (есть ли новая версия сервера, сообщение)."""
    try:
        rel = _api(SERVER_REPO)
        latest = rel.get("tag_name", "").lstrip("v")
        asset = next((a for a in rel.get("assets", []) if a["name"].lower().endswith("setup.exe")), None)
        _status["server_latest"] = latest
        _status["server_url"] = asset["browser_download_url"] if asset else ""
        if latest and _parse_version(latest) > _parse_version(VERSION) and asset:
            return True, f"Доступна новая версия сервера {latest} (сейчас {VERSION})"
        return False, f"Установлена последняя версия сервера ({VERSION})"
    except Exception as e:
        return False, f"Не удалось проверить обновления сервера: {e}"


def install_server_update() -> str:
    """Скачивает установщик из релиза и запускает его тихо. Работает только в установленной версии."""
    if not FROZEN:
        return "Автообновление сервера работает только в установленной версии (PCControl-Setup.exe)"
    has, msg = check_server_update()
    if not has or not _status["server_url"]:
        return msg
    try:
        dest = os.path.join(tempfile.gettempdir(), "PCControl-Setup.exe")
        _download(_status["server_url"], dest)
        log("[Updates] Запуск установщика новой версии сервера…")
        subprocess.Popen([dest, "/SILENT", "/NORESTART", "/CLOSEAPPLICATIONS"], creationflags=DETACHED_FLAGS)
        return "Устанавливаю обновление, сервер перезапустится…"
    except Exception as e:
        return f"Не удалось обновить сервер: {e}"


def _loop() -> None:
    time.sleep(20)
    while True:
        if CFG.get("auto_update_app", True):
            _status["app"] = fetch_latest_apk()
        has, msg = check_server_update()
        _status["server"] = msg
        if has:
            log(f"[Updates] {msg}")
            if FROZEN and CFG.get("auto_update_server", False):
                install_server_update()
        time.sleep(CHECK_EVERY_SEC)


def start_github_updates() -> None:
    threading.Thread(target=_loop, daemon=True).start()
