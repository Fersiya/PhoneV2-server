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

    def _run_pa(self) -> bool:
        """Захват через PyAudioWPatch (WASAPI loopback). True — звук реально работал."""
        ok = False
        try:
            import pyaudiowpatch as pa
        except Exception as e:
            self.error = f"нет модуля захвата звука: {e}"
            log(f"[Audio] {self.error}")
            return False
        p = None
        stream = None
        try:
            p = pa.PyAudio()
            try:
                apis = [p.get_host_api_info_by_index(i)["name"] for i in range(p.get_host_api_count())]
                log(f"[Audio] звуковые API: {apis}; устройств всего: {p.get_device_count()}")
                for i in range(p.get_device_count()):
                    d = p.get_device_info_by_index(i)
                    log(f"[Audio]   #{i} {d.get('name')} | API {d.get('hostApi')} | вх {d.get('maxInputChannels')} вых {d.get('maxOutputChannels')} | loopback={d.get('isLoopbackDevice', False)}")
            except Exception as e:
                log(f"[Audio] не удалось перечислить устройства: {e}")
            loopbacks = list(p.get_loopback_device_info_generator())
            if not loopbacks:
                raise RuntimeError("в системе нет loopback-устройств WASAPI")
            first = None
            try:
                wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
                dflt = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
                if dflt.get("isLoopbackDevice"):
                    first = dflt
                else:
                    first = next((lb for lb in loopbacks if dflt["name"] in lb["name"]), None)
            except Exception as e:
                log(f"[Audio] устройство вывода по умолчанию не определено ({e}) — перебираю все")
            candidates = ([first] if first else []) + [lb for lb in loopbacks if not first or lb["index"] != first["index"]]
            log("[Audio] кандидаты: " + "; ".join(f"{d['name']} ({d['maxInputChannels']} кан., {int(d['defaultSampleRate'])} Гц)" for d in candidates))
            stream = None
            last_err = None
            for dev in candidates:
                native = max(1, int(dev["maxInputChannels"]))
                rate = int(dev["defaultSampleRate"])
                for ch_open in dict.fromkeys((native, 2, 1)):
                    out_ch = min(ch_open, 2)

                    def cb(in_data, frame_count, time_info, status, _o=ch_open, _c=out_ch):
                        data = in_data
                        if _o > _c:  # многоканальный звук (5.1/7.1) — берём передние левый и правый
                            import numpy as np
                            data = np.frombuffer(in_data, dtype=np.int16).reshape(-1, _o)[:, :_c].tobytes()
                        self._publish(data)
                        return (None, pa.paContinue)

                    try:
                        stream = p.open(format=pa.paInt16, channels=ch_open, rate=rate, frames_per_buffer=rate // 50,
                                        input=True, input_device_index=dev["index"], stream_callback=cb)
                        spk = dev
                        ch = out_ch
                        break
                    except Exception as e:
                        last_err = f"{dev.get('name')} ({ch_open} кан., {rate} Гц): {e}"
                        log(f"[Audio] не удалось открыть {last_err}")
                if stream is not None:
                    break
            if stream is None:
                raise RuntimeError(f"не удалось открыть ни одно устройство; последнее: {last_err}")
            self.rate, self.channels = rate, ch
            log(f"[Audio] захват звука: {spk['name']}, {rate} Гц, каналов {ch}")
            with self.cond:
                self.ready = True
                self.cond.notify_all()
            ok = True
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
        return ok

    def _run_sc(self) -> bool:
        """Запасной способ: библиотека soundcard (WASAPI loopback через Media Foundation)."""
        ok = False
        try:
            import numpy as np
            import soundcard as sc
        except Exception as e:
            log(f"[Audio] запасной захват (soundcard) недоступен: {e}")
            return False
        try:
            spk = sc.default_speaker()
            log(f"[Audio] soundcard: устройство вывода «{spk.name}»; все выходы: {[str(x.name) for x in sc.all_speakers()]}")
            mic = sc.get_microphone(str(spk.id), include_loopback=True)
            rate, ch = 48000, 2
            block = rate // 50
            with mic.recorder(samplerate=rate, channels=ch, blocksize=block) as rec:
                self.rate, self.channels = rate, ch
                log(f"[Audio] захват звука (soundcard): {spk.name}, {rate} Гц, стерео")
                with self.cond:
                    self.ready = True
                    self.cond.notify_all()
                ok = True
                while self.alive and self.clients > 0:
                    data = rec.record(numframes=block)
                    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
                    self._publish(pcm.tobytes())
        except Exception as e:
            self.error = f"soundcard: {e}"
            log(f"[Audio] ошибка запасного захвата: {e}")
        return ok

    def _run(self) -> None:
        try:
            if not self._run_pa() and self.alive and self.clients > 0:
                log("[Audio] основной захват не сработал — пробую запасной (soundcard)")
                if not self._run_sc():
                    threading.Thread(target=_log_windows_audio_diag, daemon=True).start()
        finally:
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


_diag_done = False


def _log_windows_audio_diag() -> None:
    """Один раз за запуск пишет в лог, какие звуковые устройства и конечные точки видит Windows."""
    global _diag_done
    if _diag_done or not IS_WINDOWS:
        return
    _diag_done = True
    import subprocess
    cmds = {
        "AudioEndpoint": "Get-PnpDevice -Class AudioEndpoint | Select-Object Status,FriendlyName | Format-Table -HideTableHeaders | Out-String -Width 200",
        "SoundDevice": "Get-CimInstance Win32_SoundDevice | Select-Object Status,Name | Format-Table -HideTableHeaders | Out-String -Width 200",
        "Сессия": "whoami; (Get-Process -Id $PID).SessionId; Get-Service Audiosrv,AudioEndpointBuilder | Select-Object Name,Status | Format-Table -HideTableHeaders | Out-String -Width 200",
    }
    for name, cmd in cmds.items():
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                               timeout=12, creationflags=0x08000000)
            out = (r.stdout or b"").decode("cp866", "replace").strip() or (r.stderr or b"").decode("cp866", "replace").strip()
            for line in out.splitlines():
                if line.strip():
                    log(f"[Audio][{name}] {line.strip()}")
        except Exception as e:
            log(f"[Audio][{name}] не удалось получить: {e}")


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
