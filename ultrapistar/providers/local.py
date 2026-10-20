"""Local-files provider: plays media already present in the media dir.

This is the always-cleared fallback provider and the one used for the demo
catalog. Files are served by the app from ``/media/<name>``.
"""
from __future__ import annotations

from pathlib import Path

from .base import PlaybackRef

MIME_BY_EXT = {
    ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".flac": "audio/flac",
    ".ogg": "audio/ogg", ".wav": "audio/wav",
}


class LocalProvider:
    name = "local"

    def __init__(self, media_dir: Path):
        self.media_dir = Path(media_dir)

    def resolve(self, entry: dict) -> PlaybackRef | None:
        rel = entry.get("local_path")
        if not rel:
            return None
        path = self.media_dir / rel
        if not path.is_file():
            return None
        mime = MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")
        return PlaybackRef("file", f"/media/{rel}", mime, entry.get("duration_sec"))
