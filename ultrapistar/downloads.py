"""Background media ingest: downloads a queued entry's file via its provider.

Entry status transitions: queued item waits while ``downloading`` -> file
lands -> ``available`` (or ``failed``). When the currently-playing item's
media becomes ready, the TV is re-pushed its playback payload so a waiting
pre-song screen starts automatically.
"""
from __future__ import annotations

import itertools
import queue as _queue
import threading
import time
from pathlib import Path

from .db import Database
from .providers import ProviderRegistry


class DownloadManager:
    def __init__(self, db: Database, providers: ProviderRegistry, media_dir,
                 emit_state, repush_now_playing, on_available=None,
                 media_extra_dirs: tuple = ()):
        self.db = db
        self.providers = providers
        self.media_dir = media_dir
        self.media_roots = (media_dir, *(Path(d) for d in media_extra_dirs))
        self._emit_state = emit_state
        self._repush = repush_now_playing
        self._on_available = on_available
        self._in_flight: set[int] = set()
        self._lock = threading.Lock()
        # one download at a time: a bulk playlist drop must not spawn
        # hundreds of yt-dlp processes and melt the box
        self._q: _queue.PriorityQueue = _queue.PriorityQueue()
        self._seq = itertools.count()
        threading.Thread(target=self._worker, daemon=True,
                         name="downloads").start()

    def ensure(self, entry: dict) -> str:
        """Queue a download if needed. Returns current status."""
        if entry.get("local_path") and self._file_exists(entry):
            return "available"
        if entry.get("status") == "failed":
            pass  # allow retry
        with self._lock:
            if entry["entry_id"] in self._in_flight:
                return "downloading"
            self._in_flight.add(entry["entry_id"])
        self._set_status(entry["entry_id"], "downloading")
        # whatever the TV is waiting on jumps the line; everything else
        # runs FIFO in the order it was queued
        playing = self.db.query(
            "SELECT media_entry_id FROM queue_item WHERE status='playing'"
            " LIMIT 1")
        prio = 0 if playing and playing[0]["media_entry_id"] == entry["entry_id"] else 1
        self._q.put((prio, next(self._seq), dict(entry)))
        return "downloading"

    def _worker(self) -> None:
        while True:
            _prio, _seq, entry = self._q.get()
            try:
                self._run(entry)
            finally:
                self._q.task_done()

    def _file_exists(self, entry) -> bool:
        return any((d / entry["local_path"]).is_file()
                   for d in self.media_roots)

    def _set_status(self, entry_id: int, status: str,
                    local_path: str | None = None) -> None:
        if local_path:
            self.db.execute(
                "UPDATE media_entry SET status=?, local_path=? WHERE id=?",
                (status, local_path, entry_id))
        else:
            self.db.execute("UPDATE media_entry SET status=? WHERE id=?",
                            (status, entry_id))
        self._emit_state()

    def _run(self, entry: dict) -> None:
        entry_id = entry["entry_id"]
        try:
            provider = self.providers.get(entry["provider"])
            if not provider or not hasattr(provider, "download"):
                raise RuntimeError(f"provider '{entry['provider']}' cannot download")
            filename = provider.download(entry, self.media_dir)
            self.db.execute(
                "UPDATE media_entry SET status='available', local_path=?, provenance=? WHERE id=?",
                (filename, f"downloaded via {entry['provider']} "
                           f"{time.strftime('%Y-%m-%dT%H:%M:%S')}", entry_id))
            self._emit_state()
            # start the map pipeline BEFORE repushing — otherwise the push
            # lands while map_inflight is still empty, the TV takes the
            # playable path and the countdown starts while stems/lyrics/
            # normalization are still running.
            if self._on_available:
                self._on_available(entry_id)
            self._repush(entry_id)
        except Exception as exc:
            # best-effort: a daemon thread can outlive the app/db during
            # shutdown — notifications are advisory, don't crash on them
            try:
                self.db.execute(
                    "UPDATE media_entry SET status='failed', provenance=? WHERE id=?",
                    (f"download error: {exc}"[:200], entry_id))
                self._emit_state()
                self._repush(entry_id)
            except Exception:
                pass
        finally:
            with self._lock:
                self._in_flight.discard(entry_id)
