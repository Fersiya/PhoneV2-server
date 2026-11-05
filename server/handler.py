"""HTTP-обработчик запросов пульта и класс сервера PcServer."""
import ipaddress
import json
import os
import re
import shutil
import socket
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import (
    PORT,
    TOKEN,
    IS_WINDOWS,
    UPDATES_DIR,
    APP_LIST,
    APPS,
    log,
    reload_config_if_needed,
)
from .auth import check_authorized
from .capture import capture_screen_jpeg
from .clipboard import get_clipboard_text, set_clipboard_text
from .windows import list_open_windows, control_window, get_active_window_title
from .apps import (
    running_processes,
    launch_app,
    close_app,
    add_custom_app,
    delete_custom_app,
    open_url_on_pc,
)
from .power import (
    POWER,
    turn_off_monitor,
    sleep_pc,
    set_custom_timer,
    run_power,
    get_shutdown_remaining_sec,
)
from .system_info import (
    get_cpu_usage_percent,
    get_ram_info,
    get_system_uptime_seconds,
    get_system_disks,
    get_real_windows_volume,
    set_exact_volume,
)
from .files import browse_files, handle_media_upload, is_safe_path
from .ota import get_latest_apk_info
from .network import get_cloudflare_url
from .media_keys import send_media_key
from .input import handle_mouse, handle_type_text

__all__ = [
    "Handler",
    "PcServer",
]


def _is_local_request(client_ip: str) -> bool:
    if not client_ip:
        return False
    if client_ip in ("127.0.0.1", "::1", "localhost"):
        return True
    try:
        ip = ipaddress.ip_address(client_ip)
        return ip.is_private or ip.is_loopback
    except Exception:
        return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60

    def _client_ip(self) -> str:
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1"):  # запрос пришёл через локальный cloudflared
            return self.headers.get("CF-Connecting-IP", ip)
        return ip

    def _authorized(self) -> bool:
        return check_authorized(self._client_ip(), self.headers)

    def _reply(self, code: int, text: str, content_type: str = "text/plain") -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reply_bytes(self, code: int, data: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> str:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return ""
        if length <= 0:
            return ""
        if length > 2 * 1024 * 1024:
            return ""
        return self.rfile.read(min(length, 2 * 1024 * 1024)).decode("utf-8", errors="replace")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        cloudflare_url = get_cloudflare_url()

        # Проверка доступности сервера и валидности токена
        if path == "/ping":
            auth_ok = self._authorized()
            pc_name = socket.gethostname() or "ПК"
            return self._reply(
                200,
                json.dumps({
                    "online": True,
                    "authorized": auth_ok,
                    "pc": pc_name,
                    "tunnel": cloudflare_url
                }, ensure_ascii=False),
                "application/json"
            )

        # Сопряжение в 1 клик для локальной сети Wi-Fi (выдача токена приложению без ручного ввода на ПК)
        if path in ("/api/pair", "/pair"):
            client_ip = self.client_address[0]
            if not _is_local_request(client_ip):
                return self._reply(
                    403,
                    json.dumps({
                        "status": "forbidden",
                        "message": "Сопряжение разрешено только в домашней сети Wi-Fi"
                    }, ensure_ascii=False),
                    "application/json"
                )
            reload_config_if_needed()
            from . import config
            tok = config.TOKEN
            pc_name = socket.gethostname() or "ПК"
            log(f"[Pairing] Телефон ({client_ip}) запросил токен сопряжения. Токен успешно выдан.")
            return self._reply(
                200,
                json.dumps({
                    "status": "ok",
                    "token": tok,
                    "pc": pc_name,
                    "tunnel": cloudflare_url
                }, ensure_ascii=False),
                "application/json"
            )

        if not self._authorized():
            return self._reply(401, "unauthorized")

        # Проверка OTA-обновления для смартфона
        if path == "/update/check":
            info = get_latest_apk_info()
            if not info:
                return self._reply(
                    200,
                    json.dumps({"available": False, "reason": "no_apk_in_folder", "updatesDir": UPDATES_DIR}, ensure_ascii=False),
                    "application/json",
                )
            return self._reply(
                200,
                json.dumps(
                    {
                        "available": True,
                        "versionCode": info["versionCode"],
                        "versionName": info["versionName"],
                        "hash": info.get("hash", ""),
                        "size": info["size"],
                        "filename": info["filename"],
                        "updatesDir": UPDATES_DIR,
                    },
                    ensure_ascii=False,
                ),
                "application/json",
            )

        # Скачивание файла APK для обновления
        if path == "/update/download":
            info = get_latest_apk_info()
            if not info or not os.path.exists(info["path"]):
                return self._reply(404, "обновление не найдено")
            try:
                with open(info["path"], "rb") as f:
                    data = f.read()
                return self._reply_bytes(200, data, "application/vnd.android.package-archive")
            except Exception as e:
                return self._reply(500, f"ошибка чтения APK: {e}")

        # Потоковая трансляция экрана (MJPEG)
        if path == "/stream":
            qs = parse_qs(parsed.query)
            try:
                fps = int(qs.get("fps", ["60"])[0])
                fps = max(15, min(144, fps))
                w = int(qs.get("w", ["720"])[0])
                q = int(qs.get("q", ["48"])[0])
                all_mons = qs.get("m", ["0"])[0] == "all"
            except ValueError:
                fps, w, q, all_mons = 60, 720, 48, False

            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass

            frame_interval = 1.0 / fps
            try:
                while True:
                    t0 = time.monotonic()
                    jpg = capture_screen_jpeg(max_width=w, quality=q, all_monitors=all_mons)
                    if jpg:
                        header = f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n\r\n".encode("ascii")
                        self.wfile.write(header)
                        self.wfile.write(jpg)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    elapsed = time.monotonic() - t0
                    sleep_rem = frame_interval - elapsed
                    if sleep_rem > 0.0005:
                        time.sleep(sleep_rem)
            except (ConnectionResetError, BrokenPipeError, socket.error):
                return

        # Одиночный скриншот экрана
        if path == "/screen":
            qs = parse_qs(parsed.query)
            try:
                w = int(qs.get("w", ["960"])[0])
                q = int(qs.get("q", ["60"])[0])
                all_mons = qs.get("m", ["0"])[0] == "all"
            except ValueError:
                w, q, all_mons = 960, 60, False
            jpg = capture_screen_jpeg(max_width=w, quality=q, all_monitors=all_mons)
            if not jpg:
                return self._reply(500, "не удалось захватить экран")
            return self._reply_bytes(200, jpg, "image/jpeg")

        if path == "/clipboard":
            return self._reply(200, get_clipboard_text())

        if path == "/windows":
            return self._reply(
                200,
                json.dumps({"windows": list_open_windows()}, ensure_ascii=False),
                "application/json",
            )

        if path == "/apps":
            running = running_processes()
            apps = []
            for a in APP_LIST:
                proc = a.get("process")
                apps.append(
                    {
                        "id": a["id"],
                        "title": a["title"],
                        "canClose": bool(proc),
                        "running": bool(proc) and proc.lower() in running,
                    }
                )
            rem_sec = get_shutdown_remaining_sec()
            ram_data = get_ram_info()
            payload = {
                "apps": apps,
                "activeWindow": get_active_window_title(),
                "ramPercent": ram_data["percent"],
                "ramTotalGb": ram_data["totalGb"],
                "ramUsedGb": ram_data["usedGb"],
                "cpuPercent": get_cpu_usage_percent(),
                "uptimeSec": get_system_uptime_seconds(),
                "disks": get_system_disks(),
                "shutdownRemainingSec": rem_sec,
                "shutdownActive": bool(rem_sec > 0),
                "volume": get_real_windows_volume(),
                "tunnelUrl": cloudflare_url,
            }
            return self._reply(
                200,
                json.dumps(payload, ensure_ascii=False),
                "application/json",
            )

        if path == "/files/browse":
            qs = parse_qs(parsed.query)
            target = qs.get("path", [""])[0].strip()
            code, resp_text = browse_files(target)
            return self._reply(code, resp_text, "application/json" if code == 200 else "text/plain")

        if path == "/files/download":
            qs = parse_qs(parsed.query)
            file_path = qs.get("path", [""])[0].strip()
            if not file_path or not os.path.isfile(file_path):
                return self._reply(404, "файл не найден")
            if not is_safe_path(file_path):
                return self._reply(403, "доступ к файлу запрещён")
            try:
                file_size = os.path.getsize(file_path)
                filename = os.path.basename(file_path)
                encoded_name = urllib.parse.quote(filename)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{encoded_name}"')
                self.send_header("Content-Length", str(file_size))
                self.end_headers()
                with open(file_path, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, length=65536)
                return
            except Exception as e:
                return self._reply(500, f"ошибка скачивания: {e}")

        self._reply(404, "not found")

    def do_POST(self) -> None:
        if not self._authorized():
            return self._reply(401, "unauthorized")
        path = urlparse(self.path).path

        # Загрузка фото со смартфона напрямую на диск
        if path == "/upload/media":
            code, msg, data = handle_media_upload(self.headers, self.rfile)
            if code == 200:
                return self._reply(200, json.dumps(data), "application/json")
            return self._reply(code, msg)

        body = self._read_body().strip()

        if path == "/apps/add":
            code, text = add_custom_app(body)
            log(f"apps/add: {code} {text}")
            return self._reply(code, text)

        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "apps" and parts[2] == "delete":
            code, text = delete_custom_app(parts[1])
            log(f"apps/delete {parts[1]}: {code} {text}")
            return self._reply(code, text)

        if path == "/mouse":
            code, text = handle_mouse(body)
            return self._reply(code, text)

        if path == "/type":
            code, text = handle_type_text(body)
            log(f"type ({len(body)} симв.): {code} {text}")
            return self._reply(code, text)

        if path == "/timer":
            try:
                data = json.loads(body)
                if "minutes" not in data:
                    return self._reply(400, "не указано поле minutes")
                mins = int(data["minutes"])
                if not 1 <= mins <= 1440:
                    return self._reply(400, "minutes должно быть от 1 до 1440")
                act = str(data.get("action", "shutdown"))
                if act not in ("shutdown", "sleep"):
                    return self._reply(400, "action: shutdown или sleep")
            except Exception:
                return self._reply(400, "некорректный JSON таймера")
            code, text = set_custom_timer(mins, act)
            log(f"timer ({mins}m, {act}): {code} {text}")
            return self._reply(code, text)

        if path == "/volume":
            code, text = set_exact_volume(body)
            log(f"volume ({body}%): {code} {text}")
            return self._reply(code, text)

        if path == "/windows":
            code, text = control_window(body)
            log(f"window action: {code} {text}")
            return self._reply(code, text)

        if path == "/clipboard":
            ok = set_clipboard_text(body)
            return self._reply(200 if ok else 500, "ok" if ok else "не удалось скопировать в буфер ПК")

        if path == "/monitor_off":
            code, text = turn_off_monitor()
            log(f"monitor_off: {code} {text}")
            return self._reply(code, text)

        if path == "/sleep":
            if not IS_WINDOWS:
                return self._reply(400, "сон поддерживается только на Windows")
            self._reply(200, "ok")
            log("Выполняю переход в режим сна")
            threading.Thread(target=sleep_pc, daemon=True).start()
            return

        if path == "/open-url":
            url = body
            if body.startswith("{"):
                try:
                    url = str(json.loads(body).get("url", "")).strip()
                except Exception:
                    pass
            code, text = open_url_on_pc(url)
            log(f"open-url ({url}): {code} {text}")
            return self._reply(code, text)

        power_entry = POWER.get(path)
        if power_entry is not None:
            if not IS_WINDOWS:
                return self._reply(400, "команды питания поддерживаются только на Windows")
            cmd, timer_sec = power_entry
            self._reply(200, "ok")
            run_power(cmd, timer_sec)
            return

        if len(parts) == 2 and parts[0] == "media":
            code, text = send_media_key(parts[1])
            log(f"media {parts[1]}: {code} {text}")
            return self._reply(code, text)

        if len(parts) == 3 and parts[0] == "apps" and parts[2] in ("launch", "close"):
            app = APPS.get(parts[1])
            if app is None:
                return self._reply(404, "нет такой программы")
            action = launch_app if parts[2] == "launch" else close_app
            code, text = action(app)
            log(f"{parts[2]} {app['id']}: {code} {text}")
            return self._reply(code, text)

        self._reply(404, "not found")

    def log_message(self, fmt, *args) -> None:
        if len(args) > 1 and str(args[1]) == "200":
            if self.command == "GET" and (
                self.path.startswith("/apps")
                or self.path.startswith("/screen")
                or self.path.startswith("/windows")
                or self.path.startswith("/update")
            ):
                return
            if self.command == "POST" and self.path.startswith("/mouse"):
                return
        line = re.sub(r"([?&](?:token|t)=)[^&\s\"]+", r"\1***", fmt % args)
        log(f"{self._client_ip()} - {line}")


class PcServer(ThreadingHTTPServer):
    allow_reuse_address = not IS_WINDOWS

    def server_bind(self):
        if IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        log(f"[!] Ошибка обработчика {client_address[0]}: {traceback.format_exc()}")
