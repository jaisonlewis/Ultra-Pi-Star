"""Regenerate .upsong charts from cached analysis sidecars.

After a genchart change, a prepared library doesn't need the GPU
stages re-run — every ``*.analysis.json`` still holds the ASR
transcript and the RMVPE pitch curve that were captured the first
time. Recharting re-fetches timed lyrics, re-picks the best record,
and re-runs ``genchart.generate()`` on the original evidence — all
CPU, roughly a second or two per song.

Songs whose regeneration fails keep their existing chart untouched;
the tool reports what it changed and what it skipped.

    python -m station.rechart I:/ups-flash/ultrapistar
    python -m station.rechart <root> --only "Little Things" --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("rechart")


def _manifest_songs(root: Path) -> list[dict]:
    m = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    songs = m.get("songs") if isinstance(m, dict) else m
    return songs or []


def _curve(doc: dict):
    p = doc.get("pitch") or {}
    f0 = np.asarray(p.get("f0") or [], dtype=float)
    conf = np.asarray(p.get("conf") or [], dtype=float)
    if not len(f0):
        return None
    hop = float(p.get("hop_ms") or 0.01)   # stored as seconds despite the name
    return np.arange(len(f0)) * hop, f0, conf


def romanize_library(root: Path, only: str = "",
                     dry_run: bool = False) -> dict:
    """Fill Note.alt on every .upsong under charts/ that needs it —
    works even on charts whose analysis sidecar is gone, since the
    source text lives in the file itself."""
    from ultrapistar import romanize
    from ultrapistar.charts import load_chart, save_native

    stats = {"ok": 0, "skipped": 0, "err": 0}
    seen = set()
    cands = sorted((root / "charts").glob("*.upsong")) \
        + sorted(root.rglob("*.upsong"))
    for path in cands:
        if path in seen:
            continue
        seen.add(path)
        if only and only.lower() not in path.stem.lower():
            continue
        try:
            chart = load_chart(path)
            if not any(n.alt for notes in chart.tracks.values()
                       for n in notes):
                if dry_run:
                    stats["ok"] += 1
                    log.info("would romanize %s", path.name)
                    continue
                if romanize.romanize_chart(chart):
                    save_native(chart, path, source=chart.source)
                    stats["ok"] += 1
                    log.info("romanized %s", path.name)
                    continue
            stats["skipped"] += 1
        except Exception as e:
            stats["err"] += 1
            log.warning("romanize failed for %s: %s", path.name, e)
    return stats


def _chart_has_lrc(path: Path) -> bool:
    """Does the existing .upsong already carry provider lyrics?
    ASR-fallback charts have source.lyrics unset — they're the ones
    --asr wants to retry now that fetch_all sees more candidates."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8",
                                        errors="replace"))
        src = doc.get("source") or {}
        return bool(src.get("lyrics") or src.get("lrc"))
    except (OSError, ValueError):
        return False


def _norm(s: str) -> str:
    import re as _re
    return _re.sub(r"\W+", "", (s or "").lower())


def _meta_ok(artist: str, title: str, la: str, lt: str) -> bool:
    """Should the chosen lyric record's artist/track replace the
    manifest metadata? Guards against two bad shapes:

    - track_name containing ' - ' ("Mötley Crüe - Girls Girls Girls"
      uploaded under MotleyCrueVEVO) — that's a channel-prefixed
      record, not canonical metadata
    - a same-title different song: "Snoop Dogg - I Wanna Rock" must
      not overwrite Twisted Sister's entry. The chosen artist (or,
      when absent, the track) has to appear somewhere in the
      original channel-artist/video-title blob — real fixes look
      like "RHINO - White Lion - Radar Love (...)" → White Lion.
    - Unknown artist / opaque yt-* titles trust the record outright:
      the ASR evidence already confirmed it's the right song, and
      anything beats "Unknown"."""
    if not lt or " - " in lt:
        return False
    # record junk: mojibake, filename-style tracks ("01_-_Song"),
    # track-number prefixes ("23.The Promise"), single-char artists
    # ("P" substring-matches everything)
    if ("\ufffd" in la + lt or "_" in lt
            or re.match(r"^\d+\s*[\._-]", lt)
            or (la and len(_norm(la)) < 3)):
        return False
    if _norm(artist) in ("", "unknown") or title.startswith("yt-"):
        return True
    blob = _norm(artist + " " + title)
    if _norm(la):
        return _norm(la) in blob
    return _norm(lt) in blob


def rechart_song(root: Path, song: dict) -> tuple[str, dict | None]:
    """Regenerate one song's chart from its cached analysis sidecar.
    Returns (status, info) — info carries the chosen lyric record when
    a chart was written, so callers can heal manifest metadata."""
    from ultrapistar import genchart, lyrics
    from ultrapistar.pipeline import _pick_lrc
    from ultrapistar.charts import save_native

    artist, title = song.get("artist") or "", song.get("title") or ""
    label = f"{artist} - {title}"
    local = song.get("local_path")
    chart_rel = song.get("chart")
    if not local or not chart_rel:
        return "skipped", None
    sidecar = (root / Path(local).parent
               / (Path(local).stem + ".analysis.json"))
    if not sidecar.exists():
        log.info("no sidecar: %s", label)
        return "no_sidecar", None
    doc = json.loads(sidecar.read_text(encoding="utf-8",
                                       errors="replace"))
    asr = [(w, float(t)) for w, t in doc.get("asr") or []]
    curve = _curve(doc)
    if len(asr) < 20 or curve is None:
        return "skipped", None
    recs = lyrics.fetch_all(
        artist, title, duration=song.get("duration_sec"))
    rec, scored = _pick_lrc(
        recs, asr, title, media_dur=song.get("duration_sec") or 0)
    meta: dict = {}
    chart = genchart.generate(artist, title, rec, asr, curve,
                              meta_out=meta)
    if chart is None:
        log.info("no chart regenerated — kept old: %s", label)
        return "kept", None
    (root / chart_rel).parent.mkdir(parents=True, exist_ok=True)
    save_native(chart, root / chart_rel, source=chart.source)
    doc["pipeline"] = meta
    doc["lrc_candidates"] = scored
    doc["lrc_chosen"] = ((rec or {}).get("artist_name"),
                         (rec or {}).get("track_name"))
    doc["lrc_src"] = (rec or {}).get("source") or "lrclib"
    doc["chart"] = {"notes": len(chart.tracks["P1"]),
                    "source": chart.source}
    sidecar.write_text(json.dumps(doc, ensure_ascii=False))
    log.info("recharted %s  (%d notes)", label,
             len(chart.tracks["P1"]))
    return "ok", {"rec": rec or {}, "chart": chart}


def rechart(root: Path, only: str = "", dry_run: bool = False,
            delay: float = 0.15, asr_only: bool = False,
            fix_meta: bool = False) -> dict:
    stats = {"ok": 0, "kept": 0, "no_sidecar": 0, "skipped": 0, "err": 0,
             "meta_fixed": 0}
    manifest_path = root / "manifest.json"
    # fix_meta mutates records in place — iterate over the manifest doc
    # itself so the mutated objects are what gets saved at the end
    songs = _manifest_songs(root)
    doc_out = None
    for song in songs:
        artist, title = song.get("artist") or "", song.get("title") or ""
        label = f"{artist} - {title}"
        if only and only.lower() not in label.lower():
            continue
        chart_rel = song.get("chart")
        if asr_only and chart_rel and _chart_has_lrc(root / chart_rel):
            continue                     # provider lyrics — fine
        local = song.get("local_path")
        if not local:
            stats["skipped"] += 1
            continue
        if not chart_rel:
            # never got a chart — synthesize its in-folder path so a
            # late lyric hit can still produce one
            from ultrapistar.mediastore import song_folder
            chart_rel = (str(Path(local).parent) + "/"
                         + song_folder(artist, title, Path(local).stem)
                         + ".upsong").replace("\\", "/")
            song["chart"] = chart_rel
        sidecar = (root / Path(local).parent
                   / (Path(local).stem + ".analysis.json"))
        if not sidecar.exists():
            stats["no_sidecar"] += 1
            log.info("no sidecar: %s", label)
            continue
        if dry_run:
            log.info("would rechart %s", label)
            stats["ok"] += 1
            continue
        try:
            status, info = rechart_song(root, song)
            stats[status] += 1
            # a provider lyric record carries canonical artist/title —
            # heal manifest entries whose artist was the uploader
            # channel and whose title was the raw video title
            if (fix_meta and info and info["rec"].get("track_name")
                    and (info["chart"].source or {}).get("lyrics")):
                la = info["rec"].get("artist_name") or ""
                lt = info["rec"].get("track_name") or ""
                if (_meta_ok(artist, title, la, lt)
                        and (_norm(lt) != _norm(title)
                             or (la and _norm(la) != _norm(artist)))):
                    song["artist"] = la or artist
                    song["title"] = lt
                    # keep the .upsong filename in step with the new
                    # metadata — the dir slug follows on organize
                    ch = root / (song.get("chart") or "")
                    if ch.is_file():
                        from ultrapistar.mediastore import song_folder
                        new_name = (song_folder(song["artist"],
                                                song["title"],
                                                ch.stem) + ".upsong")
                        if new_name != ch.name:
                            tgt = ch.parent / new_name
                            tgt.unlink(missing_ok=True)
                            ch.rename(tgt)
                            song["chart"] = (
                                str(tgt.parent.relative_to(root))
                                .replace("\\", "/") + "/" + new_name)
                    stats["meta_fixed"] += 1
                    log.info("meta: %s → %s - %s", label,
                             song["artist"], song["title"])
                    doc_out = True
        except Exception as e:
            stats["err"] += 1
            log.warning("rechart failed for %s: %s", label, e)
        time.sleep(delay)
    if doc_out:
        from . import bundle as _bundle
        _bundle.save_manifest(
            root, {"songs": songs} if not manifest_path.exists()
            else _patch_manifest(manifest_path, songs))
        log.info("manifest saved (%d metadata fixes)",
                 stats["meta_fixed"])
    return stats


def _patch_manifest(manifest_path: Path, songs: list) -> dict:
    """Manifest may carry extra top-level keys — preserve them."""
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    if isinstance(doc, dict):
        doc["songs"] = songs
        return doc
    return {"songs": songs}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="station.rechart")
    ap.add_argument("root", type=Path,
                    help="prepared library root (contains manifest.json)")
    ap.add_argument("--only", default="",
                    help="only songs whose 'Artist - Title' contains this")
    ap.add_argument("--romanize", action="store_true",
                    help="skip chart regen — just fill Note.alt romanized "
                         "alternates on existing .upsong files (offline)")
    ap.add_argument("--asr", dest="asr_only", action="store_true",
                    help="only retry charts whose lyrics were ASR-"
                         "generated (no provider lyrics used)")
    ap.add_argument("--fix-meta", action="store_true",
                    help="adopt the chosen lyric record's canonical "
                         "artist/title into the manifest (then run "
                         "organize to rename dirs)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--delay", type=float, default=0.15,
                    help="seconds between songs (lyrics fetch pacing)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.romanize:
        stats = romanize_library(args.root, only=args.only,
                                 dry_run=args.dry_run)
        print("romanize:", stats)
        return 0
    stats = rechart(args.root, only=args.only, dry_run=args.dry_run,
                    delay=args.delay, asr_only=args.asr_only,
                    fix_meta=args.fix_meta)
    print("rechart:", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
