"""JSON API: catalog search, queue operations, state.

Host-only endpoints require ``session['is_host']`` (set via /host?key=...)
or the ``X-Host-Key`` header, so guests cannot invoke disruptive actions.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from functools import wraps
from pathlib import Path

from flask import (Blueprint, current_app, jsonify, request, send_from_directory,
                   session)

from ..catalog import _norm, clean_title
from ..charts import load_chart
# pipeline lives in ..pipeline — re-exported here so existing callers
# (app.py, sockets.py, tests) importing from web.api keep working
from ..pipeline import (ensure_map, ensure_processed, push_now_playing,
                        _audio_only, _fetch_lyrics, _gen_chart,
                        _harvest_meta, _media_in_use, _pick_lrc,
                        _start_align, _map_work, _build_warp,
                        _apply_warp_live, _persist_warp, _norm_media,
                        _write_analysis)

bp = Blueprint("api", __name__)


def _ctx():
    return current_app.extensions["ups"]


def is_host() -> bool:
    cfg = _ctx()["config"]
    if session.get("is_host"):
        return True
    if request.headers.get("X-Host-Key") == cfg.host_key:
        return True
    # the room host is whichever performer claimed the code on their phone
    perf = session.get("performer_id")
    return bool(perf) and perf == _ctx().get("host_performer_id")


def host_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not is_host():
            return jsonify({"error": "host key required"}), 403
        return fn(*a, **kw)
    return wrapper


def is_singer() -> bool:
    """True when this session's performer is singing the current song."""
    perf = session.get("performer_id")
    item = _ctx()["queue"].now_playing()
    return bool(perf and item and
                perf in (item["performer_id"], item.get("performer2_id")))


def host_or_singer_required(fn):
    """Host always may; the singer at the mic may while their song plays."""
    @wraps(fn)
    def wrapper(*a, **kw):
        if not (is_host() or is_singer()):
            return jsonify({"error": "host or current singer required"}), 403
        return fn(*a, **kw)
    return wrapper


def _norm_fx(raw: dict) -> dict:
    """Validate/clamp voice-FX fields for a performer profile — the same
    keys the monitor's per-source FX accepts."""
    out = {}
    if "pitch" in raw:
        out["pitch"] = max(-6.0, min(6.0, float(raw["pitch"] or 0)))
    if "autotune" in raw:
        out["autotune"] = max(0.0, min(1.0, float(raw["autotune"] or 0)))
    if "polish" in raw:
        out["polish"] = bool(raw["polish"])
    if "reverb" in raw:
        out["reverb_wet"] = max(0.0, min(1.0, float(raw["reverb"] or 0)))
    if "reverb_wet" in raw:
        out["reverb_wet"] = max(0.0, min(1.0, float(raw["reverb_wet"] or 0)))
    if "echo_wet" in raw:
        out["echo_wet"] = max(0.0, min(1.0, float(raw["echo_wet"] or 0)))
    return out


def _perf_device(ctx, perf_id) -> str | None:
    """Mic source serving this performer right now — host assignment
    first, then whichever capture their track resolved to."""
    micman = ctx.get("micman")
    dev = micman.device_for(perf_id) if micman else None
    if dev:
        return dev
    sess = ctx.get("sing")
    item = ctx["queue"].now_playing()
    if sess and item:
        p1 = item.get("part_choice") or "P1"
        track = p1 if item["performer_id"] == perf_id else (
            "P2" if p1 == "P1" else "P1")
        return getattr(sess.mics.get(track), "device", None)
    return None


# youtube.com/watch?v=, youtu.be/, shorts/, embed/, music.youtube.com
_YT_LINK = re.compile(
    r"(?:youtube\.com/(?:watch\?[^#]*v=|shorts/|embed/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})")


@bp.get("/search")
def search():
    q = request.args.get("q", "").strip()
    ctx = _ctx()
    # pasted YouTube link → resolve it directly, no searching
    m = _YT_LINK.search(q)
    if m and ctx["providers"].enabled("youtube"):
        vid = m.group(1)
        known = ctx["db"].query(
            "SELECT id FROM media_entry WHERE provider='youtube'"
            " AND source_id=?", (vid,))
        if known:
            ent = ctx["catalog"].entry(known[0]["id"])
            return jsonify({"results": [ent] if ent else []})
        yt = ctx["providers"].get("youtube")
        hit = yt.lookup(vid) if hasattr(yt, "lookup") else None
        return jsonify({"results": [hit] if hit else []})
    results = ctx["catalog"].search(q)
    # merge remote provider results (e.g. YouTube) when query is non-empty
    if q and request.args.get("remote", "1") != "0":
        remote = _ctx()["providers"].search_remote(q)
        # annotate remote results already in the catalog instead of hiding
        # them — queueing reuses the existing entry and never re-downloads
        known = {_row["source_id"]: _row["id"]
                 for _row in _ctx()["db"].query(
                     "SELECT id, source_id FROM media_entry WHERE provider='youtube'")}
        for r in remote:
            existing = known.get(r.get("source_id"))
            if existing:
                r["entry_id"] = existing
                r["in_library"] = True
        results += remote
    return jsonify({"results": results})


@bp.get("/library")
def library():
    return jsonify({"results": _ctx()["catalog"].library()})


# -- uploads -----------------------------------------------------------------
# Phones can push their own MP3/MP4/etc straight into the catalog — the file
# lands in the media dir as a local-provider entry and goes straight through
# the queue-time pipeline (separation → lyric fetch → chart generation).

UPLOAD_EXTS = {".mp3", ".m4a", ".ogg", ".wav", ".flac",
               ".mp4", ".webm", ".mkv"}
UPLOAD_MAX_BYTES = 400 * 1024 * 1024


def _probe_duration(path: Path) -> float | None:
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=20)
        return float(out.stdout.strip())
    except Exception:
        return None


@bp.post("/upload")
def upload():
    ctx = _ctx()
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "pick a file to upload"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in UPLOAD_EXTS:
        return jsonify({"error": f"unsupported file type '{ext or '(none)'}' "
                        "— mp3/m4a/ogg/wav/flac/mp4/webm/mkv"}), 400
    if request.content_length and request.content_length > UPLOAD_MAX_BYTES:
        return jsonify({"error": "file too large (400 MB max)"}), 413

    from werkzeug.utils import secure_filename
    raw_stem = Path(f.filename).stem
    artist = (request.form.get("artist") or "").strip()[:200]
    title = (request.form.get("title") or "").strip()[:200]
    if " - " in raw_stem and not (artist and title):
        # "Artist - Title.ext" is the common file-name convention
        a, t = (s.strip() for s in raw_stem.split(" - ", 1))
        artist = artist or a
        title = title or t
    title = title or raw_stem
    artist = artist or "Unknown"
    stem = secure_filename(raw_stem) or "song"

    media_dir = Path(ctx["config"].media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    name = f"upload-{int(time.time())}-{stem[:60]}{ext}"
    dest = media_dir / name
    f.save(dest)
    if dest.stat().st_size > UPLOAD_MAX_BYTES:
        dest.unlink(missing_ok=True)
        return jsonify({"error": "file too large (400 MB max)"}), 413

    row = ctx["db"].query(
        "SELECT id FROM song WHERE artist=? AND title=? ORDER BY id LIMIT 1",
        (artist, title))
    if not row:
        ctx["catalog"].add_song(artist, title, edition="upload")
        row = ctx["db"].query(
            "SELECT id FROM song WHERE artist=? AND title=? ORDER BY id "
            "LIMIT 1", (artist, title))
    song_id = row[0]["id"]
    entry_id = ctx["catalog"].add_media_entry(
        song_id, "local", f"upload:{name}", local_path=name,
        duration_sec=_probe_duration(dest), status="available",
        provenance=f"uploaded file {f.filename[:120]}")
    ctx["catalog"].attach_matching_charts(song_id)
    ctx["artwork"].ensure(entry_id, artist, title)
    ensure_map(ctx, entry_id)     # stems + chart gen start now, not at playback
    return jsonify({"ok": True, "entry": ctx["catalog"].entry(entry_id)})


# -- song/version management -------------------------------------------------
# The "Songs" tab lists every media version on disk per song; the host can
# delete individual versions (duplicate/karaoke-channel downloads) or whole
# songs. Deleting frees disk space and removes queued items for the version
# while keeping play-history scores.

@bp.get("/manage/songs")
def manage_songs():
    """Every song with each of its media versions + queue/chart state."""
    ctx = _ctx()
    rows = ctx["db"].query(
        "SELECT s.id AS song_id, s.artist, s.title, s.is_duet,"
        " e.id AS entry_id, e.provider, e.source_id, e.status,"
        " e.duration_sec, e.local_path, e.thumbnail, e.inst_path,"
        " (SELECT COUNT(*) FROM chart c WHERE c.media_entry_id=e.id"
        "   AND c.verified=1) AS scored,"
        " (SELECT c.source_repo FROM chart c WHERE c.media_entry_id=e.id"
        "   AND c.verified=1 LIMIT 1) AS chart_source,"
        " (SELECT COUNT(*) FROM chart c WHERE c.media_entry_id=e.id"
        "   AND c.verified=0) AS possible_chart,"
        " (SELECT COUNT(*) FROM queue_item q WHERE q.media_entry_id=e.id"
        "   AND q.status='playing') AS playing,"
        " (SELECT COUNT(*) FROM queue_item q WHERE q.media_entry_id=e.id"
        "   AND q.status='queued') AS queued"
        " FROM song s JOIN media_entry e ON e.song_id=s.id"
        " ORDER BY s.artist COLLATE NOCASE, s.title COLLATE NOCASE, e.id")
    media_dir = Path(ctx["config"].media_dir)
    inflight = ctx.get("map_inflight", set())
    for r in rows:
        p = media_dir / r["local_path"] if r["local_path"] else None
        r["size_bytes"] = p.stat().st_size if p and p.is_file() else 0
        stem = (media_dir / (Path(r["local_path"]).stem + ".vocals.wav")
                if r["local_path"] else None)
        r["has_stem"] = bool(stem and stem.is_file())
        r["has_inst"] = bool(
            r["inst_path"] and (media_dir / r["inst_path"]).is_file())
        r["processing"] = r["entry_id"] in inflight
    return jsonify({"songs": rows})


def _media_files_for(ctx, entries):
    """Disk files backing media entries (video + cached artwork)."""
    media_dir = Path(ctx["config"].media_dir)
    art_dir = Path(ctx["config"].artwork_dir)
    for e in entries:
        if e.get("local_path"):
            yield media_dir / e["local_path"]
            yield media_dir / (Path(e["local_path"]).stem + ".vocals.wav")
        if e.get("inst_path"):
            yield media_dir / e["inst_path"]
        thumb = e.get("thumbnail") or ""
        if thumb.startswith("/api/art/"):
            yield art_dir / thumb.rsplit("/", 1)[-1]


def _delete_media_entries(ctx, entries):
    """FK-safe removal of media entries: play-history scores survive (the
    queue link is nulled), queued items are removed, chart rows cascade,
    then the backing files come off disk."""
    ids = [e["id"] for e in entries]
    marks = ",".join("?" * len(ids))
    ctx["db"].execute(
        f"UPDATE play_history SET queue_item_id=NULL WHERE queue_item_id IN"
        f" (SELECT id FROM queue_item WHERE media_entry_id IN ({marks}))",
        tuple(ids))
    ctx["db"].execute(
        f"DELETE FROM queue_item WHERE media_entry_id IN ({marks})",
        tuple(ids))
    ctx["db"].execute(
        f"DELETE FROM media_entry WHERE id IN ({marks})", tuple(ids))
    for p in _media_files_for(ctx, entries):
        p.unlink(missing_ok=True)


def _guard_deletable(ctx, song_id):
    """Refuse while the song is on the TV or a version is mid-download."""
    if ctx["db"].query(
            "SELECT 1 FROM queue_item q JOIN media_entry e"
            " ON e.id=q.media_entry_id"
            " WHERE e.song_id=? AND q.status='playing'", (song_id,)):
        return jsonify({"error": "can't delete — this song is playing"}), 409
    if ctx["db"].query(
            "SELECT 1 FROM media_entry WHERE song_id=?"
            " AND status='downloading'", (song_id,)):
        return jsonify({"error": "download in progress — wait or retry"}), 409
    return None


@bp.post("/manage/entry/<int:entry_id>/delete")
@host_required
def manage_delete_entry(entry_id):
    """Remove one media version of a song (its file + queue refs). A song
    left with zero versions is removed as well."""
    ctx = _ctx()
    rows = ctx["db"].query(
        "SELECT * FROM media_entry WHERE id=?", (entry_id,))
    if not rows:
        return jsonify({"error": "not found"}), 404
    entry = rows[0]
    block = _guard_deletable(ctx, entry["song_id"])
    if block:
        return block
    _delete_media_entries(ctx, [entry])
    if not ctx["db"].query(
            "SELECT 1 FROM media_entry WHERE song_id=?",
            (entry["song_id"],)):
        ctx["db"].execute("DELETE FROM song WHERE id=?",
                          (entry["song_id"],))
    ctx["socketio"].emit("state", ctx["queue"].snapshot())
    return jsonify({"ok": True})


@bp.post("/manage/entry/<int:entry_id>/repair")
@host_required
def manage_repair_entry(entry_id):
    """Re-run chart processing for a downloaded version.

    Always retries our own generator first (LRCLIB re-match + ASR
    timing + RMVPE pitch on the cached vocal stem). An imported
    (USDB/library) chart is only the fallback: it keeps its notes and
    gets its media warp recomputed when generation can't produce one.
    Entries with media but no chart — e.g. generation was interrupted —
    get the full pipeline too."""
    ctx = _ctx()
    rows = ctx["db"].query(
        "SELECT * FROM media_entry WHERE id=?", (entry_id,))
    if not rows:
        return jsonify({"error": "not found"}), 404
    entry = rows[0]
    if not entry["local_path"]:
        return jsonify({"error": "no media on disk"}), 409
    if entry["status"] == "downloading":
        return jsonify({"error": "download in progress"}), 409
    if entry_id in ctx.get("map_inflight", set()):
        return jsonify({"error": "already processing"}), 409
    _repair_entry(ctx, entry)
    return jsonify({"ok": True, "regenerating": True})


def _repair_entry(ctx, entry) -> None:
    """Delete the generated chart + analysis sidecar for one media entry
    and re-run the whole pipeline. Imported (library) charts keep their
    notes — only their media warp is rebuilt."""
    entry_id = entry["id"]
    gen = ctx["db"].query(
        "SELECT id, file_path FROM chart WHERE media_entry_id=?"
        " AND source_repo='generated'", (entry_id,))
    for c in gen:
        Path(c["file_path"]).unlink(missing_ok=True)
        ctx["db"].execute("DELETE FROM chart WHERE id=?", (c["id"],))
    if not gen:
        # imported chart — keep the notes, force the warp rebuild
        ctx["db"].execute(
            "UPDATE chart SET sync_warp='{}' WHERE media_entry_id=?",
            (entry_id,))
    # the analysis sidecar marks "generation already attempted" — clear
    # it so a repair actually re-runs the pipeline instead of falling
    # straight back to the imported chart
    media = Path(ctx["config"].media_dir) / entry["local_path"]
    (media.parent / (media.stem + ".analysis.json")).unlink(
        missing_ok=True)
    ensure_map(ctx, entry_id)


@bp.post("/manage/repair-all")
@host_required
def manage_repair_all():
    """Re-run generation for every entry whose chart is ASR-only.

    After a lyrics-pipeline fix (canonical metadata, better candidates),
    the library upgrades itself instead of each song needing a manual
    repair. Imported charts are untouched; entries already carrying a
    lyric-sourced chart are skipped. Serial worker — songs repair one
    at a time in the background."""
    ctx = _ctx()
    if ctx.get("repair_all_inflight"):
        return jsonify({"ok": True, "running": True})
    ctx["repair_all_inflight"] = True
    app = current_app._get_current_object()

    def sweep():
        try:
            rows = ctx["db"].query(
                "SELECT e.id AS entry_id, e.local_path, c.file_path"
                " FROM media_entry e"
                " JOIN chart c ON c.media_entry_id=e.id"
                " WHERE c.source_repo='generated'"
                " AND e.local_path IS NOT NULL AND e.local_path != ''")
            queued = 0
            for row in rows:
                try:
                    src = load_chart(row["file_path"]).source or {}
                except Exception:
                    continue             # unreadable — leave it alone
                if src.get("lrc") or src.get("lyrics") == "lrc":
                    continue             # already real lyrics
                _repair_entry(ctx, {"id": row["entry_id"],
                                    "local_path": row["local_path"]})
                queued += 1
            app.logger.info("repair-all: %d ASR-only charts queued",
                            queued)
        finally:
            ctx.pop("repair_all_inflight", None)

    threading.Thread(target=sweep, daemon=True,
                     name="repair-all").start()
    return jsonify({"ok": True, "running": True})


@bp.post("/manage/normalize")
@host_required
def manage_normalize():
    """Sweep every local media file up to the standard loudness.

    Normalizes each entry's playback file and karaoke backing that lack
    a .normed marker, skipping the file the TV may currently have open.
    Runs on a background thread; new files normalize themselves at
    queue time — this sweep exists for libraries that predate the
    pipeline or had normalization off.
    """
    ctx = _ctx()
    if ctx.get("norm_inflight"):
        return jsonify({"ok": True, "running": True})
    ctx["norm_inflight"] = True
    app = current_app._get_current_object()

    def sweep():
        from .. import loudness
        done = skipped = 0
        try:
            rows = ctx["db"].query(
                "SELECT id, local_path, inst_path FROM media_entry"
                " WHERE local_path IS NOT NULL AND local_path != ''")
            mdir = Path(ctx["config"].media_dir)
            for row in rows:
                # never compete with the show — loudnorm's two-pass
                # encode starves the renderer and stalls the player.
                # Wait out whatever is on stage, then continue.
                while ctx["queue"].now_playing() is not None:
                    time.sleep(5)
                if _media_in_use(ctx, row["id"]):
                    skipped += 1
                    continue
                targets = [mdir / row["local_path"]]
                if row["inst_path"]:
                    targets.append(mdir / row["inst_path"])
                target = ctx["config"].target_lufs
                for f in targets:
                    if not f.exists() or loudness.is_normed(f, target):
                        continue
                    if loudness.normalize(f, target_i=target) is not None:
                        done += 1
        except Exception as exc:
            app.logger.warning("loudness sweep failed: %s", exc)
        finally:
            ctx.pop("norm_inflight", None)
            app.logger.info(
                "loudness sweep: %d normalized, %d entries skipped",
                done, skipped)

    threading.Thread(target=sweep, daemon=True,
                     name="loudness-sweep").start()
    return jsonify({"ok": True, "running": True})


@bp.post("/manage/song/<int:song_id>/delete")
@host_required
def manage_delete_song(song_id):
    """Remove a song and every media version (files included)."""
    ctx = _ctx()
    if not ctx["db"].query("SELECT 1 FROM song WHERE id=?", (song_id,)):
        return jsonify({"error": "not found"}), 404
    block = _guard_deletable(ctx, song_id)
    if block:
        return block
    entries = ctx["db"].query(
        "SELECT * FROM media_entry WHERE song_id=?", (song_id,))
    _delete_media_entries(ctx, entries)
    ctx["db"].execute("DELETE FROM song WHERE id=?", (song_id,))
    ctx["socketio"].emit("state", ctx["queue"].snapshot())
    return jsonify({"ok": True})


@bp.get("/me")
def me():
    """Who am I: performer + whether this session holds host rights."""
    ctx = _ctx()
    perf = session.get("performer_id")
    row = ctx["db"].get_performer(perf) if perf else None
    host_id = ctx.get("host_performer_id")
    host_row = ctx["db"].get_performer(host_id) if host_id else None
    return jsonify({"performer": row, "is_host": is_host(),
                    "host": host_row})


@bp.post("/host/login")
def host_login():
    """Phone-side host unlock: correct host key claims room host for this
    performer. Only one player can hold host at a time."""
    ctx = _ctx()
    key = (request.json or {}).get("key", "")
    if not key or key.strip().upper() != ctx["config"].host_key.upper():
        return jsonify({"error": "wrong host key"}), 403
    perf = session.get("performer_id")
    if not perf:
        return jsonify({"error": "join with a display name first"}), 400
    claimed = ctx.get("host_performer_id")
    if claimed and claimed != perf:
        host = ctx["db"].get_performer(claimed)
        name = host["name"] if host else "someone else"
        return jsonify({"error": f"host is already {name}"}), 409
    ctx["host_performer_id"] = perf
    session["is_host"] = True
    ctx["socketio"].emit("host:state",
                       {"claimed": True, "key": ctx["config"].host_key})
    return jsonify({"ok": True})


@bp.post("/host/release")
def host_release():
    """Step down as room host — another player can then claim the code."""
    ctx = _ctx()
    if session.get("performer_id") == ctx.get("host_performer_id"):
        ctx["host_performer_id"] = None
    session.pop("is_host", None)
    ctx["socketio"].emit("host:state",
                       {"claimed": bool(ctx.get("host_performer_id")),
                        "key": ctx["config"].host_key})
    ctx["socketio"].emit("state", ctx["queue"].snapshot())
    return jsonify({"ok": True})


@bp.post("/session/end")
@host_required
def session_end():
    """End the party session: stop playback, clear the queue, release host.
    The TV returns to the join screen so a fresh group can start."""
    ctx = _ctx()
    ctx["queue"].stop_current()
    ctx["queue"].clear()
    ctx["host_performer_id"] = None
    ctx.pop("play_at", None)
    session.pop("is_host", None)
    if ctx.get("sing"):
        ctx["sing"].stop_mic()
        ctx["sing"] = None
    ctx["socketio"].emit("now_playing", None)
    ctx["socketio"].emit("host:state",
                       {"claimed": False, "key": ctx["config"].host_key})
    ctx["socketio"].emit("state", ctx["queue"].snapshot())
    ctx["socketio"].emit("session:ended")
    return jsonify({"ok": True})


@bp.post("/join")
def join():
    """Register a singer's display name; returns their performer record."""
    name = (request.json or {}).get("name", "").strip()[:24]
    if not name:
        return jsonify({"error": "name required"}), 400
    perf = _ctx()["db"].add_performer(name)
    session["performer_id"] = perf["id"]
    return jsonify({"performer": perf})


@bp.post("/queue")
def queue_add():
    ctx = _ctx()
    body = request.json or {}
    name = (body.get("name") or "").strip()[:24]
    part = (body.get("part") or "").strip()

    perf_id = session.get("performer_id")
    if name:
        perf_id = ctx["db"].add_performer(name)["id"]
        session["performer_id"] = perf_id
    if not perf_id:
        return jsonify({"error": "join with a display name first"}), 400

    status, val = _queue_one(ctx, body, perf_id)
    if status == "error":
        msg, code = val
        return jsonify({"error": msg}), code
    if status == "skipped":
        return jsonify({"ok": True, "skipped": True, "reason": val})
    return jsonify({"ok": True, "queue_item_id": val,
                    "state": ctx["queue"].snapshot()})


def _queue_one(ctx, body, perf_id):
    """Shared resolve → ingest → queue core for /queue and /playlist.
    Returns ("queued", item_id) | ("skipped", reason) | ("error", (msg, code))."""
    entry = _resolve_entry(ctx, body)
    if entry is None:
        return ("error", ("unknown catalog entry", 404))
    if isinstance(entry, tuple):
        resp, code = entry
        msg = (resp.get_json(silent=True) or {}).get("error", "error")
        return ("error", (msg, code))
    if entry.get("skipped"):
        return ("skipped", "song already in library")
    if entry["status"] not in ("available", "downloading", "failed"):
        return ("error", (f"entry not playable (status={entry['status']})", 409))
    if not ctx["providers"].enabled(entry["provider"]):
        return ("error", (f"provider '{entry['provider']}' not enabled", 409))

    # start ingest if the media isn't local yet
    if not ctx["providers"].resolve(entry):
        ctx["downloads"].ensure(entry)
    else:
        # file already on disk — start building its timing map now so
        # it's ready before the song reaches the TV
        ensure_map(ctx, entry["entry_id"])

    # duet: optional second singer
    part = (body.get("part") or "").strip()
    perf2_id = None
    name2 = (body.get("name2") or "").strip()[:24]
    if entry["is_duet"] and name2:
        perf2_id = ctx["db"].add_performer(name2)["id"]

    # per-song vocal suppression: NULL follows the server default
    vc = body.get("vocal_cut")
    vocal_cut = None if vc is None else int(bool(vc))
    item_id = ctx["queue"].add(entry["id"], entry["entry_id"], perf_id,
                               performer2_id=perf2_id, part_choice=part,
                               vocal_cut=vocal_cut)
    # if the player is idle, promote this item and start it immediately —
    # the TV holds a "downloading" screen if the media isn't ready yet
    if not ctx["queue"].now_playing():
        ctx["queue"].start_next()
        push_now_playing(ctx)
    else:
        ctx["socketio"].emit("state", ctx["queue"].snapshot())
    return ("queued", item_id)


@bp.post("/playlist")
def playlist_import():
    """Queue every song in a remote playlist, one media entry per entry.

    Expansion is a background job — downloads already serialize through
    DownloadManager, and each entry gets the full queue-time pipeline
    (stems, lyrics, chart) as it lands. Songs already in the library are
    skipped (skip_existing_song semantics for every entry)."""
    ctx = _ctx()
    body = request.json or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    name = (body.get("name") or "").strip()[:24]
    perf_id = session.get("performer_id")
    if name:
        perf_id = ctx["db"].add_performer(name)["id"]
        session["performer_id"] = perf_id
    if not perf_id:
        return jsonify({"error": "join with a display name first"}), 400
    found = ctx["providers"].playlist(url)
    if not found:
        return jsonify({"error": "no provider can read that playlist URL"}), 400
    pname, plist = found
    total = len(plist["entries"])
    job_id = secrets.token_hex(4)
    ctx.setdefault("playlist_jobs", {})[job_id] = {
        "title": plist["title"], "total": total, "done": 0}

    def work():
        queued = skipped = failed = 0
        try:
            for e in plist["entries"]:
                ebody = {"provider": pname, "source_id": e["source_id"],
                         "title": e["title"], "artist": e.get("artist") or "",
                         "duration_sec": e.get("duration_sec"),
                         "thumbnail": e.get("thumbnail") or "",
                         "skip_existing_song": True}
                try:
                    status, _ = _queue_one(ctx, ebody, perf_id)
                    if status == "skipped":
                        skipped += 1
                    elif status == "queued":
                        queued += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
                job = ctx["playlist_jobs"][job_id]
                job["done"] += 1
                ctx["socketio"].emit("playlist:progress", {
                    "job": job_id, "done": job["done"], "total": total,
                    "queued": queued, "skipped": skipped})
        finally:
            ctx["socketio"].emit("playlist:done", {
                "job": job_id, "title": plist["title"], "queued": queued,
                "skipped": skipped, "failed": failed})
            ctx["playlist_jobs"].pop(job_id, None)

    threading.Thread(target=work, daemon=True,
                     name="playlist-import").start()
    return jsonify({"ok": True, "job": job_id, "title": plist["title"],
                    "total": total})


def _song_has_media(ctx, song_id: int) -> bool:
    # 'downloading' counts too — an entry mid-pipeline is already being
    # processed; queueing a second copy of the same song would duplicate
    # the download
    return bool(ctx["db"].query(
        "SELECT 1 FROM media_entry WHERE song_id=?"
        " AND status IN ('available','downloading') LIMIT 1", (song_id,)))


def _resolve_entry(ctx, body) -> dict | tuple | None:
    """Existing catalog entry by id, or create one from a remote result."""
    if body.get("entry_id"):
        return ctx["catalog"].entry(int(body["entry_id"]))

    provider = (body.get("provider") or "").strip()
    source_id = (body.get("source_id") or "").strip()
    if not provider or not source_id:
        return None
    if not ctx["providers"].enabled(provider):
        return jsonify({"error": f"provider '{provider}' not enabled"}), 409

    found = ctx["db"].query(
        "SELECT id FROM media_entry WHERE provider=? AND source_id=?",
        (provider, source_id))
    if found:
        ent = ctx["catalog"].entry(found[0]["id"])
        # batch imports can opt out of re-processing a song the library
        # already has — checked BEFORE a second media entry is created
        # on the same song row
        if (body.get("skip_existing_song") and ent
                and _song_has_media(ctx, ent["id"])):
            return {"skipped": True}
        return ent

    title = (body.get("title") or source_id).strip()[:200]
    artist = (body.get("artist") or provider).strip()[:200]
    # karaoke channels name videos "Artist - Title" OR "Title - Artist"
    # and append "| Karaoke Version | Channel" tags — strip the tags
    # first, then pick the field order the chart library recognizes
    title = re.sub(r"\s*\|.*$", "", title).strip()
    title = re.sub(r"\s*\[[^\]]*karaoke[^\]]*\]", "", title,
                   flags=re.IGNORECASE).strip()
    title = clean_title(title) or title
    artist = re.sub(r"\s*-\s*Topic\s*$", "", artist).strip() or artist
    if " - " in title:
        a, t = (s.strip()[:200] for s in title.split(" - ", 1))
        # "Song Title" - Source/Movie is a common uploader convention —
        # a fully quote-wrapped segment names the song, not the artist
        unq = lambda s: s.strip("\"'‘’“”").strip()
        lib = lambda n: ctx["db"].query(
            "SELECT 1 FROM chart_library WHERE artist_norm=? LIMIT 1",
            (_norm(n),))
        if unq(a) != a and unq(t) == t:
            artist, title = t, unq(a)         # "Title" - Source order
        elif lib(t) and not lib(a):
            artist, title = t, a          # Title - Artist order
        else:
            artist, title = a, t          # Artist - Title order
        artist, title = unq(artist) or artist, unq(title) or title
    # reuse the existing artist+title song so multiple provider videos for
    # the same song stay grouped under one catalog row
    row = ctx["db"].query(
        "SELECT id FROM song WHERE artist=? AND title=? ORDER BY id LIMIT 1",
        (artist, title))
    if row:
        song_id = row[0]["id"]
        if body.get("skip_existing_song") and _song_has_media(ctx, song_id):
            return {"skipped": True}
    else:
        song_id = ctx["catalog"].add_song(
            artist, title, edition=f"{provider}-{source_id}")
        row = ctx["db"].query(
            "SELECT id FROM song WHERE artist=? AND title=? AND edition=?",
            (artist, title, f"{provider}-{source_id}"))
        song_id = row[0]["id"] if row else song_id
    entry_id = ctx["catalog"].add_media_entry(
        song_id, provider, source_id, status="downloading",
        duration_sec=body.get("duration_sec"),
        thumbnail=(body.get("thumbnail") or "")[:500],
        provenance=f"{provider} source {source_id}",
        license_note="")
    if not entry_id:
        entry_id = ctx["db"].query(
            "SELECT id FROM media_entry WHERE provider=? AND source_id=?",
            (provider, source_id))[0]["id"]
    ctx["catalog"].attach_matching_charts(song_id)
    # album art lookup runs in the background; a provider thumbnail is the
    # immediate fallback and is replaced if real artwork is found
    ctx["artwork"].ensure(entry_id, artist, title,
                          existing_thumb=body.get("thumbnail") or "")
    return ctx["catalog"].entry(entry_id)


@bp.get("/queue")
def queue_list():
    return jsonify(_ctx()["queue"].snapshot())


@bp.post("/queue/<int:item_id>/remove")
def queue_remove(item_id: int):
    ctx = _ctx()
    item = ctx["queue"].get(item_id)
    if not item:
        return jsonify({"error": "not found"}), 404
    # guests may remove only their own queued songs; host may remove any
    if not (is_host() or item["performer_id"] == session.get("performer_id")):
        return jsonify({"error": "not your song"}), 403
    ctx["queue"].remove(item_id)
    ctx["socketio"].emit("state", ctx["queue"].snapshot())
    return jsonify({"ok": True})


@bp.post("/queue/<int:item_id>/join-duet")
def queue_join_duet(item_id: int):
    """Claim the open P2 slot on a queued duet song."""
    ctx = _ctx()
    perf = session.get("performer_id")
    if not perf:
        return jsonify({"error": "join with a display name first"}), 400
    item = ctx["queue"].join_duet(item_id, perf)
    if not item:
        return jsonify({"error": "not an open duet slot"}), 409
    ctx["socketio"].emit("state", ctx["queue"].snapshot())
    # let the TV update its pre-song screen without a full now_playing re-push
    # (re-pushing would restart the video if the song already began)
    ctx["socketio"].emit("item:update", item)
    return jsonify({"ok": True, "item": item})


@bp.post("/queue/<int:item_id>/move")
@host_required
def queue_move(item_id: int):
    direction = (request.json or {}).get("direction", "up")
    ctx = _ctx()
    if not ctx["queue"].move(item_id, direction):
        return jsonify({"error": "cannot move"}), 400
    ctx["socketio"].emit("state", ctx["queue"].snapshot())
    return jsonify({"ok": True})


@bp.post("/queue/clear")
@host_required
def queue_clear():
    ctx = _ctx()
    n = ctx["queue"].clear()
    ctx["socketio"].emit("state", ctx["queue"].snapshot())
    return jsonify({"ok": True, "cleared": n})


@bp.get("/art/<path:filename>")
def artwork_file(filename: str):
    """Serve locally-cached album/single art."""
    return send_from_directory(_ctx()["config"].artwork_dir, filename)


@bp.post("/catalog/backfill-art")
@host_required
def backfill_art():
    """Fetch artwork for catalog entries that have none."""
    ctx = _ctx()
    rows = ctx["db"].query(
        "SELECT e.id, s.artist, s.title, e.thumbnail FROM media_entry e"
        " JOIN song s ON s.id=e.song_id"
        " WHERE e.thumbnail='' OR e.thumbnail IS NULL")
    for r in rows:
        ctx["artwork"].ensure(r["id"], r["artist"], r["title"], "")
    return jsonify({"ok": True, "queued": len(rows)})


@bp.post("/catalog/rescan")
@host_required
def catalog_rescan():
    ctx = _ctx()
    added = ctx["catalog"].rescan_charts([ctx["config"].chart_library_dir])
    return jsonify({"ok": True, "charts_added": added})


@bp.get("/catalog/pending-charts")
@host_required
def pending_charts():
    return jsonify({"charts": _ctx()["catalog"].pending_charts()})


@bp.post("/catalog/chart/<int:chart_id>/verify")
@host_required
def verify_chart(chart_id: int):
    ctx = _ctx()
    body = request.json or {}
    entry_id = int(body.get("media_entry_id") or 0)
    if not entry_id:
        # fall back to the chart's song's first media entry
        row = ctx["db"].query(
            "SELECT e.id FROM chart c JOIN media_entry e ON e.song_id=c.song_id"
            " WHERE c.id=? ORDER BY e.id LIMIT 1", (chart_id,))
        if not row:
            return jsonify({"error": "no media entry for this song"}), 400
        entry_id = row[0]["id"]
    ok = ctx["catalog"].verify_chart(
        chart_id, entry_id, int(body.get("sync_offset_ms") or 0))
    return jsonify({"ok": ok})


@bp.post("/playback/next")
@host_required
def playback_next():
    ctx = _ctx()
    current = ctx["queue"].skip_current() if ctx["queue"].now_playing() else ctx["queue"].start_next()
    push_now_playing(ctx)
    return jsonify({"ok": True, "now_playing": current})


@bp.post("/playback/pause")
@host_required
def playback_pause():
    ctx = _ctx()
    item = ctx["queue"].now_playing()
    if item:
        ctx["tv_paused"] = item["id"]  # watchdog: silence while paused is fine
    ctx["socketio"].emit("player:pause")
    return jsonify({"ok": True})


@bp.post("/playback/volume")
@host_or_singer_required
def playback_volume():
    """Music volume on the TV — host or the singer on the mic may set it."""
    ctx = _ctx()
    try:
        vol = max(0.0, min(1.0, float((request.json or {}).get("volume"))))
    except (TypeError, ValueError):
        return jsonify({"error": "volume 0..1 required"}), 400
    ctx["db"].set_setting("music_volume", str(vol))
    ctx["socketio"].emit("player:volume", vol)
    return jsonify({"ok": True, "music_volume": vol})


@bp.post("/playback/resume")
@host_required
def playback_resume():
    ctx = _ctx()
    item = ctx["queue"].now_playing()
    if item:
        ctx.pop("tv_paused", None)
        ctx["tv_expect_pos"] = time.time()  # watchdog: positions should return
    ctx["socketio"].emit("player:resume")
    return jsonify({"ok": True})


@bp.post("/playback/sync")
@host_required
def playback_sync():
    """Nudge chart->media sync for the song now playing.

    Community charts carry a GAP timed to the original track; karaoke
    videos often start vocals earlier/later, so the host needs a live
    offset control. delta_ms shifts lyrics/notes earlier (-) or later (+).
    """
    ctx = _ctx()
    try:
        delta = int((request.json or {}).get("delta_ms", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "delta_ms must be an integer"}), 400
    item = ctx["queue"].now_playing()
    if not item:
        return jsonify({"error": "nothing playing"}), 409
    row = ctx["catalog"].chart_row_for_entry(item["entry_id"])
    if not row:
        return jsonify({"error": "no verified chart on this song"}), 404
    new_off = int(row["sync_offset_ms"]) + delta
    ctx["db"].execute("UPDATE chart SET sync_offset_ms=? WHERE id=?",
                      (new_off, row["id"]))
    # live re-send of the lyric payload — TV swaps it in without touching
    # the video element (a full now_playing push would restart playback)
    lyrics = ctx["catalog"].chart_payload(item["entry_id"])
    ctx["socketio"].emit("lyrics", lyrics)
    if ctx.get("sing"):
        ctx["sing"].set_sync_offset(new_off)
    return jsonify({"ok": True, "sync_offset_ms": new_off})


@bp.post("/playback/resync")
@host_or_singer_required
def playback_resync():
    """Re-solve chart->media sync on demand: drops prior sync state and
    collects ~10s of fresh room audio, then runs the same two-stage
    pitch/whistle autosync. The result lands via the sing:autosync event
    like the automatic solve."""
    from ..sing import RESYNC_COLLECT_SEC
    ctx = _ctx()
    sess = ctx.get("sing")
    if not sess:
        return jsonify({"error": "no scored song playing"}), 409
    sess.request_resync()
    return jsonify({"ok": True, "collect_sec": RESYNC_COLLECT_SEC})


@bp.get("/mic")
def mic_status():
    ctx = _ctx()
    mon = ctx.get("monitor")
    mic = ctx.get("mic")
    micman = ctx.get("micman")
    return jsonify({
        "capture": bool(mic and mic.running),
        "monitor": bool(mon and mon.running),
        "device": mic.device if mic else "",
        "level": round(mic.level, 3) if mic and mic.running else 0,
        "mics": micman.status() if micman else [],
        "error": (mic.error if mic else "no mic") if not
                 (mic and mic.running) else "",
        "volume": mon.volume if mon else 0,
        "echo_ms": mon.echo_ms if mon else 0,
        "echo_wet": mon.echo_wet if mon else 0,
        "pitch": mon.pitch if mon else 0,
        "reverb_wet": mon.reverb_wet if mon else 0,
        "autotune": mon.autotune if mon else 0,
        "polish": mon.polish if mon else False,
        "my_fx": (ctx["db"].performer_fx(session.get("performer_id"))
                  if session.get("performer_id") else None),
        "muted": mon.muted if mon else True,
        "passthrough": mon.passthrough if mon else False,
        "mic_delay_ms": ctx.get("mic_delay_ms", 0),
        "music_volume": float(
            ctx["db"].get_setting("music_volume", "1") or 1),
        "can_mix": is_host() or is_singer(),
    })


@bp.get("/audio")
def audio_status():
    """Host audio panel: output targets + mics + performers to assign."""
    ctx = _ctx()
    from .. import audio as pw
    micman = ctx.get("micman")
    return jsonify({
        "outputs": pw.playback_targets() if pw.available() else [],
        "mics": micman.status() if micman else [],
        "performers": ctx["db"].performers(),
        "monitor": {
            "running": bool(ctx.get("monitor") and ctx["monitor"].running),
            "volume": ctx["monitor"].volume if ctx.get("monitor") else 0,
            "echo_wet": ctx["monitor"].echo_wet if ctx.get("monitor") else 0,
            "muted": ctx["monitor"].muted if ctx.get("monitor") else True,
        } if ctx.get("monitor") else None,
    })


@bp.post("/audio/output")
@host_required
def audio_output():
    """Route all audio (music + mic monitor) to a sink or enable a card."""
    from .. import audio as pw
    body = request.json or {}
    if "sink" in body:
        res = pw.select_output("sink", body["sink"])
    elif "sink_name" in body:
        res = pw.select_output("sink_name", body["sink_name"])
    elif "card" in body:
        res = pw.select_output("card", body["card"])
    else:
        return jsonify({"error": "sink or card required"}), 400
    if res.get("ok"):
        # persist the stable node name — wpctl ids change across restarts
        saved = {"sink_name": res["node"]} if res.get("node") else (
            {"card": body["card"]} if "card" in body else None)
        if saved:
            _ctx()["db"].set_setting("output_target", json.dumps(saved))
    return jsonify(res), 200 if res.get("ok") else 502


@bp.post("/mic/assign")
@host_required
def mic_assign():
    """Assign a capture device to a performer (or null to unassign)."""
    ctx = _ctx()
    micman = ctx.get("micman")
    if not micman:
        return jsonify({"error": "no mics"}), 404
    body = request.json or {}
    device = body.get("device")
    if device not in micman.captures:
        return jsonify({"error": "unknown device"}), 404
    micman.assign(device, body.get("performer_id"))
    return jsonify({"ok": True, "mics": micman.status()})


@bp.get("/scores")
def scores():
    """Phone scoreboard: per-song records + singer standings."""
    return jsonify(_ctx()["catalog"].leaderboard())


@bp.post("/mic/calibrate")
@host_or_singer_required
def mic_calibrate():
    """Play loud clicks through the output sink and time them coming back
    through the mic. The measured speaker->mic loop delay shifts the media
    position at scoring time so hit windows match when notes were actually
    sung — same idea as USDX's per-device Latency config, but measured.
    Blocks ~2s while the clicks play."""
    ctx = _ctx()
    micman = ctx.get("micman")
    if not micman:
        return jsonify({"error": "no mic manager"}), 404
    res = micman.calibrate()
    if res.get("ok"):
        ctx["mic_delay_ms"] = res["mic_delay_ms"]
        ctx["db"].set_setting("mic_delay_ms", str(res["mic_delay_ms"]))
    return jsonify(res), 200 if res.get("ok") else 502


@bp.post("/mic")
@host_or_singer_required
def mic_set():
    """Live mic monitor controls: speaker volume, echo, mute."""
    ctx = _ctx()
    mon = ctx.get("monitor")
    if not mon:
        return jsonify({"error": "no mic monitor running"}), 404
    body = request.json or {}
    # mix controls are room-wide; voice FX belong to the singer's profile
    mon.set(volume=body.get("volume"), echo_ms=body.get("echo_ms"),
            muted=body.get("muted"))
    fx = _norm_fx(body)
    perf_id = session.get("performer_id")
    prof = None
    if fx and perf_id:
        # identified singer: merge into their saved profile and apply it
        # to the mic serving them right now
        prof = ctx["db"].performer_fx(perf_id)
        prof.update(fx)
        ctx["db"].set_performer_fx(perf_id, prof)
        dev = _perf_device(ctx, perf_id)
        if dev:
            mon.set_source_fx(dev, **prof)
    elif fx:
        g = dict(fx)
        if "reverb_wet" in g:          # set() calls it 'reverb'
            g["reverb"] = g.pop("reverb_wet")
        mon.set(**g)          # no identity -> room default
    return jsonify({"ok": True, "volume": mon.volume,
                    "echo_ms": mon.echo_ms, "echo_wet": mon.echo_wet,
                    "pitch": mon.pitch, "reverb_wet": mon.reverb_wet,
                    "autotune": mon.autotune, "polish": mon.polish,
                    "my_fx": prof, "muted": mon.muted})


@bp.get("/playback/sync")
def playback_sync_get():
    """Current sync offset for the playing chart (any client may read)."""
    ctx = _ctx()
    item = ctx["queue"].now_playing()
    if not item:
        return jsonify({"sync_offset_ms": None})
    row = ctx["catalog"].chart_row_for_entry(item["entry_id"])
    return jsonify({
        "sync_offset_ms": row["sync_offset_ms"] if row else None,
        "autosync_mic": ctx["db"].get_setting("autosync_mic", "1") != "0"})


@bp.post("/playback/autosync")
@host_required
def playback_autosync():
    """Toggle live mic autosync (pitch/whistle solves). The deterministic
    media-file alignment is unaffected — it always runs at song start."""
    body = request.json or {}
    en = bool(body.get("enabled", True))
    _ctx()["db"].set_setting("autosync_mic", "1" if en else "0")
    return jsonify({"autosync_mic": en})
