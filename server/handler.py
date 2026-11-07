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
from .audio import AudioConverter, acquire_audio, audio_info, release_audio
from .capture import acquire_hub, capture_screen_jpeg, release_hub, set_stream_view
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
from .network import (
    get_network_info,
    get_pc_mac,
    get_tailscale_ip,
)
from .media_keys import send_media_key
from .input import handle_mouse, handle_type_text

__all__ = [
    "Handler",
    "PcServer",
    "is_valid_host_header",
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


_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def is_valid_host_header(host_hdr: str, client_ip: str = "") -> bool:
    """Защита от атак DNS Rebinding: проверка допустимости заголовка Host."""
    host_hdr = (host_hdr or "").strip()
    if not host_hdr:
        return _is_local_request(client_ip)

    if host_hdr.startswith("["):
        bracket_end = host_hdr.find("]")
        if bracket_end != -1:
            host = host_hdr[1:bracket_end]
        else:
            host = host_hdr
    else:
        host = host_hdr.split(":")[0].strip()

    host_lower = host.lower()
    if host_lower in ("localhost", "127.0.0.1", "::1"):
        return True
    if host_lower.endswith((".trycloudflare.com", ".ts.net", ".local")):
        return True

    try:
        ip = ipaddress.ip_address(host)
        # 100.64.0.0/10 — адреса Tailscale (Python не считает их private)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip in _TAILSCALE_NET:
            return True
    except ValueError:
        pass

    try:
        local_hostname = socket.gethostname().lower()
        if host_lower == local_hostname or host_lower == f"{local_hostname}.local":
            return True
    except Exception:
        pass

    return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60

    def _via_proxy(self) -> bool:
        """True, если запрос пришёл через туннель/прокси (а не напрямую из локальной сети)."""
        h = self.headers
        if any(h.get(name) for name in ("CF-Connecting-IP", "CF-Ray", "X-Forwarded-For", "X-Real-IP", "Forwarded")):
            return True
        return (h.get("Host", "") or "").lower().split(":")[0].endswith(".trycloudflare.com")

    def _client_ip(self) -> str:
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1"):  # запрос пришёл через локальный cloudflared
            return self.headers.get("CF-Connecting-IP", ip)
        return ip

    def _is_valid_host(self) -> bool:
        return is_valid_host_header(self.headers.get("Host", ""), self.client_address[0])

    def _authorized(self) -> bool:
        return check_authorized(self._client_ip(), self.headers)

    # Через обратный прокси закрываем соединение после каждого ответа: прокси держит пул
    # соединений, и если сервер закрыл простаивающее, пользователь видит 502 / долгие таймауты.
    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # кадры стрима уходят без задержки Nagle
        except Exception:
            pass

    def send_response(self, code, message=None):
        self._conn_set = False
        super().send_response(code, message)

    def send_header(self, keyword, value):
        if keyword.lower() == "connection":
            self._conn_set = True
            if self._via_proxy():
                value = "close"
        super().send_header(keyword, value)

    def end_headers(self):
        if not getattr(self, "_conn_set", False) and self._via_proxy():
            super().send_header("Connection", "close")
        super().end_headers()

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
        if not self._is_valid_host():
            return self._reply(400, "Invalid Host header (DNS rebinding protection)\n")
        parsed = urlparse(self.path)
        path = parsed.path

        # Проверка доступности сервера и валидности токена
        if path == "/ping":
            auth_ok = self._authorized()
            pc_name = socket.gethostname() or "ПК"
            return self._reply(
                200,
                json.dumps({
                    "online": True,
                    "authorized": auth_ok,
                    "pc": pc_name
                }, ensure_ascii=False),
                "application/json"
            )

        if path == "/api/show_window":
            # Повторный запуск программы просит уже работающий экземпляр показать окно (только с этого же ПК)
            if self.client_address[0] in ("127.0.0.1", "::1") and not self._via_proxy():
                from .tray import show_status_dialog
                show_status_dialog()
                return self._reply(200, "ok\n")
            return self._reply(403, "forbidden\n")

        if path in ("/api/network", "/api/tunnel"):  # /api/tunnel — для старых версий приложения
            info = get_network_info()
            info.update({"running": False, "url": "", "installed": False})
            return self._reply(200, json.dumps(info, ensure_ascii=False), "application/json")

        # Сопряжение в 1 клик для локальной сети Wi-Fi (выдача токена приложению без ручного ввода на ПК)
        if path in ("/api/pair", "/pair"):
            client_ip = self.client_address[0]
            # Запросы из Cloudflare-туннеля приходят с 127.0.0.1, поэтому смотрим на заголовки прокси
            if self._via_proxy() or not _is_local_request(client_ip):
                return self._reply(
                    403,
                    json.dumps({
                        "status": "forbidden",
                        "message": "Сопряжение разрешено только в домашней сети Wi-Fi"
                    }, ensure_ascii=False),
                    "application/json"
                )
            from .auth import is_pairing_open, close_pairing
            if not is_pairing_open():
                log(f"[Pairing] {client_ip} просил токен, но сопряжение закрыто")
                return self._reply(
                    403,
                    json.dumps({
                        "status": "closed",
                        "message": "Сопряжение закрыто. На ПК дважды щёлкните значок «Пульт ПК» у часов и повторите"
                    }, ensure_ascii=False),
                    "application/json"
                )
            reload_config_if_needed()
            from . import config
            tok = config.TOKEN
            pc_name = socket.gethostname() or "ПК"
            log(f"[Pairing] Телефон ({client_ip}) запросил токен сопряжения. Токен успешно выдан.")
            close_pairing()
            if not config.CFG.get("paired"):
                config.CFG["paired"] = True
                config.save_config(config.CFG)
            return self._reply(
                200,
                json.dumps({
                    "status": "ok",
                    "token": tok,
                    "pc": pc_name,
                    "mac": get_pc_mac(self.connection.getsockname()[0]),
                    "tailscale": get_tailscale_ip()
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

            hub = acquire_hub(max(320, min(1920, w)), max(20, min(95, q)), all_mons, fps)
            if "x0" in qs:  # приложение сразу говорит, какую область экрана показывать
                try:
                    hub.set_view(*(float(qs[k][0]) for k in ("x0", "y0", "x1", "y1")))
                except (KeyError, ValueError):
                    pass
            last = -1
            try:
                while True:
                    seq, jpg, vw = hub.wait_frame_view(last)
                    if seq == last or not jpg:
                        continue
                    last = seq
                    header = (f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n"
                              f"X-View: {vw[0]:.5f},{vw[1]:.5f},{vw[2]:.5f},{vw[3]:.5f}\r\n\r\n").encode("ascii")
                    self.wfile.write(header + jpg + b"\r\n")
                    self.wfile.flush()
            except (ConnectionResetError, BrokenPipeError, socket.error):
                return
            finally:
                release_hub(hub)

        if path == "/api/audio_info":
            return self._reply(200, json.dumps(audio_info(), ensure_ascii=False), "application/json")

        # Звук ПК: сырой PCM 16 бит; mode=remote — лёгкий (моно, ~24 кГц)
        if path == "/audio":
            qs = parse_qs(parsed.query)
            light = qs.get("mode", ["lan"])[0] == "remote"
            ahub = acquire_audio()
            try:
                if not ahub.wait_ready():
                    return self._reply(503, "звук недоступен: " + (ahub.error or "нет устройства вывода"))
                conv = AudioConverter(ahub.rate, ahub.channels, light)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("X-Audio-Rate", str(conv.out_rate))
                self.send_header("X-Audio-Channels", str(conv.out_channels))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except Exception:
                    pass
                nxt = 0
                silence = bytes(conv.out_rate // 20 * conv.out_channels * 2)  # 50 мс тишины, когда ПК молчит
                while ahub.alive:
                    chunks, nxt2 = ahub.next_chunks(nxt)
                    if chunks:
                        nxt = nxt2
                        self.wfile.write(b"".join(conv.convert(c) for c in chunks))
                    else:
                        self.wfile.write(silence)
                    self.wfile.flush()
            except (ConnectionResetError, BrokenPipeError, socket.error):
                return
            finally:
                release_audio(ahub)
            return

        # Приближение: какую часть экрана отдавать в потоке (x0,y0,x1,y1 — доли 0..1)
        if path == "/api/view":
            qs = parse_qs(parsed.query)
            try:
                x0, y0, x1, y1 = (float(qs.get(k, [d])[0]) for k, d in (("x0", "0"), ("y0", "0"), ("x1", "1"), ("y1", "1")))
            except ValueError:
                return self._reply(400, "bad view")
            set_stream_view(x0, y0, x1, y1)
            return self._reply(200, "ok")

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
        if not self._is_valid_host():
            return self._reply(400, "Invalid Host header (DNS rebinding protection)\n")
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
