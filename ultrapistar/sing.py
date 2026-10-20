"""Live singing session: ties the video clock, mic pitch, and the ported
WorldParty scorer together.

Flow:
  * created when a verified-chart song reaches the player
  * the TV's ``player:position`` events (authoritative media clock) drive
    ``tick()``; each position covers a span of chart beats
  * the latest mic pitch feeds each beat into the ``Scorer``
  * per-tick ``sing:update`` and per-phrase ``sing:line`` events are
    emitted for the TV overlay; ``finish()`` returns the final score map
"""
from __future__ import annotations

import collections
import threading
import time

from .charts import load_chart
from .mic import MicCapture
from .scoring import SCORE_FACTOR, Scorer

# pitch-contour autosync: once this much sung audio exists, solve the
# chart->media offset by sliding the expected melody across +-WINDOW
AUTOSYNC_AFTER_MS = 8000
AUTOSYNC_WINDOW_MS = 150000   # community GAPs target media with ~90s
                              # intros — a +-20s window can't reach them
AUTOSYNC_MIN_SAMPLES = 60
# host-triggered resync: seconds of fresh audio collected before solving
RESYNC_COLLECT_SEC = 10


def _norm_word(t: str) -> str:
    return "".join(ch for ch in t.lower() if ch.isalnum())


def word_align(words: list[tuple[str, float, float]],
               chart_words: list[tuple[str, int]],
               beat_ms: float):
    """Align transcript words to chart words by offset voting.

    ``words``: [(text, media_ms, probability)] from a speech model;
    ``chart_words``: [(text, start_beat)] at word boundaries.
    Every same-text match votes for ``base = media_ms - beat*beat_ms`` —
    the media time of chart beat 0. The histogram peak is the solved
    base; nearby votes are median-refined.

    Returns (base_ms, supporting_votes, coverage) or (None, 0, 0).
    """
    idx: dict[str, list[int]] = {}
    for text, beat in chart_words:
        idx.setdefault(text, []).append(beat)
    votes = []
    for text, ms, prob in words:
        for beat in idx.get(text, ()):
            votes.append((ms - beat * beat_ms, prob))
    if len(votes) < 4:
        return None, 0, 0.0
    hist: dict[int, float] = {}
    for d, p in votes:
        b = int(d // 100)
        hist[b] = hist.get(b, 0.0) + p
    peak = max(hist, key=hist.get)
    near = sorted(d for d, _p in votes if abs(d - peak * 100) <= 100)
    support = len(near)
    base = near[support // 2]
    matched = sum(1 for t, _ms, _p in words if t in idx)
    coverage = support / max(1, matched)
    if support < 5 or coverage < 0.5:
        return None, support, coverage
    return base, support, coverage


class SingSession:
    """One scored performance of a charted media entry."""

    def __init__(self, chart_path: str, sync_offset_ms: int = 0,
                 tracks: list[str] | None = None, difficulty: int = 0,
                 mic: MicCapture | None = None,
                 mics: dict | None = None,
                 warp: list | None = None, tempo: float = 1.0):
        self.chart = load_chart(chart_path)
        self.difficulty = difficulty
        self.tempo = tempo or 1.0
        self.beat_ms = (15000.0 / self.chart.bpm * self.tempo
                        if self.chart.bpm else 0)
        self.base_ms = self.chart.gap_ms + sync_offset_ms
        # learned per-phrase offsets [[phrase_start_beat, offset_ms]] —
        # segments let a video's cut/extended instrumentals hold their
        # own correction instead of dragging every later line off
        self._warp = sorted((int(b), float(o)) for b, o in (warp or []))
        self.track_map = tracks or sorted(self.chart.tracks)
        self.scorers = {track: Scorer(self.chart, track, difficulty)
                        for track in self.track_map}
        # {track: MicCapture} — host-assigned per-singer mics; a bare `mic`
        # feeds the first track only (single-mic behavior from Phase 3)
        self.mics = dict(mics or {})
        if mic and self.track_map:
            self.mics.setdefault(self.track_map[0], mic)
        self._last_beat = -1
        self.mic_ok = False
        self._owns_mic = False
        self._events: list[dict] = []
        self._tone_log: dict[str, list[tuple[float, int]]] = {
            t: [] for t in self.track_map}
        self.autosync_done = False
        # wall-clock -> media-clock anchors for mapping captured PCM to
        # song position (needed by the speech-model sync stage)
        self._clock_pts: collections.deque = collections.deque(maxlen=800)
        self._voice_state = "idle"        # idle | running | done
        self._pitch_attempted = False
        self._voice_result: dict | None = None
        self._voice_gen = 0   # bumped on resync so stale workers can't land
        self._voice_track = self.track_map[0] if self.track_map else "P1"
        self._resync_until: float | None = None   # manual collect deadline
        self._roll_next: int | None = None  # next rolling-check sample count
        self._onset_next = 0.0              # next onset-snap evaluation
        self.targets: dict[str, int] = {}   # device -> expected pitch

    # -- mic ----------------------------------------------------------------
    def _tone(self, track: str) -> tuple:
        cap = self.mics.get(track)
        if cap and cap.latest.valid:
            return cap.latest.tone_abs, cap.latest.tone_str
        return None, "-"

    def start_mic(self, device: str = "default") -> bool:
        """Mark mic state; shared app captures are already running.

        Only spawns a private capture when no mics were provided at all —
        ALSA allows a single opener per device, so a second arecord would
        fail while the monitor is running.
        """
        self._owns_mic = False
        if not self.mics:
            cap = MicCapture(device)
            self.mics = {self.track_map[0]: cap} if self.track_map else {}
            self._owns_mic = True
            status = cap.start()
            self.mic_ok = status.available
            return status.available
        self.mic_ok = any(getattr(c, "running", True)
                          for c in self.mics.values())
        return self.mic_ok

    def stop_mic(self) -> None:
        # only stop a capture we spawned ourselves — shared app captures
        # keep running so speaker monitoring stays live between songs
        if self._owns_mic:
            for cap in self.mics.values():
                cap.stop()

    # -- clock ---------------------------------------------------------------
    def set_sync_offset(self, offset_ms: int) -> None:
        """Live chart->media sync adjust (host nudge mid-song)."""
        self.base_ms = self.chart.gap_ms + offset_ms

    def apply_sync_offset(self, offset_ms: int, position_ms: float) -> None:
        """Sync adjust that also rebases the beat cursor — without this a
        large shift would make the next tick 'catch up' dozens of beats
        against a single pitch sample."""
        self.set_sync_offset(offset_ms)
        self._last_beat = self.beat_at(position_ms)

    def beat_at(self, position_ms: float) -> int:
        if self.beat_ms <= 0:
            return -1
        # segmented map: the active segment is the last learned phrase
        # boundary whose warped start has already been reached; between
        # boundaries, beats continue at the chart rate
        for wb, wo in reversed(self._warp):
            start_ms = self.chart.gap_ms + wb * self.beat_ms + wo
            if position_ms >= start_ms:
                return int(wb + (position_ms - start_ms) / self.beat_ms)
        return int((position_ms - self.base_ms) / self.beat_ms)

    def learn_warp(self, beat: int, offset_ms: int) -> None:
        """Record that from ``beat`` (a phrase boundary) onward the media
        runs at ``offset_ms`` — persisted on the chart row by the caller."""
        self._warp = [e for e in self._warp if e[0] != beat]
        self._warp.append((int(beat), float(offset_ms)))
        self._warp.sort()

    def warp_boundary(self, position_ms: float) -> int:
        """First unstarted phrase boundary on the voice track — where a
        freshly learned offset takes effect."""
        beat = self.beat_at(position_ms)
        for line in self.scorers[self._voice_track].lines:
            if line and line[0].start > beat:
                return line[0].start
        return max(beat, 0)

    def tick(self, position_ms: float) -> dict:
        """Advance scoring to ``position_ms`` of media time."""
        # warp segments can map a position back over already-scored beats
        # when a boundary switches — the cursor only moves forward
        beat = max(self.beat_at(position_ms), self._last_beat)
        self._clock_pts.append((time.monotonic(), position_ms))
        tones = {track: self._tone(track) for track in self.scorers}
        for track, (tone, _s) in tones.items():
            if tone is not None and tone >= 0:
                log = self._tone_log[track]
                log.append((position_ms, tone))
                if len(log) > 4000:
                    del log[:1000]
        first = self.track_map[0]
        tone, tone_str = tones.get(first, (None, "-"))
        last_hit = None
        # process every beat between last tick and now (catch-up)
        line_events = []
        while self._last_beat < beat:
            self._last_beat += 1
            for track, scorer in self.scorers.items():
                ev = scorer.tick(self._last_beat, tones.get(track, (None,))[0])
                if ev and track == first:
                    last_hit = ev
                    if ev.get("line"):
                        line_events.append(ev["line"])
        # expected chart pitch per mic device — the monitor's autotune
        # snaps sung notes toward these (nearest semitone when uncharted)
        self.targets = {}
        for track, scorer in self.scorers.items():
            device = getattr(self.mics.get(track), "device", None)
            if not device:
                continue
            for line in scorer.lines:
                note = next((n for n in line
                             if n.start <= beat < n.start + n.length
                             and n.kind != "freestyle"), None)
                if note is not None:
                    self.targets[device] = note.pitch
                    break
        out = {
            "beat": beat,
            "ms": position_ms,
            "mic": self.mic_ok,
            "players": {
                track: {"score": s.state.total,
                        "golden": s.state.score_golden_int,
                        "line": s.state.score_line_int,
                        "streak": s.state.streak}
                for track, s in self.scorers.items()
            },
            "tone": tone, "tone_str": tone_str,
            "tones": {t: v[0] for t, v in tones.items()},
            "hit": last_hit,
            "line_events": line_events,
        }
        self._events.append({"type": "tick", **out})
        return out

    # -- autosync ------------------------------------------------------------
    def maybe_autosync(self) -> dict | None:
        """Solve chart->media offset from the first seconds of singing.

        The recorded sung tones are scored against the expected melody at
        every candidate offset in +-AUTOSYNC_WINDOW_MS; the winner must
        cover enough of the singing and clearly beat the current offset.
        Returns {delta_ms, offset_ms, score} or {"delta_ms": 0} when no
        change is justified, None while still collecting samples.
        """
        if self.beat_ms <= 0:
            return None
        if self._resync_until is None:
            snap = self._onset_snap()
            if snap:
                return snap
        if self.autosync_done:
            return self._roll_check()
        if self._pitch_attempted:
            # pitch solve already failed — poll the speech-model worker
            if self._voice_result is not None:
                self.autosync_done = True
                return self._voice_result or {"delta_ms": 0,
                                              "method": "whistle"}
            return None
        track, samples = None, []
        for t, log in self._tone_log.items():
            if t in self.mics and len(log) > len(samples):
                track, samples = t, log
        # manual resync forces a solve once its collect window ends; the
        # automatic path waits for AUTOSYNC_AFTER_MS of sung audio
        manual = (self._resync_until is not None
                  and time.monotonic() >= self._resync_until)
        ready = (track is not None and len(samples) >= AUTOSYNC_MIN_SAMPLES
                 and samples[-1][0] - samples[0][0] >= AUTOSYNC_AFTER_MS)
        if not (ready or manual):
            return None
        self._pitch_attempted = True
        self._resync_until = None
        if track is not None and len(samples) >= 15:
            delta, score, base_score, cov = self._solve_offset(
                track, samples)
            # far shifts (community GAPs off by ~90s) need stronger proof
            # than small drift corrections — a confident-but-wrong lock
            # would ratchet the persisted offset out of reach
            need = 0.6 if abs(delta) > 20000 else 0.5
            if (abs(delta) > 40 and score >= need
                    and cov >= 0.4 and score >= base_score + 0.1):
                self.autosync_done = True
                self._roll_next = len(samples) + self.ROLL_CHECK_SAMPLES
                return {"delta_ms": delta, "score": round(score, 3),
                        "method": "pitch",
                        "offset_ms": int(self.base_ms - self.chart.gap_ms
                                         + delta)}
            if base_score >= 0.5:
                # already aligned well — settle without the model
                self.autosync_done = True
                return {"delta_ms": 0, "score": round(base_score, 3),
                        "method": "pitch"}
        # pitch contour couldn't lock (off-key or spoken singing) — try
        # the speech model on the captured audio, on a worker thread so
        # the position-tick loop never stalls
        if self._voice_state == "idle":
            self._voice_state = "running"
            if track:
                self._voice_track = track
            gen = self._voice_gen
            threading.Thread(target=self._voice_worker, args=(gen,),
                             daemon=True).start()
        return None

    ROLL_CHECK_SAMPLES = 350   # ~7s+ of new singing between re-solves
    ROLL_WINDOW = 400          # recent sung tones scored per re-solve
    ROLL_WINDOW_MS = 45000     # ±45s — media can't teleport mid-phrase;
                             # bigger steps are gaps, handled by onset snap

    def _roll_check(self) -> dict | None:
        """Rolling re-solve on recent sung tones.

        Karaoke videos whose arrangement differs from the chart's
        reference media (a cut instrumental break, a shortened bridge)
        step the true gap mid-song; a tempo mismatch wanders it. When the
        recent window aligns somewhere clearly different, apply the step —
        the same confident-match gate as the first solve, plus the current
        offset must score materially worse on the recent window."""
        track, log = None, []
        for t, l in self._tone_log.items():
            if t in self.mics and len(l) > len(log):
                track, log = t, l
        if track is None:
            return None
        if self._roll_next is not None and len(log) < self._roll_next:
            return None
        self._roll_next = len(log) + self.ROLL_CHECK_SAMPLES
        recent = log[-self.ROLL_WINDOW:]
        if len(recent) < 80:
            return None
        delta, score, base_score, cov = self._solve_offset(
            track, recent, window_ms=self.ROLL_WINDOW_MS)
        if (abs(delta) > 800 and score >= 0.6 and cov >= 0.4
                and score >= base_score + 0.15):
            return {"delta_ms": delta, "score": round(score, 3),
                    "method": "rolling",
                    "offset_ms": int(self.base_ms - self.chart.gap_ms
                                     + delta)}
        return None

    ONSET_MIN_RUN = 8        # contiguous sung tones needed to call an onset
    ONSET_RUN_GAP_MS = 350   # max silence inside a run
    ONSET_MIN_SCORE = 0.6
    ONSET_MIN_SHIFT_MS = 400
    ONSET_EVAL_SEC = 1.5     # throttle between evaluations

    def _onset_snap(self) -> dict | None:
        """Snap the offset when the next phrase's melody re-enters early
        or late.

        Karaoke videos routinely shorten or stretch instrumental breaks
        versus the media a chart was timed on; a single offset can't
        express that. While a gap runs we watch the trailing run of
        detected tones (the singer's voice, or TV vocal bleed through
        the mic): once it matches the *next* phrase's pitch contour we
        retime so the line lands on the actual onset, not the chart's
        guess. Only fires during a gap — once the phrase starts, normal
        scoring/rolling checks take over."""
        now_ms = (self._clock_pts[-1][1] if self._clock_pts else None)
        if now_ms is None or time.monotonic() < self._onset_next:
            return None
        self._onset_next = time.monotonic() + self.ONSET_EVAL_SEC
        for track, log in self._tone_log.items():
            if track not in self.mics or not log:
                continue
            # trailing run of contiguous sung tones
            run = [log[-1]]
            for ms, t in reversed(log[-60:-1]):
                if run[0][0] - ms > self.ONSET_RUN_GAP_MS:
                    break
                run.insert(0, (ms, t))
            if len(run) < self.ONSET_MIN_RUN or run[-1][0] < now_ms - 400:
                continue
            # the next line that hasn't started yet on this track
            nxt = next((l for l in self.scorers[track].lines
                        if l and l[0].start > self._last_beat), None)
            if not nxt:
                continue
            pstart = nxt[0].start
            exp = {}
            for n in nxt:
                if n.kind == "freestyle":
                    continue
                for b in range(n.start, n.start + n.length):
                    exp[b] = n.pitch
            if not exp:
                continue
            # anchor: the run's first tone is the phrase's first note
            onset = run[0][0]
            hits = 0
            for ms, tone in run:
                want = exp.get(pstart + int((ms - onset) / self.beat_ms))
                if want is None:
                    continue
                d = abs(want - tone) % 12
                if min(d, 12 - d) <= 2:
                    hits += 1
            score = hits / len(run)
            new_off = int(onset
                          - (self.chart.gap_ms + pstart * self.beat_ms))
            delta = int(new_off - (self.base_ms - self.chart.gap_ms))
            if score >= self.ONSET_MIN_SCORE \
                    and abs(delta) > self.ONSET_MIN_SHIFT_MS:
                return {"delta_ms": delta, "offset_ms": new_off,
                        "score": round(score, 3), "method": "onset",
                        "warp_beat": pstart}
        return None

    def request_resync(self) -> None:
        """Host-triggered resync: drop prior sync state and captured
        audio, then collect RESYNC_COLLECT_SEC of fresh room audio before
        re-solving the offset (pitch first, speech model fallback)."""
        self.autosync_done = False
        self._pitch_attempted = False
        self._roll_next = None
        self._voice_result = None
        self._voice_gen += 1        # an in-flight worker's result is stale
        self._voice_state = "idle"
        self._resync_until = time.monotonic() + RESYNC_COLLECT_SEC
        for log in self._tone_log.values():
            log.clear()
        for cap in self.mics.values():
            if hasattr(cap, "clear_pcm"):
                cap.clear_pcm()

    # -- speech-model sync (Cactus Whistle, optional) -------------------------
    def _media_at(self, mono: float) -> float | None:
        """Media-clock ms at a wall-clock instant, interpolated between
        the position-tick anchors."""
        pts = self._clock_pts
        if not pts:
            return None
        if mono <= pts[0][0]:
            return pts[0][1]
        if mono >= pts[-1][0]:
            return pts[-1][1]
        for i in range(1, len(pts)):
            if pts[i][0] >= mono:
                (m0, p0), (m1, p1) = pts[i - 1], pts[i]
                return p0 + (mono - m0) / max(1e-6, m1 - m0) * (p1 - p0)
        return pts[-1][1]

    def _chart_words(self, track: str) -> list[tuple[str, int]]:
        """[(normalized_word, start_beat)] — syllables merged at
        word_break boundaries."""
        out = []
        for line in (self.chart.phrases().get(track) or []):
            text, start = "", 0
            for s in line["syls"]:
                if s["wb"] and text:
                    out.append((_norm_word(text), start))
                    text = ""
                if not text:
                    start = s["s"]
                text += s["t"]
            if text:
                out.append((_norm_word(text), start))
        return [w for w in out if w[0]]

    def _voice_worker(self, gen: int) -> None:
        try:
            res = self._solve_voice() or {}
        except Exception:
            res = {}
        if gen == self._voice_gen:      # a resync superseded this run
            self._voice_result = res

    def _solve_voice(self) -> dict | None:
        """Transcribe the captured mic window and word-align it to the
        chart. Optional: needs the ``needle`` engine (cactus-needle pip
        package) + whistle.cact weights, both cached locally."""
        cap = self.mics.get(self._voice_track)
        if cap is None or not hasattr(cap, "pcm_window"):
            return None
        chunks = cap.pcm_window()
        if len(chunks) < 10:
            return None
        import os
        os.environ.setdefault("NEEDLE_TELEMETRY", "0")   # no usage pings
        try:
            import needle
        except ImportError:
            return None
        import numpy as np
        audio = np.frombuffer(b"".join(c for _t, c in chunks),
                              dtype=np.int16).astype(np.float32) / 32768.0
        rate = cap.rate or 44100
        if rate != 16000:    # whistle expects 16 kHz mono
            n16 = int(len(audio) * 16000 / rate)
            audio = np.interp(np.linspace(0, len(audio) - 1, n16),
                              np.arange(len(audio)), audio
                              ).astype(np.float32)
        r = needle.transcribe(audio, word_timestamps=True)
        words = (r or {}).get("words") or []
        media0 = self._media_at(chunks[0][0])
        if media0 is None or len(words) < 4:
            return None
        dur_ms = len(audio) / 16.0   # samples -> ms at 16 kHz
        starts = [float(w.get("start") or 0) for w in words]
        scale = 1.0 if (starts and max(starts) > dur_ms * 1.5) else 1000.0
        tw = [(_norm_word(str(w.get("word") or w.get("text") or "")),
               media0 + float(w.get("start") or 0) * scale,
               float(w.get("probability") or w.get("prob") or 1.0))
              for w in words]
        tw = [(t, ms, p) for t, ms, p in tw if t]
        base, support, cov = word_align(
            tw, self._chart_words(self._voice_track), self.beat_ms)
        if base is None:
            return None
        delta = base - self.base_ms
        if abs(delta) <= 40:
            return {"delta_ms": 0, "method": "whistle"}
        return {"delta_ms": int(delta), "method": "whistle",
                "words": support,
                "offset_ms": int(base - self.chart.gap_ms)}

    def _solve_offset(self, track: str,
                      samples: list[tuple[float, int]],
                      window_ms: int | None = None):
        scorer = self.scorers[track]
        notes = [n for line in scorer.lines for n in line]
        lo = min(n.start for n in notes)
        hi = max(n.start + n.length for n in notes)
        exp = [-1] * (hi - lo + 1)          # expected tone per beat, -1 = gap
        for n in notes:
            if n.kind == "freestyle":
                continue
            for b in range(n.start, n.start + n.length):
                exp[b - lo] = n.pitch

        def score(base: float, pts) -> tuple[float, float]:
            hits = cov = 0
            for ms, tone in pts:
                b = int((ms - base) / self.beat_ms) - lo
                if 0 <= b < len(exp) and exp[b] >= 0:
                    cov += 1
                    d = abs(exp[b] - tone) % 12
                    if min(d, 12 - d) <= 2:   # +-2 semitones, any octave
                        hits += 1
            n_s = len(pts)
            return hits / n_s, cov / n_s

        def metric(base, pts):
            s, c = score(base, pts)
            return (s, c) if c >= 0.3 else (0.0, c)

        base0 = self.base_ms
        base_score, _ = score(base0, samples)
        best_d, (best_s, best_c) = 0, metric(base0, samples)
        win = window_ms or AUTOSYNC_WINDOW_MS
        # three-tier search: coarse pass over the full window on a
        # subsample (charts with ~90s lead-in GAPs sit far out), then
        # refine + fine around the winner
        coarse = samples[::3] if len(samples) > 60 else samples
        near_best = []
        for d in range(-win, win + 1, 250):
            s, c = metric(base0 + d, coarse)
            near_best.append((s, d))
            if s > best_s:
                best_d, best_s, best_c = d, s, c
        # melodic aliasing: repetitive charts (verse~chorus contours)
        # score nearly as well at several offsets. Among near-ties pick
        # the SMALLEST shift — a conservative correction beats a risky
        # far lock, and stops aliases from walking the offset away
        tie = [d for s, d in near_best if s >= best_s - 0.03 and s >= 0.5]
        if tie:
            best_d = min(tie, key=abs)
            # rescore the tie winner on the full sample set
            best_s, best_c = score(base0 + best_d, samples)
        for step, span in ((25, 250), (5, 25)):
            for d in range(max(-win, best_d - span),
                           min(win, best_d + span) + 1, step):
                s, c = metric(base0 + d, samples)
                if s > best_s:
                    best_d, best_s, best_c = d, s, c
        return best_d, best_s, base_score, best_c

    def finish(self) -> dict:
        self.stop_mic()
        return {"tracks": {t: s.finish() for t, s in self.scorers.items()},
                "score": next(iter(self.scorers.values())).state.total
                if self.scorers else 0}
