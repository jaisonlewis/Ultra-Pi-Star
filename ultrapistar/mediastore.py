"""Human-readable media layout — 'Artist - Title/<files>' folders.

The catalog stays flat internally (local_path is a root-relative path);
this module reorganizes the FILES into per-song folders so the media
root is browsable on a file manager — housekeeping on a USB stick
doesn't mean scrolling 400 yt-*.mp4 names. File names keep their unique
stems so two versions of a song simply share the folder.

``organize`` is idempotent and safe to run at boot: same-root moves are
renames, in-use entries are skipped, and entries already in their
folder are left alone.
"""
from __future__ import annotations

import re
from pathlib import Path

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def song_folder(artist: str, title: str, fallback: str = "") -> str:
    """Filesystem-safe 'Artist - Title' folder name (≤80 chars)."""
    name = _BAD.sub("", f"{artist or ''} - {title or ''}").strip(" .-")
    if not name:
        name = _BAD.sub("", fallback or "").strip(" .-")
    return (name[:80].rstrip(" .") or "unknown")


def _in_use(ctx, entry_id: int) -> bool:
    from .pipeline import _media_in_use
    return _media_in_use(ctx, entry_id)


def organize(ctx) -> dict:
    """Move every cataloged media file + its sidecars into the song's
    folder on whichever root the file already lives, then rewrite
    local_path/inst_path to match. Returns {moved, skipped}."""
    db, cfg = ctx["db"], ctx["config"]
    rows = db.query(
        "SELECT e.id, e.local_path, e.inst_path, s.artist, s.title"
        " FROM media_entry e JOIN song s ON s.id=e.song_id"
        " WHERE e.local_path IS NOT NULL AND e.local_path != ''")
    moved = skipped = 0
    for r in rows:
        rel = Path(r["local_path"])
        if rel.parent.name == song_folder(
                r["artist"], r["title"], rel.stem):
            continue                          # already organized
        src = cfg.media_path(rel)
        if not src.exists() or _in_use(ctx, r["id"]):
            skipped += 1
            continue
        # stay on the root the file is on — never copy across roots
        root = next((ro for ro in cfg.media_roots()
                     if (ro / rel).exists()), Path(cfg.media_dir))
        folder = song_folder(r["artist"], r["title"], rel.stem)
        dest = root / folder
        try:
            dest.mkdir(parents=True, exist_ok=True)
            for p in src.parent.glob(rel.stem + ".*"):
                if p.is_file() and not (dest / p.name).exists():
                    p.rename(dest / p.name)
        except OSError:
            skipped += 1
            continue
        try:
            src.parent.rmdir()   # empty shell of an old folder layout
        except OSError:
            pass               # media root or still populated — fine
        inst = r["inst_path"] or ""
        new_inst = (f"{folder}/{Path(inst).name}"
                    if inst and (dest / Path(inst).name).exists()
                    else inst)
        db.execute(
            "UPDATE media_entry SET local_path=?, inst_path=? WHERE id=?",
            (f"{folder}/{rel.name}", new_inst, r["id"]))
        moved += 1
    return {"moved": moved, "skipped": skipped}
