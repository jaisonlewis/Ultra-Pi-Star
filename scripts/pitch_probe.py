"""Pitch-extraction feasibility probe — USDB-free timing.

Run on the Pi:  .venv/bin/python scripts/pitch_probe.py

LRC synced timestamps give line windows; ASR anchors measure the
video's constant shift; PitchDetector samples the vocal-band melody
inside each line. The USDB chart is used ONLY as ground truth to score
extraction quality (agreement stats), not for timing.
"""
import os, subprocess, sys, json, sqlite3
import numpy as np

os.environ.setdefault("NEEDLE_TELEMETRY", "0")
sys.path.insert(0, "/opt/ultrapistar")
from ultrapistar import songmap, lyrics as lyr     # noqa: E402
from ultrapistar.charts import load_chart          # noqa: E402
from ultrapistar.pitch import PitchDetector        # noqa: E402

MEDIA = "media/yt-pGhwBFYtn1s.mp4"
CHART_ID = 25          # validation reference only
ENTRY_ID = 26
RATE = 16000

# --- decode: mid channel + vocal band --------------------------------
out = subprocess.run(
    ["ffmpeg", "-v", "error", "-i", MEDIA,
     "-af", "pan=mono|c0=0.5*c0+0.5*c1,highpass=f=180,lowpass=f=800",
     "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
    capture_output=True, check=True).stdout
pcm = np.frombuffer(out, dtype=np.int16)
dur_ms = len(pcm) / RATE * 1000

# --- timing: LRC lines + ASR shift ------------------------------------
asr = songmap.transcribe_media(MEDIA)
rec = lyr.fetch("Green Day", "Wake Me Up When September Ends",
                duration=286)
synced = rec["synced"]
spans = songmap.align_lines(asr, [t for _, t in synced])
deltas = [sp["onset_ms"] - synced[li][0] for li, sp in spans.items()]
shift = float(np.median(deltas))
print(f"asr {len(asr)}w | lrc {len(synced)} lines | "
      f"anchors {len(deltas)} | shift {shift:+.0f}ms")

# --- extraction: sample tone every ~92ms inside each line --------------
def detect(a_s, b_s):
    det = PitchDetector(RATE)
    tones = []
    for i in range(a_s, b_s - 1024, 2048):
        det.feed(pcm[i:i + 2048].tobytes())
        r = det.analyze()
        if r.valid:
            tones.append((i / RATE * 1000, r.tone_abs))
    return tones


melody = []            # (ms, tone_abs)
for li, (lrc_ms, text) in enumerate(synced):
    t0 = lrc_ms + shift
    t1 = (synced[li + 1][0] + shift if li + 1 < len(synced)
          else t0 + 6000)
    t1 = min(t1, t0 + 8000, dur_ms)
    a = max(0, int(t0 * RATE / 1000))
    b = int(t1 * RATE / 1000)
    melody += detect(a, b)
print(f"melody points: {len(melody)}")

# --- validation vs USDB (reference only) ------------------------------
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


mel = np.array(melody) if melody else np.zeros((0, 2))
deltas_all, used = [], []
for n in chart.tracks["P1"]:
    s_ms = gap + n.start * beat_ms + warp_at(n.start)
    e_ms = s_ms + n.length * beat_ms
    mid = (s_ms + e_ms) / 2
    if e_ms > dur_ms or len(mel) == 0:
        break
    near = mel[np.abs(mel[:, 0] - mid).argmin()]
    if abs(near[0] - mid) > 300:
        continue
    deltas_all.append(near[1] - n.pitch)
    used.append((n.start, n.pitch, near[1], n.text))

d = np.array(deltas_all)
med = np.median(d)
resid = d - med
agree = int((np.abs(resid) <= 1.5).sum())
pc_agree = int((np.abs(np.mod(resid, 12)) <= 1.5).sum()
               + (np.abs(np.mod(resid, 12) - 12) <= 1.5).sum())
print(f"\nUSDB notes compared: {len(used)}")
print(f"best transpose: {med:+.1f} st")
print(f"agree abs  ±1.5: {agree} ({100*agree/len(used):.0f}%)")
print(f"agree pitchclass: {pc_agree} ({100*pc_agree/len(used):.0f}%)")
print("\nfirst 30 (beat, exp, det, resid):")
for (s, p, t, txt), r in list(zip(used, resid))[:30]:
    print(f"  {s:5d} exp {p:3d} det {t:5.1f} r{r:+5.1f}  {txt}")
