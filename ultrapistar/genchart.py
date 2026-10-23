"""Chart generation — build .upsong charts for songs with no USDB entry.

Pipeline: lyric lines (LRC preferred, plain lyrics or nothing) + needle
ASR word timings + RMVPE pitch curve on the vocal stem → native chart.

The chart is born media-timed: notes are stored on a 1 beat = 1 ms grid
(BPM=15000) so no warp marks are ever needed — the generated map IS the
timing truth for this exact video, including cut intros and edited
interludes, because every line position comes from heard words.

Words the extractor can't pitch confidently become ``freestyle`` notes —
they still display and time correctly but aren't pitch-scored, which
keeps a low-confidence detection from punishing the singer.
"""
from __future__ import annotations

import re
from bisect import bisect_left

import numpy as np

from .charts import Chart, Note, save_native
from .lyrics import norm_word
from . import melody, songmap

GEN_BPM = 15000.0          # 1 beat = 1 ms — no quantization loss
MIN_WORD_MS = 120          # shorter than this → merge into previous note
MIN_CONF = 0.35            # pitch-mode fraction below → freestyle note


def _line_word_spans(line_text: str, span: dict,
                     asr: list[tuple[str, float]]) -> list:
    """Lyric line + its ASR match → [(word, start_ms, end_ms)].

    Matched lyric words take the ASR word's onset; unmatched words
    (ASR skipped/misheard them) interpolate evenly between the bracketing
    matched words so every word still gets a real time.
    """
    raw = line_text.split()
    # keep any token with a letter/digit (CJK included — norm_word only
    # knows latin, so it would drop whole Japanese lines) but drop pure
    # punctuation; ``keep`` maps each output position to its raw split
    # index, which is the position space span["words"] uses
    keep = [i for i, w in enumerate(raw) if re.search(r"[^\W_]", w)]
    words = [raw[i] for i in keep]
    if not words:
        return []
    wmap = span["words"]
    times = {}
    for op, rp in enumerate(keep):
        if rp in wmap:
            times[op] = asr[wmap[rp]][1]
    # interpolate unmatched positions between matched neighbours
    known = sorted(times)
    for pos in range(len(words)):
        if pos in times:
            continue
        prev = max((k for k in known if k < pos), default=None)
        nxt = min((k for k in known if k > pos), default=None)
        if prev is not None and nxt is not None:
            frac = (pos - prev) / (nxt - prev)
            times[pos] = times[prev] + frac * (times[nxt] - times[prev])
        elif nxt is not None:
            times[pos] = times[nxt] - 350 * (nxt - pos)
        elif prev is not None:
            times[pos] = times[prev] + 350 * (pos - prev)
        else:
            times[pos] = span["onset_ms"] + pos * 300
    # a word's time must stay inside its line's span — extrapolated
    # tails would bleed notes into the next line's territory
    lo, hi = span["onset_ms"], max(span["end_ms"] - 50,
                                   span["onset_ms"] + 50)
    starts = np.maximum.accumulate(
        [min(max(times[i], lo), hi) for i in range(len(words))]).tolist()
    ends = starts[1:] + [min(starts[-1] + 400, span["end_ms"])]
    return [(w, s, e) for w, s, e in zip(words, starts, ends)]


def _asr_lines(asr: list[tuple[str, float]],
               gap_ms: float = 1300) -> tuple[list[str], dict]:
    """No online lyrics → phrase lines from ASR word-pause gaps.

    A sung line almost always ends in a breath; consecutive words more
    than ~1.3 s apart split cleanly into phrases. Returns the same
    shapes ``align_lines`` produces: line_texts + spans {li: {onset_ms,
    end_ms, cov, words:{lyric_pos: asr_index}}}.
    """
    texts, spans, cur, idxs = [], {}, [], []
    for i, (w, t) in enumerate(asr):
        if idxs and t - asr[idxs[-1]][1] > gap_ms:
            texts.append(" ".join(x[0] for x in cur))
            li = len(texts) - 1
            spans[li] = {"onset_ms": cur[0][1], "end_ms": cur[-1][1],
                         "cov": 1.0,
                         "words": {p: a for p, a in enumerate(idxs)}}
            cur, idxs = [], []
        cur.append((w, t))
        idxs.append(i)
    if cur:
        texts.append(" ".join(x[0] for x in cur))
        li = len(texts) - 1
        spans[li] = {"onset_ms": cur[0][1], "end_ms": cur[-1][1],
                     "cov": 1.0,
                     "words": {p: a for p, a in enumerate(idxs)}}
    # absurd micro-lines (single stray word between gaps) merge forward
    merged_t, merged_s = [], {}
    for li, t in enumerate(texts):
        if merged_s and len(t.split()) < 2:
            prev = len(merged_t) - 1
            merged_t[prev] += " " + t
            prev_span = merged_s[prev]
            prev_span["end_ms"] = spans[li]["end_ms"]
            off = len(prev_span["words"])
            for p, a in spans[li]["words"].items():
                prev_span["words"][off + p] = a
        else:
            merged_t.append(t)
            merged_s[len(merged_t) - 1] = spans[li]
    return merged_t, merged_s


def _lrc_shift(deltas: list, synced: list,
               asr: list[tuple[str, float]]) -> float | None:
    """Anchor deltas → media shift, or None when LRC can't be trusted.

    Repeated lyrics anchor to the wrong repetition, so deltas can't be
    trusted as votes. Score candidate shifts directly: how many LRC
    lines find their own words inside the shifted window. The true
    shift puts verses on verses; wrong ones only ever hit the repeated
    choruses. Cluster medians plus a coarse sweep are the candidates —
    the sweep rescues runs where every anchor landed wrong.
    """
    if len(deltas) < 2:
        return None
    times = [ms for ms, _ in synced]
    lws = [[norm_word(w) for w in t.split() if norm_word(w)]
           for _, t in synced]

    def score(shift):
        # two signals: predicted line ONSETS should land on heard words
        # (sharp, punishes shifts that sit between phrases), and line
        # windows should contain their own words (rewards real text).
        # Wide windows alone are too tolerant to discriminate on
        # repetitive songs, so containment can't stand alone.
        onsets = [t + shift for t in times]
        hits = sum(1 for _, t in asr
                   if any(abs(t - o) <= 700 for o in onsets))
        tot = 0
        for li, lw in enumerate(lws):
            if not lw:
                continue
            lo = times[li] + shift - 250
            hi = (times[li + 1] + shift if li + 1 < len(times)
                  else times[li] + shift + 4000)
            p = 0
            for w, t in asr:
                if lo <= t < hi and p < len(lw) and w == lw[p]:
                    p += 1
            tot += p
        return hits + tot

    clusters, cur = [], [sorted(deltas)[0]]
    for d in sorted(deltas)[1:]:
        if d - cur[-1] <= 1500:
            cur.append(d)
        else:
            clusters.append(cur)
            cur = [d]
    clusters.append(cur)
    cands = [float(np.median(c)) for c in clusters]
    cands += list(np.arange(-5000, 60000, 1000.0))
    best = max(cands, key=score)
    total_lw = sum(len(lw) for lw in lws)
    if score(best) < max(8, int(0.08 * total_lw)):
        return None
    # refine around the coarse winner
    fine = max(np.arange(best - 900, best + 901, 100.0), key=score)
    return float(fine)


def _lrc_spans(spans: dict, texts: list[str], times: list,
               shift: float, asr: list[tuple[str, float]]) -> None:
    """Trusted shift → unanchored lines sit at lrc+shift with a uniform
    word fill (also marks those heard words used). Anchored lines keep
    their real positions — they are media truth and survive cuts that
    compress the timeline. Windows with no heard words stay unmapped —
    a sung line always produces *some* ASR output."""
    for li, lms in enumerate(times):
        if li in spans:
            continue
        onset = lms + shift
        hi = times[li + 1] + shift if li + 1 < len(times) else onset + 4000
        # a sung line's first words can land a little early — the
        # catch window reaches ~1.2s ahead of the nominal onset
        win = [i for i, (_, t) in enumerate(asr)
               if onset - 1200 <= t < hi]
        if not win:
            continue
        lw_n = len([w for w in texts[li].split() if norm_word(w)])
        wmap = {p: win[int(p * len(win) / lw_n)]
                for p in range(min(lw_n, len(win)))} if lw_n else {}
        spans[li] = {"onset_ms": onset, "end_ms": hi,
                     "cov": len(wmap) / max(1, lw_n),
                     "words": wmap, "lrc": True}


def _interp_spans(spans: dict, texts: list[str],
                  lrc_times: list | None,
                  asr: list[tuple[str, float]]) -> None:
    """Give un-anchored lines an estimated span between the bracketing
    anchored lines — proportional to LRC position when synced times
    exist, else by line index. Long LRC stretches the media skipped
    entirely stay unmapped (a cut verse, not a pause). Estimated lines
    borrow ASR words in their window so the gap-fill step won't lay
    duplicate text on top."""
    anchored = sorted(spans)
    if not anchored:
        return
    for li in range(len(texts)):
        if li in spans:
            continue
        prev = max((a for a in anchored if a < li), default=None)
        nxt = min((a for a in anchored if a > li), default=None)
        if prev is not None and nxt is not None:
            media_gap = spans[nxt]["onset_ms"] - spans[prev]["end_ms"]
            if lrc_times:
                lgap = lrc_times[nxt] - lrc_times[prev]
                if lgap > 30000 and media_gap < 15000:
                    continue               # verse cut from this media
                frac = ((lrc_times[li] - lrc_times[prev]) / lgap
                        if lgap > 0 else 0.0)
            else:
                frac = (li - prev) / (nxt - prev)
            frac = min(max(frac, 0.0), 1.0)
            onset = (spans[prev]["onset_ms"] + frac
                     * (spans[nxt]["onset_ms"] - spans[prev]["onset_ms"]))
        elif nxt is not None:
            gap = ((lrc_times[nxt] - lrc_times[li]) if lrc_times
                   else (nxt - li) * 2500)
            onset = max(0.0, spans[nxt]["onset_ms"] - max(400.0, gap))
        else:
            gap = ((lrc_times[li] - lrc_times[prev]) if lrc_times
                   else (li - prev) * 2500)
            onset = spans[prev]["end_ms"] + max(400.0, min(gap, 30000))
        hi = spans[nxt]["onset_ms"] if nxt is not None else onset + 4000
        win = [i for i, (_, t) in enumerate(asr) if onset - 200 <= t < hi]
        lw_n = len([w for w in texts[li].split() if norm_word(w)])
        wmap = {p: win[int(p * len(win) / lw_n)]
                for p in range(min(lw_n, len(win)))} if lw_n else {}
        spans[li] = {"onset_ms": onset, "end_ms": onset + 2500,
                     "cov": 0.0, "words": wmap, "interp": True}


def _onset_shift(lrc_ms, heard_ms, deltas) -> float | None:
    """Global offset placing LRC line onsets on heard-word onsets.

    ASR word *times* are media truth even when the text is wrong.
    Three votes decide between the phantom shifts repeated lyrics
    produce: consecutive line-onset hits (the true offset lands runs
    of lines on runs of real phrase starts), anchor-delta agreement
    (matched lines cluster near one shift), and coverage — a phantom
    leaves heard words with no lyric window covering them."""
    if not heard_ms or len(lrc_ms) < 3:
        return None
    ons = np.asarray(sorted(heard_ms))
    lrc = np.asarray(lrc_ms, dtype=float)

    def score(s):
        # proximity-weighted: a dense word stream makes every shift
        # "hit" something within a boolean window — distance decides
        d = np.array([np.abs(ons - (l + s)).min() for l in lrc])
        v = np.clip(1.0 - d / 600.0, 0.0, 1.0)
        sc = float(v.sum() + np.minimum(v[:-1], v[1:]).sum() * 3)
        sc += sum(2 * max(0.0, 1 - abs(dd - s) / 1500.0) for dd in deltas)
        lo = lrc + s - 400
        hi = np.append(lrc[1:] + s, lrc[-1] + s + 5000)
        covered = np.zeros(len(ons), bool)
        for a_, b_ in zip(lo, hi):
            covered |= (ons >= a_) & (ons < b_)
        return sc - int((~covered).sum()) * 2

    s = max(np.arange(-15000, 90000, 200.0), key=score)
    if score(s) < 10:
        return None
    return float(max(np.arange(s - 400, s + 401, 50.0), key=score))


def _sung_spans(spans, texts, times, shift, heard_ms,
                asr: list[tuple[str, float]]) -> None:
    """LRC+shift predicts each line; the onset snaps to the nearest
    heard word within ±3 s (monotone — two lines can't share a phrase).
    Crowd-timed LRC is usually within ~0.4 s of the media, and a
    bigger miss means the reference diverged there, so the real phrase
    start wins. Lines with nothing heard anywhere near stay unmapped —
    an intro, skit, or cut section shows no text rather than drifted
    text. Word anchors inside the surviving window are kept."""
    ons = np.asarray(sorted(heard_ms))
    prev, placed, dropped = -1e9, [], []
    for li, lms in enumerate(times):
        pred = lms + shift
        cands = ons[(ons >= prev) & (np.abs(ons - pred) <= 3000)]
        if len(cands):
            onset = float(cands[np.abs(cands - pred).argmin()])
        else:
            near = ons[(ons >= prev) & (np.abs(ons - pred) <= 4000)]
            if not len(near):
                dropped.append((li, pred, spans.pop(li, None)))
                continue
            onset = float(pred)
        prev = onset + 100
        placed.append((li, onset))
    # ASR gaps inside a confirmed region aren't cuts — the sung region
    # is bounded on both sides, so a dropped line keeps its LRC time.
    # Outside it, only a strong text anchor rescues a dropped line: the
    # media edited the timeline (cut/reordered section) so the global
    # shift can't reach it, but its words were verifiably heard.
    pl_onsets = [o for _, o in placed]
    for li, pred, sp in dropped:
        if 0 < bisect_left(pl_onsets, pred) < len(pl_onsets):
            placed.append((li, float(pred)))
        elif sp and len(sp.get("words", {})) >= 3:
            spans[li] = sp
            placed.append((li, float(sp["onset_ms"])))
        else:
            continue
        pl_onsets = sorted(o for _, o in placed)
    placed.sort(key=lambda p: p[1])
    for li, onset in placed:
        lrc_dur = (times[li + 1] - times[li]
                   if li + 1 < len(times) else 6000)
        nxt = min((o for _, o in placed if o > onset),
                  default=onset + lrc_dur)
        end = min(max(onset + 800, nxt - 100), onset + lrc_dur + 1500)
        sp = spans.get(li) or {}
        words = {p: a for p, a in sp.get("words", {}).items()
                 if onset - 300 <= asr[a][1] < end}
        lw_n = len([w for w in texts[li].split() if norm_word(w)])
        win = [i for i, (_, t) in enumerate(asr)
               if onset - 300 <= t < end
               and i not in set(words.values())]
        for p, ai in zip([p for p in range(lw_n) if p not in words],
                         win):
            words[p] = ai
        spans[li] = {"onset_ms": onset, "end_ms": end,
                     "cov": len(words) / max(1, lw_n),
                     "words": words, "lrc": True}


def _merge_short(spans):
    """Words under MIN_WORD_MS merge forward into the next word."""
    out, i = [], 0
    while i < len(spans):
        w, s, e = spans[i]
        if e - s < MIN_WORD_MS and i + 1 < len(spans):
            nw, ns, ne = spans[i + 1]
            spans[i + 1] = (w + " " + nw, s, ne)
        else:
            out.append((w, s, e))
        i += 1
    return out


def _phrase_map(times: list[int],
                onsets: list[float]) -> list[float] | None:
    """LRC line times → media phrase onsets, preserving rhythm shape.

    A global shift can't fix a different arrangement: the reference
    spaces lines one way, this media another. The DP picks the monotone
    onset subsequence whose gaps distort the LRC's least — every lyric
    line lands on the start of real singing instead of on a timestamp
    for a recording this media isn't."""
    n, m = len(times), len(onsets)
    if n < 3 or m < n:
        return None
    dp: dict = {}
    for j in range(m):
        dp[(0, j)] = (0.0, None)
    for i in range(1, n):
        g = max(200, times[i] - times[i - 1])
        for j in range(i, m):
            best = None
            for pj in range(i - 1, j):
                prev = dp.get((i - 1, pj))
                if prev is None:
                    continue
                pg = max(200, onsets[j] - onsets[pj])
                cost = prev[0] + abs(np.log(pg / g))
                if best is None or cost < best[0]:
                    best = (cost, pj)
            if best is not None:
                dp[(i, j)] = best
    end = min(range(n - 1, m), key=lambda j: dp.get((n - 1, j), (1e9,))[0])
    if (n - 1, end) not in dp or dp[(n - 1, end)][0] / n > 0.7:
        return None                        # rhythm doesn't fit at all
    path, j = [], end
    for i in reversed(range(n)):
        path.append(j)
        j = dp[(i, j)][1]
    return [onsets[k] for k in reversed(path)]


def _phrase_onsets(curve) -> list[float]:
    """Voiced-segment starts (ms) from the RMVPE pitch curve.

    ASR onsets fail when the transcript is hallucination — needle drops
    invented words into instrumental gaps, corrupting the onset map.
    Pitch confidence can't be fooled the same way: a sung phrase is a
    voiced run, whatever the words were."""
    if not curve:
        return []
    t, f0, conf = (np.asarray(x, dtype=float) for x in curve)
    voiced = (f0 > 0) & (conf >= 0.3)
    onsets, run_start, last_v = [], None, None
    for i, tt in enumerate(t):
        if voiced[i]:
            if run_start is None:
                run_start = float(tt)
            last_v = float(tt)
        elif run_start is not None and tt - last_v > 0.25:
            if last_v - run_start > 0.5:
                onsets.append(run_start * 1000)
            run_start = None
    if run_start is not None and last_v - run_start > 0.5:
        onsets.append(run_start * 1000)
    return onsets


def generate(artist: str, title: str, lyric_rec: dict | None,
             asr: list[tuple[str, float]],
             pitch_curve, meta_out: dict | None = None) -> Chart | None:
    """Build a media-timed chart for this song, or None when the song's
    vocal line can't be located at all.

    ``lyric_rec``:   LRCLIB record {"synced": [(ms, text)], "plain": [...]}
    ``asr``:         needle words [(word, ms)] — vocal stem preferred
    ``pitch_curve``: (times, f0, conf) from melody.extract_curve
    ``meta_out``:    optional dict — filled with the pipeline's evidence
                     (anchors, shift, coverage) for the analysis sidecar
    """
    if len(asr) < 20:
        return None
    synced = (lyric_rec or {}).get("synced") or []
    plain = (lyric_rec or {}).get("plain") or []
    line_texts, spans, lyrics_kind = [], {}, None
    cands = ([([t for _, t in synced], [ms for ms, _ in synced], "lrc")]
             if synced else [])
    if plain:
        cands.append((plain, None, "plain"))
    for texts, times, kind in cands:
        sp = songmap.map_line_words(asr, texts)
        matched = sum(len(s["words"]) for s in sp.values())
        total = max(1, sum(len([w for w in t.split() if norm_word(w)])
                           for t in texts))
        if meta_out is not None:
            meta_out[f"{kind}_match"] = {"matched": matched,
                                         "total": total}
        # enough lyric words anchored → this record IS this song. The
        # bar is low on purpose: even sparse anchors + interpolation
        # produce correct text, while a same-title different song
        # anchors almost nothing.
        if matched >= max(8, int(0.07 * total)):
            line_texts, spans, lyrics_kind = texts, sp, kind
            # The LRC timestamps ARE the lyric timing — the media is the
            # same recording, so line gaps are identical and only the
            # global offset is unknown. Find it by landing consecutive
            # line onsets on heard-word onsets (media truth), then snap
            # every line to the real phrase start. Text anchors only
            # decide WHICH record this is, never where lines sit.
            heard = [t for _, t in asr]
            deltas = ([spans[li]["onset_ms"] - times[li] for li in spans]
                      if times else [])
            shift = (_onset_shift(times, heard, deltas)
                     or _lrc_shift(deltas, synced, asr)
                     if times else None)
            if shift is not None:
                _sung_spans(spans, texts, times, shift, heard, asr)
                if meta_out is not None:
                    meta_out["lrc_shift_ms"] = round(shift, 1)
            else:
                _interp_spans(spans, texts, times, asr)
            break
    # text-match failed on every candidate — but when the transcript is
    # the unreliable party (needle hallucinates whole phrases on quiet
    # or orchestral songs), a synced record can still anchor by phrase
    # onset structure alone: heard-word TIMES are media truth even when
    # the words are wrong. A same-title different song produces no
    # onset agreement and still falls through to ASR lines.
    if lyrics_kind is None and synced:
        times = [ms for ms, _ in synced]
        # vocal-phrase onsets beat ASR word onsets here — the transcript
        # failed text-matching BECAUSE it's hallucinated, so its words
        # (and where it claims they sit) can't be trusted; the pitch
        # curve only reports real singing
        heard = _phrase_onsets(pitch_curve) or [t for _, t in asr]
        texts = [t for _, t in synced]
        mapped = _phrase_map(times, heard)
        if mapped:
            for li, on in enumerate(mapped):
                dur = (times[li + 1] - times[li]
                       if li + 1 < len(times) else 6000)
                nxt = mapped[li + 1] if li + 1 < len(mapped) else on + dur
                spans[li] = {"onset_ms": on,
                             "end_ms": min(max(on + 800, nxt - 100),
                                           on + dur + 1500),
                             "words": {}, "lrc": True}
            if len(spans) >= 3:
                line_texts, lyrics_kind = texts, "lrc"
                if meta_out is not None:
                    meta_out["lrc_onset_only"] = True
                    meta_out["lrc_phrase_map"] = True
            else:
                spans = {}
        else:
            shift = _onset_shift(times, heard, [])
            if shift is not None:
                _sung_spans(spans, texts, times, shift, heard, asr)
                if len(spans) >= 3:
                    line_texts, lyrics_kind = texts, "lrc"
                    if meta_out is not None:
                        meta_out["lrc_shift_ms"] = round(shift, 1)
                        meta_out["lrc_onset_only"] = True
                else:
                    spans = {}
    # fill the gaps: words the record didn't claim still need lines,
    # otherwise sparse coverage would leave those stretches textless.
    # Words sitting just outside a line's window are misheard fragments
    # OF that line ("bye thank you" where "bicycle" was sung) — claim
    # them so the filler can't render gibberish next to real text.
    used = {a for sp in spans.values() for a in sp["words"].values()}
    if spans:
        wins = sorted((sp["onset_ms"] - 1800, sp["end_ms"] + 1800)
                      for sp in spans.values())
        for i, (_, t) in enumerate(asr):
            if i not in used and any(a <= t < b for a, b in wins):
                used.add(i)
    left_idx = [i for i in range(len(asr)) if i not in used]
    if (lyrics_kind is None and len(left_idx) >= 3
            and (len(spans) < 3 or len(used) < 0.5 * len(asr))):
        t2, s2 = _asr_lines([asr[i] for i in left_idx])
        base = len(line_texts)
        line_texts += t2
        for li, sp in s2.items():
            sp["asr_native"] = True
            sp["words"] = {p: left_idx[a] for p, a in sp["words"].items()}
            spans[base + li] = sp
    if len(spans) < 3:
        return None

    # word spans per matched line, in media time — onset order, since
    # lyric-line and ASR-filler indices interleave in real time
    all_words = []        # (word, start_ms, end_ms, line_idx, is_last)
    line_breaks = []
    for li in sorted(spans, key=lambda i: spans[i]["onset_ms"]):
        text = line_texts[li]
        ws = _line_word_spans(text, spans[li], asr)
        ws = _merge_short(ws)
        if not ws:
            continue
        for w, s, e in ws:
            all_words.append((w, s, e, li))
        line_breaks.append(ws[-1][2])
    if len(all_words) < 15:
        return None
    all_words.sort(key=lambda x: x[1])

    # pitch per word
    wn = melody.word_notes(*pitch_curve,
                           [(w, s, e) for w, s, e, _ in all_words])

    chart = Chart()
    chart.headers = {"ARTIST": artist, "TITLE": title,
                     "BPM": str(int(GEN_BPM)), "GAP": "0",
                     "CREATOR": "ultrapistar-genchart"}
    p1 = []
    for (w, s, e, li), p in zip(all_words, wn):
        tone = p["tone"]
        kind = "normal" if (tone is not None
                            and p["conf"] >= MIN_CONF) else "freestyle"
        p1.append(Note(kind, int(s), max(50, int(e - s)),
                       tone if tone is not None else 0,
                       w, True))
    chart.tracks["P1"] = p1
    chart.breaks["P1"] = [int(b) for b in line_breaks[:-1]]
    chart.source = {"type": "generated", "extractor": "rmvpe",
                    "lrc": lyrics_kind == "lrc",
                    "lyrics": lyrics_kind}
    if meta_out is not None:
        meta_out.update({
            "lyrics": lyrics_kind,
            "lines": len(spans),
            "interp_lines": sum(1 for sp in spans.values()
                                if sp.get("interp")),
            "asr_lines": sum(1 for sp in spans.values()
                             if sp.get("asr_native")),
            "words": len(all_words), "asr_words": len(asr)})
    if lyrics_kind is not None:
        chart.source["lrc_track"] = (lyric_rec or {}).get("track_name")
        chart.source["lrc_artist"] = (lyric_rec or {}).get("artist_name")
    key = (melody.estimate_key(*pitch_curve) if pitch_curve else None)
    if key:
        chart.source["key"] = key
    return chart if not chart.validate() else None


def save_for_entry(chart: Chart, charts_dir, entry_id: int,
                   artist: str, title: str) -> "path":
    """Write the generated chart into the generated-charts area."""
    from pathlib import Path
    import re
    safe = re.sub(r"[^A-Za-z0-9 _-]+", "", f"{artist} - {title}")[:80]
    d = Path(charts_dir) / "generated"
    d.mkdir(parents=True, exist_ok=True)
    return save_native(chart, d / f"{safe} [{entry_id}].upsong",
                       source=chart.source)
