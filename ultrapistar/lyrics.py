"""Online lyrics via LRCLIB — synced (LRC) and plain lyrics for a track.

LRCLIB is free and keyless. ``syncedLyrics`` gives per-line timestamps for
the official recording; ``plainLyrics`` is the text-only fallback. Both feed
the song-map builder: LRC times are recording-relative (a video may add an
intro slate), so the ASR pass re-anchors lines to the media either way.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request

API = "https://lrclib.net/api/get"
SEARCH_API = "https://lrclib.net/api/search"
_UA = "UltraPiStar/1.0 (https://github.com/)"

_LRC_TS = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")

# youtube-title cleanup: bracketed segments like "(Official Video)",
# "[Color Coded Han_Rom_Eng]", "(가사)" carry flavor, not title words
_BRACKETED = re.compile(r"[\(\[][^\)\]]{0,90}[\)\]]")
_JUNKY = re.compile(
    r"(?i)(lyrics?|가사|color.?coded|han.?rom.?eng|official|music video|"
    r"\bm/?v\b|audio|sub(?!l)|english|español|espanol|\bhd\b|\b4k\b|"
    r"\bcc\b|karaoke|fan.?made|ver\.?|version)")
_TRAIL_JUNK = re.compile(
    r"(?i)\b(lyrics?|lyric video|official|audio|video|m/?v)\b")
_QUOTED = re.compile(
    r"^([^'\"‘’“”]{2,60}?)\s*['\"‘’“”]([^'\"‘’“”]{1,80})['\"‘’“”]")
_LATIN_INNER = re.compile(r"[\(\[]\s*([A-Za-z][A-Za-z0-9 .&'!-]{1,40}?)\s*[\)\]]")
_NON_LATIN = re.compile(r"[^\x00-\x7f]")
_CHAN_JUNK = re.compile(
    r"(?i)\s*(?:-\s*topic|vevo|\s+official|\s+music)\s*$")


def _chan_clean(a: str) -> str:
    """'SiaVEVO'/'Artist - Topic'/'BBC Music' → the bare artist name —
    channel decorations make the uploader worthless as a lyric query."""
    return _CHAN_JUNK.sub("", a).strip()


def _latin_alias(s: str) -> str:
    """'진 (Jin)' → 'Jin': Hangul-first names carry the romanized alias in
    parens — searches need the LATIN part, not the Hangul it decorates."""
    m = _LATIN_INNER.search(s)
    if m and _NON_LATIN.search(_BRACKETED.sub("", s)):
        return m.group(1).strip()
    return ""


def _clean_title(t: str) -> str:
    t = _BRACKETED.sub(
        lambda m: "" if _JUNKY.search(m.group(0)) else m.group(0), t)
    t = _TRAIL_JUNK.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip(" \t-–—'\"")


def _plain(s: str) -> str:
    """Strip ALL bracketed segments — "Jungkook (정국)" → "Jungkook".
    Romanized/Hangul aliases in parens defeat exact-match lookups."""
    return re.sub(r"\s+", " ", _BRACKETED.sub("", s)).strip(" \t-–—")


def yt_candidates(artist: str, title: str) -> list[tuple[str, str]]:
    """(uploader, yt_title) → ordered (artist, title) guesses to try
    against LRCLIB. Lyric/fan videos bury the real metadata in the
    title: ``BTS 'Into the Sun' Lyrics (...)`` or ``A - B (Video)``."""
    cands: list[tuple[str, str]] = []
    t = title.strip()
    m = _QUOTED.match(t)
    if m:
        cands.append((_clean_title(m.group(1)), _clean_title(m.group(2))))
        # the quoted piece alone is often the exact song title — search it
        # bare too ("… 'Running Wild' Official MV" → "", "Running Wild")
        cands.append(("", _clean_title(m.group(2))))
    for sep in (" - ", " – ", " — ", " | "):
        if sep in t:
            a, tt = t.split(sep, 1)
            cands.append((_clean_title(a), _clean_title(tt)))
            break
    cands.append((artist, _clean_title(t)))
    # bracketed aliases ("Jungkook (정국)") break exact lookups — retry
    # each guess with parens fully stripped, and for Hangul-first names
    # retry with the romanized alias itself ("진 (Jin)" → "Jin")
    for ar, ti in list(cands):
        if (_plain(ar), _plain(ti)) != (ar, ti):
            cands.append((_plain(ar) or ar, _plain(ti) or ti))
        alt = _latin_alias(ar)
        if alt and alt != ar:
            cands.append((alt, ti))
    # channel decorations poison the artist field — "SiaVEVO" never
    # exact-matches; retry guesses with the bare channel name
    for ar, ti in list(cands):
        cc = _chan_clean(ar)
        if cc and cc != ar:
            cands.append((cc, ti))
    # musical/film/misattributed uploads carry no usable artist — a bare
    # title lookup still finds the song (ASR picks the right record)
    for ti in {_clean_title(t), _plain(_clean_title(t))} - {""}:
        cands.append(("", ti))
    seen, out = set(), []
    for ar, ti in cands:
        if ti and (ar, ti) not in seen:
            seen.add((ar, ti))
            out.append((ar, ti))
    return out


def fetch(artist: str, title: str, album: str = "",
          duration: float | None = None, timeout: float = 10) -> dict | None:
    """Best-match lyrics record for a track, or None.

    Returns {"synced": [(ms, text), ...], "plain": [text, ...],
    "has_word_sync": bool} — either list may be empty.
    """
    q = {"track_name": title}
    if artist:
        q["artist_name"] = artist
    if album:
        q["album_name"] = album
    if duration:
        q["duration"] = f"{duration:.0f}"
    url = API + "?" + urllib.parse.urlencode(q)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("trackName"):
        return None
    return _record(data)


def _record(data: dict) -> dict:
    return {"synced": parse_lrc(data.get("syncedLyrics") or ""),
            "plain": [ln.strip() for ln in
                      (data.get("plainLyrics") or "").splitlines()
                      if ln.strip()],
            "has_word_sync": bool(data.get("hasWordSync")),
            "track_name": data.get("trackName"),
            "artist_name": data.get("artistName"),
            "duration": data.get("duration")}


def search(query: str, duration: float | None = None,
           timeout: float = 10) -> list[dict]:
    """Fuzzy lookup — every hit whose duration agrees with the media."""
    url = SEARCH_API + "?" + urllib.parse.urlencode({"q": query})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for d in data:
        if not isinstance(d, dict) or not d.get("trackName"):
            continue
        if not d.get("syncedLyrics") and not d.get("plainLyrics"):
            continue
        dur = d.get("duration")
        if duration and dur and abs(dur - duration) > 15:
            continue                          # wrong recording/remix
        out.append(_record(d))
    return out


SYNCLRC_API = "https://api.synclrc.dev/lyrics"
SYNCLRC_SEARCH = "https://api.synclrc.dev/search"


def _synclrc_record(d: dict) -> dict:
    """Normalize a SyncLRC hit to the lrclib record shape. /lyrics
    returns synced/plain/karaoke at top level; /search nests them
    under a "lyrics" object."""
    lyr = d.get("lyrics") if isinstance(d.get("lyrics"), dict) else {}
    synced = (lyr.get("synced") or d.get("synced") or "")
    plain = (lyr.get("plain") or d.get("plain") or "")
    return {"synced": parse_lrc(synced),
            "plain": [ln.strip() for ln in plain.splitlines()
                      if ln.strip()],
            "has_word_sync": bool(lyr.get("karaoke") or d.get("karaoke")),
            "track_name": d.get("track"),
            "artist_name": d.get("artist"),
            "duration": d.get("duration"),
            "source": "synclrc"}


def _http_json(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def synclrc_fetch(artist: str, title: str,
                  duration: float | None = None,
                  timeout: float = 10) -> dict | None:
    """Exact-match fetch from synclrc.dev (the lrclib `fetch` twin)."""
    q = {"track": title}
    if artist:
        q["artist"] = artist
    if duration:
        q["duration"] = f"{duration:.0f}"
    url = SYNCLRC_API + "?" + urllib.parse.urlencode(q)
    try:
        data = _http_json(url, timeout)
    except Exception:
        return None
    if (not isinstance(data, dict) or not data.get("track")
            or data.get("instrumental")):
        return None
    return _synclrc_record(data)


def synclrc_search(query: str, duration: float | None = None,
                   timeout: float = 10) -> list[dict]:
    """Fuzzy synclrc.dev search — same ±15s duration sanity filter."""
    url = (SYNCLRC_SEARCH + "?"
           + urllib.parse.urlencode({"q": query, "limit": 20}))
    try:
        data = _http_json(url, timeout)
    except Exception:
        return []
    hits = data.get("results") if isinstance(data, dict) else data
    if not isinstance(hits, list):
        return []
    out = []
    for d in hits:
        if not isinstance(d, dict) or not d.get("track"):
            continue
        lyr = d.get("lyrics") or {}
        if not lyr.get("synced") and not lyr.get("plain"):
            continue
        dur = d.get("duration")
        if duration and dur and abs(dur - duration) > 15:
            continue                          # wrong recording/remix
        out.append(_synclrc_record(d))
    return out


def _queries(artist: str, title: str) -> list[str]:
    qs = [f"{ar} {ti}".strip() for ar, ti in yt_candidates(artist, title)]
    cleaned = _clean_title(title)
    if cleaned and cleaned not in qs:
        qs.append(cleaned)
    return [q for q in qs if q]


def fetch_all(artist: str, title: str,
              duration: float | None = None,
              seeds: list[tuple[str, str]] | None = None) -> list[dict]:
    """Ordered candidate lyric records for a YouTube-derived entry:
    exact gets first, then fuzzy hits, across LRCLIB + SyncLRC.
    Same-title different songs are common — the caller should pick the
    record that best matches its own evidence (ASR), not trust ordering
    alone.

    ``seeds`` are high-trust (artist, title) pairs tried before the
    title-parsed guesses — e.g. the canonical music metadata YouTube
    tagged on the upload."""
    recs, seen = [], set()

    def add(r):
        if not r:
            return
        key = (r.get("source") or "lrclib",
               r.get("artist_name"), r.get("track_name"))
        if key not in seen:
            seen.add(key)
            recs.append(r)

    pairs = [s for s in (seeds or ()) if s and s[1]] + \
        yt_candidates(artist, title)
    for ar, ti in pairs:
        add(fetch(ar, ti, duration=duration))
        add(synclrc_fetch(ar, ti, duration=duration))
    seed_qs = [f"{a} {t}".strip() for a, t in (seeds or ()) if t]
    for q in dict.fromkeys(seed_qs + _queries(artist, title)):
        hits = search(q, duration=duration)
        hits += synclrc_search(q, duration=duration)
        if not hits:
            # 503s/rate-limits are common — one retry before
            # concluding this query truly has no records
            time.sleep(1.5)
            hits = search(q, duration=duration)
            hits += synclrc_search(q, duration=duration)
        for r in hits:
            add(r)
    return recs


def fetch_best(artist: str, title: str,
               duration: float | None = None) -> dict | None:
    recs = fetch_all(artist, title, duration=duration)
    return recs[0] if recs else None


def parse_lrc(text: str) -> list[tuple[int, str]]:
    """LRC → [(start_ms, line_text)]. Skips metadata tags, keeps blank
    lyric rows out (they mark instrumental gaps, not sung lines)."""
    out = []
    for ln in text.splitlines():
        stamps = _LRC_TS.findall(ln)
        body = _LRC_TS.sub("", ln).strip()
        if not body:
            continue
        for mnt, sec in stamps:
            out.append((int(mnt) * 60000 + int(float(sec) * 1000), body))
    out.sort()
    return out


def norm_word(w: str) -> str:
    """Lowercase alphanumerics — the common form for lyric/ASR matching."""
    return re.sub(r"[^a-z0-9']", "", w.lower())
