"""Работа с файлами: файловый браузер ПК, скачивание и загрузка фото с защитой от path traversal."""
import json
import os
import shutil
import time
import urllib.parse
from typing import Tuple, Dict, Any, List

from .config import IS_WINDOWS, MEDIA_DIR, log

__all__ = [
    "MEDIA_EXTS",
    "MEDIA_MAX_BYTES",
    "browse_files",
    "is_safe_path",
    "handle_media_upload",
]

MEDIA_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".dng",
    ".mp4", ".mov", ".mkv", ".3gp", ".avi", ".webm"
}
MEDIA_MAX_BYTES: int = 8 << 30

BLOCKED_SYSTEM_DIRS = [
    r"\windows\system32\config",
    r"\windows\system32\sam",
    r"\windows\system32\security",
]


def is_safe_path(target_path: str) -> bool:
    """Проверяет безопасность пути для предотвращения Path Traversal и доступа к системным секретам."""
    abs_path = os.path.abspath(target_path)
    # Запрещаем доступ к сетевым UNC путям типа \\attacker\share (проверяем и исходную строку: не зависит от ОС)
    if target_path.startswith((r"\\", "//")) or abs_path.startswith((r"\\", "//")):
        return False
    lower_path = abs_path.lower()
    for blocked in BLOCKED_SYSTEM_DIRS:
        if blocked in lower_path:
            return False
    return True


def browse_files(target: str) -> Tuple[int, str]:
    if not target or target == "/":
        target = "D:\\" if (IS_WINDOWS and os.path.exists("D:\\")) else ("C:\\" if IS_WINDOWS else "/")

    target = os.path.abspath(target)
    if not is_safe_path(target) or not os.path.exists(target):
        target = "C:\\" if IS_WINDOWS else "/"

    target = os.path.abspath(target)
    parent = os.path.dirname(target.rstrip("\\/")) if target not in ("C:\\", "D:\\", "E:\\", "F:\\", "/") else ""

    drives: List[str] = []
    if IS_WINDOWS:
        for letter in ["C:", "D:", "E:", "F:", "G:"]:
            if os.path.exists(letter + "\\"):
                drives.append(letter)
    else:
        drives = ["/"]

    entries: List[Dict[str, Any]] = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                try:
                    if entry.name.startswith("$") or entry.name in (
                        "System Volume Information", "pagefile.sys", "hiberfil.sys", "swapfile.sys"
                    ):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                    is_dir = entry.is_dir(follow_symlinks=False)
                    entries.append({
                        "name": entry.name,
                        "isDir": is_dir,
                        "size": stat.st_size if not is_dir else 0,
                        "mtime": int(stat.st_mtime)
                    })
                except Exception:
                    continue
    except PermissionError:
        return 403, "нет доступа к папке"
    except Exception as e:
        return 500, f"ошибка чтения папки: {e}"

    entries.sort(key=lambda x: (not x["isDir"], x["name"].lower()))

    return 200, json.dumps({
        "path": target,
        "parent": parent,
        "drives": drives,
        "entries": entries[:250]
    }, ensure_ascii=False)


def handle_media_upload(headers, rfile) -> Tuple[int, str, Dict[str, Any]]:
    """Обрабатывает загрузку медиа-файлов со смартфона."""
    raw_filename = headers.get("X-Filename", "")
    try:
        filename = urllib.parse.unquote_plus(raw_filename)
    except Exception:
        filename = raw_filename
    filename = os.path.basename(filename).strip()
    if not filename:
        filename = f"photo_{int(time.time())}.jpg"

    # Поддержка кириллицы и латиницы в названии фото
    clean_name = "".join(c for c in filename if c.isalnum() or c in "._- ()").strip()
    if not clean_name:
        clean_name = f"photo_{int(time.time())}.jpg"

    ext_ok = os.path.splitext(clean_name)[1].lower() in MEDIA_EXTS
    try:
        declared = int(headers.get("Content-Length", 0) or 0)
    except ValueError:
        declared = 0
    os.makedirs(MEDIA_DIR, exist_ok=True)
    if not ext_ok or declared > MEDIA_MAX_BYTES or (
        declared and shutil.disk_usage(MEDIA_DIR).free < declared + (1 << 30)
    ):
        return 413 if ext_ok else 415, "файл не принят: тип, размер или нет места", {}

    dest_path = os.path.join(MEDIA_DIR, clean_name)
    base_part, ext_part = os.path.splitext(clean_name)
    counter = 1
    while os.path.exists(dest_path):
        dest_path = os.path.join(MEDIA_DIR, f"{base_part}_{counter}{ext_part}")
        counter += 1

    is_chunked = "chunked" in headers.get("Transfer-Encoding", "").lower()
    try:
        content_length = int(headers.get("Content-Length", 0))
    except ValueError:
        content_length = 0

    temp_path = dest_path + f"_{os.getpid()}_{int(time.time()*1000)}.part"
    try:
        bytes_received = 0
        with open(temp_path, "wb") as out_f:
            if is_chunked:
                while True:
                    line = rfile.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        chunk_len = int(line, 16)
                    except ValueError:
                        break
                    if chunk_len == 0:
                        rfile.readline()
                        break
                    if chunk_len > (64 << 20) or bytes_received + chunk_len > MEDIA_MAX_BYTES:
                        raise ValueError("слишком большой блок")
                    data = rfile.read(chunk_len)
                    out_f.write(data)
                    bytes_received += len(data)
                    rfile.readline()
            else:
                remaining = content_length
                chunk_size = 65536
                while remaining > 0:
                    chunk = rfile.read(min(remaining, chunk_size))
                    if not chunk:
                        break
                    out_f.write(chunk)
                    bytes_received += len(chunk)
                    remaining -= len(chunk)

        if content_length > 0 and bytes_received < content_length:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return 400, "файл передан не полностью", {}

        if os.path.exists(temp_path):
            if os.path.exists(dest_path):
                try:
                    os.remove(dest_path)
                except Exception:
                    pass
            os.replace(temp_path, dest_path)

        file_size = os.path.getsize(dest_path)
        size_mb = round(file_size / (1024 * 1024), 2)
        log(f"[+] Новое фото сохранено на ПК: {os.path.basename(dest_path)} ({size_mb} МБ)")
        return 200, "ok", {
            "status": "ok",
            "filename": os.path.basename(dest_path),
            "size": file_size
        }
    except Exception as e:
        log(f"Ошибка сохранения фото: {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return 500, f"ошибка сохранения: {e}", {}
