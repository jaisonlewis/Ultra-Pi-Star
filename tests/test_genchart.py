"""genchart + melody — generated-chart pipeline with synthetic inputs."""
import numpy as np
import pytest

from ultrapistar import genchart, melody
from ultrapistar.charts import load_chart


def _curve(sine_hz=220.0, dur_s=10.0):
    """A constant-pitch confident RMVPE-like curve."""
    t = np.arange(0, dur_s, 0.01)
    return t, np.full_like(t, sine_hz), np.full_like(t, 0.9)


def _asr(words_times):
    return [(w, t) for w, t in words_times]


LYRIC = {"synced": [(1000, "Summer has come and passed"),
                    (4000, "the innocent can never last"),
                    (7000, "wake me up when september ends"),
                    (10000, "like my fathers come to pass")],
         "plain": ["Summer has come and passed",
                   "the innocent can never last",
                   "wake me up when september ends",
                   "like my fathers come to pass"]}


def _matching_asr(shift=200.0):
    """ASR words matching LYRIC lines, media-shifted by `shift`."""
    out, t = [], 0.0
    for lms, text in LYRIC["synced"]:
        t = lms + shift
        for w in text.split():
            out.append((w.lower(), t))
            t += 450
    return out


def test_word_notes_pitched_and_unvoiced():
    times, f0, conf = _curve(220.0)
    spans = [("held", 1000, 1600), ("gap", 5000, 5100)]
    out = melody.word_notes(times, f0, conf, spans)
    assert out[0]["tone"] is not None
    # 220 Hz ≈ tone_abs ~21 (A3 in C2-based units)
    assert abs(out[0]["tone"] - 21) <= 1
    # "gap" lands past the 10 s curve? inside — make a truly empty window
    out2 = melody.word_notes(times, f0 * 0, conf * 0, spans)
    assert out2[0]["tone"] is None


def test_estimate_key():
    # C-major-ish melody histogram
    times = np.arange(0, 30, 0.01)
    pcs = np.array([0, 2, 4, 5, 7, 9, 11]) * 1.0
    f0 = 65.4064 * 2 ** ((24 + np.tile(
        np.random.choice(pcs, len(times)), 1)) / 12)
    conf = np.full_like(times, 0.9)
    key = melody.estimate_key(times, f0, conf)
    assert key is not None
    assert key.endswith(("major", "minor"))


def test_generate_basic_chart():
    asr = _matching_asr()
    curve = _curve(220.0, dur_s=14)
    chart = genchart.generate("A", "B", LYRIC, asr, curve)
    assert chart is not None
    assert chart.bpm == genchart.GEN_BPM
    notes = chart.tracks["P1"]
    assert len(notes) >= 15
    # notes are ms-gridded: first note starts ~1200 (line1 + shift)
    assert 900 <= notes[0].start <= 1600
    assert all(n.word_break for n in notes)
    # line breaks recorded (all but last line)
    assert len(chart.breaks["P1"]) == 3


def test_generate_no_asr_returns_none():
    assert genchart.generate("A", "B", LYRIC, [], _curve()) is None


def test_generate_asr_only_lines():
    """No online lyrics → phrase lines from ASR pause gaps."""
    # realistic stream: ~400ms between words, ~2s breaths between lines
    asr, t = [], 1000.0
    for line in ("summer has come and passed",
                 "the innocent can never last",
                 "wake me up when september ends",
                 "like my fathers come to pass"):
        for w in line.split():
            asr.append((w, t))
            t += 400
        t += 2000
    rec = {"synced": [], "plain": []}
    chart = genchart.generate("A", "B", rec, asr, _curve(220.0, dur_s=14))
    assert chart is not None
    assert len(chart.breaks["P1"]) == 3        # 4 lines → 3 breaks
    # first line's text came from ASR itself
    first = " ".join(n.text for n in
                     [x for x in chart.tracks["P1"]
                      if x.start < chart.breaks["P1"][0]])
    assert "summer" in first.lower()


def test_generate_too_few_asr_words_returns_none():
    rec = {"synced": [], "plain": []}
    sparse = [("hi", 1000.0), ("there", 3000.0)]
    assert genchart.generate("A", "B", rec, sparse, _curve()) is None


def test_generate_low_conf_is_freestyle():
    asr = _matching_asr()
    t, f0, _ = _curve(220.0, dur_s=14)
    weak = (t, f0, np.full_like(t, 0.1))       # confidence below thr
    chart = genchart.generate("A", "B", LYRIC, asr, weak)
    assert chart is not None
    kinds = {n.kind for n in chart.tracks["P1"]}
    assert "freestyle" in kinds


def test_save_for_entry_roundtrip(tmp_path):
    asr = _matching_asr()
    chart = genchart.generate("Artist", "Title", LYRIC, asr,
                              _curve(220.0, dur_s=14))
    path = genchart.save_for_entry(chart, tmp_path, 42, "Artist", "Title")
    loaded = load_chart(path)
    assert not loaded.validate()
    assert len(loaded.tracks["P1"]) == len(chart.tracks["P1"])
    assert loaded.source.get("type") == "generated"


def test_hybrid_sparse_match_fills_from_asr():
    """A lyric record matching only some lines must not leave the rest
    textless — heard ASR words fill the unmatched regions in time
    order."""
    # record has 4 lines; the media only sings line 1 at ~1s, then a
    # later verse the record doesn't cover (~10s)
    rec = {"synced": [],
           "plain": ["summer has come and passed",
                     "some unrelated lyric",
                     "another unrelated line",
                     "and a fourth one too"]}
    asr, t = [], 1000.0
    for w in "summer has come and passed".split():   # matches line 1
        asr.append((w, t)); t += 400
    t += 2500
    for w in "unheard words the singer sang out loud".split():
        asr.append((w, t)); t += 400
    t += 2500
    for w in "more words that were never in the record".split():
        asr.append((w, t)); t += 400
    chart = genchart.generate("A", "B", rec, asr, _curve(220.0, dur_s=20))
    assert chart is not None
    notes = chart.tracks["P1"]
    texts = [n.text for n in notes]
    # the matched line's real text AND the ASR filler both appear
    assert "summer" in texts
    assert "unheard" in texts
    # notes stay in media-time order despite mixed line sources
    starts = [n.start for n in notes]
    assert starts == sorted(starts)


def test_shift_applies_media_offset():
    """LRC at recording-time + ASR at media-time → media-timed notes."""
    asr = _matching_asr(shift=2000.0)
    chart = genchart.generate("A", "B", LYRIC, asr,
                              _curve(220.0, dur_s=14))
    n0 = chart.tracks["P1"][0]
    assert n0.start > 2500          # 1000 LRC + ~2000 shift


def test_cjk_lyric_line_keeps_words():
    """norm_word drops Japanese tokens entirely — the span builder must
    still emit them or a CJK record dies at the <15-words gate."""
    span = {"onset_ms": 10000, "end_ms": 16000, "words": {}}
    ws = genchart._line_word_spans("嗚呼 革命道中の向こう側", span, [])
    assert [w for w, _, _ in ws] == ["嗚呼", "革命道中の向こう側"]
    assert all(10000 <= s < e <= 16000 for _, s, e in ws)


def test_mixed_line_word_positions_align():
    """span['words'] positions index the RAW split — a leading CJK token
    must not shift the latin word's ASR anchor one position off."""
    asr = [("mm", 5000.0), ("hello", 5200.0), ("world", 5600.0)]
    span = {"onset_ms": 5000, "end_ms": 6000, "words": {1: 1, 2: 2}}
    ws = genchart._line_word_spans("嗚呼 hello world", span, asr)
    assert ws[1] == ("hello", 5200, 5600)
    assert ws[2] == ("world", 5600, 6000)
    assert ws[0][0] == "嗚呼"


def test_map_line_words_anchors_stream():
    """Whole-stream alignment anchors every line at real word times."""
    from ultrapistar import songmap
    asr = _matching_asr()           # words at lms + 200
    texts = [t for _, t in LYRIC["synced"]]
    spans = songmap.map_line_words(asr, texts)
    assert set(spans) == {0, 1, 2, 3}
    assert abs(spans[0]["onset_ms"] - 1200) < 10
    assert spans[0]["words"][0] == 0       # first lyric word → asr[0]


def test_record_text_beats_asr_mishearing():
    """ASR 'septemper' where the record says 'september' → the note
    shows the record's word at the heard time."""
    asr = _matching_asr()
    # mangle the ASR's copy of 'september' (index of the word in line 2)
    asr = [(("septemper" if w == "september" else w), t)
           for w, t in asr]
    chart = genchart.generate("A", "B", LYRIC, asr,
                              _curve(220.0, dur_s=14))
    texts = [n.text for n in chart.tracks["P1"]]
    assert "september" in texts
    assert "septemper" not in texts


def test_cut_verse_stays_unmapped():
    """A big LRC stretch the media never sings (cut verse) must not get
    fabricated interpolated lines."""
    rec = {"synced": [(1000, "the first line of this song is sung"),
                      (6000, "the second line of it is sung too"),
                      (40000, "this whole verse was cut out"),
                      (44000, "the last line of this song is sung")],
           "plain": []}
    asr, t = [], 1200.0
    for w in "the first line of this song is sung".split():
        asr.append((w, t)); t += 400
    t += 2000
    for w in "the second line of it is sung too".split():
        asr.append((w, t)); t += 400
    t += 2000                      # ~2s gap where 40s of LRC verse was
    for w in "the last line of this song is sung".split():
        asr.append((w, t)); t += 400
    meta = {}
    chart = genchart.generate("A", "B", rec, asr,
                              _curve(220.0, dur_s=20), meta_out=meta)
    texts = [n.text for n in chart.tracks["P1"]]
    assert "verse" not in texts    # cut line not fabricated
    assert "last" in texts


def test_repeated_chorus_prefers_covering_shift():
    """Sweet Dreams case: a heavily repeated chorus makes phantom
    shift candidates whose lines also land on heard words. The true
    offset covers the media's sung regions; the phantom maps them
    onto a later repeat and leaves the real chorus bare."""
    text = ["yeah",
            "sweet dreams come after hours",
            "you should never sleep alone",
            "love you like love you like that",
            "sweet dreams come after hours",
            "you should never sleep alone",
            "love you like love you like that"]
    lms = [4000, 10000, 14000, 18000, 70000, 74000, 78000]
    rec = {"synced": list(zip(lms, text)), "plain": []}
    asr = []              # MV intro: media sings at lrc + 15 s
    for l, t in zip(lms, text):
        for i, w in enumerate(t.split()):
            asr.append((w, l + 15000 + i * 400))
    chart = genchart.generate("A", "B", rec, asr, _curve(220.0, dur_s=110))
    texts = [n.text for n in chart.tracks["P1"]]
    assert "yeah" in texts
    n0 = chart.tracks["P1"][0]
    # true shift is ~+15 s — a phantom (~+60 s onto the second chorus)
    # would put the first line around 64 s
    assert n0.start < 25000


def test_asr_silent_stretch_keeps_interior_lines():
    """ASR hearing nothing for 20 s is not a cut — the sung region is
    confirmed on both sides, so interior lines keep their LRC timing."""
    rec = {"synced": [(1000, "first verse line is really sung out loud"),
                      (5000, "second verse line is also sung out loud"),
                      (20000, "bridge words needle never heard at all"),
                      (24000, "more unheard bridge words in this part"),
                      (30000, "final chorus line comes back to us now")],
           "plain": []}
    asr, t = [], 1200.0
    for w in "first verse line is really sung out loud".split():
        asr.append((w, t)); t += 400
    t += 1500
    for w in "second verse line is also sung out loud".split():
        asr.append((w, t)); t += 400
    t = 31200.0                        # ASR heard nothing 10–30 s
    for w in "final chorus line comes back to us now".split():
        asr.append((w, t)); t += 400
    chart = genchart.generate("A", "B", rec, asr, _curve(220.0, dur_s=40))
    texts = [n.text for n in chart.tracks["P1"]]
    assert "bridge" in texts           # interior lines kept
    assert "needle" in texts
    starts = [n.start for n in chart.tracks["P1"]]
    assert starts == sorted(starts)


def test_merged_words_keep_spacing():
    """Sub-MIN_WORD_MS words merge forward — with a space, not glued."""
    spans = genchart._merge_short([("i", 1000, 1050),
                                   ("want", 1050, 1400),
                                   ("ride", 1400, 1900)])
    assert spans[0][0] == "i want"


def _runs_curve(runs_s, dur_s, hz=220.0):
    """RMVPE-like curve voiced only inside (start,end) second windows."""
    t = np.arange(0, dur_s, 0.01)
    f0 = np.zeros_like(t); conf = np.zeros_like(t)
    for a, b in runs_s:
        m = (t >= a) & (t <= b)
        f0[m] = hz; conf[m] = 0.9
    return t, f0, conf


def test_held_tail_extends_to_voiced_run_end():
    """A final syllable held past the LRC-implied end keeps sounding —
    the line ends near the pitch run's end, not last-word + 400ms."""
    rec = {"synced": [(10000, "hold the note right there"),
                      (18000, "second line comes after it"),
                      (26000, "third line finishes the song")],
           "plain": []}
    asr = [("hold", 10500), ("the", 10900), ("note", 11300),
           ("right", 11700), ("there", 12100),
           ("second", 18300), ("line", 18700), ("comes", 19100),
           ("after", 19500), ("it", 19900),
           ("third", 26300), ("line", 26700), ("finishes", 27100),
           ("the", 27500), ("song", 27900)]
    asr += [(f"pad{i}", 30000 + i * 400) for i in range(6)]
    curve = _runs_curve([(10.2, 16.8), (18.2, 20.5), (26.2, 28.6),
                         (29.8, 32.5)], dur_s=34)
    chart = genchart.generate("A", "B", rec, asr, curve)
    assert chart is not None
    notes = chart.tracks["P1"]; bm = 15000 / chart.bpm
    line1 = [n for n in notes if n.start * bm < 17500]
    end1 = (line1[-1].start + line1[-1].length) * bm
    # before the fix the last word ended ~12100+400; the run says the
    # voice holds to 16800 — the line must not die mid-syllable
    assert end1 > 15000


def test_onset_snaps_back_to_run_start():
    """Whisper hears only the tail of a phrase, but the pitch run shows
    singing began earlier — the line onset covers the run start and
    leading unheard words spread across it."""
    rec = {"synced": [(10000, "i bleach the sky every night"),
                      (20000, "loaded on wrong and further out"),
                      (28000, "closing line brings it all home")],
           "plain": []}
    asr = [("sky", 12500), ("every", 12900), ("night", 13300),
           ("loaded", 20500), ("on", 20900), ("wrong", 21300),
           ("and", 21700), ("further", 22100), ("out", 22500),
           ("closing", 28500), ("line", 28900), ("brings", 29300),
           ("it", 29700), ("all", 30100), ("home", 30500)]
    asr += [(f"pad{i}", 32000 + i * 400) for i in range(6)]
    curve = _runs_curve([(11.0, 14.5), (20.2, 23.0), (28.2, 31.0),
                         (32.0, 34.0)], dur_s=35)
    chart = genchart.generate("A", "B", rec, asr, curve)
    assert chart is not None
    bm = 15000 / chart.bpm
    first = chart.tracks["P1"][0]
    assert first.start * bm <= 11500          # at the 11000 run start


def test_romanize_fills_note_alt():
    """Native-script notes get romanized alternates; latin notes stay bare."""
    pytest.importorskip("pykakasi")
    from ultrapistar import romanize
    from ultrapistar.charts import Chart, Note, to_native, from_native
    c = Chart()
    c.tracks = {"P1": [Note("normal", 0, 4, 10, "上を向いて", True),
                       Note("normal", 4, 4, 10, "hello", True)]}
    assert romanize.romanize_chart(c)
    n0 = c.tracks["P1"][0]
    assert n0.alt and "ue" in n0.alt
    assert c.tracks["P1"][1].alt == ""
    # round-trips through the native format
    c2 = from_native(to_native(c))
    assert c2.tracks["P1"][0].alt == n0.alt
    ph = c2.phrases()["P1"][0]
    assert "atext" in ph and "at" in ph["syls"][0]


def test_romanize_korean():
    pytest.importorskip("hangul_romanize")
    from ultrapistar import romanize
    out = romanize.romanize_text("나의 우주", hint="kr")
    assert out and "na" in out
    # mixed latin + hangul keeps both
    out = romanize.romanize_text("me, 끝나지 않을 history")
    assert "me," in out and "history" in out


def test_romanize_latin_unchanged():
    from ultrapistar import romanize
    assert romanize.romanize_text("summer has come") == ""
