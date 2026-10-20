"""ASR-anchored song maps (ultrapistar.songmap) + LRCLIB lyrics parsing."""
import pytest

from ultrapistar import songmap
from ultrapistar.charts import parse_chart
from ultrapistar.lyrics import norm_word, parse_lrc




def test_parse_lrc_basic():
    lrc = ("[ti:Song]\n[ar:Artist]\n[00:10.50]First line here\n"
           "[00:20.00]Second line\n[00:25.250]Third  line!\n")
    out = parse_lrc(lrc)
    assert out == [(10500, "First line here"),
                   (20000, "Second line"),
                   (25250, "Third  line!")]


def test_parse_lrc_skips_blanks_and_unstamped():
    lrc = ("[00:01.00]one\n[00:02.00]\nno timestamp line\n"
           "[00:03.00]   \n[00:04.00]two\n")
    out = parse_lrc(lrc)
    assert out == [(1000, "one"), (4000, "two")]


def test_norm_word():
    assert norm_word("Don't!") == "don't"
    assert norm_word("  HELLO... ") == "hello"
    assert norm_word("♪") == ""


def test_align_lines_sequential():
    """ASR stream maps lyric lines in order; a skipped line doesn't
    derail the following ones."""
    asr = [("the", 1000), ("innocent", 1200), ("can", 1400),
           ("never", 1600), ("last", 1800),
           # 'extra words between lines' the lyrics don't have
           ("yeah", 2500),
           ("wake", 5000), ("me", 5200), ("up", 5400),
           ("when", 5600), ("september", 5800), ("ends", 6000)]
    lines = ["the innocent can never last",
             "here comes the rain again",          # absent from media
             "wake me up when september ends"]
    spans = songmap.align_lines(asr, lines)
    assert set(spans) == {0, 2}
    assert spans[0]["onset_ms"] == 1000
    assert spans[2]["onset_ms"] == 5000


def test_align_lines_repeated_lines_map_in_order():
    """The same lyric twice must anchor to the first then the second
    occurrence — the sequential cursor prevents both landing on one."""
    words = ["summer", "has", "come", "and", "passed"]
    asr = [(w, 10000 + i * 400) for i, w in enumerate(words)]
    asr += [(w, 60000 + i * 400) for i, w in enumerate(words)]
    lines = ["summer has come and passed", "summer has come and passed"]
    spans = songmap.align_lines(asr, lines)
    assert spans[0]["onset_ms"] == 10000
    assert spans[1]["onset_ms"] == 60000


WB_CHART = ("#ARTIST:T\n#TITLE:S\n#BPM:240\n#GAP:1000\n"
            ": 0 4 30 Summer \n: 4 4 32 time \n"
            ": 8 4 34 has \n: 12 4 35 come \n: 16 4 33 on\n- 24\n"
            ": 48 4 32 Wake \n: 52 4 36 me \n: 56 4 34 up\n- 64\nE\n")

# 8 lines at the same beats as CHART, but with real word boundaries —
# like actual USDB files whose syllable text carries trailing spaces
CHART8 = ("#ARTIST:T\n#TITLE:S\n#BPM:240\n#GAP:1000\n"
          ": 0 4 30 Summer \n: 4 4 32 time \n: 8 4 34 has \n"
          ": 12 4 35 come \n: 16 4 33 on\n- 24\n"
          ": 48 4 32 Rain \n: 52 4 36 keeps \n: 56 4 34 fall \n"
          ": 60 4 31 ing \n: 64 4 33 down \n: 68 4 35 now\n- 72\n"
          ": 100 4 36 Take \n: 104 4 34 a \n: 108 4 30 lit \n"
          ": 112 4 33 tle \n: 116 4 35 piece \n: 120 4 32 of\n- 124\n"
          ": 140 4 30 My \n: 144 4 34 bro \n: 148 4 36 ken \n"
          ": 152 4 33 heart \n: 156 4 31 to \n: 160 4 35 night\n- 164\n"
          ": 200 4 38 Sev \n: 204 4 36 en \n: 208 4 34 days \n"
          ": 212 4 32 with \n: 216 4 35 out \n: 220 4 31 you\n- 224\n"
          ": 240 4 36 An \n: 244 4 32 o \n: 248 4 30 ther \n"
          ": 252 4 34 end \n: 256 4 33 less \n: 260 4 35 night\n- 264\n"
          ": 300 4 34 Sev \n: 304 4 32 en \n: 308 4 36 dead \n"
          ": 312 4 30 ly \n: 316 4 33 sins \n: 320 4 35 up\n- 324\n"
          ": 340 4 32 Wake \n: 344 4 30 me \n: 348 4 34 up \n"
          ": 352 4 36 when \n: 356 4 33 Sep \n: 360 4 35 tember \n"
          ": 364 4 35 ends\nE\n")


def test_chart_lyric_lines(tmp_path):
    """Syllables rejoin into words at word_breaks (trailing-space
    convention); a second line's words stay separate."""
    p = tmp_path / "s.txt"
    p.write_text(WB_CHART)
    chart = parse_chart(p)
    lines = songmap.chart_lyric_lines(chart, "P1")
    assert lines == ["summer time has come on", "wake me up"]


def _asr_for_chart(chart, true_off_ms, cut_ms=0, cut_before_beat=10**9):
    """Synthesize an ASR word stream: each chart line's words at their
    media positions (offset + optional late-section cut)."""
    beat_ms = 15000.0 / chart.bpm
    asr = []
    for line in chart.lines("P1"):
        if not line:
            continue
        words = songmap._chart_line_words(line)
        if not words:
            continue
        s = min(n.start for n in line)
        e = max(n.start + n.length for n in line)
        onset = chart.gap_ms + s * beat_ms + true_off_ms
        if s >= cut_before_beat:
            onset -= cut_ms
        span = max(400, (e - s) * beat_ms)
        for i, w in enumerate(words):
            asr.append((w, onset + i * span / len(words)))
    return sorted(asr, key=lambda x: x[1])


def test_build_warp_recovers_offset(tmp_path, monkeypatch):
    p = tmp_path / "s.txt"
    p.write_text(CHART8)
    chart = parse_chart(p)
    monkeypatch.setattr(songmap, "transcribe_media",
                        lambda _m: _asr_for_chart(chart, 8000))
    res = songmap.build_warp(chart, "dummy.mp4",
                             songmap.chart_lyric_lines(chart, "P1"))
    assert res is not None
    assert abs(res["offset_ms"] - 8000) < 400
    assert abs(res["tempo"] - 1.0) < 0.05
    assert res["matched"] == 8


def test_build_warp_finds_cut_interlude(tmp_path, monkeypatch):
    """Media skipping 4s before the last lines → a warp mark at the
    boundary with a ~4s-lower offset, like the chroma path produces."""
    p = tmp_path / "s.txt"
    p.write_text(CHART8)
    chart = parse_chart(p)
    monkeypatch.setattr(songmap, "transcribe_media",
                        lambda _m: _asr_for_chart(chart, 8000, cut_ms=4000,
                                                  cut_before_beat=200))
    res = songmap.build_warp(chart, "dummy.mp4", None)  # chart-line text
    assert res is not None
    assert abs(res["offset_ms"] - 8000) < 400
    step = [o for b, o in res["warp"] if b >= 200]
    assert step and abs(step[0] - 4000) < 400


def test_build_warp_lrc_path_needs_few_asr_anchors(tmp_path, monkeypatch):
    """Synced LRC times describe every line; only a handful of ASR
    anchors are needed to measure the video's constant shift. Sparse
    ASR that would fail the pure-ASR path still produces a full map."""
    p = tmp_path / "s.txt"
    p.write_text(CHART8)
    chart = parse_chart(p)
    beat_ms = 15000.0 / chart.bpm
    lines = songmap.chart_lyric_lines(chart, "P1")
    shift = 4200.0                       # video runs 4.2s late vs recording
    # LRC times at the chart's own line positions (recording-relative)
    synced = []
    for line in chart.lines("P1"):
        if line:
            synced.append((int(chart.gap_ms
                               + min(n.start for n in line) * beat_ms),
                           " ".join(songmap._chart_line_words(line))))
    # ASR: only every other line, matching at shifted media positions
    asr = []
    for li, line in enumerate(chart.lines("P1")):
        if not line or li % 2:
            continue
        words = songmap._chart_line_words(line)
        onset = synced[li][0] + shift
        span = max(400, (max(n.start + n.length for n in line)
                         - min(n.start for n in line)) * beat_ms)
        asr += [(w, onset + i * span / len(words))
                for i, w in enumerate(words)]
    monkeypatch.setattr(songmap, "transcribe_media", lambda _m: asr)
    res = songmap.build_warp(chart, "dummy.mp4", lines, synced=synced)
    assert res is not None
    assert res["matched"] == 8            # all lines mapped via LRC
    assert abs(res["offset_ms"] - shift) < 400


def test_build_warp_lrc_rejects_inconsistent_deltas(tmp_path, monkeypatch):
    """ASR deltas scattered across tens of seconds mean the video
    doesn't follow the recording — the LRC shortcut must abstain."""
    p = tmp_path / "s.txt"
    p.write_text(CHART8)
    chart = parse_chart(p)
    beat_ms = 15000.0 / chart.bpm
    lines = songmap.chart_lyric_lines(chart, "P1")
    synced = []
    for line in chart.lines("P1"):
        if line:
            synced.append((int(chart.gap_ms
                               + min(n.start for n in line) * beat_ms),
                           " ".join(songmap._chart_line_words(line))))
    # deltas alternate 0s / +20s — no coherent shift exists
    asr = []
    for li, line in enumerate(chart.lines("P1")):
        if not line:
            continue
        words = songmap._chart_line_words(line)
        onset = synced[li][0] + (20000 if li % 2 else 0)
        span = 800.0
        asr += [(w, onset + i * span / len(words))
                for i, w in enumerate(words)]
    monkeypatch.setattr(songmap, "transcribe_media", lambda _m: asr)
    res = songmap.build_warp(chart, "dummy.mp4", lines, synced=synced)
    # the LRC shortcut must abstain; if the pure-ASR path still maps it,
    # its per-line marks must carry BOTH offset levels (0s and ~+20s) —
    # never a bogus single constant shift
    if res is not None:
        offs = [o for _, o in res["warp"]]
        assert min(offs) < 2000 and max(offs) > 15000


def test_build_warp_none_without_asr(tmp_path, monkeypatch):
    p = tmp_path / "s.txt"
    p.write_text(CHART8)
    chart = parse_chart(p)
    monkeypatch.setattr(songmap, "transcribe_media", lambda _m: [])
    assert songmap.build_warp(chart, "dummy.mp4", None) is None


def test_build_warp_none_on_garbage_asr(tmp_path, monkeypatch):
    """ASR that doesn't match the lyric text must not produce a map."""
    p = tmp_path / "s.txt"
    p.write_text(CHART8)
    chart = parse_chart(p)
    monkeypatch.setattr(
        songmap, "transcribe_media",
        lambda _m: [(f"zz{i}", i * 500.0) for i in range(80)])
    assert songmap.build_warp(chart, "dummy.mp4", None) is None
