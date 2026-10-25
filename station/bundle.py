"""Bundle output: manifest bookkeeping, prepared-drive discovery, LAN push.

A prepared media root is any directory containing ``manifest.json`` —
or a USB stick root carrying an ``ultrapistar/`` folder or a
``.upsmedia`` marker (the same convention the Pi's boot-time detector
uses). The station writes per-song folders into that root:

    <root>/<artist>/<title>/yt-<id>.mp4        merged video+audio (normed)
    <root>/<artist>/<title>/yt-<id>.inst.mp4   karaoke backing (normed)
    <root>/<artist>/<title>/yt-<id>.jpg        album art
    <root>/<artist>/<title>/yt-<id>.vocals.wav 16k vocal stem
    <root>/<artist>/<title>/yt-<id>.analysis.json  pipeline evidence
    <root>/<artist>/<title>/<name>.upsong      generated chart
    <root>/manifest.json                       every finished song
"""
from __future__ import annotations

import io
import json
import os
import re
import zipfile
from pathlib import Path

MANIFEST = "manifest.json"


def load_manifest(root: Path) -> dict:
    p = Path(root) / MANIFEST
    if not p.is_file():
        return {"format": 1, "songs": []}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc.setdefault("songs", [])
        return doc
    except (OSError, ValueError):
        return {"format": 1, "songs": []}


def save_manifest(root: Path, doc: dict) -> None:
    p = Path(root) / MANIFEST
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(p)


def append_song(root: Path, rec: dict) -> None:
    doc = load_manifest(root)
    doc["songs"] = [s for s in doc["songs"]
                    if s.get("source_id") != rec.get("source_id")]
    doc["songs"].append(rec)
    save_manifest(root, doc)


def known_ids(root: Path) -> set:
    return {s.get("source_id") for s in load_manifest(root)["songs"]}


def known_songs(root: Path) -> list[tuple[str, str]]:
    """(artist, title) pairs already in the manifest — fuzzy song-level
    dedup for re-uploads whose source_id differs."""
    return [(s.get("artist") or "", s.get("title") or "")
            for s in load_manifest(root)["songs"] if s.get("title")]


def is_prepared_root(path: Path) -> bool:
    p = Path(path)
    return ((p / "ultrapistar").is_dir()
            or (p / ".upsmedia").exists()
            or (p / MANIFEST).is_file())


def media_root_of(stick: Path) -> Path:
    """A prepared stick's media root — the ultrapistar dir the Pi's
    detector mounts, or the stick root when only a marker exists."""
    inner = Path(stick) / "ultrapistar"
    if inner.is_dir():
        return inner
    inner.mkdir(parents=True, exist_ok=True)   # .upsmedia → adopt root
    return inner


def find_prepared_drives() -> list[Path]:
    """Removable-drive roots ready to receive bundles. Windows scans
    drive letters; POSIX scans the usual mount bases."""
    found = []
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase[2:]:   # skip A:/B:
            root = Path(f"{letter}:/")
            try:
                if root.is_dir() and is_prepared_root(root):
                    found.append(media_root_of(root))
            except OSError:
                continue
    else:
        for base in (Path("/media"), Path("/run/media"), Path("/mnt")):
            if not base.is_dir():
                continue
            try:
                for u in base.iterdir():
                    try:
                        for m in ([u, *u.iterdir()] if u.is_dir() else [u]):
                            if is_prepared_root(m):
                                found.append(media_root_of(m))
                    except OSError:
                        continue
            except OSError:
                continue
    return found


def build_zip(rec: dict, root: Path, chart_path: Path | None) -> bytes:
    """One song's bundle: media + sidecars + chart + manifest.

    The zip stays flat (the Pi's unpack_bundle flattens anyway and
    re-organizes into artist/title dirs on its own root), so manifest
    paths in the bundle are basenames even when ``rec`` carries nested
    ones."""
    root = Path(root)
    media_rel = Path(rec["local_path"])
    names = {media_rel.name}
    names.update(p.name for p in (root / media_rel.parent)
                 .glob(media_rel.stem + ".*"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(names):
            p = root / media_rel.parent / name
            if p.is_file():
                z.write(p, name)
        if chart_path and Path(chart_path).is_file():
            z.write(chart_path, "chart.upsong")
        flat = {**rec, "local_path": media_rel.name,
                "inst_path": (Path(rec["inst_path"]).name
                              if rec.get("inst_path") else ""),
                "art": (Path(rec["art"]).name if rec.get("art") else ""),
                "chart": "chart.upsong" if chart_path else ""}
        man = {"format": 1, "songs": [flat]}
        z.writestr(MANIFEST, json.dumps(man, ensure_ascii=False))
    return buf.getvalue()


def push_to_pi(rec: dict, root: Path, chart_path: Path | None,
               pi_url: str, host_key: str,
               timeout: int = 300) -> dict:
    """POST one song's bundle to /api/import-bundle. Returns the API
    response dict ({added, skipped, missing})."""
    import requests
    data = build_zip(rec, root, chart_path)
    r = requests.post(
        pi_url.rstrip("/") + "/api/import-bundle",
        files={"bundle": ("bundle.zip", data, "application/zip")},
        headers={"X-Host-Key": host_key}, timeout=timeout)
    r.raise_for_status()
    return r.json()
