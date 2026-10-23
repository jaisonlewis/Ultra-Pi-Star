"""Serial job queue — one song in the GPU pipeline at a time.

URL intake happens on the caller's thread (fast yt-dlp metadata);
heavy work runs on a single daemon worker so the GUI stays live and
the disk/GPU never thrash."""
from __future__ import annotations

import itertools
import queue
import re
import threading
import traceback
from pathlib import Path


_MEDIA_EXTS = (".mp4", ".mp3", ".m4a", ".webm", ".mkv")


def _tokens(s: str) -> set:
    return {t for t in re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split()
            if t}


def _chan_tokens(s: str) -> set:
    """Channel/uploader names → plausible artist tokens ("SiaVEVO" also
    yields "sia")."""
    toks = _tokens(s)
    out = set(toks)
    for t in toks:
        for suf in ("vevo", "official", "topic", "tv"):
            if t.endswith(suf) and len(t) > len(suf) + 2:
                out.add(t[:-len(suf)])
    return out


def _is_known_song(rec: dict, pairs) -> bool:
    """Song-level dedup for re-uploads under a different video id.

    A manifest (artist, title) counts as already present when every
    title token shows in the entry's video title AND every artist token
    shows in the video title or the (de-VEVO'd) channel name. Deliberately
    strict — distinct versions ("Chandelier Live") can still collide and
    skip, which is accepted: a skipped dupe beats a doubled library."""
    et = _tokens(rec.get("title") or "")
    ea = _chan_tokens(rec.get("artist") or "")
    if not et:
        return False
    for a, t in pairs:
        ta, tt = _tokens(a), _tokens(t)
        if ta and tt and tt <= et and ta <= (ea | et):
            return True
    return False


def _is_source_media(p: Path) -> bool:
    """Original media only — derived sidecars (inst/vocals) never get
    their own jobs."""
    return (p.is_file() and p.suffix.lower() in _MEDIA_EXTS
            and ".inst." not in p.name
            and not p.name.endswith(".vocals.wav"))


def _folder_rec(p: Path, row: dict | None) -> dict:
    """Catalog row (or a bare ``yt-<id>`` filename) → a job rec the
    pipeline can run without touching YouTube."""
    import re
    m = re.fullmatch(r"yt-([A-Za-z0-9_-]{6,})", p.stem)
    return {
        "provider": (row or {}).get("provider")
                    or ("youtube" if m else "local"),
        "source_id": (row or {}).get("source_id")
                     or (m.group(1) if m else p.stem),
        "artist": (row or {}).get("artist") or "",
        "title": (row or {}).get("title") or p.stem,
        "duration_sec": (row or {}).get("duration_sec"),
        "thumbnail": (row or {}).get("thumbnail") or "",
        "local_path": p.name,
        "local_file": str(p),
    }


class Station:
    def __init__(self, dest: Path, models_dir: Path, on_event,
                 push: dict | None = None, tier: str | None = None):
        """``on_event(job_id, event, payload)`` — events: stage, done,
        failed, pushed. ``push``: {"url": pi, "key": host_key} enables
        LAN push after each finished song. ``tier`` selects the GPU
        model set (see gpu.TIERS); None leaves the current/default."""
        if tier:
            from . import gpu
            gpu.configure(tier)
        self.dest = Path(dest)
        self.models_dir = Path(models_dir)
        self.on_event = on_event
        self.push = push
        self.jobs: dict[int, dict] = {}
        self._q: queue.Queue = queue.Queue()
        self._ids = itertools.count(1)
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._loop, daemon=True, name="station-worker")
        self._worker.start()

    def add(self, rec: dict) -> int:
        jid = next(self._ids)
        job = {"id": jid, "rec": rec, "status": "queued", "stage": ""}
        self.jobs[jid] = job
        self._q.put(job)
        return jid

    def expand_and_add(self, url: str) -> tuple[str, list[int]]:
        """Playlist URL → a job per track; single video → one job.
        Returns (title, job_ids). Raises on dead/unexpandable URLs."""
        from ultrapistar.providers.youtube import YouTubeProvider
        from . import bundle
        prov = YouTubeProvider(self.dest)
        known = bundle.known_ids(self.dest)
        pairs = bundle.known_songs(self.dest)
        pl = prov.playlist(url)
        if pl:
            entries = pl["entries"]
            title = pl["title"]
        else:
            from ultrapistar.providers.youtube import video_id_from_url
            vid = video_id_from_url(url)
            entry = prov.lookup(vid) if vid else None
            if not entry:
                raise ValueError("not a YouTube video/playlist URL")
            entries, title = [entry], entry.get("title") or url
        ids, skipped = [], []
        for e in entries:
            if e.get("source_id") in known or _is_known_song(e, pairs):
                skipped.append(e)
                continue
            ids.append(self.add(e))
        return title, ids, len(skipped)

    def add_folder(self, folder: Path,
                   include_charted: bool = False) -> tuple[int, int]:
        """Enqueue every media file in a staging folder that still needs
        processing. A ``pi-catalog.json`` (exported from the Pi's
        /api/manage/songs) supplies catalog metadata and tells us which
        entries already carry a verified chart; bare ``yt-<id>`` files
        self-describe. ``include_charted`` re-processes even charted
        entries — a quality upgrade pass over the whole library.
        Returns (added, skipped)."""
        import json
        from . import bundle
        folder = Path(folder)
        cat = {}
        catfile = folder / "pi-catalog.json"
        if catfile.is_file():
            try:
                doc = json.loads(catfile.read_text(encoding="utf-8"))
                for s in doc.get("songs", []):
                    if s.get("local_path"):
                        cat[s["local_path"]] = s
            except (OSError, ValueError):
                pass
        known = bundle.known_ids(self.dest)
        pairs = bundle.known_songs(self.dest)
        added = skipped = 0
        for p in sorted(folder.iterdir()):
            if not _is_source_media(p):
                continue
            row = cat.get(p.name)
            if cat and row is None:
                continue                    # orphan fragment
            if row and row.get("scored") and not include_charted:
                skipped += 1
                continue                    # verified chart already
            rec = _folder_rec(p, row)
            if rec["source_id"] in known or _is_known_song(rec, pairs):
                skipped += 1
                continue
            self.add(rec)
            added += 1
        return added, skipped

    def _loop(self):
        from .pipeline import process_song
        from . import bundle
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                def stage(s, _j=job):
                    _j["stage"] = s
                    self.on_event(_j["id"], "stage", s)
                rec = bundle_rec = process_song(
                    job["rec"], self.dest, self.models_dir,
                    on_stage=stage)
                job["record"] = bundle_rec
                bundle.append_song(self.dest, bundle_rec)
                chart_path = ((self.dest / bundle_rec["chart"])
                              if bundle_rec.get("chart") else None)
                self.on_event(job["id"], "done", bundle_rec)
                if self.push and self.push.get("url"):
                    stage("pushing to Pi")
                    resp = bundle.push_to_pi(
                        bundle_rec, self.dest, chart_path,
                        self.push["url"], self.push["key"])
                    self.on_event(job["id"], "pushed", resp)
            except Exception as exc:
                job["status"] = "failed"
                job["error"] = str(exc)
                traceback.print_exc()
                self.on_event(job["id"], "failed", str(exc))
            finally:
                self._q.task_done()

    def stop(self):
        self._stop.set()
