"""SQLite persistence layer.

Schema implements the Phase 4 state/storage decisions: canonical song
records, one or more media entries per song, verified chart matches with
per-entry sync offsets, performers, the shared queue and play history.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS song (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    artist        TEXT NOT NULL,
    title         TEXT NOT NULL,
    language      TEXT DEFAULT '',
    is_duet       INTEGER NOT NULL DEFAULT 0,
    tracks        TEXT NOT NULL DEFAULT '["P1"]',
    edition       TEXT DEFAULT '',
    year          INTEGER,
    UNIQUE (artist, title, edition)
);

CREATE TABLE IF NOT EXISTS media_entry (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id       INTEGER NOT NULL REFERENCES song(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    local_path    TEXT,
    duration_sec  REAL,
    status        TEXT NOT NULL DEFAULT 'available',  -- available|downloading|failed
    thumbnail     TEXT DEFAULT '',
    provenance    TEXT DEFAULT '',
    license_note  TEXT DEFAULT '',
    UNIQUE (provider, source_id)
);

-- A chart row records a *candidate or verified* match between a chart file
-- and a specific media entry (Phase 4: sync is stored against entry+chart,
-- not artist/title).
CREATE TABLE IF NOT EXISTS chart (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id        INTEGER NOT NULL REFERENCES song(id) ON DELETE CASCADE,
    media_entry_id INTEGER REFERENCES media_entry(id) ON DELETE CASCADE,
    revision       TEXT DEFAULT '',
    source_repo    TEXT DEFAULT '',
    file_path      TEXT NOT NULL,
    is_duet        INTEGER NOT NULL DEFAULT 0,
    verified       INTEGER NOT NULL DEFAULT 0,
    confidence     REAL NOT NULL DEFAULT 0.0,
    sync_offset_ms INTEGER NOT NULL DEFAULT 0,
    sync_warp      TEXT NOT NULL DEFAULT '[]',
    license_note   TEXT DEFAULT ''
);

-- Lightweight index of every UltraStar .txt found on disk (chart dir +
-- imported libraries like usdx-songs). Library rows hold no song link —
-- they become chart rows once a matching song+media entry exists.
CREATE TABLE IF NOT EXISTS chart_library (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT NOT NULL UNIQUE,
    artist      TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    artist_norm TEXT NOT NULL DEFAULT '',
    title_norm  TEXT NOT NULL DEFAULT '',
    is_duet     INTEGER NOT NULL DEFAULT 0,
    source_repo TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS performer (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    color      TEXT NOT NULL,
    sid        TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS queue_item (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id       INTEGER NOT NULL REFERENCES song(id),
    media_entry_id INTEGER NOT NULL REFERENCES media_entry(id),
    performer_id  INTEGER NOT NULL REFERENCES performer(id),
    performer2_id INTEGER REFERENCES performer(id),
    part_choice   TEXT DEFAULT '',
    position      REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',  -- queued|playing|done|skipped|error
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS play_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_item_id INTEGER REFERENCES queue_item(id),
    song_id       INTEGER NOT NULL,
    performer_id  INTEGER NOT NULL,
    played_at     REAL NOT NULL,
    score         INTEGER
);

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON queue_item(status, position);
CREATE INDEX IF NOT EXISTS idx_song_search ON song(artist, title);
"""

PLAYER_COLORS = [
    "#ff5a5a", "#4da3ff", "#ffd24d", "#5ad27e",
    "#c07bff", "#ff8ac2", "#4dd7d0", "#ffa14d",
]


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def session(self):
        """Yield a connection that commits on success and always closes
        afterwards (sqlite3's own ``with`` only commits — it never closes,
        which locks the file on Windows)."""
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.session() as conn:
            conn.executescript(SCHEMA)
            # lightweight column migrations for existing databases
            cols = {r[1] for r in conn.execute("PRAGMA table_info(media_entry)")}
            if "thumbnail" not in cols:
                conn.execute(
                    "ALTER TABLE media_entry ADD COLUMN thumbnail TEXT DEFAULT ''")
            if "inst_path" not in cols:
                conn.execute(
                    "ALTER TABLE media_entry ADD COLUMN inst_path TEXT"
                    " DEFAULT ''")
            if "meta_track" not in cols:
                # canonical music metadata the provider tagged on the
                # upload (YouTube "music in this video" artist/track) —
                # harvested lazily by the map worker for lyric search
                for col in ("meta_artist", "meta_track", "meta_album"):
                    conn.execute(
                        f"ALTER TABLE media_entry ADD COLUMN {col}"
                        " TEXT DEFAULT ''")
            cols = {r[1] for r in conn.execute("PRAGMA table_info(queue_item)")}
            if "vocal_cut" not in cols:
                # NULL = follow the server default; 0/1 = per-song choice
                conn.execute(
                    "ALTER TABLE queue_item ADD COLUMN vocal_cut INTEGER")
            if "lyrics_rom" not in cols:
                # NULL/0 = native lyric script; 1 = romanized when the
                # chart carries Note.alt alternates
                conn.execute(
                    "ALTER TABLE queue_item ADD COLUMN lyrics_rom INTEGER")
            cols = {r[1] for r in conn.execute("PRAGMA table_info(chart)")}
            if "sync_warp" not in cols:
                conn.execute(
                    "ALTER TABLE chart ADD COLUMN sync_warp TEXT NOT NULL"
                    " DEFAULT '[]'")
            if "has_alt" not in cols:
                # chart carries romanized alternates (Note.alt)
                conn.execute(
                    "ALTER TABLE chart ADD COLUMN has_alt INTEGER NOT NULL"
                    " DEFAULT 0")

    # -- performers ---------------------------------------------------------
    def add_performer(self, name: str, sid: str | None = None) -> dict:
        with self.session() as conn:
            # same name -> same singer: keeps host claims, queue ownership and
            # color stable when a phone session drops and rejoins
            row = conn.execute(
                "SELECT * FROM performer WHERE name=? COLLATE NOCASE",
                (name,)).fetchone()
            if row:
                if sid:
                    conn.execute("UPDATE performer SET sid=? WHERE id=?",
                                 (sid, row["id"]))
                return dict(row)
            count = conn.execute("SELECT COUNT(*) FROM performer").fetchone()[0]
            color = PLAYER_COLORS[count % len(PLAYER_COLORS)]
            cur = conn.execute(
                "INSERT INTO performer (name, color, sid, created_at) VALUES (?,?,?,?)",
                (name, color, sid, time.time()),
            )
            return self.get_performer(cur.lastrowid, conn)

    def get_performer(self, pid: int, conn: sqlite3.Connection | None = None) -> dict:
        own = conn is None
        conn = conn or self.connect()
        try:
            row = conn.execute("SELECT * FROM performer WHERE id=?", (pid,)).fetchone()
            return dict(row) if row else {}
        finally:
            if own:
                conn.close()

    def bind_sid(self, performer_id: int, sid: str) -> None:
        with self.session() as conn:
            conn.execute(
                "UPDATE performer SET sid=? WHERE id=?", (sid, performer_id)
            )

    def performer_by_sid(self, sid: str) -> dict | None:
        with self.session() as conn:
            row = conn.execute(
                "SELECT * FROM performer WHERE sid=? ORDER BY id DESC", (sid,)
            ).fetchone()
            return dict(row) if row else None

    def performers(self) -> list[dict]:
        return self.query(
            "SELECT id,name,color FROM performer ORDER BY name")

    # -- settings ------------------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        rows = self.query("SELECT value FROM setting WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO setting (key,value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))

    # -- per-singer voice FX profiles -----------------------------------------
    # Each performer keeps their own monitor FX (pitch/autotune/polish/
    # wet sends) so the sound follows the singer, not the room.

    def performer_fx(self, performer_id: int) -> dict:
        import json
        raw = self.get_setting(f"performer_fx:{performer_id}", "")
        try:
            return json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            return {}

    def set_performer_fx(self, performer_id: int, fx: dict) -> None:
        import json
        self.set_setting(f"performer_fx:{performer_id}",
                         json.dumps(fx, separators=(",", ":")))

    # -- generic helpers -----------------------------------------------------
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self.session() as conn:
            return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.session() as conn:
            cur = conn.execute(sql, tuple(params))
            return cur.lastrowid or cur.rowcount
