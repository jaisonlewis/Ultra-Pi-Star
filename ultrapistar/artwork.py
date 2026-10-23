"""Album/single artwork lookup + local cache.

Chain: iTunes Search API -> Deezer API (both free, no key). Results are
downloaded into the artwork dir and referenced by local path so the Pi
works offline once cached. Falls back to the provider thumbnail (e.g.
YouTube) when no artwork match is found.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.parse
import urllib.request
from pathlib import Path

UA = {"User-Agent": "ultrapistar/0.4 (+https://github.com)"}
TIMEOUT = 8

_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


def _clean(s: str) -> str:
    s = re.sub(r"[\(\[](karaoke|cover|version|instrumental|lyrics?|official|hd|4k)[^\)\]]*[\)\]]",
               " ", s, flags=re.I)
    return _NON_ALNUM.sub(" ", s.lower()).strip()


def _get_json(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _itunes_art(artist: str, title: str) -> str | None:
    q = urllib.parse.quote(f"{_clean(artist)} {_clean(title)}".strip())
    data = _get_json(
        f"https://itunes.apple.com/search?term={q}&entity=song&limit=5")
    if not data or not data.get("results"):
        return None
    want = (_clean(artist), _clean(title))
    best = None
    for item in data["results"]:
        got = (_clean(item.get("artistName", "")),
               _clean(item.get("trackName", "")))
        art = item.get("artworkUrl100")
        if not art:
            continue
        score = (got[0] == want[0]) + (want[1] in got[1] or got[1] in want[1])
        if best is None or score > best[0]:
            best = (score, art)
    # require at least an artist or title match — otherwise the "best" is
    # just a random result (e.g. demo band names matching nothing real)
    if not best or best[0] == 0:
        return None
    # upsize 100px -> 600px artwork
    return best[1].replace("100x100", "600x600")


def _deezer_art(artist: str, title: str) -> str | None:
    q = urllib.parse.quote(f'artist:"{artist}" track:"{title}"')
    data = _get_json(f"https://api.deezer.com/search?q={q}&limit=5")
    if not data or not data.get("data"):
        return None
    want_a, want_t = _clean(artist), _clean(title)
    for item in data["data"]:
        got_a = _clean(item.get("artist", {}).get("name", ""))
        got_t = _clean(item.get("title", ""))
        if not (got_a == want_a or want_t in got_t or got_t in want_t):
            continue
        art = item.get("album", {}).get("cover_xl") or \
            item.get("album", {}).get("cover_big")
        if art:
            return art
    return None


def lookup_artwork(artist: str, title: str) -> str | None:
    """Best remote artwork URL for artist/title, or None."""
    return _itunes_art(artist, title) or _deezer_art(artist, title)


def save_artwork(artist: str, title: str, dest: Path,
                 thumb_url: str = "") -> bool:
    """Fetch album art for artist/title into ``dest`` (iTunes → Deezer →
    provider thumbnail). Returns True when a real image landed; never
    raises — art is nice-to-have."""
    for url in (lookup_artwork(artist, title), thumb_url):
        if not url:
            continue
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=UA),
                    timeout=TIMEOUT) as r:
                img = r.read()
            if len(img) > 5000:
                Path(dest).write_bytes(img)
                return True
        except Exception:
            continue
    return False


class ArtworkFetcher:
    """Caches artwork locally in the background and stores the serving
    path on the media entry."""

    def __init__(self, db, artwork_dir: Path):
        self.db = db
        self.dir = Path(artwork_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._inflight: set[int] = set()
        self._lock = threading.Lock()

    def ensure(self, entry_id: int, artist: str, title: str,
               existing_thumb: str = "") -> None:
        """Queue a background fetch; keeps an existing thumb if found."""
        with self._lock:
            if entry_id in self._inflight:
                return
            self._inflight.add(entry_id)
        threading.Thread(
            target=self._run, args=(entry_id, artist, title, existing_thumb),
            daemon=True).start()

    def _run(self, entry_id: int, artist: str, title: str,
             existing_thumb: str) -> None:
        try:
            url = lookup_artwork(artist, title)
            if not url:
                return
            ext = ".png" if ".png" in url.lower() else ".jpg"
            dest = self.dir / f"entry-{entry_id}{ext}"
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=UA),
                    timeout=TIMEOUT) as r:
                dest.write_bytes(r.read())
            self.db.execute(
                "UPDATE media_entry SET thumbnail=? WHERE id=?",
                (f"/api/art/{dest.name}", entry_id))
        except Exception:
            pass  # art is nice-to-have; never break ingest over it
        finally:
            with self._lock:
                self._inflight.discard(entry_id)
