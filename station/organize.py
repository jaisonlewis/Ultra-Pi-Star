"""Relocate a flat prepared root into per-song folders.

``python -m station.organize <root> [--dry-run]``

The original flat layout scattered yt-<id>.* across the root. This
moves each song's files — media, .inst backing, vocals stem, analysis
sidecar, artwork, .normed markers — plus its generated chart into

    <root>/<artist_slug>/<title_slug>/

and rewrites manifest.json paths to match, so the Pi's importer and the
Switch's scanner resolve identically. Files with no manifest entry land
in ``_unsorted/<stem>/``. Idempotent — already-organized songs skip.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from ultrapistar.mediastore import song_reldir
from . import bundle


def _stem_of(local_path: str) -> str:
    return Path(local_path).stem


def organize_root(root: Path, dry: bool = False) -> dict:
    root = Path(root)
    doc = bundle.load_manifest(root)
    by_id = {s.get("source_id"): s for s in doc.get("songs", [])
             if s.get("source_id")}
    known_files = {Path(s.get("local_path") or "").name
                   for s in doc.get("songs", [])}
    moved = skipped = 0
    for rec in doc["songs"]:
        local = rec.get("local_path") or ""
        if not local:
            skipped += 1
            continue
        rel = Path(local)
        reldir = song_reldir(rec.get("artist", ""), rec.get("title", ""),
                             rel.stem)
        if str(rel.parent).replace("\\", "/") == reldir:
            skipped += 1
            continue                    # already in its song dir
        src = root / rel
        if not src.is_file():
            # interrupted prior run may have moved the files but left
            # the manifest pointing at the old flat path — heal it
            if (root / reldir / rel.name).is_file():
                rec["local_path"] = f"{reldir}/{rel.name}"
                if rec.get("inst_path"):
                    rec["inst_path"] = \
                        f"{reldir}/{Path(rec['inst_path']).name}"
                if rec.get("chart"):
                    rec["chart"] = f"{reldir}/{Path(rec['chart']).name}"
                if rec.get("art"):
                    rec["art"] = f"{reldir}/{Path(rec['art']).name}"
                moved += 1
            else:
                skipped += 1
            continue
        dest = root / reldir
        names = ([p.name for p in src.parent.glob(rel.stem + ".*")
                  if p.is_file()])
        # the generated chart ships inside the song folder too
        ch = rec.get("chart") or ""
        ch_path = root / ch if ch else None
        ch_name = ""
        if ch_path and ch_path.is_file() and ch_path.parent != dest:
            ch_name = ch_path.name
            names.append(ch_name)
        if not dry:
            dest.mkdir(parents=True, exist_ok=True)
            for name in names:
                p = (ch_path.parent if name == ch_name
                     else src.parent) / name
                if not p.is_file():
                    continue
                tgt = dest / name
                tgt.unlink(missing_ok=True)
                shutil.move(str(p), tgt)
        rec["local_path"] = f"{reldir}/{rel.name}"
        if rec.get("inst_path"):
            rec["inst_path"] = f"{reldir}/{Path(rec['inst_path']).name}"
        if ch:
            rec["chart"] = f"{reldir}/{Path(ch).name}"
        if rec.get("art"):
            rec["art"] = f"{reldir}/{Path(rec['art']).name}"
        moved += 1

    # strays: media files with no manifest record — can't name them,
    # but at least gather each under its own folder
    unsorted = 0
    media_re = re.compile(r"\.(mp4|mp3|m4a|webm|mkv)$", re.I)
    for p in sorted(root.iterdir()):
        if not (p.is_file() and media_re.search(p.name)):
            continue
        if ".inst." in p.name or p.name.endswith(".vocals.wav"):
            continue                # derived sidecar, follows its media
        tail = p.stem.split(".")[-1]
        if tail.startswith("f") and tail[1:].isdigit():
            continue                # yt-dlp format fragment — junk
        sid = p.stem[3:] if p.stem.startswith("yt-") else p.stem
        if sid in by_id or p.name in known_files:
            continue                # manifest song (already handled)
        dest = root / "_unsorted" / p.stem
        names = [q.name for q in p.parent.glob(p.stem + ".*")
                 if q.is_file()]
        if not dry:
            dest.mkdir(parents=True, exist_ok=True)
            for name in names:
                shutil.move(str(p.parent / name), dest / name)
        unsorted += 1

    if not dry:
        bundle.save_manifest(root, doc)
    return {"organized": moved, "skipped": skipped, "unsorted": unsorted}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m station.organize")
    ap.add_argument("root", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    out = organize_root(args.root, dry=args.dry_run)
    print(("would organize" if args.dry_run else "organized"),
          out["organized"], "songs,",
          out["skipped"], "skipped,",
          out["unsorted"], "unsorted")


if __name__ == "__main__":
    main()
