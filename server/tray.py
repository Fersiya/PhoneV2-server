"""Интеграция с системным треем Windows (System Tray).

Работает без сторонних зависимостей (только чистый WinAPI через ctypes и встроенный tkinter).
Позволяет пользователю видеть статус Хоста, копировать токен в 1 клик,
открывать логи и мягко выходить из приложения.
"""
import ctypes
from ctypes import wintypes
import os
import socket
import sys
import threading
import time
from typing import Optional

from .config import BASE_DIR, PORT, TOKEN, IS_WINDOWS, log, restart_command
from .auth import open_pairing, pairing_seconds_left
from .version import VERSION
from .network import (
    get_local_ips,
    get_tailscale_ip,
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

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.DefWindowProcW.restype = LRESULT
    user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p
    ]
    user32.TrackPopupMenu.restype = wintypes.UINT
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostQuitMessage.restype = None
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.DestroyMenu.restype = wintypes.BOOL
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL

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


def _icon_path() -> str:
    """Путь к icon.ico — той же картинке, что на ярлыке и в установщике."""
    base = getattr(sys, "_MEIPASS", "")
    cands = [os.path.join(base, "icon.ico") if base else "",
             os.path.join(BASE_DIR, "installer", "icon.ico"),
             os.path.join(BASE_DIR, "icon.ico")]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


def show_status_dialog() -> None:
    """Окно статуса: адреса, токен, интернет-туннель. Обновляется само."""
    global _dialog_open
    if _dialog_open:
        return

    def _ui_thread():
        global _dialog_open
        _dialog_open = True
        try:
            import tkinter as tk

            BG, CARD, FIELD = "#0B0F1A", "#141B2D", "#0E1424"
            TEXT, MUTED = "#E6EDF7", "#8A96AD"
            ACCENT, OK, WARN, BAD, BTN = "#4CC9F0", "#34D399", "#FBBF24", "#F87171", "#222C45"
            FONT = "Segoe UI"

            root = tk.Tk()
            root.title("Пульт ПК Хост")
            root.geometry("520x620")
            root.resizable(True, True)
            root.minsize(480, 560)
            root.configure(bg=BG)
            root.attributes("-topmost", True)
            root.after(600, lambda: root.attributes("-topmost", False))
            _ico = _icon_path()
            if _ico:
                try:
                    root.iconbitmap(_ico)
                except Exception:
                    pass

            def label(parent, text, size=9, bold=False, fg=TEXT, bg=None):
                return tk.Label(parent, text=text, font=(FONT, size, "bold" if bold else "normal"),
                                fg=fg, bg=bg or parent["bg"])

            def button(parent, text, cmd, bg=BTN, fg=TEXT, bold=False):
                return tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg, activebackground=ACCENT,
                                 activeforeground="#00131A", relief="flat", bd=0, cursor="hand2",
                                 font=(FONT, 9, "bold" if bold else "normal"), padx=12, pady=5)

            # --- шапка ---
            head = tk.Frame(root, bg=BG)
            head.pack(fill="x", padx=20, pady=(18, 6))
            label(head, "⚡ ПУЛЬТ ПК", 17, True, ACCENT).pack(side="left")
            status_pill = label(head, "● Хост работает", 10, True, OK)
            status_pill.pack(side="right")
            label(root, f"Пульт ПК Хост · порт {PORT}", 9, False, MUTED).pack(anchor="w", padx=22)

            def card(title):
                f = tk.Frame(root, bg=CARD, padx=14, pady=10)
                f.pack(fill="x", padx=20, pady=(10, 0))
                label(f, title.upper(), 8, True, MUTED).pack(anchor="w")
                return f

            def field_row(parent, value, fg=TEXT, btn_text="Копировать", masked=False):
                row = tk.Frame(parent, bg=CARD)
                row.pack(fill="x", pady=(4, 0))
                var = tk.StringVar(value=("•" * 24) if masked else value)
                ent = tk.Entry(row, textvariable=var, state="readonly", readonlybackground=FIELD, fg=fg,
                               relief="flat", font=("Consolas", 10), bd=6)
                ent.pack(side="left", fill="x", expand=True)
                state = {"shown": not masked}

                if masked:
                    def toggle():
                        state["shown"] = not state["shown"]
                        var.set(value if state["shown"] else "•" * 24)
                        eye.config(text="🙈" if state["shown"] else "👁")
                    eye = button(row, "👁", toggle)
                    eye.pack(side="right", padx=(6, 0))
                copy_btn = button(row, btn_text, lambda: _copy_to_clipboard(value), bg=ACCENT, fg="#00131A", bold=True)
                copy_btn.pack(side="right", padx=(6, 0))
                return var

            # --- адреса в Wi-Fi ---
            c1 = card("Адрес в домашней сети (Wi-Fi)")
            ips = [ip for ip in get_local_ips() if not ip.startswith("169.254.")] or ["127.0.0.1"]
            for ip in ips:
                field_row(c1, f"{ip}", fg=TEXT)

            # --- сопряжение с телефоном ---
            open_pairing(300)  # пока окно открыто, телефон в домашней сети может получить токен кнопкой
            c0 = card("Подключение телефона")
            pair_lbl = label(c0, "", 10, True, OK)
            pair_lbl.pack(anchor="w", pady=(4, 0))
            label(c0, "В приложении: Опции → «Найти ПК в сети Wi-Fi»", 9, False, MUTED).pack(anchor="w", pady=(2, 0))
            button(c0, "🔓 Продлить на 5 минут", lambda: open_pairing(300), bg=BTN).pack(anchor="w", pady=(6, 0))

            # --- токен ---
            c2 = card("Секретный токен (вручную)")
            field_row(c2, TOKEN, fg=OK, btn_text="Копировать", masked=True)

            # --- вне дома: Tailscale ---
            c3 = card("Вне дома (Tailscale)")
            tun_state = label(c3, "", 10, True, WARN)
            tun_state.pack(anchor="w", pady=(4, 0))
            ts_var = tk.StringVar(value="")
            tun_url = tk.Entry(c3, state="readonly", readonlybackground=FIELD, fg=WARN, relief="flat",
                               font=("Consolas", 9), bd=6, textvariable=ts_var)
            tun_url.pack(fill="x", pady=(4, 0))
            label(c3, "Установите Tailscale на ПК и телефоне и войдите в один аккаунт — приложение подключится само.", 9, False, MUTED).pack(anchor="w", pady=(6, 0))
            button(c3, "Копировать адрес", lambda: _copy_to_clipboard(ts_var.get()), bg=BTN).pack(anchor="w", pady=(6, 0))

            def refresh():
                left = pairing_seconds_left()
                if left > 0:
                    pair_lbl.config(text=f"🔓 Сопряжение открыто ещё {left // 60}:{left % 60:02d}", fg=OK)
                else:
                    pair_lbl.config(text="🔒 Сопряжение закрыто", fg=MUTED)
                ts_ip = get_tailscale_ip()
                if ts_ip:
                    tun_state.config(text="● Tailscale подключён", fg=OK)
                else:
                    tun_state.config(text="● Tailscale не найден (не запущен или не установлен)", fg=BAD)
                ts_var.set(ts_ip or "—")
                root.after(1500, refresh)

            refresh()

            # --- нижние кнопки ---
            foot = tk.Frame(root, bg=BG)
            foot.pack(fill="x", padx=20, pady=16)
            button(foot, "📁 Папка данных", lambda: os.startfile(BASE_DIR)).pack(side="left")
            upd_lbl = label(foot, f"v{VERSION}", 9, False, MUTED)
            upd_lbl.config(wraplength=300, justify="left")
            upd_lbl.pack(side="left", padx=10)

            def check_updates():
                from .updates import check_server_update, install_server_update, fetch_latest_apk
                def work():
                    has, msg = check_server_update()
                    apk_msg = fetch_latest_apk()
                    root.after(0, lambda: upd_lbl.config(text=f"{msg}. {apk_msg}"))
                    if has:
                        root.after(0, lambda: upd_lbl.config(text="Устанавливаю новую версию, Хост перезапустится сам…"))
                        res = install_server_update()
                        root.after(0, lambda: upd_lbl.config(text=res))
                threading.Thread(target=work, daemon=True).start()
                upd_lbl.config(text="Проверяю…")

            btn_upd = button(foot, "⬆ Обновления", check_updates)
            btn_upd.pack(side="left")
            button(foot, "Закрыть", root.destroy).pack(side="right")

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

            user32.AppendMenuW(hmenu, MF_STRING, 6, "🔓 Разрешить сопряжение телефона (5 мин)")
            user32.AppendMenuW(hmenu, MF_STRING, 7, "⬆️ Проверить обновления")
            user32.AppendMenuW(hmenu, MF_STRING, 3, "📁 Открыть папку данных и логов")
            user32.AppendMenuW(hmenu, MF_STRING, 4, "🔄 Перезапустить Хост")
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
            elif cmd == 6:
                open_pairing(300)
            elif cmd == 7:
                show_status_dialog()
            elif cmd == 3:
                os.startfile(BASE_DIR)
            elif cmd == 4:
                log("[Tray] Перезапуск Хоста по запросу из трея...")
                time.sleep(0.5)
                cmd_line = restart_command()
                from .config import DETACHED_FLAGS
                import subprocess
                subprocess.Popen(cmd_line, cwd=BASE_DIR, creationflags=DETACHED_FLAGS)
                remove_tray_icon()
                os._exit(0)
            elif cmd == 5:
                log("[Tray] Завершение работы Хоста по запросу пользователя.")
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
        hicon = None
        _ico = _icon_path()
        if _ico:
            try:
                user32.LoadImageW.restype = wintypes.HANDLE
                hicon = user32.LoadImageW(None, _ico, 1, 0, 0, 0x10 | 0x40)  # IMAGE_ICON, LR_LOADFROMFILE|LR_DEFAULTSIZE
            except Exception:
                hicon = None
        if not hicon:
            hicon = user32.LoadIconW(0, IDI_APPLICATION)

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = hicon
        nid.szTip = f"Пульт ПК Хост {VERSION} · порт {PORT}"

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
