"""Remove non-song entries from a prepared library.

Three kinds of uploads don't belong in a karaoke catalog:

- **full-album rips** — "Green Mind (Full Album)", hour-long
  compilations, playlists and megamixes: not a song, can't be charted
- **live / non-official footage** — concerts, festivals, bootlegs,
  pro-shot rips, teasers: crowd noise and live timing make downloaded
  lyrics useless
- **obscure rips** — charts that fell back to ASR-fabricated lyrics
  because no lyric provider knows the song, plus entries with no real
  metadata ("Unknown - yt-xxx")

Manifest entries are dropped and the song folders moved to
``<root>/_removed/`` — recoverable, and invisible to the manifest
scan and the Switch's two-level folder fallback. Delete ``_removed``
to reclaim the space.

    python -m station.prune <root> [--dry-run]
    python -m station.prune <root> --keep-live --keep-albums --keep-asr
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

_ALBUM = re.compile(
    r"(?i)(full.?album|entire album|whole album|complete albums?|"
    r"discography|all.?in.?one|playlist|megamix|compilation|medley|"
    r"plus early singles|the singles collection|greatest hits|"
    r"anthology|box ?set)")
# "unplugged" and "mtv" are NOT here — MTV Unplugged is an official
# release series, not bootleg footage
_LIVE = re.compile(
    r"(?i)(\(live[^)\]]*[\)\]]|\blive at\b|\blive in\b|"
    r"\blive '?\d{2,4}\b|concert|festival|\btour\b|"
    r"bootleg|pro.?shot|soundboard|on stage|\bgig\b|teaser|"
    r"trailer|rare video)")
_ALBUM_MIN_SEC = 20 * 60        # 20+ min of media is a compilation
_NONSONG_ARTIST = re.compile(r"(?i)^(unknown|youtube|yt-|various)$")


def _chart_is_asr(path: Path) -> bool:
    """Chart exists but its lyrics were ASR-fabricated — providers
    were queried and returned nothing usable."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8",
                                        errors="replace"))
        src = doc.get("source") or {}
        return not src.get("lyrics") and not src.get("lrc")
    except (OSError, ValueError):
        return False


def _reasons(root: Path, song: dict, keep: set) -> list[str]:
    out = []
    artist = (song.get("artist") or "").strip()
    title = (song.get("title") or "").strip()
    blob = f"{artist} {title}"
    dur = song.get("duration_sec") or 0

    if "albums" not in keep and (
            _ALBUM.search(blob) or (dur and dur >= _ALBUM_MIN_SEC)):
        out.append("album/compilation")
    if "live" not in keep and _LIVE.search(blob):
        out.append("live/non-official")
    if "asr" not in keep:
        if _NONSONG_ARTIST.match(artist) or title.startswith("yt-"):
            out.append("no metadata")
        else:
            cp = song.get("chart") or ""
            if (not cp or not (root / cp).is_file()
                    or _chart_is_asr(root / cp)):
                if cp and (root / cp).is_file():
                    out.append("ASR-only lyrics")
    return out


def prune(root: Path, keep: set = frozenset(), dry: bool = False) -> dict:
    root = Path(root)
    doc = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    songs = doc.get("songs") if isinstance(doc, dict) else doc
    removed = []
    for s in list(songs or []):
        why = _reasons(root, s, keep)
        if not why:
            continue
        label = f'{s.get("artist")} - {s.get("title")}'
        removed.append((s, why, label))
        if dry:
            print(f'  [{", ".join(why)}] {label}')
            continue
        lp = s.get("local_path") or ""
        src = root / Path(lp).parent if lp else None
        if src and src.is_dir():
            dest = root / "_removed" / src.relative_to(root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(src), dest)
        songs.remove(s)
    if removed and not dry:
        (root / "manifest.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=1),
            encoding="utf-8")
    return {"removed": len(removed)}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m station.prune")
    ap.add_argument("root", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-albums", action="store_true")
    ap.add_argument("--keep-live", action="store_true")
    ap.add_argument("--keep-asr", action="store_true")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    keep = set()
    if args.keep_albums:
        keep.add("albums")
    if args.keep_live:
        keep.add("live")
    if args.keep_asr:
        keep.add("asr")
    out = prune(args.root, keep=keep, dry=args.dry_run)
    print(("would remove" if args.dry_run else "removed"),
          out["removed"], "songs")


if __name__ == "__main__":
    main()
