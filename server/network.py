"""Сетевые сервисы: UDP мышь/геймпад, Wi-Fi discovery, MAC и Tailscale-адрес, автоперезапуск."""
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
    "start_network_services",
    "get_network_info",
    "check_paths",
]













def get_network_info() -> dict:
    """Сведения о сети ПК для приложения (Tailscale-адрес, если Tailscale запущен)."""
    return {"tailscale": get_tailscale_ip()}


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






def check_paths() -> None:
    for a in APP_LIST:
        exe = os.path.expandvars(a["launch"][0])
        if not os.path.exists(exe):
            log(f"[!] {a['title']}: не найден файл {exe}. Поправь путь в config.json")


_ts_cache = ("", 0.0)


def get_tailscale_ip() -> str:
    """Адрес этого ПК в Tailscale (100.x.x.x), если Tailscale установлен и подключён."""
    global _ts_cache
    if time.time() - _ts_cache[1] < 60:
        return _ts_cache[0]
    ip = ""
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            a = info[4][0]
            p = a.split(".")
            if len(p) == 4 and p[0] == "100" and 64 <= int(p[1]) <= 127:
                ip = a
                break
        if not ip and IS_WINDOWS:
            import subprocess
            exe = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Tailscale", "tailscale.exe")
            if os.path.isfile(exe):
                out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=4, creationflags=0x08000000).stdout.strip()
                if out.startswith("100."):
                    ip = out.splitlines()[0].strip()
    except Exception:
        ip = ""
    _ts_cache = (ip, time.time())
    return ip


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
    """Запускает фоновые сетевые потоки: отслеживание файлов, UDP-мышь."""
    threading.Thread(target=_watch_server_file_and_restart, daemon=True).start()
    threading.Thread(target=_udp_mouse_worker, daemon=True).start()

