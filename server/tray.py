"""Интеграция с системным треем Windows (System Tray).

Работает без сторонних зависимостей (только чистый WinAPI через ctypes и встроенный tkinter).
Позволяет пользователю видеть статус сервера, копировать токен в 1 клик,
управлять Cloudflare туннелем, открывать логи и мягко выходить из приложения.
"""
import ctypes
from ctypes import wintypes
import os
import socket
import sys
import threading
import time
from typing import Optional

from .config import BASE_DIR, PORT, TOKEN, IS_WINDOWS, log
from .network import (
    get_local_ips,
    get_cloudflare_url,
    is_cloudflare_running,
    start_cloudflare_tunnel,
    stop_cloudflare_tunnel,
)

if IS_WINDOWS:
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32

    NIM_ADD = 0x00000000
    NIM_MODIFY = 0x00000001
    NIM_DELETE = 0x00000002

    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004

    WM_USER = 0x0400
    WM_TRAY = WM_USER + 20
    WM_COMMAND = 0x0111
    WM_DESTROY = 0x0002
    WM_RBUTTONUP = 0x0205
    WM_LBUTTONDBLCLK = 0x0203

    MF_STRING = 0x00000000
    MF_SEPARATOR = 0x00000800
    TPM_RIGHTBUTTON = 0x0002
    IDI_APPLICATION = 32512

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", wintypes.BYTE * 8),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", GUID),
            ("hBalloonIcon", wintypes.HICON),
        ]

    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_tray_hwnd: Optional[int] = None
_tray_nid = None
_dialog_open = False


def _copy_to_clipboard(text: str) -> None:
    try:
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(text)
        r.update()
        r.destroy()
    except Exception:
        pass


def show_status_dialog() -> None:
    """Отображает компактное диалоговое окно со статусом, IP, токеном и туннелем."""
    global _dialog_open
    if _dialog_open:
        return

    def _ui_thread():
        global _dialog_open
        _dialog_open = True
        try:
            import tkinter as tk
            from tkinter import ttk

            root = tk.Tk()
            root.title("Пульт ПК (PhoneV2 Server)")
            root.geometry("460x360")
            root.resizable(False, False)
            root.attributes("-topmost", True)

            # Цветовая гамма
            bg_color = "#1E1E2E"
            fg_color = "#CDD6F4"
            card_bg = "#313244"
            accent_color = "#89B4FA"
            btn_bg = "#45475A"

            root.configure(bg=bg_color)

            # Заголовок
            title_lbl = tk.Label(
                root,
                text="🖥️ Пульт ПК — Сервер управления",
                font=("Segoe UI", 13, "bold"),
                bg=bg_color,
                fg=accent_color,
            )
            title_lbl.pack(pady=(16, 8))

            # Рамка с данными
            card = tk.Frame(root, bg=card_bg, padx=14, pady=12, relief="flat")
            card.pack(fill="x", padx=16, pady=6)

            ips = ", ".join(get_local_ips()) or "127.0.0.1"
            local_addr = f"http://{ips}:{PORT}"

            # IP
            tk.Label(card, text="Локальный адрес Wi-Fi:", font=("Segoe UI", 9, "bold"), bg=card_bg, fg="#A6ADC8").pack(anchor="w")
            ip_row = tk.Frame(card, bg=card_bg)
            ip_row.pack(fill="x", pady=(2, 6))
            ip_entry = tk.Entry(ip_row, font=("Consolas", 10), bg="#181825", fg=fg_color, relief="flat", highlightthickness=1)
            ip_entry.insert(0, local_addr)
            ip_entry.config(state="readonly")
            ip_entry.pack(side="left", fill="x", expand=True)

            btn_copy_ip = tk.Button(
                ip_row,
                text="Копировать",
                font=("Segoe UI", 8),
                bg=btn_bg,
                fg="white",
                relief="flat",
                command=lambda: _copy_to_clipboard(local_addr),
            )
            btn_copy_ip.pack(side="right", padx=(6, 0))

            # Токен
            tk.Label(card, text="Секретный токен доступа:", font=("Segoe UI", 9, "bold"), bg=card_bg, fg="#A6ADC8").pack(anchor="w")
            tok_row = tk.Frame(card, bg=card_bg)
            tok_row.pack(fill="x", pady=(2, 6))
            tok_entry = tk.Entry(tok_row, font=("Consolas", 10), bg="#181825", fg="#A6E3A1", relief="flat", highlightthickness=1)
            tok_entry.insert(0, TOKEN)
            tok_entry.config(state="readonly")
            tok_entry.pack(side="left", fill="x", expand=True)

            btn_copy_tok = tk.Button(
                tok_row,
                text="Копировать токен",
                font=("Segoe UI", 8, "bold"),
                bg="#A6E3A1",
                fg="#11111B",
                relief="flat",
                command=lambda: _copy_to_clipboard(TOKEN),
            )
            btn_copy_tok.pack(side="right", padx=(6, 0))

            # Туннель
            cf_url = get_cloudflare_url()
            cf_text = cf_url if is_cloudflare_running() and cf_url else ("Запускается…" if is_cloudflare_running() else "Выключен")
            tk.Label(card, text="Интернет-туннель (Cloudflare):", font=("Segoe UI", 9, "bold"), bg=card_bg, fg="#A6ADC8").pack(anchor="w")
            cf_lbl = tk.Label(card, text=cf_text, font=("Consolas", 9), bg=card_bg, fg="#F9E2AF")
            cf_lbl.pack(anchor="w", pady=(1, 4))

            # Кнопки действий
            actions = tk.Frame(root, bg=bg_color)
            actions.pack(fill="x", padx=16, pady=10)

            def _toggle_tunnel():
                if is_cloudflare_running():
                    stop_cloudflare_tunnel()
                    cf_lbl.config(text="Выключен")
                else:
                    start_cloudflare_tunnel()
                    cf_lbl.config(text="Запуск туннеля…")

            btn_tunnel = tk.Button(
                actions,
                text="🌐 Вкл/Выкл туннель",
                font=("Segoe UI", 9),
                bg=btn_bg,
                fg="white",
                relief="flat",
                command=_toggle_tunnel,
            )
            btn_tunnel.pack(side="left", padx=(0, 6))

            btn_logs = tk.Button(
                actions,
                text="📁 Открыть папку",
                font=("Segoe UI", 9),
                bg=btn_bg,
                fg="white",
                relief="flat",
                command=lambda: os.startfile(BASE_DIR),
            )
            btn_logs.pack(side="left", padx=6)

            btn_close = tk.Button(
                actions,
                text="Закрыть",
                font=("Segoe UI", 9),
                bg=btn_bg,
                fg="white",
                relief="flat",
                command=root.destroy,
            )
            btn_close.pack(side="right")

            root.protocol("WM_DELETE_WINDOW", root.destroy)
            root.mainloop()
        except Exception as e:
            log(f"[Tray] Ошибка диалога: {e}")
        finally:
            _dialog_open = False

    threading.Thread(target=_ui_thread, daemon=True).start()


def _tray_window_proc(hwnd, msg, wparam, lparam):
    if msg == WM_TRAY:
        if lparam == WM_LBUTTONDBLCLK:
            show_status_dialog()
            return 0
        elif lparam == WM_RBUTTONUP:
            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))

            hmenu = user32.CreatePopupMenu()
            user32.AppendMenuW(hmenu, MF_STRING, 1, "🖥️ Информация и токен...")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)

            cf_running = is_cloudflare_running()
            tunnel_item = "🌐 Остановить Cloudflare Tunnel" if cf_running else "🌐 Включить Cloudflare Tunnel"
            user32.AppendMenuW(hmenu, MF_STRING, 2, tunnel_item)
            user32.AppendMenuW(hmenu, MF_STRING, 3, "📁 Открыть папку сервера и логов")
            user32.AppendMenuW(hmenu, MF_STRING, 4, "🔄 Перезапустить сервер")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, 5, "❌ Выход")

            user32.SetForegroundWindow(hwnd)
            cmd = user32.TrackPopupMenu(
                hmenu, TPM_RIGHTBUTTON | 0x0100, pt.x, pt.y, 0, hwnd, None
            )
            user32.PostMessageW(hwnd, 0, 0, 0)
            user32.DestroyMenu(hmenu)

            if cmd == 1:
                show_status_dialog()
            elif cmd == 2:
                if is_cloudflare_running():
                    stop_cloudflare_tunnel()
                else:
                    start_cloudflare_tunnel()
            elif cmd == 3:
                os.startfile(BASE_DIR)
            elif cmd == 4:
                log("[Tray] Перезапуск сервера по запросу из трея...")
                time.sleep(0.5)
                py_exe = sys.executable
                script = os.path.join(BASE_DIR, "server.py")
                cmd_line = [py_exe, script] + sys.argv[1:]
                from .config import DETACHED_FLAGS
                import subprocess
                subprocess.Popen(cmd_line, cwd=BASE_DIR, creationflags=DETACHED_FLAGS)
                remove_tray_icon()
                os._exit(0)
            elif cmd == 5:
                log("[Tray] Завершение работы сервера по запросу пользователя.")
                stop_cloudflare_tunnel()
                remove_tray_icon()
                os._exit(0)
            return 0
    elif msg == WM_DESTROY:
        remove_tray_icon()
        user32.PostQuitMessage(0)
        return 0

    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_wndproc_cb = None


def setup_tray_icon() -> bool:
    """Регистрирует иконку в системном трее Windows."""
    global _tray_hwnd, _tray_nid, _wndproc_cb
    if not IS_WINDOWS:
        return False

    try:
        class_name = "PhoneV2TrayWindowClass"
        hinst = kernel32.GetModuleHandleW(None)

        _wndproc_cb = WNDPROC(_tray_window_proc)
        wc = WNDCLASSW()
        wc.hInstance = hinst
        wc.lpszClassName = class_name
        wc.lpfnWndProc = _wndproc_cb
        user32.RegisterClassW(ctypes.byref(wc))

        hwnd = user32.CreateWindowExW(
            0, class_name, "PhoneV2TrayWindow", 0, 0, 0, 0, 0, 0, 0, hinst, None
        )
        if not hwnd:
            return False

        _tray_hwnd = hwnd
        hicon = user32.LoadIconW(0, IDI_APPLICATION)

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = hicon
        nid.szTip = f"Пульт ПК v6.0 (Порт {PORT})"

        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        _tray_nid = nid
        log("[Tray] Иконка успешно добавлена в системный трей Windows.")
        return True
    except Exception as e:
        log(f"[Tray] Не удалось создать иконку в трее: {e}")
        return False


def remove_tray_icon() -> None:
    global _tray_nid
    if _tray_nid and IS_WINDOWS:
        try:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(_tray_nid))
            _tray_nid = None
        except Exception:
            pass


def start_tray_thread() -> None:
    """Запускает цикл обработки сообщений трея в отдельном потоке."""
    if not IS_WINDOWS:
        return

    def _loop():
        if not setup_tray_icon():
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
