"""Offline chart→media chroma alignment (ultrapistar.align)."""
import math
import wave

import numpy as np
import pytest

from ultrapistar.align import align_chart
from ultrapistar.charts import parse_chart

RATE = 22050


def _freq(pc: int) -> float:
    """Frequency whose pitch class matches the chart note's pitch%12."""
    return 440.0 * 2 ** (((pc % 12) - 9) / 12.0)


def _synth(chart_path, true_off_ms: float, cut_ms: float = 0,
           cut_before_beat: int = 200, tempo: float = 1.0):
    """Render the chart's notes as sine bursts into a wav; notes at or
    after cut_before_beat are shifted earlier by cut_ms (the video 'cut'
    an instrumental)."""
    chart = parse_chart(chart_path)
    beat_ms = 15000.0 / chart.bpm * tempo
    dur_ms = (chart.gap_ms
              + max(n.start + n.length
                    for n in chart.tracks["P1"]) * beat_ms
              + abs(true_off_ms) + abs(cut_ms) + 2000)
    audio = np.zeros(int(dur_ms * RATE / 1000) + RATE, dtype=np.float32)
    t_all = np.arange(len(audio)) / RATE
    for n in chart.tracks["P1"]:
        start = chart.gap_ms + n.start * beat_ms + true_off_ms
        if n.start >= cut_before_beat and cut_ms:
            start -= cut_ms
        a, b = (int(start * RATE / 1000),
                int((start + n.length * beat_ms) * RATE / 1000))
        f = _freq(n.pitch)
        audio[a:b] += 0.6 * np.sin(2 * np.pi * f * t_all[a:b])
        audio[a:b] += 0.3 * np.sin(2 * np.pi * 2 * f * t_all[a:b])
    wav = chart_path.with_suffix(".wav")
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((audio * 30000).astype(np.int16).tobytes())
    return chart, wav


CHART = ("#ARTIST:T\n#TITLE:S\n#BPM:240\n#GAP:1000\n"
         ": 0 4 30 Sum\n: 4 4 32 mer\n: 8 4 34 time\n"
         ": 12 4 35 has\n: 16 4 33 come\n: 20 4 31 on\n- 24\n"
         ": 48 4 32 rain\n: 52 4 36 keeps\n: 56 4 34 fall\n"
         ": 60 4 31 ing\n: 64 4 33 down\n: 68 4 35 now\n- 72\n"
         ": 100 4 36 take\n: 104 4 34 a\n: 108 4 30 lit\n"
         ": 112 4 33 tle\n: 116 4 35 piece\n: 120 4 32 of\n- 124\n"
         ": 140 4 30 my\n: 144 4 34 bro\n: 148 4 36 ken\n"
         ": 152 4 33 heart\n: 156 4 31 to\n: 160 4 35 night\n- 164\n"
         ": 200 4 38 sev\n: 204 4 36 en\n: 208 4 34 days\n"
         ": 212 4 32 with\n: 216 4 35 out\n: 220 4 31 you\n- 224\n"
         ": 240 4 36 an\n: 244 4 32 o\n: 248 4 30 ther\n"
         ": 252 4 34 end\n: 256 4 33 less\n: 260 4 35 night\n- 264\n"
         ": 300 4 34 seven\n: 304 4 32 dead\n: 308 4 36 ly\n"
         ": 312 4 30 sins\n: 316 4 33 pile\n: 320 4 35 up\n- 324\n"
         ": 340 4 32 wake\n: 344 4 30 me\n: 348 4 34 up\n"
         ": 352 4 36 when\n: 356 4 33 sep\n: 360 4 35 tember\nE\n")


def test_align_recovers_offset(tmp_path):
    p = tmp_path / "s.txt"
    p.write_text(CHART)
    chart, wav = _synth(p, true_off_ms=8000)
    res = align_chart(chart, wav)
    assert res is not None
    assert abs(res["offset_ms"] - 8000) < 1200
    assert res["matched"] == 8


def test_align_finds_cut_instrumental(tmp_path):
    """A 4s cut before the last phrase must show up as a warp entry at
    that phrase's start beat — earlier phrases keep the base offset."""
    p = tmp_path / "s.txt"
    p.write_text(CHART)
    chart, wav = _synth(p, true_off_ms=8000, cut_ms=4000,
                        cut_before_beat=200)
    res = align_chart(chart, wav)
    assert res is not None
    assert abs(res["offset_ms"] - 8000) < 1200
    assert res["warp"] and res["warp"][0][0] == 200
    assert abs(res["warp"][0][1] - 4000) < 1200   # 8s - 4s cut


def test_align_finds_stretched_instrumental(tmp_path):
    """A 4s-late last phrase (video extended the solo) records a warp
    entry past the base offset."""
    p = tmp_path / "s.txt"
    p.write_text(CHART)
    chart, wav = _synth(p, true_off_ms=8000, cut_ms=-4000,
                        cut_before_beat=200)
    res = align_chart(chart, wav)
    assert res is not None
    assert res["warp"] and res["warp"][0][0] == 200
    assert abs(res["warp"][0][1] - 12000) < 1200


def test_align_tempo_ratio(tmp_path):
    """A video running 4% slow lands on tempo ~1.04 and still aligns."""
    p = tmp_path / "s.txt"
    p.write_text(CHART)
    chart, wav = _synth(p, true_off_ms=8000, tempo=1.04)
    res = align_chart(chart, wav)
    assert res is not None
    assert 1.0 < res["tempo"] <= 1.10
    assert res["matched"] >= 2


def test_align_finds_song_start_after_slate(tmp_path):
    """A 40s title-slate pause before the music: the whole-chart contour
    still lands the offset — this is the 'detect where the song starts'
    case and the full pass resolves it without ambiguity."""
    p = tmp_path / "s.txt"
    p.write_text(CHART)
    chart, wav = _synth(p, true_off_ms=40000)
    res = align_chart(chart, wav)
    assert res is not None
    assert abs(res["offset_ms"] - 40000) < 1500


def test_align_rejects_silence(tmp_path):
    p = tmp_path / "s.txt"
    p.write_text(CHART)
    chart = parse_chart(p)
    wav = tmp_path / "empty.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"\x00" * RATE * 4)     # 2s of silence
    assert align_chart(chart, wav) is None
