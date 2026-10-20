"""Pitch detection — Python port of UltraStar WorldParty's
``TCaptureBuffer`` analyzer (``src/base/URecord.pas``, GPL-3.0).

Algorithm (faithful to upstream):
  * keep the newest 4096 int16 samples in an analysis buffer
  * gate on volume: max amplitude of the first 1024 samples must clear a
    threshold (Ini.ThresholdIndex -> 0.05/0.10/0.15/0.20)
  * score each of 46 halftone candidates C2..A5 by one-period-lag
    autocorrelation: weight = 1 - mean(|s[i] - s[i+lag]|)/65535, where
    lag = round(sample_rate / candidate_freq)
  * best weight wins -> tone_abs (0..45) and tone (0..11 within octave)

Pure-Python fallback is kept for environments without numpy, but numpy is
used when available (the 46-lag scan is ~200k ops otherwise).
"""
from __future__ import annotations

import math
from array import array
from dataclasses import dataclass

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy present in normal installs
    np = None

# --- constants ported verbatim from URecord.pas / UIni.pas --------------
BASE_TONE_FREQ = 65.4064          # lowest analyzed (half-)tone: C2
NUM_HALFTONES = 46                # C2..A5
HALFTONE_BASE = 2 ** (1 / 12)     # HalftoneBase^12 = 2 (one octave)
ANALYSIS_BUFFER_SIZE = 4096
VOLUME_WINDOW = 1024              # samples inspected for the volume gate
THRESHOLDS = (0.05, 0.10, 0.15, 0.20)   # IThresholdVals[Ini.ThresholdIndex]

TONE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# candidate frequencies, C2..A5
TONE_FREQS = [BASE_TONE_FREQ * HALFTONE_BASE ** i for i in range(NUM_HALFTONES)]


@dataclass
class PitchResult:
    valid: bool
    tone: int = -1        # semitone within octave, 0..11
    tone_abs: int = -1    # 0..45 (C2..A5)
    volume: float = 0.0

    @property
    def tone_str(self) -> str:
        """Port of TCaptureBuffer.GetToneString — e.g. 'A4'."""
        if not self.valid:
            return "-"
        return f"{TONE_NAMES[self.tone]}{self.tone_abs // 12 + 2}"

    @property
    def freq(self) -> float:
        return TONE_FREQS[self.tone_abs] if self.valid else 0.0


class PitchDetector:
    """Streaming pitch detector; feed it int16 mono PCM chunks."""

    def __init__(self, sample_rate: int = 44100, threshold_index: int = 2):
        self.sample_rate = sample_rate
        self.threshold = THRESHOLDS[min(max(threshold_index, 0), 3)]
        # newest 4096 samples (upstream: AnalysisBuffer ring of 4096)
        self.buffer = array("h")          # int16
        # lag per candidate tone (samples per period)
        self._lags = [max(1, round(sample_rate / f)) for f in TONE_FREQS]

    def feed(self, pcm: bytes) -> None:
        """Append int16 mono PCM bytes; keeps the newest 4096 samples."""
        n = len(pcm) // 2
        if not n:
            return
        new = array("h")
        new.frombytes(pcm[: n * 2])
        self.buffer.extend(new)
        if len(self.buffer) > ANALYSIS_BUFFER_SIZE:
            del self.buffer[: len(self.buffer) - ANALYSIS_BUFFER_SIZE]

    def analyze(self) -> PitchResult:
        """Port of TCaptureBuffer.AnalyzeBuffer."""
        if len(self.buffer) < VOLUME_WINDOW:
            return PitchResult(False, volume=0.0)

        if np is not None:
            return self._analyze_np()
        return self._analyze_py()

    # a pure tone also correlates at lags that are integer multiples of its
    # period (subharmonics win on weight alone); among weights within EPS of
    # the maximum prefer the *highest* frequency — upstream's own TODO.
    _EPS = 0.01

    # -- numpy path --------------------------------------------------------
    def _analyze_np(self) -> PitchResult:
        buf = np.asarray(self.buffer, dtype=np.int32)
        volume = float(np.abs(buf[:VOLUME_WINDOW]).max()) / 32768.0
        if volume < self.threshold:
            return PitchResult(False, volume=volume)

        n = len(buf)
        weights = np.full(NUM_HALFTONES, -1.0)
        for tone, lag in enumerate(self._lags):
            if lag >= n:
                continue
            # upstream divides by buffer size, not sample count — kept faithful
            weights[tone] = 1.0 - np.abs(buf[: n - lag] - buf[lag:]).sum() / 65535.0 / n
        best_w = weights.max()
        # highest tone within EPS of the max (subharmonic guard)
        best_tone = int(np.flatnonzero(weights >= best_w - self._EPS).max())
        return PitchResult(True, best_tone % 12, best_tone, volume)

    # -- pure-python path ----------------------------------------------------
    def _analyze_py(self) -> PitchResult:
        buf = self.buffer
        volume = max(abs(s) for s in buf[:VOLUME_WINDOW]) / 32768.0
        if volume < self.threshold:
            return PitchResult(False, volume=volume)

        n = len(buf)
        weights = []
        for tone in range(NUM_HALFTONES):
            lag = self._lags[tone]
            if lag >= n:
                weights.append(-1.0)
                continue
            accum = 0
            for i in range(n - lag):
                accum += abs(buf[i] - buf[i + lag])
            weights.append(1.0 - accum / 65535.0 / n)
        best_w = max(weights)
        # highest tone within EPS of the max (subharmonic guard)
        best_tone = max(t for t, w in enumerate(weights)
                        if w >= best_w - self._EPS)
        return PitchResult(True, best_tone % 12, best_tone, volume)


def freq_to_tone_abs(freq: float) -> int:
    """Nearest tone_abs index for a Hz value (C2=0 .. A5=45)."""
    if freq <= 0:
        return -1
    return min(range(NUM_HALFTONES),
               key=lambda i: abs(math.log2(freq / TONE_FREQS[i])))
