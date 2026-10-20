"""Background media ingest: downloads a queued entry's file via its provider.

Entry status transitions: queued item waits while ``downloading`` -> file
lands -> ``available`` (or ``failed``). When the currently-playing item's
media becomes ready, the TV is re-pushed its playback payload so a waiting
pre-song screen starts automatically.
"""
from __future__ import annotations

import threading
import time

from .db import Database
from .providers import ProviderRegistry


class DownloadManager:
    def __init__(self, db: Database, providers: ProviderRegistry, media_dir,
                 emit_state, repush_now_playing, on_available=None):
        self.db = db
        self.providers = providers
        self.media_dir = media_dir
        self._emit_state = emit_state
        self._repush = repush_now_playing
        self._on_available = on_available
        self._in_flight: set[int] = set()
        self._lock = threading.Lock()

    def ensure(self, entry: dict) -> str:
        """Start a download if needed. Returns current status."""
        if entry.get("local_path") and self._file_exists(entry):
            return "available"
        if entry.get("status") == "failed":
            pass  # allow retry
        with self._lock:
            if entry["entry_id"] in self._in_flight:
                return "downloading"
            self._in_flight.add(entry["entry_id"])
        self._set_status(entry["entry_id"], "downloading")
        threading.Thread(target=self._run, args=(dict(entry),), daemon=True).start()
        return "downloading"

    def _file_exists(self, entry) -> bool:
        from pathlib import Path
        return (Path(self.media_dir) / entry["local_path"]).is_file()

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
