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


def rechart(root: Path, only: str = "", dry_run: bool = False,
            delay: float = 0.15) -> dict:
    from ultrapistar import genchart, lyrics
    from ultrapistar.pipeline import _pick_lrc
    from ultrapistar.charts import save_native

    stats = {"ok": 0, "kept": 0, "no_sidecar": 0, "skipped": 0, "err": 0}
    for song in _manifest_songs(root):
        artist, title = song.get("artist") or "", song.get("title") or ""
        label = f"{artist} - {title}"
        if only and only.lower() not in label.lower():
            continue
        local = song.get("local_path")
        chart_rel = song.get("chart")
        if not local or not chart_rel:
            stats["skipped"] += 1
            continue
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
            doc = json.loads(sidecar.read_text(encoding="utf-8",
                                               errors="replace"))
            asr = [(w, float(t)) for w, t in doc.get("asr") or []]
            curve = _curve(doc)
            if len(asr) < 20 or curve is None:
                stats["skipped"] += 1
                continue
            recs = lyrics.fetch_all(
                artist, title, duration=song.get("duration_sec"))
            rec, scored = _pick_lrc(
                recs, asr, title, media_dur=song.get("duration_sec") or 0)
            meta: dict = {}
            chart = genchart.generate(artist, title, rec, asr, curve,
                                      meta_out=meta)
            if chart is None:
                stats["kept"] += 1
                log.info("no chart regenerated — kept old: %s", label)
                continue
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
            stats["ok"] += 1
            log.info("recharted %s  (%d notes)", label,
                     len(chart.tracks["P1"]))
        except Exception as e:
            stats["err"] += 1
            log.warning("rechart failed for %s: %s", label, e)
        time.sleep(delay)
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="station.rechart")
    ap.add_argument("root", type=Path,
                    help="prepared library root (contains manifest.json)")
    ap.add_argument("--only", default="",
                    help="only songs whose 'Artist - Title' contains this")
    ap.add_argument("--romanize", action="store_true",
                    help="skip chart regen — just fill Note.alt romanized "
                         "alternates on existing .upsong files (offline)")
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
                    delay=args.delay)
    print("rechart:", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
