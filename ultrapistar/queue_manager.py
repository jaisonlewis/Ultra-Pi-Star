"""Shared singing queue: ordering, state machine, host controls.

Positions are floats so reordering is a midpoint update — no renumbering.
Exactly one item may be ``playing`` at a time; ``advance()`` moves it to
``done`` (recording play history) and promotes the next queued item.
"""
from __future__ import annotations

import json
import time

from .db import Database

STATUS_QUEUED, STATUS_PLAYING = "queued", "playing"
TERMINAL = ("done", "skipped", "error")

ITEM_SQL = """
SELECT q.id, q.status, q.position, q.part_choice, q.created_at,
       q.song_id, q.performer_id, q.performer2_id, q.vocal_cut, q.lyrics_rom,
       s.artist, s.title, s.is_duet,
       e.provider, e.local_path, e.id AS entry_id, e.status AS entry_status,
       p.name AS singer, p.color AS singer_color,
       p2.name AS singer2, p2.color AS singer2_color
FROM queue_item q
JOIN song s ON s.id = q.song_id
JOIN media_entry e ON e.id = q.media_entry_id
JOIN performer p ON p.id = q.performer_id
LEFT JOIN performer p2 ON p2.id = q.performer2_id
"""


class QueueManager:
    def __init__(self, db: Database):
        self.db = db

    def add(self, song_id: int, entry_id: int, performer_id: int,
            performer2_id: int | None = None, part_choice: str = "",
            vocal_cut: int | None = None,
            lyrics_rom: int | None = None) -> int:
        with self.db.session() as conn:
            top = conn.execute(
                "SELECT COALESCE(MAX(position),0) FROM queue_item WHERE status=?",
                (STATUS_QUEUED,)).fetchone()[0]
            cur = conn.execute(
                "INSERT INTO queue_item (song_id,media_entry_id,performer_id,"
                " performer2_id,part_choice,vocal_cut,lyrics_rom,position,"
                " status,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (song_id, entry_id, performer_id, performer2_id, part_choice,
                 vocal_cut, lyrics_rom, top + 1.0, STATUS_QUEUED, time.time()))
            return cur.lastrowid

    def list(self, include_terminal: bool = False) -> list[dict]:
        where = "" if include_terminal else "WHERE q.status NOT IN ('done','skipped','error')"
        return self.db.query(
            f"{ITEM_SQL} {where} ORDER BY CASE q.status WHEN 'playing' THEN 0 ELSE 1 END, q.position")

    def get(self, item_id: int) -> dict | None:
        rows = self.db.query(f"{ITEM_SQL} WHERE q.id=?", (item_id,))
        return rows[0] if rows else None

    def now_playing(self) -> dict | None:
        rows = self.db.query(f"{ITEM_SQL} WHERE q.status='playing' LIMIT 1")
        return rows[0] if rows else None

    def _positions(self, conn) -> list[tuple[int, float]]:
        return [(r["id"], r["position"]) for r in conn.execute(
            "SELECT id, position FROM queue_item WHERE status=? ORDER BY position",
            (STATUS_QUEUED,)).fetchall()]

    def move(self, item_id: int, direction: str) -> bool:
        """Move a queued item up/down one slot."""
        with self.db.session() as conn:
            order = self._positions(conn)
            idx = next((i for i, (iid, _) in enumerate(order) if iid == item_id), None)
            if idx is None:
                return False
            swap = idx - 1 if direction == "up" else idx + 1 if direction == "down" else None
            if swap is None or swap < 0 or swap >= len(order):
                return False
            (id_a, pos_a), (id_b, pos_b) = order[idx], order[swap]
            conn.execute("UPDATE queue_item SET position=? WHERE id=?", (pos_b, id_a))
            conn.execute("UPDATE queue_item SET position=? WHERE id=?", (pos_a, id_b))
            return True

    def reorder_to(self, item_id: int, new_index: int) -> bool:
        with self.db.session() as conn:
            order = self._positions(conn)
            ids = [i for i, _ in order]
            if item_id not in ids:
                return False
            ids.remove(item_id)
            new_index = max(0, min(new_index, len(ids)))
            ids.insert(new_index, item_id)
            for idx, iid in enumerate(ids):
                conn.execute("UPDATE queue_item SET position=? WHERE id=?",
                             (float(idx + 1), iid))
            return True

    def remove(self, item_id: int) -> bool:
        with self.db.session() as conn:
            cur = conn.execute(
                "UPDATE queue_item SET status='skipped' WHERE id=? AND status=?",
                (item_id, STATUS_QUEUED))
            return cur.rowcount > 0

    def clear(self) -> int:
        with self.db.session() as conn:
            return conn.execute(
                "UPDATE queue_item SET status='skipped' WHERE status=?",
                (STATUS_QUEUED,)).rowcount

    def start_next(self) -> dict | None:
        """Promote the next queued item to playing. Returns it or None."""
        with self.db.session() as conn:
            if conn.execute("SELECT 1 FROM queue_item WHERE status='playing'").fetchone():
                return self.now_playing()
            row = conn.execute(
                "SELECT id FROM queue_item WHERE status=? ORDER BY position LIMIT 1",
                (STATUS_QUEUED,)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE queue_item SET status='playing' WHERE id=?", (row["id"],))
        return self.get(row["id"])

    def finish_current(self, outcome: str = "done", score: int | None = None) -> dict | None:
        """End the current item (done|skipped|error), record history, start next."""
        current = self.now_playing()
        if not current:
            return self.start_next()
        assert outcome in TERMINAL
        with self.db.session() as conn:
            conn.execute("UPDATE queue_item SET status=? WHERE id=?",
                         (outcome, current["id"]))
            conn.execute(
                "INSERT INTO play_history (queue_item_id,song_id,performer_id,played_at,score)"
                " VALUES (?,?,?,?,?)",
                (current["id"], current["song_id"],
                 current["performer_id"], time.time(), score))
        return self.start_next()

    def skip_current(self) -> dict | None:
        return self.finish_current("skipped")

    def stop_current(self) -> None:
        """End the playing item without promoting a next one (session end)."""
        with self.db.session() as conn:
            conn.execute(
                "UPDATE queue_item SET status='skipped' WHERE status='playing'")

    def join_duet(self, item_id: int, performer_id: int) -> dict | None:
        """Claim the open P2 slot on a queued duet. Returns the item or None."""
        with self.db.session() as conn:
            cur = conn.execute(
                "UPDATE queue_item SET performer2_id=?"
                " WHERE id=? AND status IN ('queued','playing')"
                " AND performer2_id IS NULL AND performer_id<>?"
                " AND song_id IN (SELECT id FROM song WHERE is_duet=1)",
                (performer_id, item_id, performer_id))
            if cur.rowcount == 0:
                return None
        return self.get(item_id)

    def snapshot(self) -> dict:
        """Serializable state pushed to all clients."""
        items = self.list()
        playing = self.now_playing()
        return {
            "now_playing": playing,
            "queue": [i for i in items if i["status"] == STATUS_QUEUED],
            "queue_count": sum(1 for i in items if i["status"] == STATUS_QUEUED),
        }
