"""Ingest of station-prepared bundles.

A prep station (GPU workstation) produces media + derived files +
charts ahead of time. Two delivery paths land here:

* Sneakernet — a prepared USB stick whose media root carries a
  ``manifest.json``; ``scan_roots`` registers every song in it.
* LAN push — ``POST /api/import-bundle`` unpacks a per-song zip into a
  staging dir, then the same ``import_manifest`` registers it.

Registration inserts song + media_entry (status 'available') rows and
attaches the shipped .upsong as a verified chart — the whole queue-time
pipeline is already done, so the Pi only ever plays these files.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"


def _song_id(db, artist: str, title: str, edition: str = "") -> int:
    db.execute(
        "INSERT OR IGNORE INTO song (artist,title,edition,tracks)"
        " VALUES (?,?,?,'[\"P1\"]')", (artist, title, edition))
    return db.query(
        "SELECT id FROM song WHERE artist=? AND title=? AND edition=?",
        (artist, title, edition))[0]["id"]


def _entry_exists(db, provider: str, source_id: str) -> bool:
    return bool(db.query(
        "SELECT 1 FROM media_entry WHERE provider=? AND source_id=?",
        (provider, source_id)))


def _register_song(ctx, rec: dict, base: Path) -> bool:
    """One manifest song → song + media_entry + verified chart rows.

    ``base`` is the directory the manifest lives in; media paths in the
    record are relative to it. Files are moved/copied into their real
    homes: media into the write root (unless already resolvable) and
    charts into chart_library_dir/generated/."""
    cfg = ctx["config"]
    local = rec.get("local_path") or ""
    src = base / local
    if not local or not src.is_file():
        log.warning("import: %s missing media %s", rec.get("title"), local)
        return False
    media = cfg.media_path(local)
    if not media.exists():
        # file isn't on any root yet — move it into the write root
        media = Path(cfg.media_dir) / local
        media.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), media)
    # sidecars travel with the song file (inst stem, vocals, analysis,
    # .normed markers) — move whatever shipped beside it
    for extra in base.glob(src.stem + ".*"):
        if extra == src:
            continue
        dest_extra = media.parent / extra.name
        if not dest_extra.exists() and extra.is_file():
            try:
                shutil.move(str(extra), dest_extra)
            except OSError:
                pass
    inst = rec.get("inst_path") or ""
    if inst and not cfg.media_path(inst).exists():
        inst = ""                       # declared but didn't ship

    song_id = _song_id(ctx["db"], rec.get("artist") or "Unknown",
                       rec.get("title") or "Unknown")
    provider = rec.get("provider") or "local"
    source_id = rec.get("source_id") or local
    ctx["catalog"].add_media_entry(
        song_id, provider, source_id, local_path=local,
        duration_sec=rec.get("duration_sec"),
        thumbnail=rec.get("thumbnail") or "",
        provenance=rec.get("provenance") or "station",
        status="available")
    entry_id = ctx["db"].query(
        "SELECT id FROM media_entry WHERE provider=? AND source_id=?",
        (provider, source_id))[0]["id"]
    if inst:
        ctx["db"].execute(
            "UPDATE media_entry SET inst_path=? WHERE id=?",
            (inst, entry_id))
    chart_rel = rec.get("chart") or ""
    chart_src = base / chart_rel if chart_rel else None
    if chart_src and chart_src.is_file():
        safe = re.sub(r"[^A-Za-z0-9 _-]+", "",
                      f"{rec.get('artist','')} - {rec.get('title','')}")[:80]
        d = Path(cfg.chart_library_dir) / "generated"
        d.mkdir(parents=True, exist_ok=True)
        dest = d / f"{safe} [{entry_id}].upsong"
        shutil.copyfile(chart_src, dest)
        ctx["catalog"].attach_chart(
            song_id, dest, media_entry_id=entry_id,
            source_repo="generated", confidence=0.4, verified=True,
            license_note="station-generated chart")
    return True


def import_manifest(ctx, manifest_path: Path) -> dict:
    """Register every song in one manifest.json. Returns counts."""
    out = {"added": 0, "skipped": 0, "missing": 0}
    try:
        doc = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("import: bad manifest %s: %s", manifest_path, exc)
        return out
    base = Path(manifest_path).parent
    for rec in doc.get("songs") or []:
        if _entry_exists(ctx["db"], rec.get("provider") or "local",
                         rec.get("source_id") or ""):
            out["skipped"] += 1
            continue
        try:
            if _register_song(ctx, rec, base):
                out["added"] += 1
            else:
                out["missing"] += 1
        except Exception as exc:
            log.warning("import: %s failed: %s", rec.get("title"), exc)
            out["missing"] += 1
    return out


def scan_roots(ctx) -> dict:
    """Import a manifest.json sitting on any configured media root."""
    found = {}
    for root in ctx["config"].media_roots():
        man = Path(root) / MANIFEST
        if man.is_file():
            found[str(root)] = import_manifest(ctx, man)
    return found


def unpack_bundle(zip_path: Path, dest: Path) -> Path:
    """Extract a pushed bundle zip into ``dest`` → manifest path."""
    import zipfile
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            # flat bundle layout only — block zip-slip surprises
            name = Path(info.filename).name
            if name:
                with z.open(info) as fh:
                    (dest / name).write_bytes(fh.read())
    man = dest / MANIFEST
    if not man.is_file():
        raise ValueError("bundle has no manifest.json")
    return man
