"""Identify _unsorted media by YouTube id and optionally requeue it.

Every quarantined cluster is ``_unsorted/yt-<id>/`` — the id IS the
identity, so no fingerprinting is needed: YouTube's keyless oEmbed
endpoint returns the original title and uploader for every video that
still exists. Results cache to ``.identify-cache.json`` so reruns are
instant.

    python -m station.identify <root>            # list what's there
    python -m station.identify <root> --process  # requeue the keepers

--process appends job records to .station-queue.json with local_file
pointing at the existing mp4 — the station's router sends those
straight to the GPU pipeline (no re-download), and finished songs land
in artist/title/ folders like any normal run. Entries matching an
existing manifest id, an already-known song, the download queue, or
the prune rules (album/live) are skipped.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

_OEMBED = ("https://www.youtube.com/oembed?url="
           "https://www.youtube.com/watch?v={}&format=json")
_CACHE = ".identify-cache.json"
_UA = {"User-Agent": "UltraPiStar/1.0"}


def _oembed(vid: str) -> dict | None:
    try:
        req = urllib.request.Request(_OEMBED.format(vid), headers=_UA)
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _clusters(root: Path):
    un = root / "_unsorted"
    if not un.is_dir():
        return
    for d in sorted(un.iterdir()):
        if not d.is_dir() or not d.name.startswith("yt-"):
            continue
        media = d / (d.name + ".mp4")
        if not media.is_file() or media.stat().st_size < 1_000_000:
            media = next((p for p in d.iterdir()
                          if p.suffix.lower() == ".mp4"
                          and p.stat().st_size >= 1_000_000), None)
        if media:
            yield d.name[3:], media


def identify(root: Path, delay: float = 0.15) -> dict:
    """→ {vid: {"author", "title", "dead"}} for every cluster —
    oEmbed results cached between runs."""
    root = Path(root)
    cache_path = root / _CACHE
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    out = {}
    pending = 0
    for vid, _ in _clusters(root):
        if vid in cache:
            out[vid] = cache[vid]
            continue
        d = _oembed(vid)
        out[vid] = {"dead": d is None,
                    "author": (d or {}).get("author_name") or "",
                    "title": (d or {}).get("title") or ""}
        cache[vid] = out[vid]
        pending += 1
        if pending % 50 == 0:                # crash-safe incremental save
            cache_path.write_text(json.dumps(cache, ensure_ascii=False),
                                  encoding="utf-8")
        time.sleep(delay)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False),
                          encoding="utf-8")
    return out


def requeue(root: Path, dry: bool = False, delay: float = 0.15,
            enqueue=None) -> dict:
    """Identify → parse → dedupe → enqueue the keepers.

    ``enqueue`` receives each job rec — pass ``station.add`` from a
    running Station so jobs route into the live pipeline. Without it
    (CLI use with the station closed) recs append to the persisted
    queue file — NEVER append externally while a station runs: its
    ``_persist()`` rewrites the file from memory and clobbers them."""
    from . import bundle
    from .fixmeta import _split_video_title, _tidy
    from .jobs import _is_known_song
    from .prune import _ALBUM, _LIVE

    root = Path(root)
    ids = identify(root, delay=delay)
    known = bundle.known_ids(root)
    pairs = bundle.known_songs(root)
    qfile = root / ".station-queue.json"
    try:
        queue_recs = json.loads(qfile.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        queue_recs = []
    queued = {r.get("source_id") for r in queue_recs}

    stats = {"queued": 0, "dup_song": 0, "dup_id": 0, "junk": 0,
             "dead": 0, "unparsed": 0}
    for vid, info in ids.items():
        if info.get("dead"):
            stats["dead"] += 1
            continue
        if vid in known or vid in queued:
            stats["dup_id"] += 1
            continue
        raw_title = info["title"]
        parsed = _split_video_title(raw_title)
        artist, title = parsed if parsed else ("", _tidy(raw_title))
        blob = f"{artist} {title} {raw_title}"
        if _ALBUM.search(blob) or _LIVE.search(blob):
            stats["junk"] += 1
            continue
        if not title:
            stats["unparsed"] += 1
            continue
        rec = {"provider": "youtube", "source_id": vid,
               "artist": artist, "title": title,
               "duration_sec": None,
               "thumbnail": "",
               "local_file": "",
               "local_path": ""}
        if _is_known_song(rec, pairs):
            stats["dup_song"] += 1
            continue
        if not dry:
            # drain the quarantine: media moves to incomplete/ where the
            # pipeline picks it up — the emptied cluster dir goes away
            grp = root / "_unsorted" / f"yt-{vid}"
            media = next(grp.glob("*.mp4"))
            work = root / "incomplete"
            work.mkdir(exist_ok=True)
            tgt = work / media.name
            tgt.unlink(missing_ok=True)
            media.rename(tgt)
            for leftover in grp.iterdir():
                leftover.unlink(missing_ok=True)
            grp.rmdir()
            rec["local_file"] = str(tgt)
            rec["local_path"] = f"incomplete/{tgt.name}"
            if enqueue:
                enqueue(rec)
            else:
                queue_recs.append(rec)
            queued.add(vid)
        stats["queued"] += 1
        pairs.append((artist, title))      # dedupe within this pass too
    if stats["queued"] and not dry and not enqueue:
        qfile.write_text(json.dumps(queue_recs, ensure_ascii=False),
                         encoding="utf-8")
    return stats


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m station.identify")
    ap.add_argument("root", type=Path)
    ap.add_argument("--process", action="store_true",
                    help="append identified songs to the station queue")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--delay", type=float, default=0.15)
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if not args.process and not args.dry_run:
        ids = identify(args.root, delay=args.delay)
        alive = [(v, i) for v, i in ids.items() if not i.get("dead")]
        dead = [v for v, i in ids.items() if i.get("dead")]
        for v, i in alive:
            print(f'  {v}  {i["author"]}  |  {i["title"]}')
        print(f"{len(alive)} identified, {len(dead)} dead/removed")
        return
    stats = requeue(args.root, dry=args.dry_run, delay=args.delay)
    print(("would queue" if args.dry_run else "queued"), stats["queued"],
          "| dup-song:", stats["dup_song"], "| dup-id:", stats["dup_id"],
          "| album/live junk:", stats["junk"],
          "| dead videos:", stats["dead"],
          "| unparsed:", stats["unparsed"])


if __name__ == "__main__":
    main()
