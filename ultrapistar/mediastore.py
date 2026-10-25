"""Human-readable media layout — 'artist/title/<files>' folders.

The catalog stays flat internally (local_path is a root-relative path);
this module reorganizes the FILES into per-song folders so the media
root is browsable on a file manager — housekeeping on a USB stick
doesn't mean scrolling 400 yt-*.mp4 names. File names keep their unique
stems so two versions of a song simply share the folder. The station
writes the same layout, so a prepared stick arrives already organized.

``organize`` is idempotent and safe to run at boot: same-root moves are
renames, in-use entries are skipped, and entries already in their
folder are left alone. When a root carries a ``manifest.json`` (a
prepared stick), its paths are rewritten to match — the Switch reads
that manifest verbatim.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_WS = re.compile(r"[^\w]+")


def _slug(s: str, fallback: str = "unknown") -> str:
    """lowercase_snake folder component — unicode letters survive
    (日本語 titles keep their characters), everything else folds to _."""
    out = _WS.sub("_", (s or "").strip().lower()).strip("_")
    return (re.sub(r"_+", "_", out)[:60].strip("_") or fallback)


def song_reldir(artist: str, title: str, fallback: str = "") -> str:
    """Root-relative 'artist_slug/title_slug' song directory."""
    a = _slug(artist, "")
    t = _slug(title, _slug(fallback))
    return f"{a or 'unknown'}/{t}"


def song_folder(artist: str, title: str, fallback: str = "") -> str:
    """Legacy 'Artist - Title' single-level name — kept for callers that
    still need a flat folder (chart file naming)."""
    name = _BAD.sub("", f"{artist or ''} - {title or ''}").strip(" .-")
    if not name:
        name = _BAD.sub("", fallback or "").strip(" .-")
    return (name[:80].rstrip(" .") or "unknown")


def _in_use(ctx, entry_id: int) -> bool:
    from .pipeline import _media_in_use
    return _media_in_use(ctx, entry_id)


def _fix_manifest(root: Path, rel_map: dict[str, str]) -> None:
    """Rewrite a prepared root's manifest.json after on-disk moves so
    the Switch (which reads it verbatim) still resolves every path."""
    man = root / "manifest.json"
    if not rel_map or not man.is_file():
        return
    try:
        doc = json.loads(man.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    changed = False
    for rec in doc.get("songs") or []:
        for key in ("local_path", "inst_path", "chart"):
            old = rec.get(key) or ""
            if old in rel_map:
                rec[key] = rel_map[old]
                changed = True
        # stem-keyed sidecars: a chart filed under the old folder name
        # also shifts when its sibling media moved into a new dir
        ch = rec.get("chart") or ""
        lp = rec.get("local_path") or ""
        if ch and lp and "/" in lp:
            d = lp.rsplit("/", 1)[0]
            if ch.rsplit("/", 1)[0] != d and (root / d
                                             / Path(ch).name).is_file():
                rec["chart"] = f"{d}/{Path(ch).name}"
                changed = True
    if changed:
        tmp = man.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(man)


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
    root_moves: dict[Path, dict[str, str]] = {}
    for r in rows:
        rel = Path(r["local_path"])
        folder = song_reldir(r["artist"], r["title"], rel.stem)
        if str(rel.parent).replace("\\", "/") == folder:
            continue                          # already organized
        src = cfg.media_path(rel)
        if not src.exists() or _in_use(ctx, r["id"]):
            skipped += 1
            continue
        # stay on the root the file is on — never copy across roots
        root = next((ro for ro in cfg.media_roots()
                     if (ro / rel).exists()), Path(cfg.media_dir))
        dest = root / folder
        try:
            dest.mkdir(parents=True, exist_ok=True)
            moved_names = {p.name for p in src.parent.glob(rel.stem + ".*")
                           if p.is_file()}
            # sibling .upsong charts ride along — a one-song folder
            # carries its own chart for the Switch's folder scan
            moved_names.update(p.name for p in src.parent.glob("*.upsong")
                               if p.is_file())
            for name in moved_names:
                p = src.parent / name
                if not (dest / name).exists():
                    p.rename(dest / name)
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
        root_moves.setdefault(root, {})[
            str(rel).replace("\\", "/")] = f"{folder}/{rel.name}"
        if inst and new_inst != inst:
            root_moves[root][inst] = new_inst
        for name in moved_names:
            if name.endswith(".upsong"):
                old = str(rel.parent / name).replace("\\", "/")
                if rel.parent == Path("."):
                    old = name
                root_moves[root].setdefault(old, f"{folder}/{name}")
        moved += 1
    for root, rel_map in root_moves.items():
        try:
            _fix_manifest(root, rel_map)
        except OSError:
            pass                          # manifest fixup is best-effort
    return {"moved": moved, "skipped": skipped}
