"""Захват экрана Windows через GDI+ в JPEG с отрисовкой курсора."""
import ctypes
import threading
import time
from .config import IS_WINDOWS
from .winapi import (
    _user32,
    _gdi32,
    _gdiplus,
    _ole32,
    _kernel32,
    CLSID_JPEG,
    GUID_QUALITY,
)
if IS_WINDOWS:
    from ctypes import wintypes
    from .winapi import EncoderParameters

__all__ = [
    "capture_screen_jpeg",
    "SCREEN_CACHE_TTL",
]

_screen_lock = threading.Lock()
_last_jpeg: bytes = b""
_last_jpeg_ts: float = 0.0
_last_jpeg_key: tuple = (0, 0, False)
SCREEN_CACHE_TTL: float = 0.003  # до 120 кадров/сек (поддержка 90 FPS без задержек)

_cached_pen = None
_cached_brush = None


def _get_cached_pen():
    global _cached_pen
    if _cached_pen is None and IS_WINDOWS:
        _cached_pen = _gdi32.CreatePen(0, 2, 0x00FFFFFF)
    return _cached_pen


def _get_cached_brush():
    global _cached_brush
    if _cached_brush is None and IS_WINDOWS:
        _cached_brush = _gdi32.CreateSolidBrush(0x003030FF)
    return _cached_brush


def capture_screen_jpeg(max_width: int = 960, quality: int = 60, all_monitors: bool = False) -> bytes:
    """Делает снимок экрана Windows с отрисовкой позиции курсора и сжимает его в JPEG в памяти."""
    global _last_jpeg, _last_jpeg_ts, _last_jpeg_key
    if not IS_WINDOWS:
        return b""
    max_width = max(320, min(1920, max_width))
    quality = max(20, min(95, quality))
    req_key = (max_width, quality, all_monitors)

    with _screen_lock:
        now = time.monotonic()
        if _last_jpeg and _last_jpeg_key == req_key and (now - _last_jpeg_ts) < SCREEN_CACHE_TTL:
            return _last_jpeg

        if all_monitors:
            src_x = _user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
            src_y = _user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
            src_w = _user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
            src_h = _user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        else:
            src_x, src_y = 0, 0
            src_w = _user32.GetSystemMetrics(0)
            src_h = _user32.GetSystemMetrics(1)

        if src_w <= 0 or src_h <= 0:
            return b""

        if src_w > max_width:
            dst_w = max_width
            dst_h = max(1, int(src_h * (max_width / src_w)))
        else:
            dst_w, dst_h = src_w, src_h

        hdc_screen = _user32.GetDC(0)
        if not hdc_screen:
            return b""
        hdc_mem = _gdi32.CreateCompatibleDC(hdc_screen)
        hbmp = _gdi32.CreateCompatibleBitmap(hdc_screen, dst_w, dst_h)
        old_obj = _gdi32.SelectObject(hdc_mem, hbmp)

        gp_bitmap = ctypes.c_void_p()
        stream = ctypes.c_void_p()
        try:
            if src_w == dst_w and src_h == dst_h:
                _gdi32.BitBlt(hdc_mem, 0, 0, dst_w, dst_h, hdc_screen, src_x, src_y, 0x00CC0020)
            else:
                _gdi32.SetStretchBltMode(hdc_mem, 3)
                _gdi32.StretchBlt(hdc_mem, 0, 0, dst_w, dst_h, hdc_screen, src_x, src_y, src_w, src_h, 0x00CC0020)

            # Рисуем курсор мыши
            pt = wintypes.POINT()
            if _user32.GetCursorPos(ctypes.byref(pt)):
                cx = int((pt.x - src_x) * dst_w / src_w)
                cy = int((pt.y - src_y) * dst_h / src_h)
                pen = _get_cached_pen()
                brush = _get_cached_brush()
                old_pen = _gdi32.SelectObject(hdc_mem, pen)
                old_brush = _gdi32.SelectObject(hdc_mem, brush)
                _gdi32.Ellipse(hdc_mem, cx - 6, cy - 6, cx + 6, cy + 6)
                _gdi32.SelectObject(hdc_mem, old_pen)
                _gdi32.SelectObject(hdc_mem, old_brush)

            if _gdiplus.GdipCreateBitmapFromHBITMAP(hbmp, 0, ctypes.byref(gp_bitmap)) != 0 or not gp_bitmap:
                return b""

            if _ole32.CreateStreamOnHGlobal(None, True, ctypes.byref(stream)) != 0 or not stream:
                return b""

            q_val = wintypes.ULONG(quality)
            params = EncoderParameters()
            params.Count = 1
            params.Parameter[0].Guid = GUID_QUALITY
            params.Parameter[0].NumberOfValues = 1
            params.Parameter[0].Type = 4
            params.Parameter[0].Value = ctypes.cast(ctypes.byref(q_val), ctypes.c_void_p)

            if _gdiplus.GdipSaveImageToStream(gp_bitmap, stream, ctypes.byref(CLSID_JPEG), ctypes.byref(params)) != 0:
                return b""

            hglobal = wintypes.HGLOBAL()
            if _ole32.GetHGlobalFromStream(stream, ctypes.byref(hglobal)) != 0 or not hglobal:
                return b""

            size = _kernel32.GlobalSize(hglobal)
            if size <= 0:
                return b""
            ptr = _kernel32.GlobalLock(hglobal)
            if not ptr:
                return b""
            try:
                jpg_bytes = ctypes.string_at(ptr, size)
                _last_jpeg = jpg_bytes
                _last_jpeg_ts = time.monotonic()
                _last_jpeg_key = req_key
                return jpg_bytes
            finally:
                _kernel32.GlobalUnlock(hglobal)
        finally:
            if stream:
                vtbl = ctypes.cast(stream, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                release_fn = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtbl[2])
                release_fn(stream)
            if gp_bitmap:
                _gdiplus.GdipDisposeImage(gp_bitmap)
            _gdi32.SelectObject(hdc_mem, old_obj)
            _gdi32.DeleteObject(hbmp)
            _gdi32.DeleteDC(hdc_mem)
            _user32.ReleaseDC(0, hdc_screen)
