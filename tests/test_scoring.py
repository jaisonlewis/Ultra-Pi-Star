"""Ports of WorldParty's pitch detection + scoring (GPL-3.0)."""
import math
from array import array

from ultrapistar.charts import Chart, Note, parse_chart
from ultrapistar.pitch import (ANALYSIS_BUFFER_SIZE, NUM_HALFTONES,
                               TONE_FREQS, PitchDetector, freq_to_tone_abs)
from ultrapistar.scoring import MAX_SONG_SCORE, Scorer


def sine_pcm(freq: float, n: int = ANALYSIS_BUFFER_SIZE,
             rate: int = 44100, amp: int = 20000) -> bytes:
    buf = array("h", (int(amp * math.sin(2 * math.pi * freq * i / rate))
                      for i in range(n)))
    return buf.tobytes()


def make_chart(notes, track="P1", breaks=None):
    c = Chart()
    c.tracks[track] = notes
    c.breaks[track] = breaks or []
    c.headers = {"ARTIST": "T", "TITLE": "S", "BPM": "120", "GAP": "0"}
    return c


# -- pitch (port of URecord.pas TCaptureBuffer) ---------------------------

def test_pitch_detects_a4():
    det = PitchDetector(44100)
    det.feed(sine_pcm(440.0))
    res = det.analyze()
    assert res.valid
    assert res.tone_str == "A4"
    # A4 = C2 + 33 semitones
    assert res.tone_abs == 33 and res.tone == 9


def test_pitch_detects_c3_and_e4():
    det = PitchDetector(44100)
    det.feed(sine_pcm(TONE_FREQS[12]))   # C3
    assert det.analyze().tone_str == "C3"
    det2 = PitchDetector(44100)
    det2.feed(sine_pcm(TONE_FREQS[28]))  # E4
    assert det2.analyze().tone_str == "E4"


def test_pitch_rejects_silence():
    det = PitchDetector(44100)
    det.feed(b"\x00" * ANALYSIS_BUFFER_SIZE * 2)
    res = det.analyze()
    assert not res.valid
    assert res.tone_str == "-"


def test_freq_to_tone_abs():
    assert freq_to_tone_abs(440.0) == 33
    assert freq_to_tone_abs(0) == -1


# -- scoring (port of UNote.pas / UScreenSingController.pas) ---------------

def sing(chart, scorer, tones, beats=None):
    """Feed tones {beat: tone_abs} for every beat of the chart."""
    if beats is None:
        beats = max(n.start + n.length for line in scorer.lines for n in line)
    for b in range(beats):
        scorer.tick(b, tones.get(b))
    return scorer.finish()


def test_perfect_sing_reaches_max_score():
    # one line: 10-beat normal + 10-beat golden, then phrase break
    chart = make_chart([Note("normal", 0, 10, 30, "la "),
                        Note("golden", 10, 10, 32, "yeah")],
                       breaks=[20])
    sc = Scorer(chart, difficulty=0, line_bonus=True)
    tones = {}
    for line in sc.lines:
        for n in line:
            for b in range(n.start, n.start + n.length):
                tones[b] = n.pitch
    res = sing(chart, sc, tones)
    # line bonus enabled: 9000 note points + 1000 bonus = 10000
    assert res["score"] == MAX_SONG_SCORE


def test_perfect_sing_no_line_bonus():
    chart = make_chart([Note("normal", 0, 10, 30, "la ")])
    sc = Scorer(chart, difficulty=0, line_bonus=False)
    tones = {b: 30 for b in range(10)}
    res = sing(chart, sc, tones)
    assert res["score"] == MAX_SONG_SCORE


def test_octave_wrong_still_hits():
    """Singing an octave off is folded onto the target (port of the +-6
    semitone normalization loop)."""
    chart = make_chart([Note("normal", 0, 4, 30, "x")])
    sc = Scorer(chart, difficulty=0, line_bonus=False)
    tones = {b: 30 + 12 for b in range(4)}  # one octave up
    res = sing(chart, sc, tones)
    assert res["score"] == MAX_SONG_SCORE


def test_wrong_tone_misses_on_hard():
    chart = make_chart([Note("normal", 0, 4, 30, "x")])
    sc = Scorer(chart, difficulty=2, line_bonus=False)  # hard: 0 tolerance
    tones = {b: 31 for b in range(4)}  # a semitone off
    res = sing(chart, sc, tones)
    assert res["score"] == 0
    assert res["hits"] == 0


def test_easy_tolerance_two_halftones():
    chart = make_chart([Note("normal", 0, 4, 30, "x")])
    sc = Scorer(chart, difficulty=0, line_bonus=False)
    tones = {b: 28 for b in range(4)}  # two flat = still a hit on easy
    res = sing(chart, sc, tones)
    assert res["score"] == MAX_SONG_SCORE


def test_rap_notes_always_hit():
    chart = make_chart([Note("rap", 0, 4, 30, "yo")])
    sc = Scorer(chart, difficulty=2, line_bonus=False)
    tones = {b: 5 for b in range(4)}  # wildly off pitch still counts
    res = sing(chart, sc, tones)
    assert res["score"] == MAX_SONG_SCORE


def test_freestyle_notes_not_scored():
    chart = make_chart([Note("freestyle", 0, 4, 30, "free")])
    sc = Scorer(chart, line_bonus=False)
    tones = {b: 30 for b in range(4)}
    res = sing(chart, sc, tones)
    assert res["score"] == 0  # freestyle excluded from beat map


def test_silence_gives_zero():
    chart = make_chart([Note("normal", 0, 4, 30, "x")])
    sc = Scorer(chart, line_bonus=False)
    res = sing(chart, sc, {})  # no tones = nothing detected
    assert res["score"] == 0
    assert res["notes"] == 0


def test_player_notes_merge_contiguous():
    chart = make_chart([Note("normal", 0, 4, 30, "x")])
    sc = Scorer(chart, line_bonus=False)
    sing(chart, sc, {b: 30 for b in range(4)})
    assert len(sc.state.notes) == 1
    assert sc.state.notes[0].length == 4
    assert sc.state.notes[0].perfect


# -- SingSession: video clock -> beats -> scorer -------------------------

class FakeMic:
    """MicCapture stand-in — injects a fixed pitch."""
    def __init__(self, tone_abs):
        from ultrapistar.pitch import PitchResult
        self.latest = PitchResult(True, tone_abs % 12, tone_abs)
        self._started = False

    def start(self, device="default"):
        from ultrapistar.mic import MicStatus
        self._started = True
        return MicStatus(True, device)

    def stop(self):
        self._started = False


def test_sing_session_video_clock(tmp_path):
    """A fake mic singing the chart's tones through position ticks scores."""
    from ultrapistar.sing import SingSession
    p = tmp_path / "song.txt"
    # bpm 240 -> 250ms per beat; 4-beat note at pitch 30 starting beat 0
    p.write_text("#ARTIST:T\n#TITLE:S\n#BPM:240\n#GAP:100\n"
                 ": 0 4 30 la\n- 4\n: 8 4 32 hey\nE\n")
    sess = SingSession(str(p), sync_offset_ms=0, mic=FakeMic(30))
    assert sess.start_mic() and sess.mic_ok
    # drive ticks by media position: beat0..3 -> 100..850ms
    for ms in range(100, 2000, 50):
        sess.tick(ms)
    final = sess.finish()
    # first line sung on pitch; second line at pitch 32 sung at 30 (miss on hard?)
    assert final["score"] > 0
    assert final["tracks"]["P1"]["notes"] > 0


def test_sing_session_no_mic(tmp_path):
    """No mic -> session still runs, scores zero, doesn't crash."""
    from ultrapistar.sing import SingSession
    p = tmp_path / "song.txt"
    p.write_text("#ARTIST:T\n#TITLE:S\n#BPM:240\n#GAP:0\n: 0 4 30 la\nE\n")
    sess = SingSession(str(p))  # mic=None
    for ms in range(0, 1200, 50):
        out = sess.tick(ms)
    assert out["mic"] is False
    assert sess.finish()["score"] == 0


def test_streak_counts_consecutive_hit_notes():
    # three notes; middle one missed -> streak 1, max_streak from note 1
    chart = make_chart([Note("normal", 0, 4, 30, "a "),
                        Note("normal", 4, 4, 32, "b "),
                        Note("normal", 8, 4, 34, "c")])
    sc = Scorer(chart, line_bonus=False)
    tones = {b: 30 for b in range(4)}          # note 1 hit
    tones.update({b: 34 for b in range(8, 12)})  # note 3 hit; note 2 silent
    res = sing(chart, sc, tones)
    assert res["max_streak"] == 1


def test_streak_builds_across_notes():
    chart = make_chart([Note("normal", 0, 4, 30, "a "),
                        Note("normal", 4, 4, 32, "b "),
                        Note("normal", 8, 4, 34, "c")])
    sc = Scorer(chart, line_bonus=False)
    tones = {0: 30, 4: 32, 8: 34}   # one hit beat per note is enough
    res = sing(chart, sc, tones)
    assert res["max_streak"] == 3


def _melody_chart(tmp_path, pitches, bpm=240, gap=1000):
    p = tmp_path / "melody.txt"
    notes = "\n".join(f": {i} 1 {t} x" for i, t in enumerate(pitches))
    p.write_text(f"#ARTIST:T\n#TITLE:S\n#BPM:{bpm}\n#GAP:{gap}\n{notes}\nE\n")
    return p


def test_autosync_finds_chart_offset(tmp_path):
    """Singing 3s later than the chart's GAP expects -> solver returns
    ~+3000ms so the melody lands back on the right beats."""
    import random
    from ultrapistar.sing import SingSession
    rng = random.Random(7)
    pitches = [rng.randrange(24, 42) for _ in range(200)]
    p = _melody_chart(tmp_path, pitches)   # beat_ms = 250
    sess = SingSession(str(p))
    sess.mics["P1"] = object()             # pretend mic assigned
    base, beat_ms, shift = 1000.0, sess.beat_ms, 3000.0
    for beat in range(200):
        sess._tone_log["P1"].append(
            (base + beat * beat_ms + shift, pitches[beat]))
    res = sess.maybe_autosync()
    # beat quantization: offsets inside one beat window are equivalent
    assert res and abs(res["offset_ms"] - shift) <= beat_ms * 4
    # applying it must not let the next tick re-score old beats
    sess.apply_sync_offset(res["offset_ms"], 1000 + 80 * beat_ms + shift)
    assert sess.beat_at(1000 + 80 * beat_ms + shift) == sess._last_beat


def test_tick_publishes_expected_chart_notes(tmp_path):
    """Each tick maps mic device -> chart pitch at that beat, for the
    monitor's autotune correction chain."""
    from ultrapistar.sing import SingSession
    p = tmp_path / "song.txt"
    p.write_text("#ARTIST:T\n#TITLE:S\n#BPM:240\n#GAP:0\n"
                 ": 0 4 30 la\n- 4\n: 8 4 32 hey\nE\n")
    cap = FakeMic(30)
    cap.device = "hw:mic1"
    sess = SingSession(str(p), mics={"P1": cap})
    sess.tick(100)            # beat ~1 -> inside the pitch-30 note
    assert sess.targets == {"hw:mic1": 30}
    sess.tick(350)            # beats ~5 -> phrase break, no note
    assert sess.targets == {}
    sess.tick(600)            # beat ~9 -> the pitch-32 note
    assert sess.targets == {"hw:mic1": 32}


def test_autosync_finds_far_offset(tmp_path):
    """Charts authored for ~90s-intro media (USDB/community GAPs) need a
    far solve — the search window must reach beyond the old +-20s."""
    import random
    from ultrapistar.sing import SingSession
    rng = random.Random(13)
    pitches = [rng.randrange(24, 42) for _ in range(400)]
    p = _melody_chart(tmp_path, pitches)
    sess = SingSession(str(p))
    sess.mics["P1"] = object()
    base, beat_ms, shift = 1000.0, sess.beat_ms, 80000.0
    for beat in range(400):
        sess._tone_log["P1"].append(
            (base + beat * beat_ms + shift, pitches[beat]))
    res = sess.maybe_autosync()
    assert res and abs(res["offset_ms"] - shift) <= beat_ms * 4


def test_rolling_resync_follows_mid_song_gap_step(tmp_path):
    """After the first solve locks, a mid-song arrangement cut (the video
    loses a section) steps the true gap — rolling checks must follow it."""
    import random
    from ultrapistar.sing import SingSession
    rng = random.Random(17)
    pitches = [rng.randrange(24, 42) for _ in range(800)]
    p = _melody_chart(tmp_path, pitches)
    sess = SingSession(str(p))
    sess.mics["P1"] = object()
    base, beat_ms = 1000.0, sess.beat_ms
    for beat in range(200):           # verse 1 sings aligned
        sess._tone_log["P1"].append((base + beat * beat_ms, pitches[beat]))
    res = sess.maybe_autosync()
    assert res and res["delta_ms"] == 0          # settled, already aligned
    # the video 'cuts' a 5s section: notes keep coming but 5s late
    for beat in range(200, 800):
        sess._tone_log["P1"].append(
            (base + beat * beat_ms + 5000, pitches[beat]))
    res = sess.maybe_autosync()
    assert res and res["method"] == "rolling"
    assert abs(res["offset_ms"] - 5000) <= beat_ms * 4


def test_rolling_resync_ignores_small_drift(tmp_path):
    """Aligned singing must not jitter — rolling checks only fire on a
    confident >800ms step."""
    import random
    from ultrapistar.sing import SingSession
    rng = random.Random(19)
    pitches = [rng.randrange(24, 42) for _ in range(800)]
    p = _melody_chart(tmp_path, pitches)
    sess = SingSession(str(p))
    sess.mics["P1"] = object()
    base, beat_ms = 1000.0, sess.beat_ms
    for beat in range(200):
        sess._tone_log["P1"].append((base + beat * beat_ms, pitches[beat]))
    sess.maybe_autosync()
    for beat in range(200, 800):      # keep singing — still aligned
        sess._tone_log["P1"].append((base + beat * beat_ms, pitches[beat]))
    assert sess.maybe_autosync() is None


def test_autosync_no_change_when_aligned(tmp_path):
    import random
    from ultrapistar.sing import SingSession
    rng = random.Random(11)
    pitches = [rng.randrange(24, 42) for _ in range(200)]
    p = _melody_chart(tmp_path, pitches)
    sess = SingSession(str(p))
    sess.mics["P1"] = object()
    for beat in range(200):
        sess._tone_log["P1"].append(
            (1000.0 + beat * sess.beat_ms, pitches[beat]))
    res = sess.maybe_autosync()
    assert res and res["delta_ms"] == 0   # already on — don't touch


def test_onset_snap_corrects_stretched_gap(tmp_path):
    """The video's instrumental runs longer than charted; when the next
    phrase's melody re-enters, snap the offset to the actual onset."""
    import time as _t
    from ultrapistar.sing import SingSession
    p = tmp_path / "gaps.txt"
    p.write_text("#ARTIST:T\n#TITLE:S\n#BPM:240\n#GAP:0\n"
                 ": 0 4 30 a\n- 4\n: 8 4 32 b\n- 12\n: 16 10 34 c\nE\n")
    sess = SingSession(str(p))
    sess.mics["P1"] = object()
    sess.autosync_done = True
    beat_ms = sess.beat_ms
    sess._last_beat = 12                 # in the gap before beat-16 phrase
    late = 800.0                         # video's gap ran 800ms long
    for i in range(10):                  # phrase 3's pitch, sung late
        sess._tone_log["P1"].append(
            (16 * beat_ms + late + i * beat_ms, 34))
    sess._clock_pts.append(
        (_t.monotonic(), 16 * beat_ms + late + 9 * beat_ms + 50))
    res = sess.maybe_autosync()
    assert res and res["method"] == "onset"
    assert abs(res["offset_ms"] - late) <= beat_ms * 2


def test_onset_snap_quiet_when_aligned(tmp_path):
    """On-time singing produces a near-zero delta -> no snap, no jitter."""
    import time as _t
    from ultrapistar.sing import SingSession
    p = tmp_path / "gaps.txt"
    p.write_text("#ARTIST:T\n#TITLE:S\n#BPM:240\n#GAP:0\n"
                 ": 0 4 30 a\n- 4\n: 8 4 32 b\n- 12\n: 16 10 34 c\nE\n")
    sess = SingSession(str(p))
    sess.mics["P1"] = object()
    sess.autosync_done = True
    beat_ms = sess.beat_ms
    sess._last_beat = 12
    for i in range(10):                  # on-time phrase 3
        sess._tone_log["P1"].append((16 * beat_ms + i * beat_ms, 34))
    sess._clock_pts.append(
        (_t.monotonic(), 16 * beat_ms + 9 * beat_ms + 50))
    assert sess.maybe_autosync() is None


def test_onset_snap_rejects_wrong_melody(tmp_path):
    """Humming that isn't the next phrase's contour must not snap."""
    import time as _t
    from ultrapistar.sing import SingSession
    p = tmp_path / "gaps.txt"
    p.write_text("#ARTIST:T\n#TITLE:S\n#BPM:240\n#GAP:0\n"
                 ": 0 4 30 a\n- 4\n: 8 4 32 b\n- 12\n: 16 10 34 c\nE\n")
    sess = SingSession(str(p))
    sess.mics["P1"] = object()
    sess.autosync_done = True
    beat_ms = sess.beat_ms
    sess._last_beat = 12
    for i in range(10):                  # wrong pitch everywhere
        sess._tone_log["P1"].append(
            (16 * beat_ms + 800 + i * beat_ms, 40))
    sess._clock_pts.append(
        (_t.monotonic(), 16 * beat_ms + 800 + 9 * beat_ms + 50))
    assert sess.maybe_autosync() is None


def test_solve_prefers_nearest_on_melodic_alias(tmp_path):
    """When the melody repeats, the same tones score equally at several
    offsets. The solver must pick the smallest shift — otherwise an
    alias at a far offset steals the lock and the offset ratchets."""
    from ultrapistar.sing import SingSession
    p = tmp_path / "alias.txt"
    p.write_text("#ARTIST:T\n#TITLE:S\n#BPM:240\n#GAP:0\n"
                 ": 0 10 30 a\n- 10\n"          # phrase 1: beats 0-9
                 ": 200 10 30 b\n- 210\n"       # identical pitch run later
                 ": 400 10 34 c\nE\n")
    sess = SingSession(str(p))
    beat_ms = sess.beat_ms
    # singer sings phrase 2 exactly on chart time (true offset = 0) —
    # an alias exists at -200 beats that maps the tones onto phrase 1
    samples = [(200 * beat_ms + i * beat_ms, 30) for i in range(10)]
    d, s, base_score, c = sess._solve_offset("P1", samples)
    assert abs(d) <= beat_ms          # not -200*beat_ms (the alias)


def test_rolling_solve_cannot_jump_past_window(tmp_path):
    """Rolling re-solves stay inside ±ROLL_WINDOW_MS — a repeated-melody
    alias 100s away must not steal the lock mid-song."""
    from ultrapistar.sing import SingSession
    p = tmp_path / "alias.txt"
    p.write_text("#ARTIST:T\n#TITLE:S\n#BPM:240\n#GAP:0\n"
                 ": 0 10 30 a\n- 10\n"
                 ": 200 10 30 b\n- 210\n"
                 ": 400 10 34 c\nE\n")
    sess = SingSession(str(p))
    beat_ms = sess.beat_ms
    # samples that only make sense 100s out — unreachable for rolling
    samples = [(i * beat_ms, 30) for i in range(10)]
    sess.base_ms = 100000.0   # pretend current lock is far right of them
    d, s, base_score, c = sess._solve_offset(
        "P1", samples, window_ms=sess.ROLL_WINDOW_MS)
    # the true -100s shift is out of reach, and nothing inside the window
    # scores — so the solve must stand still rather than jump to the alias
    assert d == 0


def test_warp_segments_retime_beat_at(tmp_path):
    """A learned boundary shifts beats from that phrase onward only —
    before it, the global offset still applies."""
    from ultrapistar.sing import SingSession
    p = tmp_path / "gaps.txt"
    p.write_text("#ARTIST:T\n#TITLE:S\n#BPM:240\n#GAP:0\n"
                 ": 0 4 30 a\n- 4\n: 8 4 32 b\n- 12\n: 16 10 34 c\nE\n")
    sess = SingSession(str(p), warp=[[16, 800]])
    beat_ms = sess.beat_ms
    # before the warped phrase's warped start: global base applies
    assert sess.beat_at(8 * beat_ms) == 8
    # the warped start of phrase 3 (16*beat_ms + 800) maps back to beat 16
    assert sess.beat_at(16 * beat_ms + 800) == 16
    # beats before the boundary are untouched
    assert sess.beat_at(4 * beat_ms) == 4


def test_warp_boundary_picks_next_phrase(tmp_path):
    from ultrapistar.sing import SingSession
    p = tmp_path / "gaps.txt"
    p.write_text("#ARTIST:T\n#TITLE:S\n#BPM:240\n#GAP:0\n"
                 ": 0 4 30 a\n- 4\n: 8 4 32 b\n- 12\n: 16 10 34 c\nE\n")
    sess = SingSession(str(p))
    beat_ms = sess.beat_ms
    # position inside phrase 2 (beat ~9) -> boundary is phrase 3's beat 16
    assert sess.warp_boundary(9 * beat_ms) == 16
    # in the gap before phrase 3 the boundary is still 16
    sess._last_beat = 12
    assert sess.warp_boundary(13 * beat_ms) == 16


def test_learn_warp_replaces_same_boundary(tmp_path):
    from ultrapistar.sing import SingSession
    p = tmp_path / "gaps.txt"
    p.write_text("#ARTIST:T\n#TITLE:S\n#BPM:240\n#GAP:0\n"
                 ": 0 4 30 a\n- 4\n: 8 4 32 b\n- 12\n: 16 10 34 c\nE\n")
    sess = SingSession(str(p), warp=[[16, 800]])
    sess.learn_warp(16, 1200)          # re-learned at the same boundary
    sess.learn_warp(30, 1500)
    assert sess._warp == [(16, 1200.0), (30, 1500.0)]


def test_onset_snap_reports_warp_beat(tmp_path):
    """The onset solve names the phrase boundary it anchored to, so the
    caller can persist it as a warp entry."""
    import time as _t
    from ultrapistar.sing import SingSession
    p = tmp_path / "gaps.txt"
    p.write_text("#ARTIST:T\n#TITLE:S\n#BPM:240\n#GAP:0\n"
                 ": 0 4 30 a\n- 4\n: 8 4 32 b\n- 12\n: 16 10 34 c\nE\n")
    sess = SingSession(str(p))
    sess.mics["P1"] = object()
    sess.autosync_done = True
    beat_ms = sess.beat_ms
    sess._last_beat = 12
    for i in range(10):
        sess._tone_log["P1"].append(
            (16 * beat_ms + 800 + i * beat_ms, 34))
    sess._clock_pts.append(
        (_t.monotonic(), 16 * beat_ms + 800 + 9 * beat_ms + 50))
    res = sess.maybe_autosync()
    assert res and res["warp_beat"] == 16


def test_word_align_solves_base():
    """Transcript words shifted +4s vs the chart must recover ~4000ms
    as the media time of chart beat 0."""
    from ultrapistar.sing import word_align
    beat_ms = 250.0
    cw = [("summer", 0), ("has", 12), ("come", 24), ("and", 30),
          ("passed", 36), ("the", 60), ("innocent", 72)]
    base_true = 4000.0
    words = [(t, base_true + b * beat_ms, 0.9) for t, b in cw[:5]]
    base, support, cov = word_align(words, cw, beat_ms)
    assert base is not None and abs(base - base_true) <= 100
    assert support >= 5 and cov >= 0.9


def test_word_align_rejects_garbage():
    from ultrapistar.sing import word_align
    base, support, cov = word_align(
        [("zzz", 1000.0, 0.9), ("qqq", 2000.0, 0.9)],
        [("summer", 0), ("has", 12)], 250.0)
    assert base is None


def test_autosync_voice_fallback_result(tmp_path):
    """After a failed pitch solve the session applies the speech-model
    result when the worker drops it in."""
    from ultrapistar.sing import SingSession
    p = _melody_chart(tmp_path, [30] * 200)
    sess = SingSession(str(p))
    sess._pitch_attempted = True          # pitch already failed
    sess._voice_result = {"delta_ms": 2500, "method": "whistle",
                          "offset_ms": 2500}
    res = sess.maybe_autosync()
    assert res["method"] == "whistle" and res["offset_ms"] == 2500
    assert sess.autosync_done


def test_autosync_pitch_fail_starts_voice_stage(tmp_path):
    """A garbage pitch match must not conclude autosync — it hands off
    to the voice worker instead of returning delta 0."""
    from ultrapistar.sing import SingSession
    p = _melody_chart(tmp_path, [30] * 200)
    sess = SingSession(str(p))
    sess.mics["P1"] = object()
    for i in range(80):                    # span > AUTOSYNC_AFTER_MS
        sess._tone_log["P1"].append((i * 150.0, (i * 7) % 45))
    res = sess.maybe_autosync()
    assert res is None                     # still working
    assert sess._voice_state == "running"  # worker kicked off


def test_request_resync_resets_and_collects(tmp_path):
    """Manual resync: clears solvers + collected audio, then the 10s
    window forces a solve even with no sung tones (voice path)."""
    import time as _t
    from ultrapistar.sing import SingSession
    p = _melody_chart(tmp_path, [30] * 200)
    sess = SingSession(str(p))
    sess.mics["P1"] = object()
    sess._tone_log["P1"].append((0.0, 30))
    sess.autosync_done = True
    sess.request_resync()
    assert not sess.autosync_done
    assert sess._pitch_attempted is False
    assert sess._tone_log["P1"] == []
    # window still open -> collecting, no solve yet
    assert sess.maybe_autosync() is None
    sess._resync_until = _t.monotonic() - 1   # pretend 10s elapsed
    res = sess.maybe_autosync()
    assert res is None                       # handed to the voice stage
    assert sess._voice_state == "running"


def test_request_resync_pitch_solves_immediately(tmp_path):
    """With enough fresh tones inside the window, the manual path solves
    by pitch right at the deadline — no voice stage needed."""
    import random
    import time as _t
    from ultrapistar.sing import SingSession
    rng = random.Random(5)
    pitches = [rng.randrange(24, 42) for _ in range(60)]
    p = _melody_chart(tmp_path, pitches)
    sess = SingSession(str(p))
    sess.mics["P1"] = object()
    sess.request_resync()
    sess._resync_until = _t.monotonic() - 1
    base, beat_ms, shift = 1000.0, sess.beat_ms, 2500.0
    for beat in range(60):                   # >15 samples: pitch can run
        sess._tone_log["P1"].append(
            (base + beat * beat_ms + shift, pitches[beat]))
    res = sess.maybe_autosync()
    assert res and abs(res["offset_ms"] - shift) <= beat_ms * 4
    assert sess.autosync_done
