"""OTA-обновления: извлечение версии из AndroidManifest APK и проверка обновлений."""
import hashlib
import json
import os
import struct
import time
import zipfile
from typing import Optional, Tuple, Dict, Any

from .config import UPDATES_DIR

__all__ = [
    "get_apk_manifest_version",
    "get_latest_apk_info",
]


def get_apk_manifest_version(apk_path: str) -> Tuple[Optional[int], Optional[str]]:
    """Извлекает реальный versionCode и versionName прямо из бинарного AndroidManifest.xml внутри APK."""
    try:
        with zipfile.ZipFile(apk_path, "r") as zf:
            if "AndroidManifest.xml" not in zf.namelist():
                return None, None
            data = zf.read("AndroidManifest.xml")
        if len(data) < 16:
            return None, None
        chunk_type, chunk_size = struct.unpack("<II", data[8:16])
        if chunk_type != 0x001C0001:
            return None, None
        str_count, _, flags, strings_start, _ = struct.unpack("<IIIII", data[16:36])
        is_utf8 = bool(flags & (1 << 8))
        offsets = struct.unpack(f"<{str_count}I", data[36 : 36 + str_count * 4])
        pool_bytes = data[8 + strings_start :]
        strings = []
        for off in offsets:
            p = off
            if is_utf8:
                if pool_bytes[p] & 0x80:
                    p += 2
                else:
                    p += 1
                if pool_bytes[p] & 0x80:
                    u8len = ((pool_bytes[p] & 0x7F) << 8) | pool_bytes[p + 1]
                    p += 2
                else:
                    u8len = pool_bytes[p]
                    p += 1
                strings.append(pool_bytes[p : p + u8len].decode("utf-8", "replace"))
            else:
                l = struct.unpack("<H", pool_bytes[p : p + 2])[0]
                if l & 0x8000:
                    l = ((l & 0x7FFF) << 16) | struct.unpack("<H", pool_bytes[p + 2 : p + 4])[0]
                    p += 4
                else:
                    p += 2
                strings.append(pool_bytes[p : p + l * 2].decode("utf-16le", "replace"))
        version_code_idx = strings.index("versionCode") if "versionCode" in strings else None
        version_name_idx = strings.index("versionName") if "versionName" in strings else None
        offset = 8 + chunk_size
        v_code, v_name = None, None
        while offset < len(data) - 8:
            tag_type, tag_size = struct.unpack("<II", data[offset : offset + 8])
            if tag_type == 0x00100102:  # START_TAG
                attr_count = struct.unpack("<H", data[offset + 28 : offset + 30])[0]
                attr_offset = offset + 36
                for _ in range(attr_count):
                    _, name, val_str, _, val_data = struct.unpack("<IIIII", data[attr_offset : attr_offset + 20])
                    if name == version_code_idx:
                        v_code = val_data
                    elif name == version_name_idx:
                        if val_str < len(strings):
                            v_name = strings[val_str]
                    attr_offset += 20
                break
            offset += tag_size
            if tag_size == 0:
                break
        return v_code, v_name
    except Exception:
        return None, None


def get_latest_apk_info() -> Optional[Dict[str, Any]]:
    """Проверяет наличие APK в папке updates/ и возвращает версию/дату."""
    if not os.path.exists(UPDATES_DIR):
        os.makedirs(UPDATES_DIR, exist_ok=True)
        return None

    apk_files = [f for f in os.listdir(UPDATES_DIR) if f.lower().endswith(".apk")]
    if not apk_files:
        return None

    # Берём самый свежий APK по дате изменения
    apk_files.sort(key=lambda f: os.path.getmtime(os.path.join(UPDATES_DIR, f)), reverse=True)
    latest = apk_files[0]
    path = os.path.join(UPDATES_DIR, latest)
    mtime = int(os.path.getmtime(path))
    size = os.path.getsize(path)

    with open(path, "rb") as f:
        file_hash = hashlib.sha256(f.read()).hexdigest()

    v_info_path = os.path.join(UPDATES_DIR, "version.json")

    # ВСЕГДА извлекаем настоящие versionCode и versionName напрямую из бинарного AndroidManifest внутри APK!
    parsed_c, parsed_n = get_apk_manifest_version(path)
    if parsed_c is not None and parsed_c > 0:
        v_code = parsed_c
        v_name = parsed_n or f"2.{parsed_c}"
    else:
        # Резервный вариант: если парсер бинарного AXML дал сбой, читаем сохранённый version.json
        v_code = 1
        v_name = "1.0"
        if os.path.exists(v_info_path):
            try:
                with open(v_info_path, "r", encoding="utf-8") as f:
                    v_data = json.load(f)
                    v_code = int(v_data.get("versionCode", 1))
                    v_name = str(v_data.get("versionName", "1.0"))
            except Exception:
                pass

    # Автоматически обновляем version.json с реальными версией, хешем и датой из APK
    try:
        with open(v_info_path, "w", encoding="utf-8") as f:
            json.dump({
                "versionCode": v_code,
                "versionName": v_name,
                "filename": latest,
                "hash": file_hash,
                "size": size,
                "updated": time.strftime("%Y-%m-%d %H:%M:%S")
            }, f, indent=2)
    except Exception:
        pass

    return {
        "filename": latest,
        "path": path,
        "versionCode": v_code,
        "versionName": v_name,
        "hash": file_hash,
        "size": size,
    }
