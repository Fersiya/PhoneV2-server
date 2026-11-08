"""H.264-поток экрана (бета): кадры от захвата кодируются через ffmpeg (аппаратно, если есть) и раздаются зрителям пакетами.

Формат пакета для приложения: >I длина, B флаги (1 = ключевой кадр), 4×float32 «view» (область экрана), затем данные H.264 (Annex-B).
"""
import collections
import os
import struct
import subprocess
import sys
import threading
import time

from .config import BASE_DIR, log

__all__ = ["H264Pipeline", "split_access_units", "pack_packet", "pick_encoder", "find_ffmpeg"]

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
_encoder_cache = {"name": None, "args": None, "tested": False}
_encoder_lock = threading.Lock()


def find_ffmpeg() -> str:
    """Путь к ffmpeg: из пакета imageio-ffmpeg, рядом с программой или из PATH."""
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    for cand in (os.path.join(BASE_DIR, "ffmpeg.exe"), os.path.join(os.path.dirname(sys.argv[0] or "."), "ffmpeg.exe")):
        if os.path.exists(cand):
            return cand
    return "ffmpeg"


def pack_packet(data: bytes, key: bool, view) -> bytes:
    return struct.pack(">IBffff", len(data), 1 if key else 0, *view) + data


def _nal_types(buf: bytes):
    """Позиции стартовых кодов и типы NAL в буфере: [(позиция_старткода, тип)]."""
    out = []
    i = 0
    n = len(buf)
    while True:
        j = buf.find(b"\x00\x00\x01", i)
        if j < 0 or j + 3 >= n:
            break
        start = j - 1 if j > 0 and buf[j - 1] == 0 else j
        out.append((start, buf[j + 3] & 0x1F))
        i = j + 3
    return out


def split_access_units(buf: bytes):
    """Режет поток Annex-B на кадры по NAL «access unit delimiter» (тип 9).
    Возвращает ([(данные, ключевой)], остаток) — последний (неполный) кадр остаётся в остатке."""
    nals = _nal_types(buf)
    aud = [p for p, t in nals if t == 9]
    if len(aud) < 2:
        return [], buf
    units = []
    for a, b in zip(aud, aud[1:]):
        chunk = buf[a:b]
        key = any(t == 5 for p, t in nals if a <= p < b)
        units.append((chunk, key))
    return units, buf[aud[-1]:]


def _encoder_options(name: str, fps: int, kbps: int):
    common = ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{max(200, kbps // 2)}k", "-g", str(max(10, fps))]
    if name == "libx264":
        return ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-x264-params", "aud=1:bframes=0"] + common
    if name == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll", "-bf", "0", "-aud", "1", "-delay", "0"] + common
    if name == "h264_amf":
        return ["-c:v", "h264_amf", "-usage", "ultralowlatency", "-quality", "speed", "-bf", "0", "-aud", "1"] + common
    if name == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-bf", "0", "-aud", "1"] + common
    return ["-c:v", name] + common


def pick_encoder(ffmpeg: str, fps: int = 60, kbps: int = 4000):
    """Находит рабочий кодировщик (видеокарта → процессор) пробным запуском. Результат запоминается."""
    with _encoder_lock:
        if _encoder_cache["tested"]:
            return _encoder_cache["name"]
        for name in ("h264_nvenc", "h264_amf", "h264_qsv", "libx264"):
            try:
                cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:r=30:d=0.5"] \
                      + _encoder_options(name, fps, kbps) + ["-pix_fmt", "yuv420p", "-f", "h264", "-"]
                r = subprocess.run(cmd, capture_output=True, timeout=15, creationflags=_NO_WINDOW if os.name == "nt" else 0)
                if r.returncode == 0 and r.stdout and any(t == 9 for _, t in _nal_types(r.stdout)):
                    _encoder_cache.update(name=name, tested=True)
                    log(f"[H264] кодировщик: {name}")
                    return name
                log(f"[H264] {name} не подошёл (код {r.returncode}, AUD {'есть' if r.stdout else 'нет'})")
            except Exception as e:
                log(f"[H264] {name} не запустился: {e}")
        _encoder_cache["tested"] = True
        return None


class H264Pipeline:
    """Один процесс ffmpeg на поток. push() подаёт кадры (BGR), клиенты читают пакеты через wait_packets()."""

    def __init__(self, fps: int, kbps: int) -> None:
        self.fps = fps
        self.kbps = kbps
        self.cond = threading.Condition()
        self.packets = collections.deque(maxlen=300)  # (номер, пакет-bytes, ключевой)
        self.seq = 0
        self.views = collections.deque()
        self.proc = None
        self.size = None
        self.error = None
        self.encoder = None
        self.alive = True

    def _start(self, w: int, h: int) -> None:
        ffmpeg = find_ffmpeg()
        name = pick_encoder(ffmpeg, self.fps, self.kbps)
        if not name:
            raise RuntimeError("ffmpeg не найден или нет рабочего H.264-кодировщика")
        self.encoder = name
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
               "-video_size", f"{w}x{h}", "-framerate", str(self.fps), "-i", "-"] \
              + _encoder_options(name, self.fps, self.kbps) \
              + ["-pix_fmt", "yuv420p", "-bsf:v", "dump_extra=freq=keyframe", "-f", "h264", "-"]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     creationflags=_NO_WINDOW if os.name == "nt" else 0)
        self.size = (w, h)
        threading.Thread(target=self._reader, daemon=True).start()
        log(f"[H264] поток {w}x{h}, {self.fps} к/с, {self.kbps} кбит/с, {name}")

    def push(self, img, view) -> None:
        if not self.alive or self.error:
            return
        try:
            if self.proc is None:
                self._start(img.shape[1], img.shape[0])
            self.views.append(view)
            self.proc.stdin.write(img.tobytes())
        except Exception as e:
            self.error = str(e)
            log(f"[H264] ошибка кодирования: {e}")
            with self.cond:
                self.cond.notify_all()

    def _reader(self) -> None:
        buf = b""
        t_log, n, nbytes = time.monotonic(), 0, 0
        try:
            while self.alive:
                chunk = self.proc.stdout.read1(65536) if hasattr(self.proc.stdout, "read1") else self.proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                units, buf = split_access_units(buf)
                for data, key in units:
                    view = self.views.popleft() if self.views else (0.0, 0.0, 1.0, 1.0)
                    pkt = pack_packet(data, key, view)
                    with self.cond:
                        self.seq += 1
                        self.packets.append((self.seq, pkt, key))
                        self.cond.notify_all()
                    n += 1
                    nbytes += len(data)
                now = time.monotonic()
                if now - t_log >= 10 and n:
                    log(f"[H264] {n / (now - t_log):.0f} кадр/с, {nbytes * 8 / (now - t_log) / 1000:.0f} кбит/с")
                    t_log, n, nbytes = now, 0, 0
        except Exception as e:
            if self.alive:
                self.error = str(e)
        finally:
            if self.alive and not self.error:
                self.error = "ffmpeg завершился"
            with self.cond:
                self.cond.notify_all()

    def wait_packets(self, cursor: int, timeout: float = 2.0):
        """Новые пакеты после cursor. cursor = -1 или слишком старый — начинаем с последнего ключевого кадра.
        Возвращает (новый_cursor, [пакеты])."""
        with self.cond:
            if not self.packets or (cursor >= 0 and self.seq == cursor):
                self.cond.wait(timeout)
            if not self.packets:
                return cursor, []
            first = self.packets[0][0]
            if cursor < 0 or cursor < first - 1 or self.seq - cursor > 90:
                keys = [s for s, _, k in self.packets if k]
                if not keys:
                    return cursor, []
                cursor = keys[-1] - 1
            out = [p for s, p, _ in self.packets if s > cursor]
            return self.seq, out

    def stop(self) -> None:
        self.alive = False
        try:
            if self.proc:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            if self.proc:
                self.proc.kill()
        except Exception:
            pass
        with self.cond:
            self.cond.notify_all()
