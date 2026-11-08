"""Проверка трансляции на собранном Хосте: ждём первый кадр (до 40 с), затем 5 секунд считаем кадры."""
import json
import os
import sys
import time
import urllib.request

cfg = os.path.join(os.environ.get("LOCALAPPDATA", ""), "PCControl", "config.json")
token = json.load(open(cfg, encoding="utf-8"))["token"]
req = urllib.request.Request("http://127.0.0.1:8765/stream?fps=60&w=960&q=52", headers={"X-Token": token})
try:
    info = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8765/api/audio_info", headers={"X-Token": token}), timeout=10).read().decode("utf-8", "replace")
    print("audio_info:", info.encode("ascii", "replace").decode())
except Exception as e:
    print("audio_info error:", str(e).encode("ascii", "replace").decode())
resp = urllib.request.urlopen(req, timeout=45)
frames, buf = 0, b""
t_start = time.time()
t_first = None
while True:
    now = time.time()
    if t_first is None and now - t_start > 40:
        break
    if t_first is not None and now - t_first >= 5:
        break
    chunk = resp.read(65536)
    if not chunk:
        break
    buf += chunk
    n = buf.count(b"--frame")
    if n and t_first is None:
        t_first = time.time()
        print(f"first frame after {t_first - t_start:.1f} s")
    frames += n
    buf = buf[-16:]
fps = frames / 5.0
print(f"stream: {frames} frames in 5 s ({fps:.1f} fps)")
ok = frames >= 5
resp.close()

# H.264 (бета): информативная проверка — не роняет сборку, но показывает, работает ли ffmpeg внутри собранного exe
try:
    import struct
    r2 = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8765/h264?fps=30&w=960&kbps=3000", headers={"X-Token": token}), timeout=30)
    print("h264 headers:", r2.headers.get("X-Width"), r2.headers.get("X-Height"), r2.headers.get("X-Encoder"))
    head = r2.read(21)
    n, flags = struct.unpack(">IB", head[:5])
    body = r2.read(min(n, 64))
    print(f"h264: first packet {n} bytes, key={flags & 1}, starts {body[:6].hex()}")
    r2.close()
except Exception as e:
    print("h264 (non-fatal):", str(e).encode("ascii", "replace").decode())
sys.exit(0 if ok else 1)
