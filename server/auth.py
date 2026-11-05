"""Авторизация запросов: проверка токена через HMAC и защита от подбора (rate-limiting)."""
import hmac
import threading
import time
from typing import Dict, Tuple

from .config import TOKEN, reload_config_if_needed, log

__all__ = [
    "check_authorized",
    "check_token",
]

_auth_lock = threading.Lock()
_auth_fails: Dict[str, Tuple[int, float, float]] = {}  # ip -> (число ошибок, блок до, начало окна)


def check_token(headers) -> bool:
    reload_config_if_needed()
    from .config import TOKEN
    given = headers.get("X-Token", "")
    if not given:
        auth_hdr = headers.get("Authorization", "")
        if auth_hdr.lower().startswith("bearer "):
            given = auth_hdr[7:].strip()
    return bool(given and hmac.compare_digest(given.encode("utf-8"), TOKEN.encode("utf-8")))


def check_authorized(client_ip: str, headers) -> bool:
    now = time.time()
    with _auth_lock:
        rec = _auth_fails.get(client_ip)
        if rec and rec[1] > now:
            return False

    ok = check_token(headers)
    with _auth_lock:
        if ok:
            _auth_fails.pop(client_ip, None)
        else:
            cnt, _until, first = _auth_fails.get(client_ip, (0, 0.0, now))
            if now - first > 600:
                cnt, first = 0, now
            cnt += 1
            until = now + 900 if cnt >= 10 else 0.0
            _auth_fails[client_ip] = (cnt, until, first)
            if cnt == 10:
                log(f"[!] {client_ip}: 10 неверных токенов, блокировка на 15 минут")
    return ok
