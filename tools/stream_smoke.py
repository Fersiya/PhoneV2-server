"""Проверка трансляции на собранном Хосте: 6 секунд читаем /stream и считаем кадры."""
import json
import os
import sys
import time
import urllib.request

cfg = os.path.join(os.environ.get("LOCALAPPDATA", ""), "PCControl", "config.json")
token = json.load(open(cfg, encoding="utf-8"))["token"]
req = urllib.request.Request("http://127.0.0.1:8765/stream?fps=60&w=960&q=52", headers={"X-Token": token})
resp = urllib.request.urlopen(req, timeout=10)
frames, buf, t0 = 0, b"", time.time()
while time.time() - t0 < 6:
    chunk = resp.read(65536)
    if not chunk:
        break
    buf += chunk
    frames += buf.count(b"--frame")
    buf = buf[-16:]
fps = frames / (time.time() - t0)
print(f"stream: {frames} кадров за 6 с ({fps:.1f} кадр/с)")
sys.exit(0 if frames >= 5 else 1)
