"""Per-song station pipeline — the Pi's queue-time work, GPU powered.

Order is deliberate: lyrics are fetched BEFORE any transcription runs.
Fetched lyrics (LRCLIB/SyncLRC) are the chart's text; ASR is only the
timing backbone that anchors each line to heard words. A provider miss
is the only case that degrades to ASR-as-text — and then whisper's
large-v3 covers ~99 languages including Hindi, where the Pi's needle
ASR couldn't follow at all.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

STAGES = ("downloading", "metadata", "normalizing", "separating",
          "lyrics", "transcribing", "pitch", "chart", "artwork", "done")


def _noop(_stage: str) -> None:
    pass


def _seed_pairs(meta: dict) -> list[tuple[str, str]]:
    if not meta.get("track"):
        return []
    return [(meta.get("artist") or "", meta["track"])]


def process_song(rec: dict, dest: Path, models_dir: Path,
                 on_stage=_noop, norm_target: float = -14.0) -> dict:
    """One entry → finished bundle files in ``dest`` + manifest record.

    ``rec`` needs provider/source_id (+ optionally artist/title/
    duration_sec/thumbnail) — the shapes YouTubeProvider.lookup and
    .playlist produce. Returns the manifest record (or raises — the
    caller marks the job failed)."""
    from ultrapistar import genchart, loudness, melody, separate, songmap
    from ultrapistar.lyrics import fetch_all
    from ultrapistar.pipeline import _pick_lrc, _write_analysis
    from ultrapistar.providers.youtube import YouTubeProvider
    from . import gpu

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    # everything below happens in <dest>/incomplete — the prepared
    # root only ever receives a song once it's fully processed, so a
    # stick copied mid-run never ships half-baked files
    work = dest / "incomplete"
    work.mkdir(exist_ok=True)
    charts_dir = work / "charts"
    charts_dir.mkdir(exist_ok=True)
    provider = YouTubeProvider(work)

    on_stage("downloading")
    if rec.get("local_file"):
        # media already on disk (pulled from the Pi / local staging /
        # a restarted queue) — work on it inside incomplete/
        import shutil
        media = Path(rec["local_file"])
        target = work / media.name
        if media.resolve() != target.resolve():
            if not target.exists():
                shutil.copy2(media, target)
            media = target
    else:
        fname = provider.download(rec, dest_dir=work)
        media = work / fname
    rel = media.name

    on_stage("metadata")
    meta = (provider.music_meta(rec["source_id"])
            if rec.get("provider") == "youtube" else None) or {}
    artist = meta.get("artist") or rec.get("artist") or "Unknown"
    title = meta.get("track") or rec.get("title") or media.stem

    on_stage("normalizing")
    if not loudness.is_normed(media, norm_target):
        loudness.normalize(media, target_i=norm_target)

    on_stage("separating")
    inst_rel = media.stem + ".inst.mp4"
    voc_wav = media.parent / (media.stem + ".vocals.wav")
    if voc_wav.exists() and (media.parent / inst_rel).exists():
        import soundfile as sf
        vocal16, _ = sf.read(str(voc_wav), dtype="float32")
    else:
        vocals, accomp = gpu.separate_stems(media)
        if not (media.parent / inst_rel).exists():
            mix = separate.karaoke_mix(vocals, accomp, guide=0.15)
            separate.karaoke_mp4(media, mix, media.parent / inst_rel)
        vocal16 = separate.mono16(vocals)
        separate.write_wav(voc_wav, vocal16, sr=16000)
    if (media.parent / inst_rel).exists():
        loudness.normalize(media.parent / inst_rel, target_i=norm_target)

    on_stage("lyrics")
    seeds = _seed_pairs(meta)
    duration = rec.get("duration_sec")
    recs = fetch_all(artist, title, duration=duration, seeds=seeds)

    on_stage("transcribing")
    asr, language = gpu.transcribe_vocals(voc_wav)
    if len(asr) < 20:
        raise RuntimeError(f"ASR produced only {len(asr)} words")

    rec_pick, scored = _pick_lrc(recs, asr, title,
                                 media_dur=duration or 0)

    on_stage("pitch")
    curve = melody.extract_curve(vocal16, Path(models_dir) / "rmvpe.onnx")

    on_stage("chart")
    ent = {"entry_id": None, "artist": artist, "title": title,
           "duration_sec": duration}
    meta_out: dict = {}
    chart = genchart.generate(artist, title, rec_pick, asr, curve,
                              meta_out=meta_out)
    _write_analysis(media, ent, asr, scored, rec_pick, curve,
                    meta_out, chart)
    chart_path = None
    if chart is not None:
        from ultrapistar.charts import save_native
        safe = re.sub(r"[^A-Za-z0-9 _-]+", "",
                      f"{artist} - {title}")[:80] or media.stem
        chart_path = save_native(chart, charts_dir / f"{safe}.upsong",
                                 source=chart.source)
    else:
        log.warning("no chart for %s — bundle ships media only", media)

    on_stage("artwork")
    art_rel = _fetch_art(media, artist, title,
                         rec.get("thumbnail") or "")

    # per-song folder: <root>/<artist>/<title>/<files> — the manifest
    # paths below are root-relative so the Pi and the Switch resolve
    # them identically on the prepared stick
    on_stage("organizing")
    from ultrapistar.mediastore import song_reldir
    import shutil
    reldir = song_reldir(artist, title, media.stem)
    song_dir = dest / reldir
    song_dir.mkdir(parents=True, exist_ok=True)
    for p in media.parent.glob(media.stem + ".*"):
        if p.is_file() and p.parent != song_dir:
            tgt = song_dir / p.name
            tgt.unlink(missing_ok=True)
            shutil.move(str(p), tgt)
    chart_rel = ""
    if chart_path:
        chart_dest = song_dir / chart_path.name
        if chart_dest.exists() and chart_dest != chart_path:
            chart_dest.unlink()           # replace a stale prior chart
        if not chart_dest.exists():
            shutil.move(str(chart_path), chart_dest)
        chart_rel = f"{reldir}/{chart_path.name}"

    on_stage("done")
    return {
        "artist": artist, "title": title,
        "language": language or "",
        "provider": rec.get("provider") or "youtube",
        "source_id": rec["source_id"],
        "duration_sec": duration,
        "thumbnail": rec.get("thumbnail") or "",
        "local_path": f"{reldir}/{media.name}",
        "inst_path": (f"{reldir}/{inst_rel}"
                      if (song_dir / inst_rel).exists() else ""),
        "chart": chart_rel,
        "art": (f"{reldir}/{art_rel}"
                if art_rel and (song_dir / art_rel).exists() else ""),
        "provenance": "station",
    }


def _fetch_art(media: Path, artist: str, title: str,
               thumb_url: str) -> str:
    """<stem>.jpg sidecar next to the media — survives flat roots where
    a shared cover.jpg would collide. Chain: iTunes/Deezer → provider
    thumbnail → video frame. Returns the rel path, or "" (never raises —
    art is nice-to-have)."""
    from ultrapistar.artwork import save_artwork
    out = media.with_suffix(".jpg")
    try:
        if save_artwork(artist, title, out, thumb_url=thumb_url):
            return out.name
    except Exception:
        pass
    # last resort: a frame out of the video itself
    if media.suffix.lower() in (".mp4", ".mkv", ".webm"):
        import subprocess
        for ts in (45, 15, 90):
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", str(ts), "-i", str(media),
                     "-frames:v", "1", "-vf", "scale=600:-2",
                     "-q:v", "4", str(out)],
                    capture_output=True, timeout=30, check=False)
            except Exception:
                break
            if out.is_file() and out.stat().st_size > 5000:
                return out.name
    if out.is_file() and out.stat().st_size <= 5000:
        out.unlink(missing_ok=True)
    return ""
