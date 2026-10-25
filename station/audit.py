"""Library audit — every manifest song gets a completeness + lyric check.

``python -m station.audit <root> [--json]``

Per song: media present, karaoke backing, vocals stem, analysis
sidecar, chart present + parseable, chart text non-blank, and the
chart's lyric source — ``lyrics: None``/``lrc: False`` means the chart
text is whisper-ASR fabrications rather than downloaded timed lyrics
(LRCLIB/SyncLRC/USDB), which is what the pipeline is supposed to use
first.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import bundle

_MEDIA_EXTS = (".mp4", ".mp3", ".m4a", ".webm", ".mkv")


def _chart_problems(chart_path: Path) -> list[str]:
    """Parse the .upsong — blank text and ASR-generated lyrics flagged."""
    if not chart_path.is_file():
        return ["no chart"]
    try:
        doc = json.loads(chart_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["chart unreadable"]
    notes = [n for tr in doc.get("tracks", {}).values() for n in tr]
    if not notes:
        return ["chart has no notes"]
    out = []
    blanks = sum(1 for n in notes if not (n.get("t") or "").strip())
    if blanks == len(notes):
        out.append("lyrics blank")
    elif blanks > len(notes) // 3:
        out.append(f"{blanks}/{len(notes)} notes blank")
    src = doc.get("source") or {}
    if src.get("type") == "generated" and not src.get("lyrics"):
        out.append("ASR-generated lyrics (no downloaded lyrics used)")
    return out


def audit(root: Path) -> dict:
    root = Path(root)
    doc = bundle.load_manifest(root)
    problems: dict[str, list[str]] = {}
    for rec in doc.get("songs", []):
        label = f"{rec.get('artist','')} - {rec.get('title','')}"
        local = rec.get("local_path") or ""
        media = root / local
        issues = []
        if not local or not media.is_file():
            issues.append("media missing")
            d = root
            stem = ""
        else:
            d, stem = media.parent, media.stem
        if stem:
            if not (d / f"{stem}.inst.mp4").is_file():
                issues.append("no inst")
            if not (d / f"{stem}.vocals.wav").is_file():
                issues.append("no vocals stem")
            if not (d / f"{stem}.analysis.json").is_file():
                issues.append("no analysis sidecar")
            if not any((d / f"{stem}{e}").is_file()
                       for e in (".jpg", ".png", ".jpeg")):
                issues.append("no artwork")
        ch = rec.get("chart") or ""
        issues += _chart_problems((root / ch) if ch else d / "")
        if issues:
            problems[label or local] = issues
    return {"songs": len(doc.get("songs", [])),
            "problem_songs": len(problems), "problems": problems}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m station.audit")
    ap.add_argument("root", type=Path)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    rep = audit(args.root)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return
    print(f"{rep['songs']} songs — {rep['problem_songs']} with problems")
    for label, issues in sorted(rep["problems"].items()):
        print(f"  {label}: {', '.join(issues)}")


if __name__ == "__main__":
    main()
