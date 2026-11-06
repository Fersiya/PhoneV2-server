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
from typing import List, Optional, Tuple

from .config import (
    PORT,
    TOKEN,
    BASE_DIR,
    APP_DIR,
    APP_LIST,
    IS_WINDOWS,
    DETACHED_FLAGS,
    log,
)
from .input import (
    _send_inputs,
    _mouse_input,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_WHEEL,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_MIDDLEDOWN,
    MOUSEEVENTF_MIDDLEUP,
)

__all__ = [
    "get_local_ips",
    "try_upnp_port_forward",
    "find_cloudflared_path",
    "start_network_services",
    "get_cloudflare_url",
    "start_cloudflare_tunnel",
    "stop_cloudflare_tunnel",
    "is_cloudflare_running",
    "get_tunnel_info",
    "check_paths",
]

_cloudflare_url: str = ""
_cloudflare_proc: Optional[subprocess.Popen] = None
_cloudflare_active: bool = False
_tunnel_ok: bool = False
_tunnel_protocol: str = ""
_cloudflare_lock = threading.Lock()
_cloudflare_thread: Optional[threading.Thread] = None


def get_cloudflare_url() -> str:
    return _cloudflare_url


def is_cloudflare_running() -> bool:
    global _cloudflare_proc
    return bool(_cloudflare_proc and _cloudflare_proc.poll() is None)


def get_tunnel_info() -> dict:
    cf_path = find_cloudflared_path()
    running = is_cloudflare_running()
    return {
        "installed": bool(cf_path),
        "running": running,
        "url": _cloudflare_url if running else "",
        "cf_path": cf_path or "",
        "healthy": bool(running and _tunnel_ok),
        "protocol": _tunnel_protocol if running else "",
    }


def _publish_tunnel_url(url: str) -> bool:
    """Кладёт адрес туннеля в облачный канал ntfy, откуда его берёт телефон вне дома."""
    try:
        channel_id = "pcremote_" + hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()[:16]
        req = urllib.request.Request(
            f"https://ntfy.sh/{channel_id}",
            data=url.encode("utf-8"),
            headers={"Title": "PC Remote Cloudflare", "User-Agent": "curl/7.0"},
        )
        urllib.request.urlopen(req, timeout=6)
        return True
    except Exception as e:
        log(f"[Cloudflare Tunnel] Не удалось опубликовать ссылку: {e}")
        return False


def _republish_loop() -> None:
    """ntfy хранит сообщения ~12 ч, поэтому живой адрес переотправляем каждые 5 минут."""
    while _cloudflare_active:
        for _ in range(60):
            if not _cloudflare_active:
                return
            time.sleep(5)
        if _cloudflare_url and _tunnel_ok:
            _publish_tunnel_url(_cloudflare_url)


def get_pc_mac(local_ip: str = "") -> str:
    """MAC-адрес сетевой карты ПК (для Wake-on-LAN), по возможности той, у которой адрес local_ip."""
    if not IS_WINDOWS:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        class IP_ADDR_STRING(ctypes.Structure):
            pass

        IP_ADDR_STRING._fields_ = [
            ("Next", ctypes.c_void_p),
            ("IpAddress", ctypes.c_char * 16),
            ("IpMask", ctypes.c_char * 16),
            ("Context", wintypes.DWORD),
        ]

        class IP_ADAPTER_INFO(ctypes.Structure):
            pass

        IP_ADAPTER_INFO._fields_ = [
            ("Next", ctypes.c_void_p),
            ("ComboIndex", wintypes.DWORD),
            ("AdapterName", ctypes.c_char * 260),
            ("Description", ctypes.c_char * 132),
            ("AddressLength", wintypes.UINT),
            ("Address", ctypes.c_ubyte * 8),
            ("Index", wintypes.DWORD),
            ("Type", wintypes.UINT),
            ("DhcpEnabled", wintypes.UINT),
            ("CurrentIpAddress", ctypes.c_void_p),
            ("IpAddressList", IP_ADDR_STRING),
            ("GatewayList", IP_ADDR_STRING),
            ("DhcpServer", IP_ADDR_STRING),
            ("HaveWins", wintypes.BOOL),
            ("PrimaryWinsServer", IP_ADDR_STRING),
            ("SecondaryWinsServer", IP_ADDR_STRING),
            ("LeaseObtained", ctypes.c_void_p),
            ("LeaseExpires", ctypes.c_void_p),
        ]

        size = wintypes.ULONG(0)
        iph = ctypes.windll.iphlpapi
        iph.GetAdaptersInfo(None, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if iph.GetAdaptersInfo(buf, ctypes.byref(size)) != 0:
            return ""
        addr = ctypes.addressof(buf)
        first = ""
        while addr:
            ad = IP_ADAPTER_INFO.from_address(addr)
            if ad.AddressLength == 6:
                mac = ":".join(f"{b:02X}" for b in ad.Address[:6])
                ips = []
                node = ctypes.addressof(ad.IpAddressList)
                while node:
                    s = IP_ADDR_STRING.from_address(node)
                    ips.append(s.IpAddress.decode("ascii", "ignore"))
                    node = s.Next
                if local_ip and local_ip in ips:
                    return mac
                if not first and any(i and i != "0.0.0.0" for i in ips):
                    first = mac
            addr = ad.Next
        return first
    except Exception:
        return ""


def stop_cloudflare_tunnel() -> Tuple[bool, str]:
    global _cloudflare_active, _cloudflare_proc, _cloudflare_url
    with _cloudflare_lock:
        _cloudflare_active = False
        if _cloudflare_proc:
            try:
                _cloudflare_proc.terminate()
                try:
                    _cloudflare_proc.wait(timeout=2.0)
                except Exception:
                    _cloudflare_proc.kill()
            except Exception as e:
                log(f"[Cloudflare Tunnel] Ошибка остановки: {e}")
            _cloudflare_proc = None
        _cloudflare_url = ""
        try:
            url_file = os.path.join(BASE_DIR, "tunnel_url.txt")
            if os.path.exists(url_file):
                os.remove(url_file)
        except Exception:
            pass
        log("[Cloudflare Tunnel] Туннель остановлен по запросу.")
        return True, "Туннель остановлен"


def start_cloudflare_tunnel() -> Tuple[bool, str]:
    global _cloudflare_active, _cloudflare_thread
    with _cloudflare_lock:
        if is_cloudflare_running():
            return True, _cloudflare_url or "Туннель уже запущен"
        cf_path = find_cloudflared_path()
        if not cf_path:
            return False, "cloudflared.exe не найден на ПК"
        _cloudflare_active = True
        if not _cloudflare_thread or not _cloudflare_thread.is_alive():
            _cloudflare_thread = threading.Thread(target=_cloudflared_worker_loop, daemon=True)
            _cloudflare_thread.start()
        log("[Cloudflare Tunnel] Запуск интернет-туннеля по запросу…")
        return True, "Запуск интернет-туннеля…" 


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
        os.path.join(APP_DIR, "cloudflared.exe"),
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


def _tunnel_selfcheck(url: str, proc) -> None:
    """Через 40 секунд проверяет, что адрес туннеля реально отвечает; если нет — перезапускает cloudflared
    (следующая попытка пойдёт по другому протоколу). Ошибка 530 на телефоне = адрес выдан, а соединения нет."""
    deadline = time.time() + 40
    while time.time() < deadline and _cloudflare_active and proc.poll() is None:
        try:
            req = urllib.request.Request(url + "/ping", headers={"User-Agent": "pccontrol-selfcheck"})
            with urllib.request.urlopen(req, timeout=5) as r:
                if r.status == 200:
                    global _tunnel_ok
                    _tunnel_ok = True
                    log(f"[Cloudflare Tunnel] Самопроверка пройдена: {url} отвечает")
                    _publish_tunnel_url(url)
                    return
        except Exception:
            pass
        time.sleep(4)
    # Сам ПК может не открыть свой же туннельный адрес (DNS/провайдер), хотя для телефона он работает —
    # поэтому туннель не трогаем, только пишем в лог.
    log("[Cloudflare Tunnel] ПК не смог открыть собственный адрес туннеля (для телефона он может работать)")


def _cloudflared_worker_loop() -> None:
    global _cloudflare_url, _cloudflare_proc, _tunnel_ok, _tunnel_protocol
    threading.Thread(target=_republish_loop, daemon=True).start()
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

    url_regex = re.compile(r"https://(?!api\.)[a-zA-Z0-9-]+\.trycloudflare\.com")
    protocols = ["http2", "quic"]  # сначала TCP (проходит почти везде), если не взлетело — QUIC
    attempt = 0
    fails = 0
    while _cloudflare_active:
        protocol = protocols[attempt % len(protocols)]
        attempt += 1
        _tunnel_ok = False
        _tunnel_protocol = protocol
        try:
            log(f"[Cloudflare Tunnel] Запуск cloudflared (протокол {protocol})…")
            cmd = [cf_path, "tunnel", "--no-autoupdate", "--protocol", protocol, "--url", f"http://127.0.0.1:{PORT}"]
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

            started = time.time()
            tail: List[str] = []
            for line in _cloudflare_proc.stdout:
                if not _cloudflare_active:
                    break
                if line.strip():
                    tail = (tail + [line.strip()])[-6:]
                if "Registered tunnel connection" in line:
                    _tunnel_ok = True
                    if _cloudflare_url:
                        _publish_tunnel_url(_cloudflare_url)
                match = url_regex.search(line)
                if match:
                    found_url = match.group(0)
                    if found_url != _cloudflare_url:
                        _cloudflare_url = found_url
                        _tunnel_ok = False
                        threading.Thread(target=_tunnel_selfcheck, args=(found_url, _cloudflare_proc), daemon=True).start()
                        try:
                            with open(os.path.join(BASE_DIR, "tunnel_url.txt"), "w", encoding="utf-8") as f:
                                f.write(found_url + "\n")
                        except Exception:
                            pass
                        _publish_tunnel_url(found_url)
                        print("\n" + "=" * 70)
                        log(f"[Cloudflare Tunnel] Внешний интернет-адрес: {found_url}")
                        print("  [*] ВНЕШНИЙ ИНТЕРНЕТ-ТУННЕЛЬ АКТИВЕН (Cloudflare Tunnel):")
                        print("  Адрес для телефона (4G/5G / Вне дома):")
                        print(f"  -> {found_url}")
                        print("  Вставьте этот адрес в приложении на смартфоне в поле 'Адрес ПК'.")
                        print("  Tailscale и сторонние VPN на телефоне больше НЕ НУЖНЫ!")
                        print("=" * 70 + "\n")
            _cloudflare_proc.wait()
            _cloudflare_url = ""
            if _cloudflare_active:
                lived = time.time() - started
                log(f"[Cloudflare Tunnel] cloudflared завершился через {lived:.0f} с (код {_cloudflare_proc.returncode}). Последние строки:")
                for ln in tail:
                    log("    " + ln[:300])
                fails = 0 if lived > 120 else fails + 1
        except Exception as e:
            log(f"[Cloudflare Tunnel] Ошибка процесса: {e}")
            fails += 1
        # Пауза растёт при повторных сбоях (до 2 минут), чтобы не спамить и не долбить Cloudflare
        for _ in range(min(120, 3 * 2 ** min(fails, 5))):
            if not _cloudflare_active:
                break
            time.sleep(1)


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
                reply = f"PC_DISCOVERY_PONG:{pc_name}"
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
                elif args == "middle":
                    _send_inputs([_mouse_input(flags=MOUSEEVENTF_MIDDLEDOWN), _mouse_input(flags=MOUSEEVENTF_MIDDLEUP)])
                elif args == "double":
                    c = [_mouse_input(flags=MOUSEEVENTF_LEFTDOWN), _mouse_input(flags=MOUSEEVENTF_LEFTUP)]
                    _send_inputs(c + c)
        except Exception:
            pass


def _watch_server_file_and_restart() -> None:
    """Отслеживает изменение файлов сервера и автоматически перезапускает процесс."""
    from .config import FROZEN, restart_command
    script_path = os.path.join(BASE_DIR, "server.py")
    if FROZEN or not os.path.exists(script_path):
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
                subprocess.Popen(restart_command(), cwd=BASE_DIR, creationflags=DETACHED_FLAGS)
                os._exit(0)
        except Exception:
            pass


def start_network_services() -> None:
    """Запускает фоновые сетевые потоки: отслеживание файлов, UDP-мышь, Cloudflare туннель (по требованию/автозапуску)."""
    threading.Thread(target=_watch_server_file_and_restart, daemon=True).start()
    threading.Thread(target=_udp_mouse_worker, daemon=True).start()
    from .config import CFG
    if CFG.get("tunnel_autostart", True):
        start_cloudflare_tunnel()
