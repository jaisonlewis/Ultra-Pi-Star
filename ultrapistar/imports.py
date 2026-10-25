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


def _entry_row(db, provider: str, source_id: str):
    rows = db.query(
        "SELECT id, song_id, local_path, inst_path FROM media_entry"
        " WHERE provider=? AND source_id=?", (provider, source_id))
    return rows[0] if rows else None


def _attach_chart(ctx, rec: dict, base: Path, song_id: int,
                  entry_id: int) -> bool:
    """Copy the shipped .upsong into chart-library/generated and bind it
    to this entry, superseding any prior generated chart. A byte-identical
    chart already attached counts as no change."""
    cfg = ctx["config"]
    chart_rel = rec.get("chart") or ""
    chart_src = base / chart_rel if chart_rel else None
    if not (chart_src and chart_src.is_file()):
        return False
    db = ctx["db"]
    shipped = chart_src.read_bytes()
    for c in db.query(
            "SELECT id, file_path FROM chart WHERE media_entry_id=?"
            " AND source_repo='generated'", (entry_id,)):
        old = Path(c["file_path"])
        if old.is_file() and old.read_bytes() == shipped:
            if len(db.query(
                    "SELECT id FROM chart WHERE media_entry_id=? AND"
                    " source_repo='generated'", (entry_id,))) == 1:
                return False                 # same chart, already bound
        old.unlink(missing_ok=True)
        db.execute("DELETE FROM chart WHERE id=?", (c["id"],))
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


def _update_entry(ctx, rec: dict, base: Path, row: dict) -> bool:
    """A bundle for an entry the catalog already has — the station
    reprocessed existing media. Sidecars land beside the entry's media
    on the WRITE root (the media may live on a nearly-full read
    fallback like the SD card), inst_path points at the shipped
    karaoke track, and the shipped chart supersedes any prior
    generated one."""
    db, cfg = ctx["db"], ctx["config"]
    entry_id, song_id = row["id"], row["song_id"]
    local = rec.get("local_path") or row["local_path"] or ""
    if not local or not cfg.media_path(local).exists():
        return False                      # nothing to update against
    src_dir = base / Path(rec.get("local_path") or local).parent
    stem = Path(local).stem
    wroot = Path(cfg.media_dir)
    dest_dir = wroot / Path(local).parent
    landed = False
    for extra in src_dir.glob(stem + ".*"):
        if not extra.is_file() or extra.name == Path(local).name:
            continue                      # the media itself stays put
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / extra.name
        if not dest.exists():
            try:
                shutil.move(str(extra), dest)
            except OSError:
                continue
        landed = True
    inst = rec.get("inst_path") or ""
    if (inst and inst != row["inst_path"]
            and cfg.media_path(inst).exists()):
        db.execute("UPDATE media_entry SET inst_path=? WHERE id=?",
                   (inst, entry_id))
        landed = True
    if _attach_chart(ctx, rec, base, song_id, entry_id):
        landed = True
    return landed


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
    artist = rec.get("artist") or "Unknown"
    title = rec.get("title") or "Unknown"
    media = cfg.media_path(local)
    if not media.exists():
        # file isn't on any root yet — move it into the write root,
        # organized into the artist/title folder convention
        from .mediastore import song_reldir
        local = f"{song_reldir(artist, title, src.stem)}/{src.name}"
        media = Path(cfg.media_dir) / local
        media.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), media)
    # sidecars travel with the song file (inst stem, vocals, analysis,
    # .normed markers) — move whatever shipped beside it
    for extra in src.parent.glob(src.stem + ".*"):
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
        # sidecars were moved beside the media — re-point inst at the
        # shipped file's new home
        moved = media.parent / Path(inst).name
        inst = (f"{str(media.parent.relative_to(cfg.media_dir))}"
                .replace("\\", "/") + f"/{moved.name}"
                if moved.is_file()
                and media.parent.is_relative_to(cfg.media_dir)
                else "")
    if inst and not cfg.media_path(inst).exists():
        inst = ""                       # declared but didn't ship

    song_id = _song_id(ctx["db"], artist, title)
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
    _attach_chart(ctx, rec, base, song_id, entry_id)
    return True


def import_manifest(ctx, manifest_path: Path) -> dict:
    """Register every song in one manifest.json. Returns counts —
    ``updated`` covers bundles that enrich an already-known entry
    (a station reprocess of media the Pi already had)."""
    out = {"added": 0, "updated": 0, "skipped": 0, "missing": 0}
    try:
        doc = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("import: bad manifest %s: %s", manifest_path, exc)
        return out
    base = Path(manifest_path).parent
    for rec in doc.get("songs") or []:
        row = _entry_row(ctx["db"], rec.get("provider") or "local",
                         rec.get("source_id") or "")
        if row is not None:
            try:
                out["updated" if _update_entry(ctx, rec, base, row)
                    else "skipped"] += 1
            except Exception as exc:
                log.warning("import: update %s failed: %s",
                            rec.get("title"), exc)
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
