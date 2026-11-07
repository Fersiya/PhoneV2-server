"""Проверка трансляции на собранном Хосте: ждём первый кадр (до 40 с), затем 5 секунд считаем кадры."""
import json
import os
import sys
import time
import urllib.request

cfg = os.path.join(os.environ.get("LOCALAPPDATA", ""), "PCControl", "config.json")
token = json.load(open(cfg, encoding="utf-8"))["token"]
req = urllib.request.Request("http://127.0.0.1:8765/stream?fps=60&w=960&q=52", headers={"X-Token": token})
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
sys.exit(0 if frames >= 5 else 1)
