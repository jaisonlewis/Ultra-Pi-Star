"""Self-built song maps: media audio → words → line onsets → warp marks.

For any video that contains the actual vocals (official/VEVO uploads),
needle ASR produces word timestamps in media time. Aligning that word
stream against online lyric lines (LRCLIB, or the chart's own text as
fallback) gives every sung line's true onset — intro slates, cut solos
and trimmed interludes all absorbed naturally because the map follows
heard words, not assumed structure.

The observed onsets then re-anchor a USDB chart (pitch + intra-line
timing kept, per-line position corrected) through the existing warp
mechanism — the same artifact the chroma aligner produces, but
resilient where melodic repetition defeats correlation.
"""
from __future__ import annotations

import difflib
import os
import subprocess

import numpy as np

from .charts import Chart, Note
from .lyrics import norm_word

RATE = 16000            # whistle/needle wants 16 kHz mono


def decode_media(media_path, rate: int = RATE) -> np.ndarray:
    """ffmpeg → mono float32 PCM at ``rate``."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(media_path),
         "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"],
        capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32)


def transcribe_audio(audio: np.ndarray) -> list[tuple[str, float]]:
    """needle ASR on 16 kHz mono float32 → [(word, start_ms)].

    Feed it the vocal stem when one was separated — isolated vocals
    transcribe cleaner than the full mix."""
    os.environ.setdefault("NEEDLE_TELEMETRY", "0")
    import needle                                   # optional dependency
    r = needle.transcribe(audio, word_timestamps=True) or {}
    words = r.get("words") or []
    if not words:
        return []
    starts = [float(w.get("start") or 0) for w in words]
    dur_ms = len(audio) / (RATE / 1000.0)
    scale = 1.0 if (starts and max(starts) > dur_ms * 1.5) else 1000.0
    out = []
    for w in words:
        t = norm_word(str(w.get("word") or w.get("text") or ""))
        if t:
            out.append((t, float(w.get("start") or 0) * scale))
    return out


def transcribe_media(media_path) -> list[tuple[str, float]]:
    """needle ASR on the media's own audio → [(word, start_ms)]."""
    return transcribe_audio(decode_media(media_path))


def _wmatch(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) < 4:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.72


def align_lines(asr: list[tuple[str, float]],
                line_texts: list[str],
                min_cov: float = 0.55,
                horizon_ms: float = 120000) -> dict:
    """Map each lyric line to its ASR span, sequentially forward.

    Returns {line_index: {"onset_ms", "end_ms", "cov",
    "words": {lyric_word_pos: asr_index}}}. Lines the media doesn't sing
    stay unmapped — missing verses and cut sections just don't appear.
    """
    onsets = {}
    cursor = 0
    last_onset = 0.0
    for li, text in enumerate(line_texts):
        lw = [norm_word(w) for w in text.split()]
        lw = [w for w in lw if w]
        if not lw:
            continue
        best = None
        j = cursor
        span = len(lw) * 3 + 6
        while j < len(asr) and asr[j][1] - last_onset < horizon_ms:
            p, matched = 0, {}
            for k in range(j, min(j + span, len(asr))):
                if p < len(lw) and _wmatch(asr[k][0], lw[p]):
                    matched[p] = k
                    p += 1
            cov = p / len(lw)
            if matched and (best is None or cov > best[0]
                            or (cov == best[0]
                                and matched.get(0, 0) < best[3])):
                best = (cov, j, matched, matched.get(0, j))
            j += 1
        if best and best[0] >= min_cov:
            cov, j, matched, _ = best
            onsets[li] = {
                "onset_ms": asr[matched[min(matched)]][1],
                "end_ms": asr[matched[max(matched)]][1],
                "cov": cov,
                "words": matched,
            }
            cursor = max(matched.values()) + 1
            last_onset = onsets[li]["onset_ms"]
    return onsets


def map_line_words(asr: list[tuple[str, float]],
                   line_texts: list[str]) -> dict:
    """Whole-stream word alignment → {line_idx: span} like align_lines.

    align_lines scans each line independently with a forward cursor —
    repeated lyrics anchor to the wrong repetition and loose ASR text
    stalls the cursor for the rest of the song. This instead aligns the
    full ASR stream against the full lyric stream with SequenceMatcher:
    longest exact-word blocks anchor first, then recursion fills the
    gaps — misheard words simply don't match, and repeats resolve by
    context length. Only lines with ≥1 anchored word are returned;
    the caller interpolates the rest.
    """
    aw = [w for w, _ in asr]
    lw, bw = [], []           # lw[i] = (line_idx, word_pos) for bw[i]
    counts = {}
    for li, text in enumerate(line_texts):
        for p, w in enumerate(text.split()):
            n = norm_word(w)
            if n:
                lw.append((li, p))
                bw.append(n)
                counts[li] = counts.get(li, 0) + 1
    if not aw or not bw:
        return {}
    spans = {}
    sm = difflib.SequenceMatcher(None, aw, bw, autojunk=False)
    for blk in sm.get_matching_blocks():
        for i in range(blk.size):
            li, p = lw[blk.b + i]
            t = asr[blk.a + i][1]
            sp = spans.setdefault(li, {"onset_ms": t, "end_ms": t,
                                       "words": {}})
            sp["words"][p] = blk.a + i
            sp["onset_ms"] = min(sp["onset_ms"], t)
            sp["end_ms"] = max(sp["end_ms"], t)
    for li, sp in spans.items():
        sp["cov"] = len(sp["words"]) / counts[li]
    return spans


def _subseq(needle_words, hay_words):
    """Greedy ordered match of needle_words inside hay_words →
    (coverage, first_hay_idx, last_hay_idx)."""
    p, a, b = 0, -1, -1
    for i, hw in enumerate(hay_words):
        if p < len(needle_words) and _wmatch(hw, needle_words[p]):
            if a < 0:
                a = i
            b = i
            p += 1
    return (p / max(1, len(needle_words)), a, b)


def _chart_line_words(line: list[Note]) -> list[str]:
    """Chart line → normalized word list (syllables join on word_break)."""
    words, cur = [], ""
    for n in line:
        if n.word_break and cur:
            words.append(cur)
            cur = ""
        cur += n.text.replace("~", "")
    if cur:
        words.append(cur)
    return [norm_word(w) for w in words if norm_word(w)]


def chart_line_onsets(chart: Chart, line_texts: list[str],
                      asr: list[tuple[str, float]],
                      line_spans: dict, track: str) -> dict:
    """Each chart line → the media time of its first word.

    A chart line is usually a substring of one lyric line, so we locate
    it inside the lyric line's word list, then read the ASR timestamp
    recorded for that word position.
    """
    lyric_words = [[norm_word(w) for w in t.split() if norm_word(w)]
                   for t in line_texts]
    out = {}
    cursor = 0          # monotonic: repeated lyrics map in order
    for li, notes in enumerate(chart.lines(track)):
        if not notes:
            continue
        cw = _chart_line_words(notes)
        if not cw:
            continue
        best = None
        for ti in range(cursor, len(lyric_words)):
            hw = lyric_words[ti]
            if ti not in line_spans or not hw:
                continue
            cov, a, b = _subseq(cw, hw)
            if best is None or cov > best[0]:
                best = (cov, ti, a, b)
            if cov >= 1.0:
                break
        if not best or best[0] < 0.5:
            continue
        cursor = best[1]
        _, ti, a, b = best
        wmap = line_spans[ti]["words"]

        def word_t(pos):
            if pos in wmap:
                return asr[wmap[pos]][1]
            keys = sorted(wmap)
            near = min(keys, key=lambda k: abs(k - pos))
            return asr[wmap[near]][1]
        out[li] = (word_t(a), word_t(b))
    return out


def chart_lyric_lines(chart: Chart, track: str) -> list[str]:
    """Line text straight from the chart — the LRCLIB fallback. Joins
    syllables per line so the ASR matcher sees words, not syllables."""
    return [" ".join(w) for w in
            (_chart_line_words(line) for line in chart.lines(track))
            if w]


def _marks_from_onsets(chart: Chart, onsets: dict, track: str,
                       beat_ms: float) -> dict | None:
    """Per-chart-line media onsets → warp-map dict (align_chart shape).

    ``onsets`` maps chart-line index → (onset_ms, end_ms). The existing
    warp mechanism applies each mark's offset from its beat onward, so a
    mark at every sung line's start IS the re-anchored map: intra-line
    syllable timing keeps the chart's own spacing while each line's
    position follows the media. Intra-line tempo is fitted from the
    observed word span vs the chart's beat span — same trick as the
    chroma prober.
    """
    lines = [l for l in chart.lines(track) if l]
    if len(onsets) < max(3, len(lines) // 2):
        return None
    rows, ratios = [], []
    for li, notes in enumerate(chart.lines(track)):
        if li not in onsets or not notes:
            continue
        s = min(n.start for n in notes)
        e = max(n.start + n.length for n in notes)
        onset, end = onsets[li]
        chart_span = (e - s) * beat_ms
        obs_span = end - onset
        if chart_span > 2000 and obs_span > 500:
            ratios.append(obs_span / chart_span)
        rows.append((s, onset))
    tempo = float(np.median(ratios)) if ratios else 1.0
    if not (0.6 < tempo < 1.6):
        tempo = 1.0
    m_coef = beat_ms * tempo
    marks, base, prev = [], None, None
    for s, onset in rows:
        off = onset - s * m_coef - chart.gap_ms
        if base is None:
            base = off
        # one mark per line whose offset actually steps — adjacent lines
        # sharing an offset collapse under the earlier mark
        if prev is None or abs(off - prev) > 800:
            marks.append([int(s), int(off)])
            prev = off
    return {"offset_ms": int(base or 0), "tempo": round(tempo, 3),
            "warp": marks, "matched": len(onsets),
            "phrases": len(lines), "method": "asr"}


def _lrc_warp(chart: Chart, synced: list, asr: list, track: str,
              beat_ms: float) -> dict | None:
    """LRCLIB synced lyrics already carry per-line recording onsets —
    this video is usually the same recording, so each chart line's media
    position is its LRC time plus ONE constant shift. ASR anchors a few
    lines and the median delta gives that shift; a wide delta spread
    means the video deviates from the recording (cuts/edits) and the
    LRC shortcut is unsafe → None, let the ASR-only/chroma paths try.

    A chart line is usually a substring of one LRC line, so its onset
    interpolates inside the LRC line's span (start→next line's start)
    by word fraction.
    """
    ltxt = [t for _, t in synced]
    spans = align_lines(asr, ltxt)
    deltas = sorted(sp["onset_ms"] - synced[li][0]
                    for li, sp in spans.items())
    if len(deltas) < 3:
        return None
    q25, q75 = np.percentile(deltas, [25, 75])
    if q75 - q25 > 2500:
        return None                     # video deviates from recording
    shift = float(np.median(deltas))
    lyric_words = [[norm_word(w) for w in t.split() if norm_word(w)]
                   for t in ltxt]
    onsets = {}
    cursor = 0          # monotonic: repeated lyrics map in order
    for li, notes in enumerate(chart.lines(track)):
        if not notes:
            continue
        cw = _chart_line_words(notes)
        if not cw:
            continue
        best = None
        for ti in range(cursor, len(lyric_words)):
            hw = lyric_words[ti]
            if not hw:
                continue
            cov, a, b = _subseq(cw, hw)
            if best is None or cov > best[0]:
                best = (cov, ti, a, b)
            # a perfect hit can't be beaten — stop scanning forward
            if cov >= 1.0:
                break
        if not best or best[0] < 0.5:
            continue
        cursor = best[1]            # later lines can't map earlier
        _, ti, a, b = best
        hw = len(lyric_words[ti])
        t0 = synced[ti][0]
        t1 = synced[ti + 1][0] if ti + 1 < len(synced) else t0 + 5000
        # position from LRC interpolation; the END used for tempo comes
        # from the ASR span when this lyric line was matched — the gap
        # to the next LRC line includes instrumental pauses, which would
        # inflate the span and over-fit tempo
        onset = t0 + (a / hw) * (t1 - t0) + shift
        wmap = spans.get(ti, {}).get("words")
        if wmap:
            def word_t(pos):
                if pos in wmap:
                    return asr[wmap[pos]][1]
                keys = sorted(wmap)
                near = min(keys, key=lambda k: abs(k - pos))
                return asr[wmap[near]][1]
            # pure-ASR span for tempo — position stays LRC-anchored
            end = onset + (word_t(b) - word_t(a))
        else:
            end = onset          # no ASR end — excluded from tempo fit
        onsets[li] = (onset, end)
    lines = [l for l in chart.lines(track) if l]
    if len(onsets) < max(3, len(lines) // 2):
        return None
    # the chart's own beat grid can drift from the recording (wrong BPM
    # or edition differences) — one regression line absorbs it cleanly,
    # so marks only remain for genuine per-line deviations
    xs = np.array([min(n.start for n in chart.lines(track)[li])
                   for li in onsets], dtype=float)
    ys = np.array([onsets[li][0] for li in onsets])
    m_coef, shift_fit = np.polyfit(xs, ys, 1)
    tempo = m_coef / beat_ms
    if not (0.5 < tempo < 2.0):
        return None                 # LRC/chart disagreement, don't trust
    marks, base, prev = [], None, None
    for s, onset in zip(xs.tolist(), ys.tolist()):
        # beat→ms under the fitted slope; residual = this line's
        # deviation from the straight fit
        off = onset - s * m_coef - chart.gap_ms
        if base is None:
            base = off
        if prev is None or abs(off - prev) > 800:
            marks.append([int(s), int(off)])
            prev = off
    return {"offset_ms": int(base or 0), "tempo": round(tempo, 3),
            "warp": marks, "matched": len(onsets),
            "phrases": len(lines), "method": "asr"}


def build_warp(chart: Chart, media_path, line_texts: list[str] | None,
               synced: list | None = None,
               track: str | None = None,
               asr: list | None = None) -> dict | None:
    """Anchor the chart's lines to the media → per-line warp marks.

    Two channels, best first:
      1. LRC path — LRCLIB synced times + ASR-measured shift (needs only
         a handful of confident ASR anchors, and LRC timestamps already
         describe every line incl. interlude lengths).
      2. ASR path — align the heard word stream directly to lyric lines
         (covers videos that deviate from the official recording).

    Returns the same shape as ``align_chart`` — {"offset_ms", "tempo",
    "warp", "matched", "phrases", "method": "asr"} — so callers persist
    and apply it identically, or None when neither path can anchor
    enough (no lead vocal — karaoke tracks fall back to chroma/sweep).
    """
    if track is None:
        track = max(chart.tracks, key=lambda t: len(chart.tracks[t]))
    beat_ms = 15000.0 / chart.bpm if chart.bpm else 0
    if beat_ms <= 0:
        return None
    if asr is None:
        asr = transcribe_media(media_path)
    if len(asr) < 20:
        return None
    if synced:
        out = _lrc_warp(chart, synced, asr, track, beat_ms)
        if out:
            return out
    line_texts = line_texts or chart_lyric_lines(chart, track)
    spans = align_lines(asr, line_texts)
    if len(spans) < 3:
        return None
    onsets = chart_line_onsets(chart, line_texts, asr, spans, track)
    return _marks_from_onsets(chart, onsets, track, beat_ms)
