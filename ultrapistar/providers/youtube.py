"""YouTube provider — search via yt-dlp, ingest to local files for playback.

Mirrors PiKaraoke's model: the phone searches YouTube, the server downloads
the chosen video once into the media dir, and the TV plays the local file.
Downloads are for the user's own karaoke session; enable/disable with
``UPS_PROVIDERS``.
"""
from __future__ import annotations

from pathlib import Path

from .base import PlaybackRef
from .local import MIME_BY_EXT

# never above 1080p — 1080 preferred, then 720, then lower; YouTube
# serves >720p only as separate video+audio streams (merged to mp4)
FORMAT = ("bv*[height<=1080]+ba[ext=m4a]/bv*[height<=1080]+ba/"
          "b[height<=1080]")


def _ydl(opts: dict):
    from yt_dlp import YoutubeDL  # imported lazily; provider is optional
    base = {"quiet": True, "no_warnings": True, "noplaylist": True}
    base.update(opts)
    return YoutubeDL(base)


class YouTubeProvider:
    name = "youtube"

    def __init__(self, media_dir: Path, extra_dirs: tuple = ()):
        self.media_dir = Path(media_dir)
        self.media_dirs = (self.media_dir,
                           *(Path(d) for d in extra_dirs))

    # -- remote catalog search ------------------------------------------------
    # karaoke-channel fingerprints — official lyric videos are NOT here:
    # they carry the real vocal track, which the ASR mapper needs
    _KARAOKE_HINTS = ("karaoke", "sing king", "karafun", "singking",
                      "sing along", "singalong", "backing track",
                      "minus one", "no vocals", "instrumental version")

    def _flat(self, ydl, term: str, limit: int):
        info = ydl.extract_info(f"ytsearch{limit}:{term}", download=False)
        return (info or {}).get("entries", [])

    def search(self, query: str, limit: int = 10) -> list[dict]:
        # two passes: plain query surfaces official/VEVO uploads, the
        # karaoke term keeps the dedicated sing-along channels — merged
        # and deduped, each result tagged so the UI can badge the kind
        ydl_opts = {"extract_flat": True, "skip_download": True}
        with _ydl(ydl_opts) as ydl:
            seen = {}
            for karaoke_term in (False, True):
                term = f"{query} karaoke" if karaoke_term else query
                try:
                    entries = self._flat(ydl, term, limit)
                except Exception:
                    entries = []
                for e in entries:
                    if not e or not e.get("id") or e["id"] in seen:
                        continue
                    seen[e["id"]] = e
        out = []
        for vid, e in seen.items():
            title = e.get("title") or "Untitled"
            chan = e.get("uploader") or e.get("channel") or "YouTube"
            hay = f"{title} {chan}".lower()
            karaoke = any(h in hay for h in self._KARAOKE_HINTS)
            out.append({
                "provider": self.name,
                "source_id": vid,
                "title": title,
                "artist": chan,
                "duration_sec": e.get("duration"),
                "thumbnail": (e.get("thumbnails") or [{}])[-1].get("url"),
                "remote": True,
                "entry_id": None,
                "scoring_available": False,
                "possible_chart": False,
                "is_duet": False,
                "karaoke": bool(karaoke),
            })
        # official videos only — karaoke versions stay hidden unless the
        # song exists nowhere else (fallback keeps obscure songs playable)
        normal = [r for r in out if not r["karaoke"]]
        return normal or out

    def playlist(self, url: str) -> dict | None:
        """Expand a YouTube playlist URL (list= param) into flat entries.
        Dead/private videos are dropped. Returns None for non-playlist
        URLs or when extraction yields nothing."""
        if "list=" not in url:
            return None
        with _ydl({"extract_flat": "in_playlist", "skip_download": True,
                   "noplaylist": False}) as ydl:
            info = ydl.extract_info(url, download=False)
        out = []
        for e in (info or {}).get("entries") or []:
            if not e or not e.get("id"):
                continue
            title = e.get("title")
            # [Deleted video] / [Private video] can't download — skip now
            # instead of failing per-entry later
            if not title or title.startswith("["):
                continue
            out.append({
                "provider": self.name,
                "source_id": e["id"],
                "title": title,
                "artist": e.get("uploader") or e.get("channel") or "",
                "duration_sec": e.get("duration"),
                "thumbnail": (e.get("thumbnails") or [{}])[-1].get("url"),
            })
        if not out:
            return None
        return {"title": (info or {}).get("title") or "Playlist",
                "entries": out}

    def lookup(self, source_id: str) -> dict | None:
        """Metadata for one video by id — the paste-a-link path. Full
        (non-flat) extract so private/blocked videos surface as errors."""
        url = f"https://www.youtube.com/watch?v={source_id}"
        try:
            with _ydl({"skip_download": True, "quiet": True}) as ydl:
                e = ydl.extract_info(url, download=False)
        except Exception:
            return None
        if not e:
            return None
        title = e.get("title") or "Untitled"
        chan = e.get("uploader") or e.get("channel") or "YouTube"
        hay = f"{title} {chan}".lower()
        return {
            "provider": self.name,
            "source_id": source_id,
            "title": title,
            "artist": chan,
            "duration_sec": e.get("duration"),
            "thumbnail": ((e.get("thumbnails") or [{}])[-1].get("url")),
            "remote": True,
            "entry_id": None,
            "scoring_available": False,
            "possible_chart": False,
            "is_duet": False,
            "karaoke": any(h in hay for h in self._KARAOKE_HINTS),
        }

    # YouTube Music's own InnerTube player endpoint — returns the
    # canonical song metadata ("Running Wild" / "Jin") even for label
    # uploads whose watch page exposes no "Music in this video" panel.
    # yt-dlp's artist/track extraction is dead since YouTube moved the
    # data out of the page response; this is the deterministic source.
    _YTM_PLAYER = ("https://music.youtube.com/youtubei/v1/player"
                   "?prettyPrint=false")
    _YTM_CTX = {"context": {"client": {
        "clientName": "WEB_REMIX",
        "clientVersion": "1.20251001.00.00",
        "hl": "en"}}}

    def music_meta(self, source_id: str) -> dict | None:
        """Canonical (artist, track) for an official music upload via
        YouTube Music's videoDetails — author is the artist even when
        the video sits on a label channel (HYBE LABELS → "Jin"), and
        title is the bare track name with the decoration stripped.
        UGC uploads (covers, lyric videos) report no musicVideoType —
        they fall back to the title-parse heuristics."""
        import json
        import urllib.request
        try:
            body = json.dumps(
                dict(self._YTM_CTX, videoId=source_id)).encode()
            req = urllib.request.Request(
                self._YTM_PLAYER, data=body,
                headers={"Content-Type": "application/json",
                         "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                vd = (json.loads(r.read()) or {}).get("videoDetails") or {}
        except Exception:
            return None
        if vd.get("musicVideoType") not in (
                "MUSIC_VIDEO_TYPE_OMV", "MUSIC_VIDEO_TYPE_ATV"):
            return None
        from ..lyrics import _clean_title
        track = _clean_title(vd.get("title") or "")
        artist = (vd.get("author") or "").strip()
        # YTM occasionally still prefixes the track with the artist
        # ("Queen - Bicycle Race") — canonical artist makes it redundant
        for pre in (f"{artist} - ", f"{artist} – ", f"{artist}: "):
            if artist and track.startswith(pre):
                track = track[len(pre):]
                break
        if not (artist or track):
            return None
        return {"artist": artist, "track": track, "album": ""}

    # -- local playback after ingest ------------------------------------------
    def resolve(self, entry: dict) -> PlaybackRef | None:
        rel = entry.get("local_path")
        if not rel:
            return None
        path = next((d / rel for d in self.media_dirs
                     if (d / rel).is_file()), None)
        if not path:
            return None
        mime = MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")
        return PlaybackRef("file", f"/media/{rel}", mime, entry.get("duration_sec"))

    # -- ingest -----------------------------------------------------------------
    def download(self, entry: dict, dest_dir: Path | None = None) -> str:
        """Blocking download -> returns filename inside the media dir."""
        dest = Path(dest_dir or self.media_dir)
        dest.mkdir(parents=True, exist_ok=True)
        stem = f"yt-{entry['source_id']}"
        existing = next(dest.glob(f"{stem}.*"), None)
        if existing:
            return existing.name
        url = f"https://www.youtube.com/watch?v={entry['source_id']}"
        # client order matters: the default (web/visionos) client exposes
        # the full format ladder incl. ≤1080p DASH; the android fallback
        # survives stricter bot-checks but is SABR-limited to low-res
        # progressive files. Try default first, degrade gracefully.
        attempts = [
            {},
            {"extractor_args": {"youtube": {"player_client": ["android"]}}},
        ]
        last_exc = None
        for extra in attempts:
            try:
                opts = {"format": FORMAT, "merge_output_format": "mp4",
                        "outtmpl": str(dest / f"{stem}.%(ext)s"), **extra}
                with _ydl(opts) as ydl:
                    ydl.download([url])
                break
            except Exception as exc:
                last_exc = exc
        else:
            raise last_exc
        found = next(dest.glob(f"{stem}.*"), None)
        if not found:
            raise RuntimeError(f"download produced no file for {entry['source_id']}")
        return found.name
