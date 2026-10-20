"""Vocal melody extraction — RMVPE pitch curve → notes + song key.

RMVPE (robust model for vocal pitch, the RVC ecosystem's F0 backend)
produces a per-frame pitch curve with confidence on a ~10 ms grid.
On Pi 5 it runs ~0.11x realtime, so a full song's curve costs ~30 s —
fine as queue-time precompute.

Validated on real mixes: where RMVPE, CREPE and the USDB chart disagree,
the two extractors agree with each other — the residual disagreement is
chart error, not extraction error. The curve is usable as-is.

Two consumers:
  * ``word_notes`` — dominant pitch per lyric-word window → chart notes
  * ``estimate_key`` — pitch-class histogram → Krumhansl key, light
    metadata + a constraint for snapping stray detections
"""
from __future__ import annotations

import numpy as np

C2 = 65.4064                    # PitchDetector's base: tone_abs 0


def extract_curve(audio16: np.ndarray, model_path, conf_thr=0.5):
    """16 kHz mono float32 → (times_s, f0_hz, conf) RMVPE curve."""
    from rmvpe_onnx import RMVPE
    rmv = RMVPE(str(model_path))
    times, f0, conf, _ = rmv.predict(
        np.asarray(audio16, dtype=np.float32), 16000)
    return times, f0, conf


def _semitone_curve(times, f0, conf, conf_thr=0.5):
    """f0 curve → semitone positions (C2=0), NaN where unvoiced."""
    s = 12 * np.log2(np.maximum(f0, 1e-6) / C2)
    s[(f0 <= 0) | (conf < conf_thr)] = np.nan
    return times, s


def word_notes(times, f0, conf, word_spans, conf_thr=0.5):
    """Dominant pitch per word window.

    ``word_spans``: [(text, start_ms, end_ms)]. Returns one dict per
    word: {"start_ms","end_ms","tone"|None,"conf"} — None tone means
    unreliable/unvoiced (emit a freestyle note, don't score pitch).
    """
    ts, s = _semitone_curve(times, f0, conf, conf_thr)
    tms = ts * 1000.0
    out = []
    for text, a, b in word_spans:
        sel = (tms >= a) & (tms < b)
        vals = s[sel]
        vals = vals[~np.isnan(vals)]
        if len(vals) < 3:
            out.append({"start_ms": a, "end_ms": b, "tone": None,
                        "conf": 0.0})
            continue
        # quantized mode, confidence-weighted: the note the vocal holds
        q = np.round(vals)
        tones, counts = np.unique(q, return_counts=True)
        tone = int(tones[counts.argmax()])
        frac = counts.max() / counts.sum()
        out.append({"start_ms": a, "end_ms": b, "tone": tone,
                    "conf": float(frac)})
    return out


def estimate_key(times, f0, conf, conf_thr=0.5):
    """Krumhansl-Schmuckler key from the pitch-class histogram."""
    _, s = _semitone_curve(times, f0, conf, conf_thr)
    vals = s[~np.isnan(s)]
    if len(vals) < 200:
        return None
    hist = np.bincount(np.mod(np.round(vals).astype(int), 12),
                       minlength=12).astype(float)
    hist /= hist.sum()
    major = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                      2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    minor = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                      2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
    names = ("C", "C#", "D", "D#", "E", "F",
             "F#", "G", "G#", "A", "A#", "B")

    def score(profile, root):
        p = np.roll(profile, -root)
        return np.corrcoef(hist, p)[0, 1]

    best = max([(score(major, r), f"{names[r]} major") for r in range(12)]
               + [(score(minor, r), f"{names[r]} minor")
                  for r in range(12)])
    return best[1] if best[0] > 0.3 else None
