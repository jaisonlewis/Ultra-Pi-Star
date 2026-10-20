"""Song catalog: canonical records, media entries, chart matching, search.

Phase 5 policy implemented here:
  * one song -> many media entries (providers) -> optional charts
  * a chart is only "scoring enabled" when verified against a specific
    media entry; ambiguous matches are listed but never auto-scored
  * artwork/provenance tracked per entry
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .charts import load_chart
from .db import Database


def parse_sync_warp(text: str, entry_id: int) -> tuple[float, list]:
    """chart.sync_warp is {"<entry_id>": {"ratio": r, "marks": [[beat,
    offset_ms]...]}} — keyed by media entry so a song-level chart row
    keeps each video's learned timing separate. Bare [[beat, off]...]
    lists are still accepted (ratio=1)."""
    try:
        w = json.loads(text or "{}")
    except (TypeError, ValueError):
        return 1.0, []
    if isinstance(w, dict):
        w = w.get(str(entry_id)) or []
    if isinstance(w, dict):
        return (float(w.get("ratio") or 1.0), w.get("marks") or [])
    return 1.0, w if isinstance(w, list) else []

_PAREN_KARAOKE = re.compile(
    r"[\(\[][^\)\]]*(karaoke|instrumental|cover|version|lyrics?|hd|4k|official)[^\)\]]*[\)\]]",
    re.IGNORECASE)
# bare trailing descriptors (no parens) that lyric/karaoke uploaders
# append — "Song with Lyrics On Screen", "Song lyric video", "Song HD"
_TITLE_TRAIL = re.compile(
    r"(?:\s+[-–—|:•]\s*|\s+)"
    r"(?:with\s+(?:on[- ]?screen\s+)?lyrics(?:\s+on\s+screen)?"
    r"|on[- ]?screen\s+lyrics|lyrics?\s+on\s+screen|lyric\s+video"
    r"|official\s+(?:lyric\s+|music\s+)?video|official\s+audio"
    r"|music\s+video|animated\s+video|sing[- ]?along|karaoke(?:\s+version)?"
    r"|lyrics?|hd|hq|4k|remaster(?:ed)?)"
    r"\s*$", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def clean_title(s: str) -> str:
    """Strip karaoke/lyric-video decorations a uploader appends to the
    real song title — parenthesized tags first, then bare suffixes."""
    s = _PAREN_KARAOKE.sub(" ", s or "")
    prev = None
    while prev != s:   # descriptors can chain: "Song (HD) lyric video"
        prev = s
        s = _TITLE_TRAIL.sub("", s).strip()
    return re.sub(r"\s{2,}", " ", s).strip()


def _norm(s: str) -> str:
    """Normalize artist/title for chart<->song matching: lowercase, drop
    karaoke-ish parentheticals, collapse punctuation and whitespace."""
    s = _PAREN_KARAOKE.sub(" ", s or "")
    return _NON_ALNUM.sub(" ", s.lower()).strip()

SEARCH_SQL = """
SELECT s.id, s.artist, s.title, s.language, s.is_duet, s.tracks,
       e.id AS entry_id, e.provider, e.source_id, e.local_path, e.status,
       e.duration_sec, e.thumbnail, e.provenance, e.inst_path,
       e.meta_artist, e.meta_track, e.meta_album,
       c.id AS chart_id, c.verified AS chart_verified, c.confidence AS chart_confidence,
       c.is_duet AS chart_duet, c.sync_offset_ms
FROM song s
JOIN media_entry e ON e.song_id = s.id
LEFT JOIN chart c ON c.id = (
    SELECT c2.id FROM chart c2
    WHERE c2.song_id = s.id AND c2.verified = 1
      AND (c2.media_entry_id = e.id OR c2.media_entry_id IS NULL)
    ORDER BY (c2.media_entry_id IS NULL),
             (c2.source_repo='generated') DESC, c2.id LIMIT 1)
WHERE (s.artist LIKE ? OR s.title LIKE ? OR (s.artist || ' ' || s.title) LIKE ?)
ORDER BY s.artist, s.title
"""

ENTRY_SQL = SEARCH_SQL.replace("WHERE (s.artist", "WHERE e.id=? AND (s.artist")


class Catalog:
    def __init__(self, db: Database, chart_dir: Path,
                 trusted_sources: tuple[str, ...] = ()):
        self.db = db
        self.chart_dir = Path(chart_dir)
        # sources trusted to auto-verify matched charts (UPS_TRUST_SOURCES)
        self.trusted_sources = set(trusted_sources)

    # -- writes --------------------------------------------------------------
    def add_song(self, artist: str, title: str, language: str = "",
                 is_duet: bool = False, tracks: list[str] | None = None,
                 edition: str = "", year: int | None = None) -> int:
        return self.db.execute(
            "INSERT OR IGNORE INTO song (artist,title,language,is_duet,tracks,edition,year)"
            " VALUES (?,?,?,?,?,?,?)",
            (artist, title, language, int(is_duet),
             json.dumps(tracks or (["P1", "P2"] if is_duet else ["P1"])),
             edition, year),
        )

    def add_media_entry(self, song_id: int, provider: str, source_id: str,
                        local_path: str | None = None, duration_sec: float | None = None,
                        thumbnail: str = "", provenance: str = "",
                        license_note: str = "", status: str = "available") -> int:
        return self.db.execute(
            "INSERT OR IGNORE INTO media_entry"
            " (song_id,provider,source_id,local_path,duration_sec,status,"
            " thumbnail,provenance,license_note)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (song_id, provider, source_id, local_path, duration_sec,
             status, thumbnail, provenance, license_note),
        )

    def attach_chart(self, song_id: int, file_path: Path, media_entry_id: int | None = None,
                     source_repo: str = "", revision: str = "", confidence: float = 0.0,
                     verified: bool = False, license_note: str = "") -> int:
        chart = load_chart(file_path)
        return self.db.execute(
            "INSERT INTO chart (song_id,media_entry_id,revision,source_repo,file_path,"
            " is_duet,verified,confidence,license_note) VALUES (?,?,?,?,?,?,?,?,?)",
            (song_id, media_entry_id, revision, source_repo, str(file_path),
             int(chart.is_duet), int(verified), confidence, license_note),
        )

    # -- reads ---------------------------------------------------------------
    def search(self, query: str = "", limit: int = 50) -> list[dict]:
        like = f"%{query.strip()}%"
        rows = self.db.query(SEARCH_SQL + " LIMIT ?", (like, like, like, limit))
        tops = self.top_scores()
        for r in rows:
            r["tracks"] = json.loads(r["tracks"])
            r["scoring_available"] = bool(r["chart_verified"])
            top = tops.get(r["id"])
            r["top_score"] = top["score"] if top else None
            r["top_scorer"] = top["scorer"] if top else ""
            # possible (unverified) chart match -> shown but never auto-scored
            if not r["scoring_available"]:
                r["possible_chart"] = bool(self.db.query(
                    "SELECT 1 FROM chart c JOIN media_entry e ON e.song_id=c.song_id"
                    " WHERE e.id=? AND c.verified=0 LIMIT 1", (r["entry_id"],)))
            else:
                r["possible_chart"] = False
        return rows

    def top_scores(self) -> dict[int, dict]:
        """Per-song record holder: {song_id: {score, scorer}}.

        SQLite picks bare columns from the MAX() row, so scorer/scorer2 come
        from the winning performance — duets show "P1 & P2".
        """
        rows = self.db.query(
            "SELECT h.song_id, MAX(h.score) AS score, p.name AS scorer,"
            " p2.name AS scorer2"
            " FROM play_history h"
            " JOIN performer p ON p.id=h.performer_id"
            " LEFT JOIN queue_item q ON q.id=h.queue_item_id"
            " LEFT JOIN performer p2 ON p2.id=q.performer2_id"
            " WHERE h.score > 0"
            " GROUP BY h.song_id")
        out = {}
        for r in rows:
            scorer = r["scorer"] + (f" & {r['scorer2']}" if r["scorer2"] else "")
            out[r["song_id"]] = {"score": r["score"], "scorer": scorer}
        return out

    def leaderboard(self) -> dict:
        """Song records + singer standings for the phone scoreboard."""
        songs = self.db.query(
            "SELECT h.song_id, s.artist, s.title, MAX(h.score) AS score,"
            " p.name AS scorer, p2.name AS scorer2, COUNT(h.id) AS plays"
            " FROM play_history h"
            " JOIN song s ON s.id=h.song_id"
            " JOIN performer p ON p.id=h.performer_id"
            " LEFT JOIN queue_item q ON q.id=h.queue_item_id"
            " LEFT JOIN performer p2 ON p2.id=q.performer2_id"
            " WHERE h.score > 0"
            " GROUP BY h.song_id ORDER BY score DESC")
        singers = self.db.query(
            "SELECT p.name, COUNT(*) AS songs, MAX(x.score) AS best,"
            " ROUND(AVG(x.score)) AS avg, SUM(x.score) AS total FROM ("
            "  SELECT h.score, h.performer_id FROM play_history h"
            "   WHERE h.score > 0"
            "  UNION ALL"
            "  SELECT h.score, q.performer2_id FROM play_history h"
            "   JOIN queue_item q ON q.id=h.queue_item_id"
            "   WHERE h.score > 0 AND q.performer2_id IS NOT NULL"
            " ) x JOIN performer p ON p.id=x.performer_id"
            " GROUP BY p.id ORDER BY total DESC")
        for s in songs:
            if s["scorer2"]:
                s["scorer"] = f"{s['scorer']} & {s['scorer2']}"
        return {"songs": songs, "singers": singers}

    def entry(self, entry_id: int) -> dict | None:
        rows = self.db.query(ENTRY_SQL + " LIMIT 1", (entry_id, "%", "%", "%"))
        if not rows:
            return None
        r = rows[0]
        r["tracks"] = json.loads(r["tracks"])
        r["scoring_available"] = bool(r["chart_verified"])
        return r

    def library(self) -> list[dict]:
        return self.search("")

    def chart_payload(self, entry_id: int) -> dict | None:
        """Timed lyric lines for the *verified* chart on this media entry.

        Beats convert to milliseconds via BPM/GAP plus the per-entry sync
        offset (Phase 4 decision: sync lives on the chart↔entry record).
        """
        rows = self.db.query(
            "SELECT * FROM chart WHERE verified=1 AND (media_entry_id=?"
            " OR (media_entry_id IS NULL AND song_id="
            " (SELECT song_id FROM media_entry WHERE id=?)))"
            " ORDER BY media_entry_id IS NULL,"
            " (source_repo='generated') DESC",
            (entry_id, entry_id))
        chart, row = None, None
        for cand in rows:
            parsed = load_chart(cand["file_path"])
            if not parsed.validate():
                chart, row = parsed, cand
                break
        if chart is None:
            return None
        # learned per-media timing: a global tempo ratio plus per-phrase
        # boundary marks. Each mark applies from its phrase onward until
        # the next one — gaps that run short/long in this media step the
        # offset at the boundary instead of drifting mid-line.
        ratio, marks = parse_sync_warp(row["sync_warp"], entry_id)
        beat_ms = 15000.0 / chart.bpm * ratio
        base = chart.gap_ms + row["sync_offset_ms"]
        warp = sorted((int(b), float(o)) for b, o in marks)

        def warp_at(beat: float) -> float:
            off = base
            for wb, wo in warp:
                if wb <= beat:
                    off = chart.gap_ms + wo
                else:
                    break
            return off

        tracks = {}
        for track, phrases in chart.phrases().items():
            tracks[track] = [
                {"start_ms": p["start"] * beat_ms + warp_at(p["start"]),
                 "end_ms": p["end"] * beat_ms + warp_at(p["start"]),
                 "text": p["text"],
                 "syls": [{"t": s["t"], "wb": s["wb"],
                           "start_ms": s["s"] * beat_ms + warp_at(s["s"]),
                           "end_ms": s["e"] * beat_ms + warp_at(s["s"])}
                          for s in p["syls"]]}
                for p in phrases
            ]
        # note lanes for the pitch display (same beat->ms mapping)
        lanes = {}
        for track in chart.tracks:
            lanes[track] = [
                {"start_ms": n.start * beat_ms + warp_at(n.start),
                 "end_ms": (n.start + n.length) * beat_ms + warp_at(n.start),
                 "tone": n.pitch, "kind": n.kind, "text": n.text}
                for n in chart.tracks[track]
            ]
        return {"is_duet": chart.is_duet, "offset_ms": row["sync_offset_ms"],
                "bpm": chart.bpm, "beat_ms": beat_ms,
                "gap_ms": chart.gap_ms,
                "warp": [[int(b), float(o)] for b, o in warp],
                "tracks": tracks, "lanes": lanes}

    def rebind_song_charts(self, song_id: int) -> None:
        """Bind unbound chart rows (media_entry_id IS NULL) to the song's
        sole available entry — covers charts attached while media was
        still downloading."""
        rows = self.db.query(
            "SELECT id, status FROM media_entry WHERE song_id=? ORDER BY id",
            (song_id,))
        avail = [r for r in rows if r["status"] == "available"]
        if len(avail) != 1:
            return
        self.db.execute(
            "UPDATE chart SET media_entry_id=?"
            " WHERE song_id=? AND media_entry_id IS NULL",
            (avail[0]["id"], song_id))

    def chart_row_for_entry(self, entry_id: int) -> dict | None:
        """The verified chart row for a media entry (for scoring). Falls
        back to a song-level verified chart (media_entry_id IS NULL) when
        the song has no chart bound to this specific version."""
        rows = self.db.query(
            "SELECT * FROM chart WHERE verified=1 AND (media_entry_id=?"
            " OR (media_entry_id IS NULL AND song_id="
            " (SELECT song_id FROM media_entry WHERE id=?)))"
            " ORDER BY media_entry_id IS NULL,"
            " (source_repo='generated') DESC",
            (entry_id, entry_id))
        for row in rows:
            if not load_chart(row["file_path"]).validate():
                return row
        return None

    # -- seeding ---------------------------------------------------------------
    def seed_if_empty(self, media_dir: Path) -> None:
        if self.db.query("SELECT 1 FROM song LIMIT 1"):
            return
        media_dir = Path(media_dir)
        demo_song = self.add_song("Ultra Pi Star Demo Band", "Signal In The Noise",
                                  language="en", year=2026, edition="demo")
        demo_video = media_dir / "demo-signal-in-the-noise.mp4"
        entry = self.add_media_entry(
            demo_song, "local", "demo-signal-in-the-noise",
            local_path=demo_video.name if demo_video.exists() else None,
            duration_sec=30,
            provenance="Generated locally with ffmpeg (synthetic, cleared for demo)",
            license_note="CC0 — generated test asset")
        chart_file = self.chart_dir / "demo-signal-in-the-noise.txt"
        if chart_file.exists():
            self.attach_chart(demo_song, chart_file, media_entry_id=entry or None,
                              source_repo="bundled-demo", revision="1",
                              confidence=1.0, verified=True, license_note="CC0 demo chart")

        uncharted = self.add_song("Ultra Pi Star Demo Band", "Video Only Groove",
                                  language="en", year=2026, edition="demo")
        v2 = media_dir / "demo-video-only-groove.mp4"
        self.add_media_entry(
            uncharted, "local", "demo-video-only-groove",
            local_path=v2.name if v2.exists() else None,
            duration_sec=20,
            provenance="Generated locally with ffmpeg (synthetic, cleared for demo)",
            license_note="CC0 — generated test asset")

        duet = self.add_song("Ultra Pi Star Demo Band", "Two Voices", language="en",
                             is_duet=True, tracks=["P1", "P2"], year=2026, edition="demo")
        v3 = media_dir / "demo-two-voices.mp4"
        e3 = self.add_media_entry(
            duet, "local", "demo-two-voices",
            local_path=v3.name if v3.exists() else None,
            duration_sec=25,
            provenance="Generated locally with ffmpeg (synthetic, cleared for demo)",
            license_note="CC0 — generated test asset")
        c2 = self.chart_dir / "demo-two-voices.txt"
        if c2.exists():
            self.attach_chart(duet, c2, media_entry_id=e3 or None,
                              source_repo="bundled-demo", revision="1",
                              confidence=1.0, verified=True, license_note="CC0 demo chart")

    def rescan_charts(self, extra_dirs: list[Path] | None = None) -> int:
        """Index every *.txt UltraStar chart under chart_dir (recursive) and
        any extra library dirs into the chart_library index, then attach
        library charts to matching catalog songs.

        Matching policy: normalized artist+title equality -> the chart is
        attached to the song's media entry (when the song has exactly one)
        as *unverified* — a host must verify before it unlocks scoring.
        """
        # prune rows whose chart file has disappeared (e.g. library re-cloned
        # to a different path) so stale rows can't shadow valid matches
        for table in ("chart", "chart_library"):
            for r in self.db.query(f"SELECT id, file_path FROM {table}"):
                if not Path(r["file_path"]).exists():
                    self.db.execute(f"DELETE FROM {table} WHERE id=?", (r["id"],))
        dirs = [self.chart_dir] + [Path(d) for d in (extra_dirs or [])]
        added = 0
        for base in dirs:
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.txt")):
                key = str(path.resolve())
                if self.db.query("SELECT 1 FROM chart_library WHERE file_path=?",
                                 (key,)):
                    continue
                if self.index_chart_file(path):
                    added += 1
        for song in self.db.query("SELECT id FROM song"):
            self.attach_matching_charts(song["id"])
        self.verify_trusted()
        return added

    def verify_trusted(self) -> None:
        """Auto-verify pending charts whose source the host trusts
        (UPS_TRUST_SOURCES). Applies to rescan and on-demand fetches."""
        if self.trusted_sources:
            marks = ",".join("?" * len(self.trusted_sources))
            self.db.execute(
                f"UPDATE chart SET verified=1, confidence=1.0"
                f" WHERE verified=0 AND source_repo IN ({marks})",
                tuple(self.trusted_sources))

    def index_chart_file(self, path: Path, source_repo: str | None = None) -> bool:
        """Index one .txt chart into chart_library (no-op if unparsable)."""
        path = Path(path)
        chart = load_chart(path)
        if chart.validate() or not (chart.artist and chart.title):
            return False
        key = str(path.resolve())
        src = source_repo or ("usdx-songs" if "usdx-songs" in key
                              else "usdb" if "usdb" in key else "local-drop")
        self.db.execute(
            "INSERT OR IGNORE INTO chart_library"
            " (file_path,artist,title,artist_norm,title_norm,is_duet,source_repo)"
            " VALUES (?,?,?,?,?,?,?)",
            (key, chart.artist, chart.title,
             _norm(chart.artist), _norm(chart.title),
             int(chart.is_duet), src))
        return True

    def attach_matching_charts(self, song_id: int) -> int:
        """Attach unverified chart rows for any library charts matching this
        song. Called on rescan and whenever a new song/media entry appears."""
        song = self.db.query(
            "SELECT id, artist, title, is_duet FROM song WHERE id=?", (song_id,))
        if not song:
            return 0
        song = song[0]
        matches = self.db.query(
            "SELECT * FROM chart_library WHERE artist_norm=? AND title_norm=?",
            (_norm(song["artist"]), _norm(song["title"])))
        entries = self.db.query(
            "SELECT id, status FROM media_entry WHERE song_id=? ORDER BY id",
            (song_id,))
        entry_id = None
        if len(entries) == 1:
            entry_id = entries[0]["id"]
        elif entries:
            avail = [e for e in entries if e["status"] == "available"]
            entry_id = avail[0]["id"] if len(avail) == 1 else None
        attached = 0
        for lib in matches:
            if self.db.query(
                    "SELECT 1 FROM chart WHERE song_id=? AND file_path=?",
                    (song_id, lib["file_path"])):
                continue
            trusted = lib["source_repo"] in self.trusted_sources
            self.db.execute(
                "INSERT INTO chart (song_id,media_entry_id,source_repo,file_path,"
                " is_duet,verified,confidence) VALUES (?,?,?,?,?,?,?)",
                (song_id, entry_id, lib["source_repo"], lib["file_path"],
                 lib["is_duet"], int(trusted), 1.0 if trusted else 0.5))
            attached += 1
            if lib["is_duet"] and not song["is_duet"]:
                self.db.execute(
                    "UPDATE song SET is_duet=1, tracks='[\"P1\",\"P2\"]' WHERE id=?",
                    (song_id,))
        return attached

    def verify_chart(self, chart_id: int, media_entry_id: int,
                     sync_offset_ms: int = 0) -> bool:
        """Host marks a candidate chart as verified against a specific
        media entry — this is what turns scoring on for that video."""
        n = self.db.execute(
            "UPDATE chart SET verified=1, media_entry_id=?, sync_offset_ms=?,"
            " confidence=1.0 WHERE id=?", (media_entry_id, sync_offset_ms, chart_id))
        return bool(n)

    def pending_charts(self) -> list[dict]:
        return self.db.query(
            "SELECT c.*, s.artist, s.title FROM chart c"
            " JOIN song s ON s.id=c.song_id WHERE c.verified=0")
