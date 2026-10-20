"""RMVPE pitch-extraction benchmark + USDB validation.

Run on the Pi:  .venv/bin/python scripts/rmvpe_probe.py [media]

Times RMVPE over the whole song (the number that decides whether
RMVPE is viable on Pi or we fall back to stem + CREPE-tiny), then
samples the f0 curve at each USDB note's media position and reports
semitone agreement — the USDB chart is ground truth for scoring
quality only, not for timing.
"""
import json
import sqlite3
import sys
import time

import numpy as np

sys.path.insert(0, "/opt/ultrapistar")
from ultrapistar import songmap                     # noqa: E402
from ultrapistar.charts import load_chart           # noqa: E402

MEDIA = sys.argv[1] if len(sys.argv) > 1 else "media/yt-pGhwBFYtn1s.mp4"
CHART_ID = 25
ENTRY_ID = 26
MODEL = "/opt/ultrapistar/models/rmvpe.onnx"

# --- decode full mix ---------------------------------------------------
t0 = time.time()
audio = songmap.decode_media(MEDIA)                   # 16k float32
dur_s = len(audio) / 16000
print(f"decode: {time.time()-t0:.1f}s | audio {dur_s:.0f}s")

# --- RMVPE over the complete song --------------------------------------
t0 = time.time()
from rmvpe_onnx import RMVPE
rmv = RMVPE(MODEL)
times, f0, conf, _ = rmv.predict(audio.astype(np.float32), 16000)
elapsed = time.time() - t0
print(f"rmvpe: {elapsed:.1f}s | audio {dur_s:.0f}s | "
      f"RTF {elapsed/dur_s:.2f}x | frames {len(f0)}")

# --- validation vs USDB -------------------------------------------------
con = sqlite3.connect("/opt/ultrapistar/data/ultrapistar.db")
row = con.execute(
    "SELECT file_path, sync_offset_ms, sync_warp FROM chart WHERE id=?",
    (CHART_ID,)).fetchone()
chart = load_chart(row[0])
wmap = json.loads(row[2] or "{}").get(str(ENTRY_ID), {})
ratio = wmap.get("ratio", 1.0)
marks = sorted((int(b), float(o)) for b, o in wmap.get("marks", []))
beat_ms = 15000.0 / chart.bpm * ratio
gap = chart.gap_ms
base = row[1]


def warp_at(beat):
    off = base
    for wb, wo in marks:
        if wb <= beat:
            off = gap + wo
        else:
            break
    return off


def tone_at(ms):
    """Median detected tone_abs inside +-60ms of ms (voiced only)."""
    sel = (np.abs(times * 1000 - ms) <= 60) & (f0 > 0) & (conf > 0.5)
    if not sel.any():
        return None
    hz = np.median(f0[sel])
    return 12 * np.log2(hz / 65.4064)


deltas, used = [], []
for n in chart.tracks["P1"]:
    s_ms = gap + n.start * beat_ms + warp_at(n.start)
    e_ms = s_ms + n.length * beat_ms
    if e_ms / 1000 > dur_s:
        break
    t = tone_at((s_ms + e_ms) / 2)
    if t is None:
        continue
    deltas.append(t - n.pitch)
    used.append((n.start, n.pitch, t, n.text))

d = np.array(deltas)
med = np.median(d)
resid = d - med
agree = int((np.abs(resid) <= 1.5).sum())
pc = int((np.abs(np.mod(resid, 12)) <= 1.5).sum()
         + (np.abs(np.mod(resid, 12) - 12) <= 1.5).sum())
print(f"\nUSDB notes compared: {len(used)}")
print(f"best transpose: {med:+.1f} st")
print(f"agree abs  ±1.5: {agree} ({100*agree/max(1,len(used)):.0f}%)")
print(f"agree pitchclass: {pc} ({100*pc/max(1,len(used)):.0f}%)")
print("\nfirst 30 (beat, exp, det, resid):")
for (s, p, t, txt), r in list(zip(used, resid))[:30]:
    print(f"  {s:5d} exp {p:3d} det {t:5.1f} r{r:+5.1f}  {txt}")
