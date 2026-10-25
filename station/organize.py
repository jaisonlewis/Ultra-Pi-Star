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


def _media_root(root: Path) -> Path:
    """Pointed at a drive/folder root instead of the media dir — drop
    into the `ultrapistar/` subdir (or any dir holding manifest.json)
    so users can aim the tool at a whole stick."""
    if (root / "manifest.json").is_file():
        return root
    for cand in (root / "ultrapistar",):
        if (cand / "manifest.json").is_file():
            return cand
    try:
        for d in sorted(root.iterdir()):
            if d.is_dir() and (d / "manifest.json").is_file():
                return d
    except OSError:
        pass
    return root


def organize_root(root: Path, dry: bool = False) -> dict:
    root = _media_root(Path(root))
    doc = bundle.load_manifest(root)
    by_id = {s.get("source_id"): s for s in doc.get("songs", [])
             if s.get("source_id")}
    known_files = {Path(s.get("local_path") or "").name
                   for s in doc.get("songs", [])}
    known_stems = {Path(s.get(k) or "").stem
                   for s in doc.get("songs", [])
                   for k in ("local_path", "inst_path")} - {""}
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

    # strays: any file with no manifest record — dead downloads,
    # format fragments, orphaned sidecars. Group by id token so a
    # song's whole cluster lands in one folder.
    unsorted = 0
    skip_names = {"manifest.json", "pi-catalog.json",
                  ".station-queue.json"}
    done_groups = set()
    for p in sorted(root.iterdir()):
        if not p.is_file() or p.name in skip_names:
            continue
        if (p.name in known_files
                or any(p.name.startswith(s + ".") for s in known_stems)):
            continue                # a manifest song's file/sidecar
        m = re.match(r"(yt-[\w-]{6,}|upload-\d+)", p.name)
        key = m.group(1) if m else p.stem
        if key in done_groups:
            continue
        done_groups.add(key)
        if " - " in p.stem:
            a, t = p.stem.split(" - ", 1)
            dest = root / song_reldir(a, t, p.stem)
        else:
            dest = root / "_unsorted" / key
        names = [q.name for q in root.glob(key + ".*")
                 if q.is_file() and q.name not in skip_names]
        if not dry:
            dest.mkdir(parents=True, exist_ok=True)
            for name in names:
                if (name in known_files or any(
                        name.startswith(s + ".") for s in known_stems)):
                    continue            # belongs to a manifest song
                tgt = dest / name
                tgt.unlink(missing_ok=True)
                shutil.move(str(root / name), tgt)
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
