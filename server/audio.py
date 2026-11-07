"""Звук ПК для телефона: захват системного звука (WASAPI loopback через PyAudioWPatch) и раздача по HTTP.

Один захват на всех слушателей. Данные — «сырой» PCM 16 бит. В режиме «remote» (через интернет)
звук сжимается: моно и частота понижена примерно до 24 кГц — около 350 кбит/с вместо 1,5 Мбит/с.
"""
import threading
import time
from collections import deque

from .config import IS_WINDOWS, log


def audio_info() -> dict:
    """Доступен ли захват звука на этом ПК (для диагностики)."""
    if not IS_WINDOWS:
        return {"available": False, "reason": "только Windows"}
    try:
        import pyaudiowpatch  # noqa: F401
    except Exception as e:
        return {"available": False, "reason": f"нет модуля захвата звука: {e}"}
    return {"available": True, "reason": ""}


class _AudioHub:
    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.chunks: deque = deque(maxlen=40)  # (seq, bytes)
        self.seq = 0
        self.clients = 0
        self.alive = False
        self.ready = False
        self.rate = 48000
        self.channels = 2
        self.error = ""

    def start(self) -> None:
        self.alive = True
        self.ready = False
        self.error = ""
        threading.Thread(target=self._run, daemon=True).start()

    def _publish(self, data: bytes) -> None:
        with self.cond:
            self.seq += 1
            self.chunks.append((self.seq, data))
            self.cond.notify_all()

    def _run(self) -> None:
        try:
            import pyaudiowpatch as pa
        except Exception as e:
            self.error = f"нет модуля захвата звука: {e}"
            log(f"[Audio] {self.error}")
            with self.cond:
                self.alive = False
                self.cond.notify_all()
            return
        p = None
        stream = None
        try:
            p = pa.PyAudio()
            wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
            spk = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
            if not spk.get("isLoopbackDevice"):
                found = None
                for lb in p.get_loopback_device_info_generator():
                    if spk["name"] in lb["name"]:
                        found = lb
                        break
                if found is None:
                    raise RuntimeError("не найден loopback-вход для устройства вывода звука")
                spk = found
            rate = int(spk["defaultSampleRate"])
            ch = max(1, min(2, int(spk["maxInputChannels"])))

            def cb(in_data, frame_count, time_info, status):
                self._publish(in_data)
                return (None, pa.paContinue)

            stream = p.open(format=pa.paInt16, channels=ch, rate=rate, frames_per_buffer=rate // 50,
                            input=True, input_device_index=spk["index"], stream_callback=cb)
            self.rate, self.channels = rate, ch
            log(f"[Audio] захват звука: {spk['name']}, {rate} Гц, каналов {ch}")
            with self.cond:
                self.ready = True
                self.cond.notify_all()
            while self.alive and self.clients > 0:
                time.sleep(0.3)
        except Exception as e:
            self.error = str(e)
            log(f"[Audio] ошибка захвата звука: {e}")
        finally:
            try:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
            try:
                if p is not None:
                    p.terminate()
            except Exception:
                pass
            with self.cond:
                self.alive = False
                self.ready = False
                self.cond.notify_all()

    def wait_ready(self, timeout: float = 4.0) -> bool:
        end = time.monotonic() + timeout
        with self.cond:
            while not self.ready and self.alive and time.monotonic() < end:
                self.cond.wait(0.2)
            return self.ready

    def next_chunks(self, next_seq: int, timeout: float = 0.06):
        """Возвращает (список кусков, следующий номер). Отставший слушатель перескакивает к свежему звуку."""
        with self.cond:
            if self.seq < next_seq:
                self.cond.wait(timeout)
            if not self.chunks:
                return [], next_seq
            first = self.chunks[0][0]
            if next_seq < first or self.seq - next_seq > 12:  # отстали больше чем на ~250 мс — не копим задержку
                next_seq = max(first, self.seq - 2)
            out = [d for s, d in self.chunks if s >= next_seq]
            return out, self.seq + 1


_hub = _AudioHub()
_hub_lock = threading.Lock()


def acquire_audio():
    """Возвращает хаб или None, если звук недоступен."""
    with _hub_lock:
        if not _hub.alive:
            _hub.chunks.clear()
            _hub.start()
        _hub.clients += 1
        return _hub


def release_audio(hub: _AudioHub) -> None:
    with _hub_lock:
        hub.clients = max(0, hub.clients - 1)


class AudioConverter:
    """Приводит куски PCM к формату для слушателя: исходный (lan) или лёгкий моно ~24 кГц (remote)."""

    def __init__(self, rate: int, channels: int, light: bool) -> None:
        self.channels = channels
        self.light = light
        self.k = max(1, int(round(rate / 24000.0))) if light else 1
        self.out_rate = rate // self.k if light else rate
        self.out_channels = 1 if light else channels

    def convert(self, data: bytes) -> bytes:
        if not self.light:
            return data
        import numpy as np
        a = np.frombuffer(data, dtype=np.int16)
        if self.channels > 1:
            n = (a.size // self.channels) * self.channels
            a = a[:n].reshape(-1, self.channels).astype(np.int32).mean(axis=1)
        else:
            a = a.astype(np.int32)
        if self.k > 1:
            n = (a.size // self.k) * self.k
            a = a[:n].reshape(-1, self.k).mean(axis=1)
        return a.astype(np.int16).tobytes()
