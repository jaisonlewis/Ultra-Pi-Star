"""Scoring engine — Python port of UltraStar WorldParty's sing-update
logic (``src/base/UNote.pas`` + line bonus from
``src/screens/controllers/UScreenSingController.pas``, GPL-3.0).

Faithful to upstream:
  * each beat with a covering non-freestyle chart note is scoreable
  * the sung tone is octave-normalized within +-6 semitones of the target
  * hit tolerance = 2 - difficulty halftones (Easy=2, Medium=1, Hard=0);
    rap notes always count as hits
  * points per hit beat = MaxSongPoints / ScoreValue * ScoreFactor[type]
    where ScoreValue = sum of note.length * factor over the whole track
  * with line bonus enabled MaxSongPoints is 9000 and up to 1000 more is
    distributed per phrase by line perfection
  * golden points accumulate separately and are rounded complementary to
    the normal score so the total caps at 10000
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .charts import Chart, Note

# --- constants ported verbatim from UNote.pas / UMusic.pas --------------
MAX_SONG_SCORE = 10000
MAX_SONG_LINE_BONUS = 1000
MAX_LINE_RATING = 8

# TNoteType = (ntFreestyle, ntNormal, ntGolden, ntRap, ntRapGolden)
SCORE_FACTOR = {"freestyle": 0, "normal": 1, "golden": 2,
                "rap": 1, "rap-golden": 2}

DIFFICULTIES = ("easy", "medium", "hard")


@dataclass
class PlayerNote:
    """One contiguous sung note (upstream: CurrentPlayer.Note[])."""
    start: int
    length: int
    tone: int
    hit: bool
    kind: str = "normal"
    perfect: bool = False


@dataclass
class PlayerScore:
    """Per-player running score (upstream: TPlayer fields)."""
    score: float = 0.0          # normal + rap note points
    score_golden: float = 0.0   # golden note points
    score_line: float = 0.0     # accumulated line bonuses
    score_last: float = 0.0     # score+golden at last phrase end
    notes: list[PlayerNote] = field(default_factory=list)
    last_sentence_perfect: bool = False
    streak: int = 0           # consecutive chart notes with >=1 hit beat
    max_streak: int = 0

    # complementary-rounded display values (upstream ScoreInt etc.)
    @property
    def score_int(self) -> int:
        return round(self.score / 10) * 10

    @property
    def score_golden_int(self) -> int:
        if self.score_int < self.score:
            return math.ceil(self.score_golden / 10) * 10
        return math.floor(self.score_golden / 10) * 10

    @property
    def score_line_int(self) -> int:
        return math.floor(self.score_line / 10) * 10

    @property
    def total(self) -> int:
        return self.score_int + self.score_golden_int + self.score_line_int


class Scorer:
    """Scores one player against one chart track, one beat at a time.

    Usage: call ``tick(beat, tone_abs|None)`` for every chart beat while a
    verified song plays, and ``phrase_end()`` when the playhead crosses a
    phrase boundary, then read ``state`` for the live score.
    """

    def __init__(self, chart: Chart, track: str = "P1",
                 difficulty: int = 0, line_bonus: bool = True):
        self.lines = chart.lines(track)
        self.range = 2 - max(0, min(difficulty, 2))
        self.line_bonus = line_bonus
        self.max_song_points = (MAX_SONG_SCORE - MAX_SONG_LINE_BONUS
                                if line_bonus else MAX_SONG_SCORE)
        # upstream ScoreValue: total weighted beats across the track
        self.score_value = sum(
            n.length * SCORE_FACTOR[n.kind]
            for line in self.lines for n in line)
        # non-empty lines count for the line bonus split
        self.num_lines = sum(1 for line in self.lines if line)
        # flat index of every scorable beat -> (note, line_index)
        self._beat_map: dict[int, tuple[Note, int]] = {}
        for li, line in enumerate(self.lines):
            for n in line:
                if n.kind == "freestyle" or n.length <= 0:
                    continue
                for b in range(n.start, n.start + n.length):
                    self._beat_map[b] = (n, li)
        self._line_max = [sum(n.length * SCORE_FACTOR[n.kind] for n in line)
                          for line in self.lines]
        self._line_end = [line[-1].start + line[-1].length - 1
                          if line else -1 for line in self.lines]
        self.state = PlayerScore()
        self._pending_line: int | None = None
        # notes not yet past their last beat, oldest first: [note, any_hit]
        self._note_eval: list[list] = []

    def points_per_beat(self, note: Note) -> float:
        if not self.score_value:
            return 0.0
        return (self.max_song_points / self.score_value
                * SCORE_FACTOR[note.kind])

    def tick(self, beat: int, tone_abs: int | None) -> dict | None:
        """Port of the UNote.pas sing loop for one player/beat.

        ``tone_abs`` is the detector's absolute tone (0..45) or None when
        no valid pitch. Returns a hit/miss event dict or None."""
        line_event = None
        self._eval_notes(beat)
        hit = self._beat_map.get(beat)
        if hit is None:
            # line bonus can still fire on noteless beats between phrases
            if self._pending_line is not None and beat > self._line_end[self._pending_line]:
                line_event = self._apply_line_bonus(self._pending_line)
                self._pending_line = None
            return ({"beat": beat, "line": line_event} if line_event else None)
        note, line_idx = hit
        if not self._note_eval or self._note_eval[-1][0] is not note:
            self._note_eval.append([note, False])

        # phrase-end bonus fires once when the playhead leaves a line
        if self._pending_line is not None and beat > self._line_end[self._pending_line]:
            line_event = self._apply_line_bonus(self._pending_line)
            self._pending_line = None
        if beat == self._line_end[line_idx]:
            self._pending_line = line_idx

        if tone_abs is None or tone_abs < 0:
            return {"beat": beat, "hit": False, "tone": None}

        # octave-normalize sung tone within +-6 of the target note
        tone = tone_abs
        while tone - note.pitch > 6:
            tone -= 12
        while tone - note.pitch < -6:
            tone += 12

        is_hit = (abs(note.pitch - tone) <= self.range
                  or note.kind in ("rap", "rap-golden"))
        display_tone = note.pitch if is_hit else tone
        self._note_eval[-1][1] = self._note_eval[-1][1] or is_hit

        if is_hit:
            pts = self.points_per_beat(note)
            if note.kind in ("golden", "rap-golden"):
                self.state.score_golden += pts
            else:
                self.state.score += pts

        # record/extend the player's note (upstream: extend when same tone
        # and contiguous)
        last = self.state.notes[-1] if self.state.notes else None
        if (last is not None and last.tone == display_tone
                and last.start + last.length == beat):
            last.length += 1
            last.hit = last.hit or is_hit
        else:
            self.state.notes.append(PlayerNote(
                start=beat, length=1, tone=display_tone,
                hit=is_hit, kind=note.kind,
                perfect=(is_hit and note.start == beat)))

        return {"beat": beat, "hit": is_hit, "tone": display_tone,
                "kind": note.kind, "score": self.state.total,
                "line": line_event}

    def _eval_notes(self, beat: int) -> None:
        """Settle completed notes into the streak counter (SingStar-style
        combo: any note with at least one hit beat keeps the streak)."""
        while (self._note_eval
               and self._note_eval[0][0].start
               + self._note_eval[0][0].length <= beat):
            _, hit = self._note_eval.pop(0)
            self.state.streak = self.state.streak + 1 if hit else 0
            self.state.max_streak = max(self.state.max_streak,
                                        self.state.streak)

    def _apply_line_bonus(self, line_idx: int) -> dict | None:
        """Port of UScreenSingController.OnSentenceEnd line bonus."""
        if not self.line_bonus or not self.num_lines:
            return None
        current = self.state.score + self.state.score_golden
        line_score = current - self.state.score_last
        # max points this line could have contributed
        max_line = (self.max_song_points / self.score_value
                    * self._line_max[line_idx]) if self.score_value else 0
        perfection = 1.0 if max_line <= 2 else line_score / (max_line - 2)
        perfection = max(0.0, min(1.0, perfection))
        self.state.score_line += (MAX_SONG_LINE_BONUS / self.num_lines
                                  ) * perfection
        self.state.score_last = current
        self.state.last_sentence_perfect = perfection >= 1
        return {"line": line_idx, "perfection": perfection,
                "rating": round(perfection * MAX_LINE_RATING),
                "score": self.state.total}

    def finish(self) -> dict:
        """Flush any trailing phrase bonus; returns final per-track state."""
        if self._pending_line is not None:
            self._apply_line_bonus(self._pending_line)
            self._pending_line = None
        while self._note_eval:
            _, hit = self._note_eval.pop(0)
            self.state.streak = self.state.streak + 1 if hit else 0
            self.state.max_streak = max(self.state.max_streak,
                                        self.state.streak)
        s = self.state
        return {"score": s.total, "score_normal": s.score_int,
                "score_golden": s.score_golden_int,
                "score_line": s.score_line_int,
                "notes": len(s.notes),
                "hits": sum(1 for n in s.notes if n.hit),
                "perfect": sum(1 for n in s.notes if n.perfect),
                "max_streak": s.max_streak}
