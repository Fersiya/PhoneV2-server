"""Работа с системным буфером обмена Windows через WinAPI."""
import ctypes
from .config import IS_WINDOWS
from .winapi import (
    _user32,
    _kernel32,
    CF_UNICODETEXT,
)

__all__ = [
    "get_clipboard_text",
    "set_clipboard_text",
]


def get_clipboard_text() -> str:
    if not IS_WINDOWS:
        return ""
    if not _user32.OpenClipboard(0):
        return ""
    try:
        hmem = _user32.GetClipboardData(CF_UNICODETEXT)
        if not hmem:
            return ""
        ptr = _kernel32.GlobalLock(hmem)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            _kernel32.GlobalUnlock(hmem)
    except Exception:
        return ""
    finally:
        _user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    if not IS_WINDOWS:
        return False
    if not _user32.OpenClipboard(0):
        return False
    try:
        _user32.EmptyClipboard()
        raw = (text + "\0").encode("utf-16-le")
        hmem = _kernel32.GlobalAlloc(0x0042, len(raw))
        if not hmem:
            return False
        ptr = _kernel32.GlobalLock(hmem)
        if not ptr:
            _kernel32.GlobalFree(hmem)
            return False
        ctypes.memmove(ptr, raw, len(raw))
        _kernel32.GlobalUnlock(hmem)
        if not _user32.SetClipboardData(CF_UNICODETEXT, hmem):
            _kernel32.GlobalFree(hmem)
            return False
        return True
    except Exception:
        return False
    finally:
        _user32.CloseClipboard()
