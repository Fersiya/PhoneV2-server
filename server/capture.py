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
    "acquire_hub",
    "set_stream_view",
    "release_hub",
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


class _Buf:
    """Буфер съёмки: своя пара DC+bitmap, чтобы кодировать один кадр, пока снимается следующий."""

    def __init__(self, hdc_screen, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.hdc = _gdi32.CreateCompatibleDC(hdc_screen)
        self.hbmp = _gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
        self.old = _gdi32.SelectObject(self.hdc, self.hbmp)

    def free(self) -> None:
        try:
            _gdi32.SelectObject(self.hdc, self.old)
            _gdi32.DeleteObject(self.hbmp)
            _gdi32.DeleteDC(self.hdc)
        except Exception:
            pass


def _screen_rect(all_monitors: bool, max_width: int):
    if all_monitors:
        sx, sy = _user32.GetSystemMetrics(76), _user32.GetSystemMetrics(77)
        sw, sh = _user32.GetSystemMetrics(78), _user32.GetSystemMetrics(79)
    else:
        sx = sy = 0
        sw, sh = _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)
    if sw <= 0 or sh <= 0:
        return None
    if sw > max_width:
        dw, dh = max_width, max(1, int(sh * (max_width / sw)))
    else:
        dw, dh = sw, sh
    return sx, sy, sw, sh, dw, dh


def _grab_into(buf: "_Buf", hdc_screen, sx, sy, sw, sh, full: "_Buf" = None) -> None:
    if sw == buf.w and sh == buf.h:
        _gdi32.BitBlt(buf.hdc, 0, 0, buf.w, buf.h, hdc_screen, sx, sy, 0x00CC0020)
    elif full is not None:
        # Способ 2: быстро копируем экран целиком в память, а уменьшаем уже из памяти
        _gdi32.BitBlt(full.hdc, 0, 0, sw, sh, hdc_screen, sx, sy, 0x00CC0020)
        _gdi32.SetStretchBltMode(buf.hdc, 3)
        _gdi32.StretchBlt(buf.hdc, 0, 0, buf.w, buf.h, full.hdc, 0, 0, sw, sh, 0x00CC0020)
    else:
        _gdi32.SetStretchBltMode(buf.hdc, 3)
        _gdi32.StretchBlt(buf.hdc, 0, 0, buf.w, buf.h, hdc_screen, sx, sy, sw, sh, 0x00CC0020)
    pt = wintypes.POINT()
    if _user32.GetCursorPos(ctypes.byref(pt)):
        cx = int((pt.x - sx) * buf.w / sw)
        cy = int((pt.y - sy) * buf.h / sh)
        old_pen = _gdi32.SelectObject(buf.hdc, _get_cached_pen())
        old_brush = _gdi32.SelectObject(buf.hdc, _get_cached_brush())
        _gdi32.Ellipse(buf.hdc, cx - 6, cy - 6, cx + 6, cy + 6)
        _gdi32.SelectObject(buf.hdc, old_pen)
        _gdi32.SelectObject(buf.hdc, old_brush)


def _encode_buf(buf: "_Buf", quality: int) -> bytes:
    gp_bitmap = ctypes.c_void_p()
    stream = ctypes.c_void_p()
    try:
        if _gdiplus.GdipCreateBitmapFromHBITMAP(buf.hbmp, 0, ctypes.byref(gp_bitmap)) != 0 or not gp_bitmap:
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
        ptr = _kernel32.GlobalLock(hglobal) if size > 0 else None
        if not ptr:
            return b""
        try:
            return ctypes.string_at(ptr, size)
        finally:
            _kernel32.GlobalUnlock(hglobal)
    finally:
        if stream:
            vtbl = ctypes.cast(stream, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtbl[2])(stream)
        if gp_bitmap:
            _gdiplus.GdipDisposeImage(gp_bitmap)


_dx_cam = None
_dx_lock = threading.Lock()


def _get_dx_camera(dxcam):
    """Одна общая DXGI-камера на процесс: повторное создание после release() в dxcam ненадёжно."""
    global _dx_cam
    with _dx_lock:
        if _dx_cam is not None and not getattr(_dx_cam, "is_released", False):
            return _dx_cam
        _dx_cam = dxcam.create(output_color="BGR")
        return _dx_cam


def _drop_dx_camera() -> None:
    global _dx_cam
    with _dx_lock:
        try:
            if _dx_cam is not None:
                _dx_cam.release()
        except Exception:
            pass
        _dx_cam = None


ZOOM_WIDTH = 1600  # ширина картинки при приближении


class _FrameHub:
    """Один поток захвата на всех зрителей: кодирует кадры с нужной частотой, а каждый зритель
    забирает самый свежий (медленный телефон пропускает кадры, а не копит задержку)."""

    def __init__(self, key: tuple) -> None:
        self.key = key  # (w, q, all_monitors, fps)
        self.cond = threading.Condition()
        self.frame = b""
        self.frame_view = (0.0, 0.0, 1.0, 1.0)
        self.view = (0.0, 0.0, 1.0, 1.0)  # какая часть экрана сейчас нужна зрителю (нормализованный прямоугольник)
        self.seq = 0
        self.clients = 0
        self.alive = True
        threading.Thread(target=self._run, daemon=True).start()

    def set_view(self, x0: float, y0: float, x1: float, y1: float) -> None:
        x0, y0 = max(0.0, min(0.98, x0)), max(0.0, min(0.98, y0))
        x1, y1 = max(x0 + 0.02, min(1.0, x1)), max(y0 + 0.02, min(1.0, y1))
        self.view = (x0, y0, x1, y1)

    def _publish(self, jpg: bytes, view=(0.0, 0.0, 1.0, 1.0)) -> None:
        with self.cond:
            self.frame = jpg
            self.frame_view = view
            self.seq += 1
            self.cond.notify_all()

    def _run_dxgi(self) -> bool:
        """Захват через DXGI Desktop Duplication (на видеокарте, в разы быстрее GDI). False — недоступно, нужен GDI."""
        import io
        import queue
        from .config import log
        try:
            import dxcam
        except Exception as e:
            log(f"[Stream] DXGI-захват недоступен (нет модулей): {e}")
            return False
        try:
            import cv2
        except Exception:
            cv2 = None
        Image = ImageDraw = None
        if cv2 is None:
            try:
                from PIL import Image, ImageDraw
            except Exception as e:
                log(f"[Stream] нет ни OpenCV, ни Pillow: {e}")
                return False
        w, q, all_mons, fps = self.key
        cam = None
        first = None
        last_err = None
        # при быстрой смене качества прошлый захват может ещё освобождаться — пробуем несколько раз
        for attempt in range(8):
            try:
                cam = _get_dx_camera(dxcam)
                for _ in range(25):
                    with _dx_lock:
                        first = cam.grab()
                    if first is not None:
                        break
                    time.sleep(0.04)
                if first is not None:
                    break
                last_err = "нет кадра"
            except Exception as e:
                last_err = e
            _drop_dx_camera()
            cam = None
            time.sleep(0.4)
        if cam is None or first is None:
            log(f"[Stream] DXGI-захват не запустился: {last_err} — перехожу на GDI")
            return False
        log("[Stream] съёмка через DXGI (Desktop Duplication)" + (", кодирование OpenCV" if cv2 else ", кодирование Pillow"))
        ready: "queue.Queue" = queue.Queue(maxsize=2)
        stats = {"n": 0, "enc": 0.0, "bytes": 0}
        pub_lock = threading.Lock()
        pub = {"seq": 0}

        def encode(arr):
            sh, sw = arr.shape[0], arr.shape[1]
            vx0, vy0, vx1, vy1 = self.view
            px0, py0 = int(vx0 * sw), int(vy0 * sh)
            px1, py1 = max(px0 + 16, int(vx1 * sw)), max(py0 + 16, int(vy1 * sh))
            px1, py1 = min(px1, sw), min(py1, sh)
            px0, py0 = min(px0, px1 - 16), min(py0, py1 - 16)
            # реальные границы (после округления до пикселей) — их же получит приложение
            view = (px0 / sw, py0 / sh, px1 / sw, py1 / sh)
            zoomed = (px1 - px0) < sw * 0.98 or (py1 - py0) < sh * 0.98
            if zoomed:
                arr = arr[py0:py1, px0:px1]
            cw, ch = arr.shape[1], arr.shape[0]
            # при приближении отдаём больше пикселей — картинка остаётся чёткой
            dw = min(max(w, ZOOM_WIDTH) if zoomed else w, cw)
            dh = max(1, int(ch * dw / cw))
            pt = wintypes.POINT()
            have_cur = bool(_user32.GetCursorPos(ctypes.byref(pt)))
            cx, cy = int((pt.x - px0) * dw / cw), int((pt.y - py0) * dh / ch)
            have_cur = have_cur and 0 <= cx < dw and 0 <= cy < dh
            if cv2 is not None:
                img = cv2.resize(arr, (dw, dh), interpolation=cv2.INTER_AREA) if dw != cw else arr.copy()
                if have_cur:
                    cv2.circle(img, (cx, cy), 6, (48, 48, 255), -1)
                    cv2.circle(img, (cx, cy), 6, (255, 255, 255), 2)
                ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), q])
                return (buf.tobytes() if ok else b""), view
            img = Image.fromarray(arr[:, :, ::-1])
            if dw != cw:
                img = img.resize((dw, dh), Image.BILINEAR, reducing_gap=2.0)
            if have_cur:
                ImageDraw.Draw(img).ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=(255, 48, 48), outline=(255, 255, 255), width=2)
            out = io.BytesIO()
            img.save(out, "JPEG", quality=q)
            return out.getvalue(), view

        def encoder() -> None:
            while self.alive:
                try:
                    seq, arr = ready.get(timeout=0.5)
                except queue.Empty:
                    continue
                t0 = time.monotonic()
                try:
                    jpg, vw = encode(arr)
                except Exception:
                    jpg, vw = b"", (0.0, 0.0, 1.0, 1.0)
                dt = time.monotonic() - t0
                if jpg:
                    with pub_lock:
                        stats["enc"] += dt
                        stats["n"] += 1
                        stats["bytes"] += len(jpg)
                        if seq > pub["seq"]:  # старый кадр не показываем поверх нового
                            pub["seq"] = seq
                            self._publish(jpg, vw)

        n_enc = 3 if cv2 is not None else 2
        for _ in range(n_enc):
            threading.Thread(target=encoder, daemon=True).start()
        hz = _refresh_hz()
        if hz > 1 and fps > hz:
            log(f"[Stream] частота монитора {hz} Гц — выше нет смысла, ограничиваю {fps} → {hz}")
            fps = hz
        interval = 1.0 / fps
        last = first
        frame_no = 0
        grab_t, grabs = 0.0, 0
        errs = 0
        t_log = time.monotonic()
        try:
            while self.alive:
                t0 = time.monotonic()
                try:
                    with _dx_lock:
                        arr = cam.grab()
                    errs = 0
                except Exception:
                    arr = None
                    errs += 1
                    if errs > 30:  # устройство потеряно (смена режима экрана и т.п.) — пересоздадим при следующем запуске
                        _drop_dx_camera()
                        break
                grab_t += time.monotonic() - t0
                grabs += 1
                if arr is not None:
                    last = arr
                frame_no += 1
                try:
                    ready.put_nowait((frame_no, last))
                except queue.Full:
                    pass
                now = time.monotonic()
                if now - t_log >= 10 and stats["n"]:
                    with pub_lock:
                        n = stats["n"]
                        log(f"[Stream] DXGI: {n / (now - t_log):.0f} кадр/с (цель {fps}), кадр ≈{stats['bytes'] / n / 1024:.0f} КБ, "
                            f"съёмка {grab_t / max(grabs, 1) * 1000:.0f} мс, ресайз+JPEG {stats['enc'] / n * 1000:.0f} мс, зрителей {self.clients}")
                        stats.update(n=0, enc=0.0, bytes=0)
                    grab_t, grabs, t_log = 0.0, 0, now
                rest = interval - (time.monotonic() - t0)
                if rest > 0.0005:
                    time.sleep(rest)
        finally:
            self.alive = False
            time.sleep(0.3)
        return True

    def _run(self) -> None:
        if not IS_WINDOWS:
            return
        if not self.key[2]:  # все мониторы сразу — только через GDI
            try:
                if self._run_dxgi():
                    return
            except Exception as e:
                try:
                    from .config import log
                    log(f"[Stream] ошибка DXGI-захвата, перехожу на GDI: {e}")
                except Exception:
                    pass
        import queue
        w, q, all_mons, fps = self.key
        interval = 1.0 / fps
        free: "queue.Queue" = queue.Queue()
        ready: "queue.Queue" = queue.Queue(maxsize=1)
        stats = {"n": 0, "grab": 0.0, "enc": 0.0, "bytes": 0}
        hdc_screen = _user32.GetDC(0)

        bufs = [None, None]

        def encoder() -> None:
            while self.alive:
                try:
                    slot = ready.get(timeout=0.5)
                except queue.Empty:
                    continue
                buf = bufs[slot]
                t0 = time.monotonic()
                try:
                    jpg = _encode_buf(buf, q)
                except Exception:
                    jpg = b""
                stats["enc"] += time.monotonic() - t0
                free.put(slot)
                if jpg:
                    stats["n"] += 1
                    stats["bytes"] += len(jpg)
                    self._publish(jpg)

        threading.Thread(target=encoder, daemon=True).start()
        free.put(0)
        free.put(1)
        t_log = time.monotonic()
        full = None
        use_full = None  # None — ещё не выбрано
        try:
            while self.alive:
                t0 = time.monotonic()
                rect = _screen_rect(all_mons, w)
                if rect is None:
                    time.sleep(0.2)
                    continue
                sx, sy, sw, sh, dw, dh = rect
                try:
                    idx = free.get(timeout=1.0)
                except queue.Empty:
                    continue
                t0 = time.monotonic()
                buf = bufs[idx]
                if buf is None or buf.w != dw or buf.h != dh:
                    if buf:
                        buf.free()
                    buf = bufs[idx] = _Buf(hdc_screen, dw, dh)
                if use_full is None and (sw != dw or sh != dh):
                    # Один раз сравниваем два способа съёмки на этом ПК и выбираем быстрее
                    try:
                        trial = _Buf(hdc_screen, sw, sh)
                        res = {}
                        for mode in (False, True):
                            ts = []
                            for _ in range(6):
                                a0 = time.monotonic()
                                _grab_into(buf, hdc_screen, sx, sy, sw, sh, trial if mode else None)
                                ts.append(time.monotonic() - a0)
                            ts.sort()
                            res[mode] = ts[len(ts) // 2]
                        use_full = res[True] < res[False] * 0.85
                        full = trial if use_full else None
                        if not use_full:
                            trial.free()
                        from .config import log
                        log(f"[Stream] съёмка: напрямую {res[False] * 1000:.0f} мс, через память {res[True] * 1000:.0f} мс → "
                            f"выбрано «{'через память' if use_full else 'напрямую'}»")
                    except Exception:
                        use_full = False
                if full is not None and (full.w != sw or full.h != sh):
                    full.free()
                    full = _Buf(hdc_screen, sw, sh)
                _grab_into(buf, hdc_screen, sx, sy, sw, sh, full)
                stats["grab"] += time.monotonic() - t0
                ready.put(idx)
                now = time.monotonic()
                if now - t_log >= 10 and stats["n"]:
                    n = stats["n"]
                    try:
                        from .config import log
                        log(f"[Stream] {n / (now - t_log):.0f} кадр/с (цель {fps}), кадр ≈{stats['bytes'] / n / 1024:.0f} КБ, "
                            f"съёмка {stats['grab'] / n * 1000:.0f} мс, JPEG {stats['enc'] / n * 1000:.0f} мс, зрителей {self.clients}")
                    except Exception:
                        pass
                    stats.update(n=0, grab=0.0, enc=0.0, bytes=0)
                    t_log = now
                rest = interval - (time.monotonic() - t0)
                if rest > 0.0005:
                    time.sleep(rest)
        finally:
            self.alive = False
            time.sleep(0.6)
            for b in bufs + [full]:
                if b:
                    b.free()
            try:
                _user32.ReleaseDC(0, hdc_screen)
            except Exception:
                pass

    def wait_frame(self, last_seq: int, timeout: float = 2.0):
        with self.cond:
            if self.seq == last_seq:
                self.cond.wait(timeout)
            return self.seq, self.frame

    def wait_frame_view(self, last_seq: int, timeout: float = 2.0):
        with self.cond:
            if self.seq == last_seq:
                self.cond.wait(timeout)
            return self.seq, self.frame, self.frame_view


_hubs: dict = {}
_hubs_lock = threading.Lock()


def acquire_hub(w: int, q: int, all_mons: bool, fps: int) -> _FrameHub:
    key = (w, q, all_mons, fps)
    with _hubs_lock:
        hub = _hubs.get(key)
        if hub is None or not hub.alive:
            hub = _FrameHub(key)
            _hubs[key] = hub
        if hub.clients == 0:
            hub.view = (0.0, 0.0, 1.0, 1.0)
        hub.clients += 1
        return hub


def set_stream_view(x0: float, y0: float, x1: float, y1: float) -> None:
    """Приближение: какую часть экрана отдавать (в полном качестве) всем текущим зрителям."""
    with _hubs_lock:
        for hub in _hubs.values():
            hub.set_view(x0, y0, x1, y1)


def _refresh_hz() -> int:
    """Частота обновления основного монитора, Гц (0 — не удалось определить)."""
    try:
        hdc = _user32.GetDC(0)
        try:
            return int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 116))  # VREFRESH
        finally:
            _user32.ReleaseDC(0, hdc)
    except Exception:
        return 0


def release_hub(hub: _FrameHub) -> None:
    with _hubs_lock:
        hub.clients -= 1
        if hub.clients <= 0:
            hub.alive = False
            if _hubs.get(hub.key) is hub:
                del _hubs[hub.key]
