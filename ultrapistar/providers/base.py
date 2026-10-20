"""Replaceable media-provider interface (Phase 4 decision).

Every catalog media entry names a provider. Providers resolve an entry to a
``PlaybackRef`` the TV player can render. YouTube (or any remote source) is
deliberately *not* implemented — it must be added behind this interface once
the permitted access method is chosen (Phase 2 / Implementation Phase 2).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class PlaybackRef:
    kind: str        # "file" (served locally) | "url" (direct remote URL)
    uri: str         # path under /media/... or absolute URL
    mime: str = "video/mp4"
    duration_sec: float | None = None


class MediaProvider(Protocol):
    name: str

    def resolve(self, entry: dict) -> PlaybackRef | None:
        """Return playback info for a catalog media entry, or None."""
        ...


class SearchableProvider(MediaProvider, Protocol):
    """Optional capability: remote catalog search (e.g. YouTube)."""

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Return remote results shaped like catalog rows:
        {provider, source_id, title, artist, duration_sec, thumbnail}."""
        ...


class DownloadableProvider(MediaProvider, Protocol):
    """Optional capability: ingest a remote entry into the local media dir."""

    def download(self, entry: dict, dest_dir) -> str:
        """Blocking download. Returns the local filename inside dest_dir."""
        ...
