"""Сетевые сервисы: UPnP, Cloudflare туннель, UDP мышь/геймпад, Wi-Fi discovery и автоперезапуск."""
import hashlib
import hmac
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import List, Optional

from .config import (
    PORT,
    TOKEN,
    BASE_DIR,
    APP_LIST,
    IS_WINDOWS,
    DETACHED_FLAGS,
    log,
)
from .input import (
    _send_inputs,
    _mouse_input,
    _key_input,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_WHEEL,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    KEYEVENTF_KEYUP,
)

__all__ = [
    "get_local_ips",
    "try_upnp_port_forward",
    "find_cloudflared_path",
    "start_network_services",
    "get_cloudflare_url",
    "check_paths",
]

_cloudflare_url: str = ""
_cloudflare_proc = None


def get_cloudflare_url() -> str:
    return _cloudflare_url


def check_paths() -> None:
    for a in APP_LIST:
        exe = os.path.expandvars(a["launch"][0])
        if not os.path.exists(exe):
            log(f"[!] {a['title']}: не найден файл {exe}. Поправь путь в config.json")


def get_local_ips() -> List[str]:
    ips: List[str] = []
    try:
        _, _, host_ips = socket.gethostbyname_ex(socket.gethostname())
        for ip in host_ips:
            if not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return ips


def try_upnp_port_forward(port: int = PORT) -> Optional[str]:
    """Автоматический проброс порта на домашнем роутере через стандартный UPnP (без сторонних библиотек)."""
    try:
        ssdp_request = (
            'M-SEARCH * HTTP/1.1\r\n'
            'HOST: 239.255.255.250:1900\r\n'
            'MAN: "ssdp:discover"\r\n'
            'MX: 2\r\n'
            'ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n'
        ).encode('utf-8')

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2.0)
        s.sendto(ssdp_request, ("239.255.255.250", 1900))
        data, _ = s.recvfrom(2048)
        s.close()

        resp = data.decode("utf-8", errors="ignore")
        loc_match = re.search(r"LOCATION:\s*([^\r\n]+)", resp, re.IGNORECASE)
        if not loc_match:
            return None
        location_url = loc_match.group(1).strip()

        req = urllib.request.Request(location_url, headers={"User-Agent": "UPnP/1.0"})
        with urllib.request.urlopen(req, timeout=2.5) as r:
            xml_data = r.read().decode("utf-8", errors="ignore")

        ctrl_match = re.search(r"<controlURL>([^<]+)</controlURL>", xml_data)
        if not ctrl_match:
            return None
        control_url = urllib.parse.urljoin(location_url, ctrl_match.group(1).strip())

        local_ip = get_local_ips()[0] if get_local_ips() else "127.0.0.1"
        soap_body = f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:AddPortMapping xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1">
<NewRemoteHost></NewRemoteHost>
<NewExternalPort>{port}</NewExternalPort>
<NewProtocol>TCP</NewProtocol>
<NewInternalPort>{port}</NewInternalPort>
<NewInternalClient>{local_ip}</NewInternalClient>
<NewEnabled>1</NewEnabled>
<NewPortMappingDescription>PC Remote</NewPortMappingDescription>
<NewLeaseDuration>0</NewLeaseDuration>
</u:AddPortMapping>
</s:Body>
</s:Envelope>"""

        headers = {
            "SOAPAction": '"urn:schemas-upnp-org:service:WANIPConnection:1#AddPortMapping"',
            "Content-Type": 'text/xml; charset="utf-8"',
        }
        soap_req = urllib.request.Request(control_url, data=soap_body.encode("utf-8"), headers=headers)
        with urllib.request.urlopen(soap_req, timeout=3.0) as _:
            pass

        ip_req = urllib.request.Request("https://api.ipify.org", headers={"User-Agent": "curl/7.0"})
        with urllib.request.urlopen(ip_req, timeout=2.5) as r:
            ext_ip = r.read().decode("utf-8").strip()
            if ext_ip:
                return f"http://{ext_ip}:{port}"
    except Exception:
        pass
    return None


def find_cloudflared_path() -> Optional[str]:
    """Ищет исполняемый файл cloudflared в системе (PATH, папки установки MSI, Downloads или папка проекта)."""
    cand = shutil.which("cloudflared") or shutil.which("cloudflared.exe")
    if cand and os.path.isfile(cand):
        return cand

    candidates = [
        os.path.join(BASE_DIR, "cloudflared.exe"),
        os.path.join(BASE_DIR, "cloudflared-windows-amd64.exe"),
        os.path.expandvars(r"%ProgramFiles%\Cloudflare\cloudflared\cloudflared.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Cloudflare\cloudflared\cloudflared.exe"),
        os.path.expandvars(r"%ProgramFiles%\Cloudflare\cloudflared.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Cloudflare\cloudflared.exe"),
        os.path.expandvars(r"%ProgramFiles%\cloudflared\cloudflared.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\cloudflared\cloudflared.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\cloudflared\cloudflared.exe"),
        os.path.expandvars(r"%USERPROFILE%\Downloads\cloudflared.exe"),
        os.path.expandvars(r"%USERPROFILE%\Downloads\cloudflared-windows-amd64.exe"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p

    for base in [os.path.expandvars(r"%ProgramFiles%\Cloudflare"), os.path.expandvars(r"%ProgramFiles(x86)%\Cloudflare")]:
        if os.path.isdir(base):
            try:
                for root, _, files in os.walk(base):
                    for f in files:
                        if f.lower() in ("cloudflared.exe", "cloudflared-windows-amd64.exe"):
                            return os.path.join(root, f)
            except Exception:
                pass

    return None


def _cloudflared_worker() -> None:
    global _cloudflare_url, _cloudflare_proc
    cf_path = find_cloudflared_path()
    if not cf_path:
        print("\n" + "-" * 60)
        print("  [!] Cloudflare Tunnel: cloudflared.exe пока не найден.")
        print("      Если вы установили MSI или скачали Executable,")
        print("      просто скопируйте cloudflared.exe в эту папку рядом с server.py!")
        print("-" * 60 + "\n")
        log("[Cloudflare Tunnel] cloudflared.exe не найден.")
        return

    log(f"[Cloudflare Tunnel] Найден cloudflared: {cf_path}")
    upnp_url = try_upnp_port_forward()
    if upnp_url:
        log(f"[UPnP] Роутер успешно открыл внешний порт! Адрес: {upnp_url}")
        try:
            channel_id = "pcremote_" + hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()[:16]
            req = urllib.request.Request(f"https://ntfy.sh/{channel_id}", data=upnp_url.encode("utf-8"), headers={"Title": "PC Remote UPnP"})
            urllib.request.urlopen(req, timeout=4)
        except Exception:
            pass
    log(f"[Cloudflare Tunnel] Запуск автоматического интернет-туннеля для порта {PORT}…")

    url_regex = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
    while True:
        try:
            cmd = [cf_path, "tunnel", "--url", f"http://127.0.0.1:{PORT}"]
            creationflags = 0x08000000 if IS_WINDOWS else 0
            _cloudflare_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
                bufsize=1,
                creationflags=creationflags
            )

            for line in _cloudflare_proc.stdout:
                match = url_regex.search(line)
                if match:
                    found_url = match.group(0)
                    if found_url != _cloudflare_url:
                        _cloudflare_url = found_url
                        try:
                            with open(os.path.join(BASE_DIR, "tunnel_url.txt"), "w", encoding="utf-8") as f:
                                f.write(found_url + "\n")
                        except Exception:
                            pass
                        try:
                            channel_id = "pcremote_" + hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()[:16]
                            sync_req = urllib.request.Request(
                                f"https://ntfy.sh/{channel_id}",
                                data=found_url.encode("utf-8"),
                                headers={"Title": "PC Remote Cloudflare", "User-Agent": "curl/7.0"}
                            )
                            urllib.request.urlopen(sync_req, timeout=4)
                            log(f"[Cloudflare Tunnel] Адрес успешно отправлен в облачный канал {channel_id}")
                        except Exception as e:
                            log(f"[Cloudflare Tunnel] Не удалось опубликовать ссылку: {e}")
                        print("\n" + "=" * 70)
                        log(f"[Cloudflare Tunnel] Внешний интернет-адрес: {found_url}")
                        print("  [*] ВНЕШНИЙ ИНТЕРНЕТ-ТУННЕЛЬ АКТИВЕН (Cloudflare Tunnel):")
                        print("  Адрес для телефона (4G/5G / Вне дома):")
                        print(f"  -> {found_url}")
                        print("  Вставьте этот адрес в приложении на смартфоне в поле 'Адрес ПК'.")
                        print("  Tailscale и сторонние VPN на телефоне больше НЕ НУЖНЫ!")
                        print("=" * 70 + "\n")
            _cloudflare_proc.wait()
        except Exception as e:
            log(f"[Cloudflare Tunnel] Ошибка процесса: {e}")
        time.sleep(3)


def _udp_mouse_worker() -> None:
    """Быстрый UDP-сервер для мгновенного перемещения мыши (125+ Гц) с нулевой задержкой без накладных расходов HTTP/TCP."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", PORT))
    except Exception as e:
        log(f"[UDP] Не удалось запустить UDP-сервер на порту {PORT}: {e}")
        return

    log(f"[UDP] Высокоскоростной мышиный UDP-сервер активен на порту {PORT}")
    while True:
        try:
            data, addr = sock.recvfrom(512)
            if not data:
                continue
            text = data.decode("utf-8", errors="ignore").strip()

            # Автопоиск ПК в локальной Wi-Fi сети (с автоматической передачей токена для сопряжения в 1 клик)
            if text == "PC_DISCOVERY_PING":
                pc_name = socket.gethostname() or "ПК"
                reply = f"PC_DISCOVERY_PONG:{pc_name}:{_cloudflare_url}:{TOKEN}"
                sock.sendto(reply.encode("utf-8"), addr)
                continue

            parts = text.split(":", 2)
            if len(parts) < 3:
                continue
            token_cand, cmd, args = parts[0], parts[1], parts[2]
            if not hmac.compare_digest(token_cand.encode("utf-8"), TOKEN.encode("utf-8")):
                continue

            # Мгновенное абсолютное следование курсора за пальцем (125+ Гц)
            if cmd == "abs":
                coords = args.split(",")
                if len(coords) == 2:
                    fnx = max(0.0, min(1.0, float(coords[0])))
                    fny = max(0.0, min(1.0, float(coords[1])))
                    ax = int(fnx * 65535)
                    ay = int(fny * 65535)
                    _send_inputs([_mouse_input(dx=ax, dy=ay, flags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE)])
            elif cmd == "scroll":
                try:
                    dy = int(args)
                    if dy:
                        _send_inputs([_mouse_input(data=dy * 120, flags=MOUSEEVENTF_WHEEL)])
                except Exception:
                    pass
            elif cmd == "kd":
                try:
                    vk = int(args, 0)
                    _send_inputs([_key_input(vk=vk, flags=0)])
                except Exception:
                    pass
            elif cmd == "ku":
                try:
                    vk = int(args, 0)
                    _send_inputs([_key_input(vk=vk, flags=KEYEVENTF_KEYUP)])
                except Exception:
                    pass
            elif cmd == "m":
                coords = args.split(",")
                if len(coords) == 2:
                    dx = max(-2000, min(2000, int(coords[0])))
                    dy = max(-2000, min(2000, int(coords[1])))
                    if dx or dy:
                        _send_inputs([_mouse_input(dx=dx, dy=dy, flags=MOUSEEVENTF_MOVE)])
            elif cmd == "tap":
                coords = args.split(",")
                if len(coords) == 2:
                    fnx = max(0.0, min(1.0, float(coords[0])))
                    fny = max(0.0, min(1.0, float(coords[1])))
                    ax = int(fnx * 65535)
                    ay = int(fny * 65535)
                    _send_inputs([
                        _mouse_input(dx=ax, dy=ay, flags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE),
                        _mouse_input(flags=MOUSEEVENTF_LEFTDOWN),
                        _mouse_input(flags=MOUSEEVENTF_LEFTUP)
                    ])
            elif cmd == "act":
                if args == "left":
                    _send_inputs([_mouse_input(flags=MOUSEEVENTF_LEFTDOWN), _mouse_input(flags=MOUSEEVENTF_LEFTUP)])
                elif args == "right":
                    _send_inputs([_mouse_input(flags=MOUSEEVENTF_RIGHTDOWN), _mouse_input(flags=MOUSEEVENTF_RIGHTUP)])
                elif args == "double":
                    c = [_mouse_input(flags=MOUSEEVENTF_LEFTDOWN), _mouse_input(flags=MOUSEEVENTF_LEFTUP)]
                    _send_inputs(c + c)
        except Exception:
            pass


def _watch_server_file_and_restart() -> None:
    """Отслеживает изменение файлов сервера и автоматически перезапускает процесс."""
    script_path = os.path.join(BASE_DIR, "server.py")
    if not os.path.exists(script_path):
        return
    try:
        initial_mtime = os.path.getmtime(script_path)
    except OSError:
        return

    while True:
        time.sleep(1.5)
        try:
            if not os.path.exists(script_path):
                continue
            current_mtime = os.path.getmtime(script_path)
            if current_mtime != initial_mtime:
                import py_compile
                try:
                    py_compile.compile(script_path, doraise=True)
                except Exception as ce:
                    log(f"[!] Файл обновлён с синтаксической ошибкой: {ce}. Перезапуск отложен.")
                    initial_mtime = current_mtime
                    continue

                log("Файл server.py был обновлён на диске! Автоматический перезапуск сервера...")
                time.sleep(0.5)
                py_exe = sys.executable
                cmd = [py_exe, script_path] + sys.argv[1:]
                subprocess.Popen(cmd, cwd=BASE_DIR, creationflags=DETACHED_FLAGS)
                os._exit(0)
        except Exception:
            pass


def start_network_services() -> None:
    """Запускает фоновые сетевые потоки: отслеживание файлов, UDP-мышь, Cloudflare туннель."""
    threading.Thread(target=_watch_server_file_and_restart, daemon=True).start()
    threading.Thread(target=_udp_mouse_worker, daemon=True).start()
    threading.Thread(target=_cloudflared_worker, daemon=True).start()
