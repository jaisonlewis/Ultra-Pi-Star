"""CTC forced alignment — lyric text forced onto the vocal stem's
acoustic model output, giving per-word onsets at frame resolution.

Backends (auto-picked by ``backend_for``):
  * ``parakeet`` — NeMo parakeet_tdt_ctc 110m, 80 ms BPE frames over an
    80-bin fbank frontend. English only, sharpest word timing.
  * ``omni`` — omnilingual 300M CTC, 20 ms char frames on raw 16k
    audio. ~1600 languages via character vocabulary.

Both run through onnxruntime directly — we need the raw frame
log-posteriors for Viterbi, not the decoded text the sherpa wrapper
returns.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import numpy as np

SR = 16000
MIN_WORD_CONF = 0.25
WIN_S = 30.0
OVERLAP_S = 4.0

# ---------------------------------------------------------------------
# NeMo fbank (parakeet frontend) — port of ups-prep mel.rs::nemo_fbank
# pre-emphasis 0.97, 25 ms Hann in a 512 FFT, 10 ms hop, reflect pad,
# power spectrum, 80 slaney mel bins 20 Hz..7.6 kHz, log(x + 2^-24),
# per-feature mean/var normalisation over the utterance.
# ---------------------------------------------------------------------

_N_FFT, _WINL, _HOPN, _NM = 512, 400, 160, 80
_GUARD = 5.9604645e-8  # 2^-24


def _hz2mel(f):
    f = np.asarray(f, dtype=np.float64)
    lo = f / (200.0 / 3.0)
    hi = 15.0 + np.log(f / 1000.0) / (np.log(6.4) / 27.0)
    return np.where(f < 1000.0, lo, hi)


def _mel2hz(m):
    m = np.asarray(m, dtype=np.float64)
    lo = m * (200.0 / 3.0)
    hi = 1000.0 * np.exp((m - 15.0) * (np.log(6.4) / 27.0))
    return np.where(m < 15.0, lo, hi)


def nemo_fbank(audio: np.ndarray) -> np.ndarray:
    """(n_mels, T) float32 — what the parakeet model consumes."""
    if len(audio) < _HOPN:
        return np.zeros((_NM, 0), np.float32)
    x = np.empty(len(audio), np.float32)
    x[0] = audio[0]
    x[1:] = audio[1:] - 0.97 * audio[:-1]
    sig = np.pad(x, _N_FFT // 2, mode="reflect")
    t = 1 + (len(sig) - _N_FFT) // _HOPN

    win = np.zeros(_N_FFT, np.float32)
    off = (_N_FFT - _WINL) // 2
    win[off:off + _WINL] = np.hanning(_WINL + 2)[1:-1]

    n_bins = _N_FFT // 2 + 1
    m0, m1 = _hz2mel(20.0), _hz2mel(SR / 2.0 - 400.0)
    fpts = _mel2hz(m0 + (m1 - m0) * np.arange(_NM + 2) / (_NM + 1))
    freqs = np.arange(n_bins) * (SR / _N_FFT)
    basis = np.zeros((_NM, n_bins), np.float32)
    for m in range(_NM):
        lo, c, hi = fpts[m], fpts[m + 1], fpts[m + 2]
        up = (freqs - lo) / (c - lo)
        dn = (hi - freqs) / (hi - c)
        basis[m] = np.minimum(up, dn).clip(0) * (2.0 / (hi - lo))

    frames = np.lib.stride_tricks.as_strided(
        sig, (t, _N_FFT),
        (sig.strides[0] * _HOPN, sig.strides[0])).copy() * win
    power = np.abs(np.fft.rfft(frames, axis=1)) ** 2     # (t, n_bins)
    feat = np.log(power @ basis.T + _GUARD).T          # (n_mels, t)
    sd = feat.std(axis=1, ddof=1, keepdims=True) + 1e-5
    return ((feat - feat.mean(axis=1, keepdims=True)) / sd).astype(
        np.float32)


# ---------------------------------------------------------------------
# Vocal onset strength — port of ups-prep onset.rs (10 ms hops,
# half-wave rectified log-magnitude flux, p95-normalised).
# ---------------------------------------------------------------------

ONSET_HOP_MS = 10.0
_ON_NFFT, _ON_HOP, _ON_BANDS = 512, 160, 40


def onset_strength(audio16: np.ndarray) -> np.ndarray:
    if len(audio16) < _ON_NFFT:
        return np.zeros(0, np.float32)
    n_fr = 1 + (len(audio16) - _ON_NFFT) // _ON_HOP
    win = np.hanning(_ON_NFFT + 2)[1:-1].astype(np.float32)
    n_bins = _ON_NFFT // 2 + 1
    edges = np.minimum(
        (80.0 * (6000.0 / 80.0) ** (np.arange(_ON_BANDS + 1) / _ON_BANDS)
         * _ON_NFFT / SR).astype(int), n_bins - 1)
    frames = np.lib.stride_tricks.as_strided(
        audio16, (n_fr, _ON_NFFT),
        (audio16.strides[0] * _ON_HOP, audio16.strides[0])).copy() * win
    mag = np.abs(np.fft.rfft(frames, axis=1)) ** 2     # (n_fr, n_bins)
    loge = np.empty((n_fr, _ON_BANDS), np.float32)
    for b in range(_ON_BANDS):
        lo, hi = edges[b], max(edges[b + 1], edges[b] + 1)
        loge[:, b] = np.log(mag[:, lo:hi].mean(axis=1) + 1e-6)
    flux = np.maximum(np.diff(loge, axis=0, prepend=loge[:1]), 0).sum(1)
    p95 = max(np.percentile(flux, 95), 1e-3)
    return (flux / p95).astype(np.float32)


def onset_snap(strength: np.ndarray, t_ms: float, back_ms: float,
               fwd_ms: float, min_strength: float):
    """Strongest onset peak in [t-back, t+fwd] or None."""
    lo = max(int((t_ms - back_ms) / ONSET_HOP_MS), 0)
    hi = min(int((t_ms + fwd_ms) / ONSET_HOP_MS), len(strength) - 1)
    if lo >= hi or len(strength) < 3:
        return None
    best = None
    for i in range(max(lo, 1), hi):
        v = strength[i]
        if v < min_strength or v < strength[i - 1] \
                or v < strength[i + 1]:
            continue
        dist = abs(i * ONSET_HOP_MS - t_ms) / (back_ms + fwd_ms)
        score = v * (1.0 - 0.3 * dist)
        if best is None or score > best[0]:
            best = (score, i)
    return None if best is None else best[1] * ONSET_HOP_MS


# ---------------------------------------------------------------------
# model discovery
# ---------------------------------------------------------------------

def _find_dir(models: Path, prefix: str):
    if not models.is_dir():
        return None
    for p in models.iterdir():
        if p.name.startswith(prefix) \
                and (p / "model.int8.onnx").exists() \
                and (p / "tokens.txt").exists():
            return p
    return None


def _parakeet_dir(models: Path):
    return _find_dir(models, "sherpa-onnx-nemo-parakeet_tdt_ctc")


def _omni_dir(models: Path):
    return _find_dir(models, "sherpa-onnx-omnilingual-asr-")


def backend_for(models, lang: str = ""):
    """'parakeet' | 'omni' | None — which aligner lang gets."""
    models = Path(models)
    en = not lang or lang == "en"
    if en and _parakeet_dir(models):
        return "parakeet"
    if _omni_dir(models):
        return "omni"
    if _parakeet_dir(models):
        return "parakeet"     # romanized fallback when no multi model
    return None


# ---------------------------------------------------------------------
# the aligner
# ---------------------------------------------------------------------

class Aligner:
    """ONNX CTC model + vocab; log_probs() and align() per song."""

    def __init__(self, models, backend: str):
        import onnxruntime as ort
        models = Path(models)
        d = (_parakeet_dir(models) if backend == "parakeet"
             else _omni_dir(models))
        if d is None:
            raise RuntimeError(f"no {backend} aligner model under {models}")
        self.sess = ort.InferenceSession(
            str(d / "model.int8.onnx"),
            providers=["CPUExecutionProvider"])
        vocab, max_id = {}, 0
        for line in (d / "tokens.txt").read_text(
                encoding="utf-8").splitlines():
            tok, _, n = line.rpartition(" ")
            if not n.isdigit():
                continue
            max_id = max(max_id, int(n))
            vocab[tok if tok else " "] = int(n)
        self.backend, self.vocab = backend, vocab
        if backend == "parakeet":
            self.blank, self.space, self.frame_ms = \
                vocab.get("<blk>", max_id), None, 80.0
        else:
            self.blank, self.space, self.frame_ms = \
                0, vocab.get(" "), 20.0
        self._in_names = [i.name for i in self.sess.get_inputs()]

    # ---- frame log-posteriors --------------------------------------

    def _run_chunk(self, chunk: np.ndarray) -> np.ndarray:
        """One window → (T, V) log-softmaxed rows."""
        if self.backend == "parakeet":
            feat = nemo_fbank(chunk)                    # (80, t)
            x = feat[None].astype(np.float32)
            out = self.sess.run(None, {
                self._in_names[0]: x,
                self._in_names[1]: np.array([x.shape[2]], np.int64)})
        else:
            out = self.sess.run(None, {
                self._in_names[0]: chunk[None].astype(np.float32)})
        rows = out[0][0]                                # (T, V)
        m = rows.max(axis=1, keepdims=True)
        lse = m + np.log(np.exp(rows - m).sum(axis=1, keepdims=True))
        return (rows - lse).astype(np.float32)

    def log_probs(self, audio16: np.ndarray) -> np.ndarray:
        """Whole stem → (frames, vocab), windowed + stitched."""
        stride = int(self.frame_ms / 1000.0 * SR)
        win, ov = int(WIN_S * SR), int(OVERLAP_S * SR)
        hop = win - ov
        edge = ov // 2 // stride
        chunks = []
        frames = 0
        start = 0
        vocab = 0
        while True:
            end = min(start + win, len(audio16))
            chunk = audio16[start:end]
            if len(chunk) < stride * 4:
                break
            rows = self._run_chunk(chunk)
            t, vocab = rows.shape
            first = 0 if start == 0 else min(edge, t)
            last = t if end == len(audio16) else max(t - edge, 0)
            base = start // stride
            for fr in range(first, last):
                ab = base + fr
                if ab < frames:
                    continue
                while frames < ab:
                    pad = (np.zeros(vocab, np.float32) if not chunks
                           else chunks[-1])
                    chunks.append(pad)
                    frames += 1
                chunks.append(rows[fr])
                frames += 1
            if end == len(audio16):
                break
            start += hop
        if not chunks:
            return np.zeros((0, vocab or 1), np.float32)
        return np.stack(chunks)

    # ---- tokenisation -----------------------------------------------

    @staticmethod
    def _align_char(c: str) -> str:
        if c.isalnum() or c == "'":
            out = "".join(
                ch for ch in unicodedata.normalize("NFD", c.lower())
                if ch.isalnum() or ch == "'"
                or unicodedata.category(ch) == "Mn")
            return out
        return ""

    def _bpe_greedy(self, word: str):
        s = "▁" + word
        out, i = [], 0
        while i < len(s):
            best = None
            for j in range(len(s), i, -1):
                piece = s[i:j]
                if piece in self.vocab:
                    best = (j - i, self.vocab[piece])
                    break
            if best is None:
                if s[i] == "▁":
                    return None
                i += 1
                continue
            out.append(best[1])
            i += best[0]
        return out or None

    def _bpe_all(self, word: str, limit: int = 32):
        """Vocab-token segmentations of ``word`` — the greedy split
        isn't always the one the model emits (it may prefer a shorter
        first piece, e.g. ``▁o bs er er ved``), so low-confidence words
        retry alternates. Each valid first piece gets its own budget
        so a greedy-heavy branch can't starve the rest."""
        s = "▁" + word
        out = []
        firsts = [j for j in range(len(s), 0, -1) if s[:j] in self.vocab]
        per = max(3, limit // max(1, len(firsts)))

        def dfs(i, acc, cap, bag):
            if len(bag) >= cap:
                return
            if i == len(s):
                bag.append(acc[:])
                return
            for j in range(len(s), i, -1):
                piece = s[i:j]
                if piece in self.vocab:
                    acc.append(self.vocab[piece])
                    dfs(j, acc, cap, bag)
                    acc.pop()
            if s[i] != "▁":
                dfs(i + 1, acc, cap, bag)   # unknown char — skip it

        for j in firsts:
            bag = []
            dfs(j, [self.vocab[s[:j]]], per, bag)
            out.extend(bag)
            if len(out) >= limit:
                break
        return out

    def tokenize_candidates(self, word: str):
        """All segmentations worth trying for one lyric word."""
        clean = "".join(c for c in word if c.isalnum() or c == "'")
        if not clean:
            return []
        seen, out = set(), []
        for c in (clean, clean.lower()):
            for ids in self._bpe_all(c):
                k = tuple(ids)
                if k not in seen:
                    seen.add(k)
                    out.append(ids)
        return out[:24]

    def tokenize(self, word: str):
        if self.backend == "omni":
            return [self.vocab[c] for c in
                    (ch for c in word for ch in self._align_char(c)
                     if ch)
                    if c in self.vocab]
        clean = "".join(c for c in word if c.isalnum() or c == "'")
        if not clean:
            return []
        a, b = self._bpe_greedy(clean), self._bpe_greedy(clean.lower())
        if a and b:
            return b if len(b) < len(a) else a
        return a or b or []

    # ---- CTC Viterbi -------------------------------------------------

    def align(self, lp: np.ndarray, lines: list[str]) -> list[dict]:
        """Lines of lyric text → per-word {line, text, start_ms,
        end_ms, conf}. <*> wildcard states between lines absorb
        unlyriced audio (instrumentals, ad-libs) without dragging."""
        words = []
        for li, line in enumerate(lines):
            for pos, w in enumerate(
                    t for t in line.split()
                    if any(c.isalnum() for c in t)):
                ids = self.tokenize(w)
                if ids:
                    words.append({"line": li, "pos": pos,
                                  "text": w, "ids": ids})
        t_n, vocab = lp.shape
        if not words or t_n == 0:
            return []

        BLANK, TOK, STAR = 0, 1, 2
        states = []                # (kind, tok, word_idx)
        states.append((STAR, -1, -1))
        states.append((BLANK, self.blank, -1))
        cur = -1
        for wi, w in enumerate(words):
            if w["line"] != cur and cur != -1:
                states.append((STAR, -1, -1))
                states.append((BLANK, self.blank, -1))
            cur = w["line"]
            for ti, tok in enumerate(w["ids"]):
                if ti > 0:
                    states.append((BLANK, self.blank, -1))
                states.append((TOK, tok, wi))
            if self.space is not None:
                states.append((BLANK, self.blank, -1))
                states.append((TOK, self.space, -1))
            states.append((BLANK, self.blank, -1))
        states.append((STAR, -1, -1))
        states.append((BLANK, self.blank, -1))
        n = len(states)

        # star emission: best non-blank discounted, or blank
        nb = lp.copy()
        nb[:, self.blank] = -np.inf
        star = np.maximum(nb.max(axis=1) - 1.0, lp[:, self.blank])

        emit = np.empty((t_n, n), np.float32)
        for s, (kind, tok, _) in enumerate(states):
            if kind == BLANK:
                emit[:, s] = lp[:, self.blank]
            elif kind == TOK:
                emit[:, s] = lp[:, tok]
            else:
                emit[:, s] = star

        def skippable(s):
            kind, tok, _ = states[s]
            return kind != TOK or tok == self.space

        def same_tok(a, b):
            ka, ta, _ = states[a]
            kb, tb, _ = states[b]
            return ka == TOK and kb == TOK and ta == tb

        preds = []
        for s in range(n):
            v = [s]
            if s >= 1:
                v.append(s - 1)
            for k in range(2, 5):
                if s < k or not all(skippable(s - j)
                                    for j in range(1, k)):
                    break
                if not same_tok(s, s - k):
                    v.append(s - k)
            preds.append(v)

        neg = -np.inf
        score = np.full(n, neg, np.float64)
        for s in range(n):
            if s == 0 or all(skippable(j) for j in range(1, s + 1)):
                score[s] = emit[0, s]
        bp = np.zeros((t_n, n), np.uint8)
        nxt = np.empty(n, np.float64)
        for t in range(1, t_n):
            for s in range(n):
                best, bi = neg, 0
                for i, p in enumerate(preds[s]):
                    if score[p] > best:
                        best, bi = score[p], i
                nxt[s] = neg if best == neg else best + emit[t, s]
                bp[t, s] = bi
            score, nxt = nxt, score
        s = int(np.argmax(score[max(0, n - 3):]) + max(0, n - 3))
        path = np.empty(t_n, np.int64)
        for t in range(t_n - 1, -1, -1):
            path[t] = s
            if t > 0:
                s = preds[s][bp[t, s]]

        first = np.full(len(words), -1, np.int64)
        last = np.zeros(len(words), np.int64)
        lp_sum = np.zeros(len(words), np.float64)
        n_tok = np.zeros(len(words), np.int64)
        prev = -1
        for t in range(t_n):
            si = path[t]
            kind, tok, w = states[si]
            if kind == TOK and w >= 0:
                if first[w] < 0:
                    first[w] = t
                last[w] = t
                if si != prev:
                    lp_sum[w] += lp[t, tok]
                    n_tok[w] += 1
            prev = si
        out = []
        for wi, w in enumerate(words):
            if first[wi] < 0:
                continue
            out.append({
                "line": w["line"], "pos": w["pos"], "text": w["text"],
                "start_ms": float(first[wi]) * self.frame_ms,
                "end_ms": float(last[wi] + 1) * self.frame_ms,
                "conf": float(np.exp(lp_sum[wi] / n_tok[wi]))
                if n_tok[wi] else 0.0,
            })

        # resegment low-confidence words — the greedy BPE split
        # sometimes differs from the tokenization the model actually
        # emits; alternates are scored on the same span and the
        # winner re-times the word
        if self.backend == 'parakeet':
            for x in out:
                if x['conf'] >= 0.5:
                    continue
                cands = self.tokenize_candidates(x['text'])
                if len(cands) <= 1:
                    continue
                f0 = max(0, int(x['start_ms'] / self.frame_ms) - 6)
                f1 = min(t_n, int(x['end_ms'] / self.frame_ms) + 6)
                best = None
                for ids in cands:
                    r = self._span_align(lp, ids, f0, f1)
                    if r and (best is None or r[0] > best[0]):
                        best = r
                if best and best[0] > x['conf']:
                    sc, first_f, last_f = best
                    x['conf'] = sc
                    x['start_ms'] = (f0 + first_f) * self.frame_ms
                    x['end_ms'] = min(
                        (f0 + last_f + 1) * self.frame_ms,
                        t_n * self.frame_ms)
        return out

    def _span_align(self, lp, ids, f0, f1):
        """Force-align one token sequence inside lp[f0:f1]. Returns
        (mean token probability, first token frame, last token frame)
        or None."""
        n = len(ids)
        if f1 - f0 < n + 2:
            return None
        BLANK, TOK = 0, 1
        # states: leading blank, then TOK B per token
        kinds, toks = [BLANK], [self.blank]
        for t in ids:
            kinds += [TOK, BLANK]
            toks += [t, self.blank]
        s_n = len(kinds)
        preds = []
        for s in range(s_n):
            p = [s]
            if s > 0 and not (kinds[s] == TOK and kinds[s - 1] == TOK
                              and toks[s] == toks[s - 1]):
                p.append(s - 1)
            k = s - 1
            for _ in range(3):
                if k <= 0 or kinds[k] == TOK:
                    break
                k -= 1
                if kinds[k] == TOK:
                    p.append(k)
                    break
            preds.append(p)
        dp = np.full(s_n, -np.inf, dtype=np.float64)
        arg = np.zeros((f1 - f0, s_n), np.uint8)
        dp[0] = float(lp[f0, self.blank])
        for t in range(1, f1 - f0):
            col = lp[f0 + t]
            nd = np.full(s_n, -np.inf, dtype=np.float64)
            for s in range(s_n):
                i = max(preds[s], key=lambda q: dp[q])
                nd[s] = dp[i] + float(col[toks[s]])
                arg[t, s] = i
            dp = nd
        s_end = s_n - 1 if dp[s_n - 1] >= dp[s_n - 2] else s_n - 2
        first = last = -1
        score, ntok = 0.0, 0
        s = s_end
        for t in range(f1 - f0 - 1, 0, -1):
            if kinds[s] == TOK:
                score += float(lp[f0 + t, toks[s]])
                ntok += 1
                if last < 0:
                    last = t
                first = t            # backtracing downward → smallest wins
            s = int(arg[t, s])
        if not ntok or first < 0:
            return None
        return float(np.exp(score / ntok)), first, last


def align_lines(rec: dict) -> list[str]:
    """Lyric lines for the aligner: synced text wins, plain as backup.
    Section markers ([Chorus], {Verse}) and (x2) repeat tags are
    dropped so they never enter the token lattice as fake words."""
    raw = ([t for _, t in rec.get("synced") or []]
           or rec.get("plain") or [])
    out = []
    for line in raw:
        keep = []
        for w in line.split():
            if w.startswith(("[", "{")):
                continue
            inner = "".join(c for c in w if c.isalnum() or c == "xX")
            rep = inner.lstrip("xX")
            if inner and rep and rep.isdigit():
                continue
            if any(c.isalnum() for c in w):
                keep.append(w)
        if keep:
            out.append(" ".join(keep))
    return out
