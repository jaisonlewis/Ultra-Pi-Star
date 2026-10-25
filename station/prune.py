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

Two guards against misclassification:

- **recognised-release guard** — a song whose analysis sidecar has
  ``lrc_chosen`` (a lyric provider holds a record for that exact
  recording) is exempt from the live/non-official and ASR rules.
  That's how "MTV Unplugged" survives while a concert bootleg doesn't.
- **``<root>/_keep.txt``** — one substring per line matched against
  "artist - title"; matching songs are never pruned.

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


def _recognised_release(root: Path, song: dict) -> bool:
    """A synced-lyric provider carries a record for this exact
    recording — proof it's a real catalog release (MTV Unplugged,
    official live albums, deluxe editions), not bootleg/obscure
    footage. ``lrc_chosen`` lives in the analysis sidecar."""
    lp = song.get("local_path") or ""
    if not lp:
        return False
    try:
        doc = json.loads((root / lp).with_suffix(".analysis.json")
                         .read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return False
    lc = doc.get("lrc_chosen")
    return bool(lc and any(lc))   # [None, None] = no match, not a record


def _keep_patterns(root: Path) -> list[str]:
    """``<root>/_keep.txt`` — one substring per line; a song whose
    "artist - title" contains it is never pruned. '#' = comment.
    Hand-curated protection for edge cases the rules can't see."""
    try:
        return [ln.strip().lower() for ln in
                (root / "_keep.txt").read_text(
                    encoding="utf-8", errors="replace").splitlines()
                if ln.strip() and not ln.startswith("#")]
    except OSError:
        return []


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


def _reasons(root: Path, song: dict, keep: set,
             keep_patterns: list = (), *,
             recognised: bool | None = None) -> list[str]:
    out = []
    artist = (song.get("artist") or "").strip()
    title = (song.get("title") or "").strip()
    blob = f"{artist} {title}"
    label = f"{artist} - {title}".lower()
    dur = song.get("duration_sec") or 0

    if any(k in label for k in keep_patterns):
        return []
    if recognised is None:
        recognised = _recognised_release(root, song)

    # album rules stand regardless of recognition — a 40-min rip is
    # unchartable even if a provider happens to know the first track
    if "albums" not in keep and (
            _ALBUM.search(blob) or (dur and dur >= _ALBUM_MIN_SEC)):
        out.append("album/compilation")
    # title keywords alone can't tell an official live release from
    # a bootleg — only flag when no provider recognises the recording
    if "live" not in keep and not recognised and _LIVE.search(blob):
        out.append("live/non-official")
    if "asr" not in keep and not recognised:
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
    kp = _keep_patterns(root)
    removed = []
    protected = 0
    for s in list(songs or []):
        recognised = _recognised_release(root, s)
        why = _reasons(root, s, keep, kp, recognised=recognised)
        label = f'{s.get("artist")} - {s.get("title")}'
        if not why:
            if dry and recognised:
                saved = _reasons(root, s, keep, kp, recognised=False)
                if saved:
                    protected += 1
                    print(f'  [kept — recognised release, would '
                          f'flag: {", ".join(saved)}] {label}')
            continue
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
    return {"removed": len(removed), "protected": protected}


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
