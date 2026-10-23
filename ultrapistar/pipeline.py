"""Queue-time media pipeline: loudness → stems → lyrics → chart → warp.

Lives outside web/api.py so the processing logic is importable and
testable without a Flask context. Everything here takes the shared
``ctx`` dict (``app.extensions["ups"]``) rather than globals, so workers
on background threads run exactly like request-time code.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from flask import current_app

from .charts import load_chart


def _stage(ctx, entry_id, label):
    """Broadcast pipeline progress — the TV's "Preparing song…" screen
    renders it so a long wait looks like work, not a frozen countdown."""
    ctx.setdefault("map_stage", {})[entry_id] = label
    try:
        ctx["socketio"].emit("pipeline:stage",
                             {"entry_id": entry_id, "label": label})
    except Exception:
        pass


def _media_in_use(ctx, entry_id: int) -> bool:
    """True when the TV may hold this entry's file open — normalization
    must not replace a file out from under the player. While the TV is
    held on a waiting screen no src is loaded, so the file is fair game.
    """
    cur = ctx["queue"].now_playing()
    if not cur or cur["entry_id"] != entry_id:
        return False
    pushed = ctx.get("np_pushed")
    return bool(pushed and pushed[0] == cur["id"])


def _norm_media(ctx, app, media: Path, entry_id: int) -> None:
    """Normalize one file to the standard loudness unless already done
    or the file is currently feeding the player."""
    from . import loudness
    target = ctx["config"].target_lufs
    if (not ctx["config"].audio_normalize
            or loudness.is_normed(media, target)
            or _media_in_use(ctx, entry_id)):
        return
    try:
        loudness.normalize(media, target_i=target)
    except Exception as exc:
        app.logger.warning("loudness normalize failed for %s: %s",
                           media, exc)


def ensure_processed(ctx, entry_id: int, item_id: int | None = None) -> None:
    """Queue-time media processing on a worker thread.

    Phase 1 — separation: spleeter int8 splits the file into vocal +
    accompaniment; the accompaniment is remuxed into a karaoke mp4
    (``inst_path``) and the vocal stem feeds ASR + pitch extraction.

    Phase 2 — chart (our pipeline first):
      * no generated chart → generate one: lyric lines + ASR word
        timings + RMVPE pitch on the vocal stem → native .upsong, born
        media-timed (tried once per repair/download — the analysis
        sidecar marks the attempt)
      * generation failed → a verified imported (USDB/library) chart is
        the fallback: ASR/LRC warp marks re-anchor it to this exact
        video (chroma fallback when there's no lead vocal)

    ``item_id``, when given, gates the live session retime to the item
    still playing — persistence always happens since the artifacts
    belong to the media entry.
    """
    from . import separate, loudness
    from .catalog import parse_sync_warp
    # local_path lives on the media entry, not the chart row
    ent = ctx["catalog"].entry(entry_id)
    if not ent or not ent.get("local_path"):
        return
    media = ctx["config"].media_path(ent["local_path"])
    if not media.exists():
        return
    mdir = Path(ctx["config"].models_dir)
    have_models = (mdir / separate.MODEL_DIR / "vocals.int8.onnx").exists()
    chart_row = ctx["catalog"].chart_row_for_entry(entry_id)
    warped = (chart_row
              and parse_sync_warp(chart_row["sync_warp"], entry_id)[1])
    gen_tried = (media.parent
                 / (media.stem + ".analysis.json")).exists()
    inst_abs = media.parent / (media.stem + ".inst.mp4")
    target = ctx["config"].target_lufs
    needs_norm = bool(
        ctx["config"].audio_normalize
        and not _media_in_use(ctx, entry_id)
        and (not loudness.is_normed(media, target)
             or (inst_abs.exists()
                 and not loudness.is_normed(inst_abs, target))))
    if (chart_row and warped
            and (chart_row["source_repo"] == "generated"
                 or gen_tried or not have_models)
            and not needs_norm):
        return                # resolved: own chart, or fallback already set
    if not chart_row and not have_models and not needs_norm:
        return                        # nothing to generate without models
    inflight = ctx.setdefault("map_inflight", set())
    if entry_id in inflight:
        return
    inflight.add(entry_id)
    # needle ASR isn't thread-safe — two workers transcribing at once
    # wedged libneedle for 20+ minutes. Serialize the whole pipeline so
    # repairs queue up instead of racing inside the native lib.
    map_lock = ctx.setdefault("map_lock", threading.Lock())
    # may be invoked from a download/ASR thread — current_app doesn't
    # exist there; the real app object travels in ctx instead
    app = ctx.get("app") or current_app._get_current_object()

    def work():
        try:
            with map_lock:
                _map_work(ctx, app, ent, media, mdir, entry_id, item_id,
                          have_models)
        except Exception as exc:                      # ffmpeg/db teardown
            app.logger.warning("align failed for %s: %s", media, exc)
        finally:
            # discard BEFORE re-pushing: push_now_playing holds the TV on
            # "Preparing lyrics…" while the entry is inflight, so a push
            # issued while still marked inflight would re-emit the wait
            # and strand the player on it forever.
            inflight.discard(entry_id)
            # if this entry is the current item, release that hold (or
            # hot-swap lyrics in) — whether generation landed a chart or
            # not, and also when the worker died mid-pipeline.
            try:
                cur = ctx["queue"].now_playing()
                if cur and cur["entry_id"] == entry_id:
                    push_now_playing(ctx)
            except Exception:
                pass

    threading.Thread(target=work, daemon=True).start()


def _map_work(ctx, app, ent, media, mdir, entry_id, item_id,
              have_models):
    """Worker body for ensure_map — called under the map lock."""
    from . import separate
    from .catalog import parse_sync_warp
    gen_tried = (media.parent
                 / (media.stem + ".analysis.json")).exists()
    # canonical artist/track the provider tagged on the upload — the
    # lyric search below seeds it ahead of every title-parse guess
    _stage(ctx, entry_id, "reading song metadata")
    _harvest_meta(ctx, ent)
    # lyric fetch is pure network — run it beside ffmpeg/separation
    # instead of serially after (seeds need the harvested metadata, so
    # it must start after _harvest_meta, not before)
    lyr_holder: dict = {}

    def _prefetch():
        try:
            lyr_holder["recs"] = _fetch_lyrics(ent)
        except Exception:
            lyr_holder["recs"] = None

    lyr_thread = threading.Thread(target=_prefetch, daemon=True)
    lyr_thread.start()

    def _lyrics():
        lyr_thread.join(90)
        recs = lyr_holder.get("recs")
        return recs if recs is not None else _fetch_lyrics(ent)

    # ---- phase 0: loudness ------------------------------------------
    # Bring the raw file to the standard integrated loudness first so
    # stems, backings and charts all derive from the normalized audio.
    _stage(ctx, entry_id, "normalizing loudness")
    _norm_media(ctx, app, media, entry_id)
    # ---- phase 1: stems ---------------------------------------------
    vocal16 = None
    if have_models:
        inst_rel = media.stem + ".inst.mp4"
        inst_abs = media.parent / inst_rel
        voc_wav = media.parent / (media.stem + ".vocals.wav")
        try:
            if voc_wav.exists() and inst_abs.exists():
                # separated before — reload the stem (16k mono
                # float32) instead of burning another pass
                import soundfile as sf
                vocal16, _ = sf.read(str(voc_wav),
                                     dtype="float32")
            else:
                _stage(ctx, entry_id, "separating vocals")
                vocals, accomp = separate.separate_stems(media, mdir)
                if not inst_abs.exists():
                    _stage(ctx, entry_id, "building karaoke track")
                    mix = separate.karaoke_mix(
                        vocals, accomp,
                        guide=ctx["config"].karaoke_guide)
                    separate.karaoke_mp4(media, mix, inst_abs)
                vocal16 = separate.mono16(vocals)
                separate.write_wav(voc_wav, vocal16, sr=16000)
            ctx["db"].execute(
                "UPDATE media_entry SET inst_path=? WHERE id=?",
                (inst_rel, entry_id))
        except Exception as exc:
            app.logger.warning(
                "separation failed for %s: %s", media, exc)
        # the karaoke backing plays in place of the raw file — same
        # loudness target so toggling vocal-cut doesn't jump the level
        if inst_abs.exists():
            _stage(ctx, entry_id, "normalizing karaoke track")
            _norm_media(ctx, app, inst_abs, entry_id)

    # ---- phase 2: own pipeline first, imported chart as fallback ----
    # Our generated charts are timed to THIS media (LRC lyrics + ASR
    # onsets + RMVPE pitch) — they beat a crowd-made USDB chart whenever
    # the pipeline can produce one. The imported chart only enters when
    # generation can't (no models) or didn't (no lyric match).
    row = ctx["catalog"].chart_row_for_entry(entry_id)
    if (have_models and not gen_tried
            and (not row or row["source_repo"] != "generated")):
        _gen_chart(ctx, app, ent, media, mdir, vocal16, recs=_lyrics())
        row = ctx["catalog"].chart_row_for_entry(entry_id)
    if (row and row["source_repo"] != "generated"
            and not parse_sync_warp(row["sync_warp"], entry_id)[1]):
        # generation produced nothing — the imported chart is the
        # fallback; warp its beats onto this media
        res = _build_warp(ctx, app, ent, media, row, vocal16,
                          recs=_lyrics())
        if res:
            _persist_warp(ctx, entry_id, row, res)
            _apply_warp_live(ctx, entry_id, item_id, res)


def _build_warp(ctx, app, ent, media, chart_row, vocal16, recs=None):
    """ASR/LRC warp marks → chroma fallback (the pre-generation path)."""
    from .align import align_chart
    from . import songmap
    chart = load_chart(chart_row["file_path"])
    res = None
    _stage(ctx, ent["entry_id"], "aligning chart")
    try:
        asr = (songmap.transcribe_audio(vocal16)
               if vocal16 is not None else None)
        if recs is None:
            recs = _fetch_lyrics(ent)
        rec, _ = _pick_lrc(recs, asr, ent.get("title") or "",
                           media_dur=ent.get("duration_sec") or 0)
        ltxt = ([t for _, t in rec["synced"]] or rec["plain"]
                if rec else None)
        res = songmap.build_warp(
            chart, media, ltxt,
            synced=(rec or {}).get("synced"), asr=asr)
    except Exception as exc:
        app.logger.warning("asr map failed for %s: %s", media, exc)
    if not res:
        res = align_chart(chart, media)
    return res


def _persist_warp(ctx, entry_id, chart_row, res) -> None:
    # this media entry's marks inside the shared warp map
    try:
        warp_map = json.loads(chart_row["sync_warp"] or "{}")
    except (TypeError, ValueError):
        warp_map = {}
    if not isinstance(warp_map, dict):
        warp_map = {}
    warp_map[str(entry_id)] = {"ratio": res["tempo"],
                               "marks": res["warp"]}
    ctx["db"].execute(
        "UPDATE chart SET sync_offset_ms=?, sync_warp=? WHERE id=?",
        (res["offset_ms"], json.dumps(warp_map), chart_row["id"]))


def _apply_warp_live(ctx, entry_id, item_id, res) -> None:
    """Retime the running session + push updated lyrics — only when the
    map belongs to the item actually playing; a queue-time build for a
    later song must not touch the live show."""
    cur = ctx["queue"].now_playing()
    sess = ctx.get("sing")
    if (cur is None or cur["entry_id"] != entry_id
            or (item_id is not None and cur["id"] != item_id)):
        return
    if sess is not None:
        sess.tempo = res["tempo"] or 1.0
        if sess.chart.bpm:
            sess.beat_ms = 15000.0 / sess.chart.bpm * sess.tempo
        for b, off in res["warp"]:
            sess.learn_warp(b, off)
        pos = sess._media_at(time.monotonic())
        if pos is not None:
            sess.apply_sync_offset(res["offset_ms"], pos)
    item = ctx["queue"].now_playing()
    ctx["socketio"].emit(
        "lyrics", ctx["catalog"].chart_payload(
            entry_id, rom=bool((item or {}).get("lyrics_rom"))))
    ctx["socketio"].emit("sing:autosync", {
        "offset_ms": res["offset_ms"],
        "method": res.get("method", "align"),
        "tempo": res["tempo"], "matched": res["matched"],
        "phrases": res["phrases"]})


def _harvest_meta(ctx, ent: dict) -> None:
    """Fetch the provider's canonical music metadata (YouTube's tagged
    artist/track/album) once per entry and stash it on media_entry —
    seeded into lyric search, it beats every title-parse heuristic."""
    if not ent or ent.get("meta_track"):
        return                          # already harvested (or nothing)
    try:
        provider = ctx["providers"].get(ent.get("provider") or "")
        meta = (provider.music_meta(ent.get("source_id") or "")
                if provider and hasattr(provider, "music_meta") else None)
    except Exception:
        meta = None
    if not meta:
        ent.setdefault("meta_track", "")
        return                          # untagged upload — heuristics only
    for k in ("artist", "track", "album"):
        ent[f"meta_{k}"] = meta.get(k) or ""
    ctx["db"].execute(
        "UPDATE media_entry SET meta_artist=?, meta_track=?, meta_album=?"
        " WHERE id=?",
        (ent["meta_artist"], ent["meta_track"], ent["meta_album"],
         ent["entry_id"]))


def _fetch_lyrics(ent) -> list:
    """fetch_all with one whole-sweep retry — LRCLIB/SyncLRC flake often
    enough that a single empty result can't be trusted (the search loop
    retries per-query, but an exact-candidates-only miss slips through)."""
    from . import lyrics as lyrmod
    seeds = []
    if ent.get("meta_track"):
        seeds.append((ent.get("meta_artist") or "", ent["meta_track"]))
        seeds.append(("", ent["meta_track"]))
    recs = lyrmod.fetch_all(ent.get("artist") or "",
                            ent.get("title") or "",
                            duration=ent.get("duration_sec"),
                            seeds=seeds)
    if not recs:
        time.sleep(3)
        recs = lyrmod.fetch_all(ent.get("artist") or "",
                                ent.get("title") or "",
                                duration=ent.get("duration_sec"),
                                seeds=seeds)
    return recs


def _pick_lrc(recs, asr, want_title="", media_dur=0):
    """→ (best record, per-record coverage rows).

    The record whose lines best match the heard words wins — or None
    when every candidate is a same-title different song (or no ASR
    exists to judge by). Records whose length is closest to the media
    get a modest edge — same-song different edits share text but not
    timing. Coverage rows feed the analysis sidecar."""
    from . import songmap, lyrics as lyrmod
    want_q = {lyrmod.norm_word(b)
              for b in lyrmod._BRACKETED.findall(want_title or "")}
    want_q.discard("")
    heard_s = ((asr[-1][1] - asr[0][1]) / 1000
               if asr and len(asr) > 1 else 0)
    best, best_cov, scored = None, 3, []
    # pass 1 — evidence per record (spans/words/qualifiers). A qualified
    # variant is only penalized when an unqualified record also matches
    # the audio — otherwise "Edelweiss (1965 Original Soundtrack)" loses
    # to nothing and the pipeline falls back to ASR hallucination
    pool = []
    for r in (recs or [])[:12]:
        lines = ([t for _, t in r.get("synced") or []]
                 or r.get("plain") or [])
        row = {"artist": r.get("artist_name"), "track": r.get("track_name"),
               "src": r.get("source") or "lrclib",
               "synced_lines": len(r.get("synced") or []),
               "plain_lines": len(r.get("plain") or []),
               "spans": 0, "words": 0}
        scored.append(row)
        if not lines or not asr:
            pool.append(None)
            continue
        spans = songmap.map_line_words(asr, lines)
        used = len({a for sp in spans.values()
                    for a in sp["words"].values()})
        row["spans"], row["words"] = len(spans), used
        extra_q = {lyrmod.norm_word(b)
                   for b in lyrmod._BRACKETED.findall(
                       r.get("track_name") or "")} - want_q
        extra_q.discard("")
        if extra_q:
            row["qualifiers"] = sorted(extra_q)
        pool.append((r, used, extra_q, row))
    clean_match = any(u >= 3 and not q for p in pool
                      if p for r, u, q, row in [p])
    for p in pool:
        if p is None:
            continue
        r, used, extra_q, row = p
        # synced timing is worth more than a marginal coverage edge —
        # LRC anchors can time every line, plain text can't. But a
        # near-zero overlap means it's probably the wrong song anyway.
        score = used + (5 if r.get("synced") and used >= 3 else 0)
        # "(Disco Remix)"/"(Band Ver.)" share the lyric text but carry
        # different-arrangement timing — deprioritize qualifiers the
        # wanted title doesn't have, but only when a clean alternative
        # actually matched (a real remix still wins on words)
        if extra_q and clean_match:
            score -= 10
        synced = r.get("synced") or []
        dur = r.get("duration")
        # a synced record that stops well short of its own declared
        # duration is a partial transcription — a fuller record of the
        # same song is always better
        if synced and dur:
            cover = synced[-1][0] / 1000 / dur
            if cover < 0.55:
                row["partial_cover"] = round(cover, 2)
                score -= 8
        # closest-length bonus: declared duration near the media's, and
        # synced span near the heard singing span — same-song different
        # edits share text but differ in length
        if media_dur and dur:
            diff = abs(dur - media_dur)
            row["dur_diff"] = round(diff, 1)
            score += max(0, 4 - diff / 4)
        if synced and heard_s > 30:
            sdiff = abs((synced[-1][0] - synced[0][0]) / 1000 - heard_s)
            row["span_diff"] = round(sdiff, 1)
            score += max(0, 4 - sdiff / 20)
        if score > best_cov:
            best, best_cov = r, score
    return best, scored


def _write_analysis(media, ent, asr, lrc_scored, rec, curve, meta, chart):
    """<media>.analysis.json — everything the pipeline gathered for this
    entry: transcript, lyric-record verdicts, anchors, pitch curve. The
    chart keeps the result; this keeps the evidence."""
    try:
        t, f0, conf = curve if curve else ([], [], [])
        import numpy as _np
        voiced = _np.asarray(f0) > 0
        doc = {
            "media": media.name, "entry_id": ent.get("entry_id"),
            "generated_at": time.time(),
            "asr": [[w, round(x, 1)] for w, x in asr],
            "lrc_candidates": lrc_scored,
            "lrc_chosen": ((rec or {}).get("artist_name"),
                           (rec or {}).get("track_name")),
            "lrc_src": (rec or {}).get("source") or "lrclib",
            "pipeline": meta,
            "pitch": {"frames": len(t),
                      "hop_ms": round(float(t[1] - t[0]), 2) if len(t) > 1 else 0,
                      "voiced_frac": round(float(voiced.mean()), 3) if len(t) else 0,
                      "f0": [round(float(x), 1) for x in f0],
                      "conf": [round(float(x), 2) for x in conf]},
            "chart": None if chart is None else {
                "notes": len(chart.tracks["P1"]),
                "source": chart.source},
        }
        (media.parent / (media.stem + ".analysis.json")).write_text(
            json.dumps(doc, ensure_ascii=False))
    except Exception:
        pass


def _gen_chart(ctx, app, ent, media, mdir, vocal16, recs=None) -> bool:
    """No-chart path: lyric timing + ASR words + RMVPE pitch → .upsong.
    Returns True when a chart was attached."""
    from . import songmap, melody, genchart
    audio16 = (vocal16 if vocal16 is not None
               else songmap.decode_media(media))
    _stage(ctx, ent["entry_id"], "transcribing vocals")
    asr = songmap.transcribe_audio(audio16)
    if len(asr) < 20:
        app.logger.warning("genchart %s: only %d ASR words — skip",
                           media, len(asr))
        return False
    # LRCLIB candidates are title-guesses — same-title different songs
    # are common. Score each record's lines against the heard words and
    # keep the one that actually matches this media.
    _stage(ctx, ent["entry_id"], "fetching lyrics")
    if recs is None:
        recs = _fetch_lyrics(ent)
    rec, lrc_scored = _pick_lrc(recs, asr, ent.get("title") or "",
                                media_dur=ent.get("duration_sec") or 0)
    _stage(ctx, ent["entry_id"], "extracting pitch")
    curve = melody.extract_curve(audio16, mdir / "rmvpe.onnx")
    meta = {}
    _stage(ctx, ent["entry_id"], "building chart")
    chart = genchart.generate(
        ent.get("artist") or "", ent.get("title") or "", rec, asr, curve,
        meta_out=meta)
    _write_analysis(media, ent, asr, lrc_scored, rec, curve, meta, chart)
    if chart is None:
        app.logger.warning(
            "genchart %s: no chart (lyrics=%s, asr=%dw)",
            media, "lrc" if (rec or {}).get("synced") else
            ("plain" if (rec or {}).get("plain") else "none"), len(asr))
        return False
    path = genchart.save_for_entry(
        chart, ctx["config"].chart_library_dir, ent["entry_id"],
        ent.get("artist") or "", ent.get("title") or "")
    n = ctx["catalog"].attach_chart(
        ent["id"], path, media_entry_id=ent["entry_id"],
        source_repo="generated", confidence=0.4, verified=True,
        license_note="auto-generated chart")
    if n:
        ctx["socketio"].emit("state", ctx["queue"].snapshot())
        app.logger.warning("generated chart %s for entry %s (%d notes)",
                           path, ent["entry_id"], len(chart.tracks["P1"]))
        return True
    return False


def ensure_map(ctx, entry_id: int, item_id: int | None = None) -> None:
    ensure_processed(ctx, entry_id, item_id)


def _start_align(ctx, item_id: int, entry_id: int, chart_row) -> None:
    ensure_processed(ctx, entry_id, item_id)


def _audio_only(ctx, media_name: str) -> bool:
    """True when the served file has no real video stream — mp3s and files
    whose only 'video' is attached cover art. Result is cached per file so
    repeated now_playing pushes don't re-probe."""
    cache = ctx.setdefault("audio_only_cache", {})
    if media_name in cache:
        return cache[media_name]
    from .separate import has_real_video
    path = ctx["config"].media_path(media_name)
    cache[media_name] = not path.is_file() or not has_real_video(path)
    return cache[media_name]


def push_now_playing(ctx):
    """Send the TV player its current playback descriptor (or null).

    playback=None + waiting='download' means the TV should show a
    buffering/getting-media state until a later push (DownloadManager
    re-pushes when the file lands) — not skip.
    """
    item = ctx["queue"].now_playing()
    # end any previous singing session
    if ctx.get("sing"):
        ctx["sing"].stop_mic()
        ctx["sing"] = None
    # drop the previous singers' FX profiles before loading the new ones
    mon = ctx.get("monitor")
    if mon:
        mon.clear_source_fx()
    payload = None
    if item:
        entry = ctx["catalog"].entry(item["entry_id"])
        ref = ctx["providers"].resolve(entry) if entry else None
        # vocal-reduced karaoke variant — built at queue time by the
        # separation worker; the original stays the fallback. The
        # singer's per-song choice overrides the server default.
        inst = (entry or {}).get("inst_path")
        want_cut = (ctx["config"].vocal_cut if item.get("vocal_cut") is None
                    else bool(item["vocal_cut"]))
        inst_ok = bool(inst and want_cut
                       and ctx["config"].media_path(inst).is_file())
        if ref and inst_ok:
            ref.uri = f"/media/{inst}"
        # tell the TV when the served file has no real video track (mp3s,
        # cover-art-only containers) so it can run the visual scene
        if ref and entry:
            played = inst if inst_ok else entry.get("local_path")
            if played:
                ref.__dict__["audio_only"] = _audio_only(ctx, played)
        waiting = None
        if not ref and entry:
            waiting = "download" if entry["status"] == "downloading" else "unavailable"
            # a failed/stalled entry reaching the top of the queue gets
            # its download re-kicked — ensure() dedupes in-flight work
            # and retries 'failed', so transient batch failures (429s,
            # restart kills) recover instead of stranding the TV
            if entry["status"] != "downloading":
                ctx["downloads"].ensure(entry)
                waiting = "download"
        scoring = bool(entry and entry.get("chart_verified"))
        # hold the countdown until the whole queue-time pipeline has
        # finished — loudness, stems, lyric/chart gen and the warp all run
        # under map_inflight; an existing chart doesn't mean the worker
        # is done (it may still be normalizing or replacing the chart)
        if ref and item["entry_id"] in ctx.get("map_inflight", ()):
            ref = None
            waiting = "processing"
        lyrics = (ctx["catalog"].chart_payload(
                      item["entry_id"], rom=bool(item.get("lyrics_rom")))
                  if scoring else None)
        # mic-handoff countdown: once media is ready, play_at marks when the
        # TV may start the video (set once per item so re-pushes don't reset it)
        play_at = None
        if ref and ctx["config"].intermission_sec > 0:
            play_at = ctx.setdefault("play_at", {}).setdefault(
                item["id"], time.time() + ctx["config"].intermission_sec)
        payload = {"item": item, "entry": entry,
                   "playback": ref.__dict__ if ref else None,
                   "waiting": waiting,
                   "stage": (ctx.get("map_stage", {}).get(item["entry_id"])
                             if waiting == "processing" else None),
                   "scoring": scoring,
                   "play_at": play_at,
                   "lyrics": lyrics,
                   "music_volume": float(
                       ctx["db"].get_setting("music_volume", "1") or 1)}
        # spin up the live scoring session for verified charts
        chart_row = (ctx["catalog"].chart_row_for_entry(item["entry_id"])
                     if scoring else None)
        if chart_row:
            from .sing import SingSession
            p1_track = item.get("part_choice") or "P1"
            track_performers = {p1_track: item["performer_id"]}
            if item.get("performer2_id"):
                track_performers["P2" if p1_track == "P1" else "P1"] = (
                    item["performer2_id"])
            micman = ctx.get("micman")
            mics = (micman.resolve(track_performers) if micman else {})
            from .catalog import parse_sync_warp
            tempo, warp = parse_sync_warp(
                chart_row["sync_warp"], item["entry_id"])
            ctx["sing"] = SingSession(
                chart_row["file_path"], chart_row["sync_offset_ms"],
                tracks=list(track_performers),
                difficulty=ctx["config"].difficulty, mics=mics, warp=warp,
                tempo=tempo)
            payload["mic"] = ctx["sing"].start_mic(ctx["config"].mic_device)
            # no learned timing for this version yet — analyze the media
            # file itself: chroma alignment finds the base offset, the
            # media's tempo ratio, and where instrumentals actually end.
            # generated charts are born media-timed — aligning them is
            # meaningless, so they never spawn the worker.
            if (not warp and item.get("local_path")
                    and chart_row["source_repo"] != "generated"):
                _start_align(ctx, item["id"], item["entry_id"], chart_row)
            # each singer's saved voice FX follows them onto their mic —
            # duets can carry two different profiles at once
            if mon:
                for track, cap in mics.items():
                    dev = getattr(cap, "device", None)
                    pid = track_performers.get(track)
                    fx = ctx["db"].performer_fx(pid) if pid else {}
                    if dev and fx:
                        mon.set_source_fx(dev, **fx)
    # watchdog marker: a playable payload went out — positions should
    # start flowing for this item soon after (pre-song + buffering)
    if payload and payload.get("playback"):
        ctx["np_pushed"] = (item["id"], time.time())
        ctx.pop("np_waiting", None)
    elif payload and payload.get("waiting") == "processing":
        # remember when the hold began (setdefault: re-pushes must not
        # push the deadline out) so the kiosk watchdog can force-release
        # a wait that outlives the pipeline
        ctx.setdefault("np_waiting", (item["id"], time.time()))
    ctx["socketio"].emit("now_playing", payload)
    ctx["socketio"].emit("state", ctx["queue"].snapshot())
