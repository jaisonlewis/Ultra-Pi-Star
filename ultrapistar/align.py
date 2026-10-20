"""Offline chart→media alignment via chroma cross-correlation.

The mic-based solvers (sing.py) can only react to singing and go blind
during instrumentals. The media file itself contains everything — this
module decodes it once, builds a 12-bin chromagram, and aligns the
chart's expected pitch-class contour against it in two stages:

1. global — FFT-correlate the whole-chart chroma template over the full
   media timeline. The peak answers "where does chart beat 0 sit in
   this media" even when the community GAP is ~90 s wrong.
2. local — slide each phrase's chroma signature around its predicted
   position. Every phrase lands at its own media time: phrases that
   jump reveal where the video cut or extended an instrumental, and
   phrases with no confident match are sections the media doesn't
   contain at all.

Pace falls out of a regression over the matched phrases: the slope of
observed-time vs chart-beat is the media's tempo ratio. Arrangement
edits show up as residual steps, not slope — a stretched solo can't
fake a faster song.

The result is the same per-phrase warp map the live solvers learn
(sync_offset_ms + chart.sync_warp entries), computed before the song
starts instead of discovered mid-performance.
"""

from __future__ import annotations

import math
import subprocess

import numpy as np

RATE = 22050
HOP = 512            # ~23 ms per chroma frame
WIN = 4096           # ~186 ms analysis window
LOCAL_SEARCH_S = 45.0    # per-phrase search radius around prediction
MIN_SCORE = 0.22         # normalized match score for a confident phrase
PROBE_RATIOS = (0.92, 0.96, 1.0, 1.04, 1.08)   # in-phrase tempo probe


def load_audio(path, rate: int = RATE) -> np.ndarray:
    """Decode any media file to mono float32 PCM via ffmpeg."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(path),
           "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32)


def chromagram(audio: np.ndarray, rate: int = RATE) -> np.ndarray:
    """(12, frames) log-magnitude pitch-class energy, normalized."""
    n = 1 + max(0, (len(audio) - WIN) // HOP)
    frames = np.lib.stride_tricks.as_strided(
        audio, shape=(n, WIN), strides=(audio.strides[0] * HOP,
                                        audio.strides[0])).copy()
    frames *= np.hanning(WIN)
    mag = np.abs(np.fft.rfft(frames, axis=1))          # (n, WIN//2+1)
    freqs = np.fft.rfftfreq(WIN, 1.0 / rate)
    keep = (freqs > 60.0) & (freqs < 2000.0)           # vocal+harmony band
    midi = np.round(69 + 12 * np.log2(freqs[keep] / 440.0)).astype(int)
    pc = midi % 12
    c = np.zeros((n, 12), dtype=np.float32)
    np.add.at(c.T, pc, np.log1p(mag[:, keep] * 100.0).T)
    c /= (c.max(axis=1, keepdims=True) + 1e-6)
    return c.T                                        # (12, n)


def _template(notes, beat_ms: float) -> tuple[np.ndarray, int]:
    """Expected chroma rows for a note list at frame resolution, plus
    the first note's beat."""
    s = min(n.start for n in notes)
    e = max(n.start + n.length for n in notes)
    fpb = beat_ms / (HOP / RATE * 1000.0)              # frames per beat
    n_fr = max(4, int(math.ceil((e - s) * fpb)))
    t = np.zeros((12, n_fr), dtype=np.float32)
    for note in notes:
        if note.kind == "freestyle":
            continue
        a = int(round((note.start - s) * fpb))
        b = int(round((note.start + note.length - s) * fpb))
        t[note.pitch % 12, a:b] = 1.0
    return t, s


def _corr(template: np.ndarray, chroma: np.ndarray) -> np.ndarray:
    """Normalized cross-correlation of a (12, w) template over the
    (12, n) chromagram; result length n - w + 1."""
    w = template.shape[1]
    n = chroma.shape[1]
    size = 1 << (n + w - 1).bit_length()
    C = np.fft.rfft(chroma, size, axis=1)
    T = np.fft.rfft(template[:, ::-1], size, axis=1)
    conv = np.fft.irfft(C * T, size, axis=1).sum(axis=0)
    return conv[w - 1:n] / max(1, int((template > 0).sum()))


def _match_at(template, chroma, pred_fr, radius_fr):
    """Best template position near pred_fr; (shift_frames, score).

    Among near-tied peaks the one nearest the prediction wins — the
    stage-1 global estimate already pins us down, so preferring the
    smallest shift keeps repeated-melody aliases from hopping the
    phrase to a different repetition."""
    w = template.shape[1]
    lo = max(0, int(pred_fr) - radius_fr)
    hi = min(chroma.shape[1] - w, int(pred_fr) + radius_fr)
    if hi <= lo:
        return 0.0, 0.0
    scores = _corr(template, chroma[:, lo:hi + w])
    if len(scores) == 0:
        return 0.0, 0.0
    best = float(scores.max())
    if best < MIN_SCORE:
        return 0.0, 0.0
    inner = scores[1:-1]
    peaks = np.nonzero((inner > scores[:-2]) & (inner >= scores[2:]))[0] + 1
    peaks = peaks[scores[peaks] >= best - 0.03]
    if len(peaks) == 0:
        peaks = np.array([int(scores.argmax())])
    k = int(peaks[np.argmin(np.abs(peaks + lo - pred_fr))])
    return float(k + lo - pred_fr), float(scores[k])


def align_chart(chart, media_path: str, track: str | None = None) -> dict | None:
    """Align ``chart`` to ``media_path``.

    Returns {"offset_ms", "tempo", "warp": [[beat, off_ms]...],
    "phrases", "matched", "peak"} or None when alignment can't be
    trusted.
    """
    if track is None:
        track = max(chart.tracks, key=lambda t: len(chart.tracks[t]))
    audio = load_audio(media_path)
    if len(audio) < RATE * 5:
        return None
    chroma = chromagram(audio)
    hop_ms = HOP / RATE * 1000.0
    beat_ms = 15000.0 / chart.bpm if chart.bpm else 0
    if beat_ms <= 0:
        return None
    lines = [ln for ln in chart.lines(track) if ln]
    if not lines:
        return None

    # --- stage 1: candidate global offsets at nominal tempo ----------- #
    # The whole-chart correlation can alias COHERENTLY: when a song's
    # melody repeats on a fixed ~16-20 s cycle, the entire template
    # scores nearly as well shifted one cycle early, and the argmax can
    # pick it. So take several strong peaks and let stage 1.5 decide —
    # under a shifted base, per-phrase predictions land a full cycle off
    # and fail tight-window matching; under the right base they sit
    # within a few seconds of where the melody actually is.
    tpl, s0 = _template(chart.tracks[track], beat_ms)
    if tpl.shape[1] >= chroma.shape[1] * 0.98:
        return None                       # media shorter than the chart
    corr = _corr(tpl, chroma)
    peak = float(corr.max())
    inner = corr[1:-1]
    pk = np.nonzero((inner >= corr[:-2]) & (inner > corr[2:]))[0] + 1
    pk = pk[corr[pk] >= peak * 0.55]
    cand_k = []
    for i in pk[np.argsort(corr[pk])[::-1]]:
        if all(abs(int(i) - c) * hop_ms > 4000 for c in cand_k):
            cand_k.append(int(i))
        if len(cand_k) >= 6:
            break
    if not cand_k:
        cand_k = [int(corr.argmax())]
    cand_offs = [c * hop_ms - chart.gap_ms - s0 * beat_ms
                 for c in cand_k]

    # --- stage 1.5: pick the base whose early phrases land sanely ----- #
    tight = int(12 * 1000 / hop_ms)
    probe_lines = lines[:12]

    def consistency(off):
        ok = tot = 0
        for ln in probe_lines:
            s = min(n.start for n in ln)
            pred = chart.gap_ms + s * beat_ms + off
            if pred < 3000:
                continue                # phrase can't live under this base
            tot += 1
            t, _s = _template(ln, beat_ms)
            shift_fr, sc = _match_at(t, chroma, pred / hop_ms, tight)
            if sc >= 0.35 and abs(shift_fr * hop_ms) <= 8000:
                ok += 1
        return ok, tot

    scored = [consistency(o) for o in cand_offs]
    best_i = max(range(len(cand_offs)), key=lambda i: scored[i])
    base_off = cand_offs[best_i]
    if scored[best_i][0] < 2:
        return None                       # no base produces sane timing

    # --- stage 2: per-phrase local match, with an in-phrase tempo probe #
    # Phrase-to-phrase spacing mixes musical tempo with gap lengths, so
    # pace has to be measured INSIDE phrases: rebuild each template at a
    # few stretch ratios and keep the best-scoring one per phrase.
    radius = int(LOCAL_SEARCH_S * 1000 / hop_ms)
    probe = PROBE_RATIOS
    matches = []
    phrase_ratios = []
    for ln in lines:
        s = min(n.start for n in ln)
        pred_ms = chart.gap_ms + s * beat_ms + base_off
        best = None
        for r in probe:
            tpl, _s = _template(ln, beat_ms * r)
            shift_fr, sc = _match_at(tpl, chroma,
                                     pred_ms / hop_ms, radius)
            if sc > 0 and (best is None or sc > best[1]):
                best = (pred_ms + shift_fr * hop_ms, sc, r)
        if best:
            obs_ms, sc, r = best
            matches.append((s, obs_ms, sc))
            phrase_ratios.append(r)
    if len(matches) < 3:
        return None

    # --- pace: median in-phrase tempo ratio -------------------------- #
    # The winning stretch per phrase votes for the media's real musical
    # tempo; arrangement edits can't corrupt it because each phrase
    # measures only its own note spacing.
    s_arr = np.array([m[0] for m in matches], dtype=np.float64)
    o_arr = np.array([m[1] for m in matches], dtype=np.float64)
    order = np.argsort(s_arr)
    ratio = float(np.median(phrase_ratios)) if phrase_ratios else 1.0
    if not (0.5 < ratio < 2.0):
        ratio = 1.0
    m_coef = ratio * beat_ms                     # fitted ms-per-beat

    # Per-phrase offset under the fitted pace. The base offset is the
    # FIRST section's offset (what beat 0 sees) — the median of the
    # earliest matched phrases; a whole-song median would land between
    # clusters on media with a cut.
    #
    # Melodically repetitive media carry correlation peaks one
    # melody-cycle (~10-16 s) away in both directions, so single phrases
    # DO land on aliases. The only trustworthy discriminator: a real
    # arrangement edit SUSTAINS — every later phrase sits at the shifted
    # level — while an alias snaps back within a phrase or two. A warp
    # mark therefore requires a run of ≥4 consecutive phrases at a
    # consistent new level (≥2 at the tail, where no follow-up exists).
    offs = o_arr - (chart.gap_ms + s_arr * m_coef)
    seq = [(float(s_arr[i]), float(offs[i])) for i in order]
    base_off = float(np.median(offs[order[:3]]))
    warp, cur, i, n = [], base_off, 0, len(seq)
    while i < n:
        off = seq[i][1]
        if abs(off - cur) <= 1500:
            i += 1
            continue
        j = i
        while j < n and abs(seq[j][1] - off) <= 1500:
            j += 1
        if j - i >= 4 or (j == n and j - i >= 2):
            warp.append([int(seq[i][0]), int(off)])
            cur = off
        i = j
    return {"offset_ms": int(base_off), "tempo": round(ratio, 3),
            "warp": warp, "phrases": len(lines), "matched": len(matches),
            "peak": round(peak, 3)}
