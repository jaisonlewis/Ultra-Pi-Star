"""Chart model + UltraStar (.txt) importer + native .upsong format.

UltraStar txt is an *import* format only. It encodes word boundaries in
syllable edge-whitespace and glues syllables with '~' — both are
normalized here into an explicit ``Note.word_break`` flag and clean
syllable text, then the whole chart is serialized to a native JSON file
(``.upsong``, sibling of the source .txt) that every consumer loads.

Native format v1::

    {"format": "upsong", "version": 1,
     "headers": {...},
     "source": {...},
     "tracks": {"P1": [{"s":18,"l":8,"p":11,"t":"mer","k":"normal",
                        "wb":1}, ...]},
     "breaks": {"P1": [80, 160]}}

``k`` is omitted for normal notes, ``wb`` when false.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

NATIVE_FORMAT = "upsong"
NATIVE_VERSION = 2


@dataclass
class Note:
    kind: str          # normal | golden | freestyle | rap | rap-golden
    start: int
    length: int
    pitch: int
    text: str          # clean syllable text (no edge spaces, no '~')
    word_break: bool = False   # this syllable starts a new display word


@dataclass
class Chart:
    headers: dict[str, str] = field(default_factory=dict)
    tracks: dict[str, list[Note]] = field(default_factory=lambda: {"P1": []})
    breaks: dict[str, list[int]] = field(default_factory=dict)  # phrase-end beats per track
    errors: list[str] = field(default_factory=list)
    source: dict = field(default_factory=dict)   # provenance (native files only)

    @property
    def artist(self) -> str:
        return self.headers.get("ARTIST", "")

    @property
    def title(self) -> str:
        return self.headers.get("TITLE", "")

    @property
    def bpm(self) -> float:
        try:
            return float(self.headers.get("BPM", "0").replace(",", "."))
        except ValueError:
            return 0.0

    @property
    def gap_ms(self) -> float:
        try:
            return float(self.headers.get("GAP", "0").replace(",", "."))
        except ValueError:
            return 0.0

    @property
    def is_duet(self) -> bool:
        return len(self.tracks) > 1

    def lines(self, track: str) -> list[list[Note]]:
        """Notes grouped into lyric lines split at '-' phrase breaks."""
        brks = sorted(set(self.breaks.get(track, [])))
        lines, cur, i = [], [], 0
        for n in self.tracks.get(track, []):
            while i < len(brks) and brks[i] <= n.start:
                if cur and cur[-1].start <= brks[i]:
                    lines.append(cur)
                    cur = []
                i += 1
            cur.append(n)
        if cur:
            lines.append(cur)
        return lines

    def phrases(self) -> dict[str, list[dict]]:
        """Group each track's notes into lyric lines split at '-' breaks.

        Returns {track: [{start, end, text, syls}]} in chart beats.
        ``syls`` are the per-syllable spans [{t, wb, s, e}] the TV uses
        for karaoke-style progressive lyric fill.
        """
        result = {}
        for track in self.tracks:
            lines = []
            for l in self.lines(track):
                text = "".join(
                    (" " if i and n.word_break else "")
                    + n.text.replace("~", "")
                    for i, n in enumerate(l)).strip()
                lines.append({
                    "start": l[0].start,
                    "end": l[-1].start + l[-1].length,
                    "text": text,
                    "syls": [{"t": n.text.replace("~", ""),
                              "wb": n.word_break,
                              "s": n.start, "e": n.start + n.length}
                             for n in l],
                })
            result[track] = lines
        return result

    def validate(self) -> list[str]:
        errs = list(self.errors)
        if not self.artist or not self.title:
            errs.append("missing ARTIST/TITLE header")
        if self.bpm <= 0:
            errs.append("invalid or missing BPM")
        if not any(self.tracks.values()):
            errs.append("no notes parsed")
        return errs


NOTE_KINDS = {":": "normal", "*": "golden", "F": "freestyle", "R": "rap",
              "G": "rap-golden"}


def parse_chart(path: Path | str) -> Chart:
    """Import an UltraStar .txt chart into the normalized model."""
    chart = Chart()
    current = "P1"
    last_raw = ""    # previous note's raw text on this track
    shifts = {"P1": 0}   # RELATIVE:YES per-track beat offsets
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        chart.errors.append(f"unreadable: {exc}")
        return chart

    for lineno, rawline in enumerate(raw.splitlines(), 1):
        line = rawline.strip().lstrip("﻿")
        if not line:
            continue
        if line.startswith("#"):
            key, _, value = line[1:].partition(":")
            chart.headers[key.strip().upper()] = value.strip()
        elif line.startswith("P") and line[1:].isdigit():
            current = line.strip().upper()
            chart.tracks.setdefault(current, [])
            shifts.setdefault(current, 0)
            last_raw = ""
        elif line[0] in NOTE_KINDS:
            # parse from rawline so syllable edge-spaces survive: charts
            # mark word boundaries with a leading space on the next
            # syllable OR a trailing space on this one — the global
            # .strip() above destroys both and lyrics run together
            parts = rawline.lstrip(" \t﻿")[1:].lstrip(" \t").split(" ", 3)
            if len(parts) < 4:
                chart.errors.append(f"line {lineno}: malformed note")
                continue
            try:
                start, length, pitch = (int(p) for p in parts[:3])
            except ValueError:
                chart.errors.append(f"line {lineno}: non-numeric note fields")
                continue
            raw_t = parts[3]
            if raw_t.startswith("~"):
                word_break = False      # '~' glues this syllable onward
            else:
                word_break = (raw_t[:1].isspace()
                              or last_raw[-1:].isspace()
                              or not chart.tracks.get(current))
            last_raw = raw_t
            chart.tracks.setdefault(current, []).append(Note(
                NOTE_KINDS[line[0]], start + shifts.get(current, 0),
                length, pitch,
                raw_t.replace("~", "").strip(), word_break))
        elif line.startswith("-"):
            # phrase break; the number after '-' is the beat the phrase ends on
            parts = line[1:].strip().split()
            try:
                beat = int(parts[0]) if parts else 0
            except ValueError:
                beat = 0
            if chart.headers.get("RELATIVE", "").upper() in ("YES", "TRUE", "1"):
                # relative mode: note beats count from their own phrase's
                # start; the '-' line carries that phrase's relative end
                # beat plus the next phrase's start offset from the
                # current phrase start (USDX: Rel += Param2)
                try:
                    nxt = int(parts[1]) if len(parts) > 1 else beat
                except ValueError:
                    nxt = beat
                shifts[current] = shifts.get(current, 0) + nxt
                chart.breaks.setdefault(current, []).append(shifts[current])
            else:
                chart.breaks.setdefault(current, []).append(beat)
        elif line == "E":
            break
        else:
            chart.errors.append(f"line {lineno}: unrecognized content")
    return chart


# ---------- native .upsong format ----------

def native_path(path: Path | str) -> Path:
    return Path(path).with_suffix(".upsong")


def _note_to_native(n: Note) -> dict:
    d = {"s": n.start, "l": n.length, "p": n.pitch, "t": n.text}
    if n.kind != "normal":
        d["k"] = n.kind
    if n.word_break:
        d["wb"] = 1
    return d


def to_native(chart: Chart, source: dict | None = None) -> dict:
    return {
        "format": NATIVE_FORMAT,
        "version": NATIVE_VERSION,
        "headers": dict(chart.headers),
        "source": source if source is not None else chart.source,
        "tracks": {t: [_note_to_native(n) for n in notes]
                   for t, notes in chart.tracks.items()},
        "breaks": {t: list(b) for t, b in chart.breaks.items()},
    }


def from_native(d: dict) -> Chart:
    if (d.get("format") != NATIVE_FORMAT
            or int(d.get("version") or 0) != NATIVE_VERSION):
        raise ValueError("unsupported upsong format/version")
    chart = Chart()
    chart.headers = {str(k).upper(): str(v)
                     for k, v in (d.get("headers") or {}).items()}
    chart.source = d.get("source") or {}
    tracks = d.get("tracks") or {}
    chart.tracks = {
        t: [Note(n.get("k", "normal"), int(n["s"]), int(n["l"]),
                 int(n["p"]), str(n.get("t", "")), bool(n.get("wb")))
            for n in notes]
        for t, notes in tracks.items()
    } or {"P1": []}
    chart.breaks = {t: [int(b) for b in bs]
                    for t, bs in (d.get("breaks") or {}).items()}
    return chart


def save_native(chart: Chart, path: Path | str,
                source: dict | None = None) -> Path:
    path = Path(path)
    path.write_text(
        json.dumps(to_native(chart, source), ensure_ascii=False,
                   separators=(",", ":")),
        encoding="utf-8")
    return path


def load_chart(path: Path | str) -> Chart:
    """Load the canonical chart — a native ``.upsong`` when present,
    otherwise import the ``.txt`` and cache the native form alongside it
    (lazy migration; ignored when the directory is read-only)."""
    path = Path(path)
    if path.suffix == ".upsong":
        chart = Chart()
        try:
            return from_native(json.loads(
                path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            txt = path.with_suffix(".txt")
            if txt.exists():
                # stale/incompatible cache — re-import the source txt
                chart = parse_chart(txt)
                if not chart.errors:
                    try:
                        save_native(chart, path)
                    except OSError:
                        pass
                return chart
            chart.errors.append(f"bad upsong: {exc}")
            return chart
    nat = native_path(path)
    # prefer the native form, but never let it shadow a freshly-edited txt
    if nat.exists() and nat.stat().st_mtime >= path.stat().st_mtime:
        return load_chart(nat)
    chart = parse_chart(path)
    if not chart.errors:
        try:
            save_native(chart, nat)
        except OSError:
            pass
    return chart
