"""YouTube-derived metadata → clean LRCLIB candidates."""
from unittest.mock import patch

from ultrapistar import lyrics
from ultrapistar.lyrics import _clean_title, parse_lrc, yt_candidates


def test_quoted_lyric_video_title():
    """BTS 'Into the Sun' Lyrics (가사) [Color Coded Han_Rom_Eng]
    → artist BTS, title Into the Sun."""
    cands = yt_candidates(
        "ShadowByYoongi",
        "BTS 'Into the Sun' Lyrics (방탄소년단 Into the Sun 가사) "
        "[Color Coded Han_Rom_Eng]")
    assert cands[0] == ("BTS", "Into the Sun")


def test_dash_title_strips_video_tags():
    cands = yt_candidates(
        "GreenDayVEVO",
        "Green Day - Wake Me Up When September Ends (Official Video)")
    assert ("Green Day", "Wake Me Up When September Ends") in cands


def test_junky_brackets_removed_meaningful_kept():
    assert _clean_title("Song Name (feat. Someone) [Official Audio]") == \
        "Song Name (feat. Someone)"


def test_trailing_flavor_words_removed():
    assert _clean_title("Great Song Official Lyric Video") == "Great Song"


def test_fallback_candidate_is_uploader_plus_clean_title():
    cands = yt_candidates("UploaderChan", "Some Title")
    assert ("UploaderChan", "Some Title") in cands


def test_title_only_candidate_is_last_resort():
    # musical/film uploads carry no usable artist — a bare title lookup
    # is the final fallback so LRCLIB search can still find the song
    cands = yt_candidates("The Sound of Music (1965)", "Edelweiss")
    assert cands[-1] == ("", "Edelweiss")


def test_channel_suffix_stripped_from_artist_candidate():
    """'SiaVEVO' and 'Artist - Topic' channel names are junk artists —
    candidates include the bare name."""
    cands = yt_candidates("GreenDayVEVO", "Basket Case (Official Video)")
    assert ("GreenDay", "Basket Case") in cands
    cands = yt_candidates("Queen - Topic", "Bicycle Race")
    assert ("Queen", "Bicycle Race") in cands


def test_parse_lrc():
    assert parse_lrc("[00:12.34] hello\n[01:00.00] world") == \
        [(12340, "hello"), (60000, "world")]


def _fake_synclrc_hit():
    return {"id": "x", "track": "Bicycle Race", "artist": "Queen",
            "duration": 184,
            "lyrics": {"synced": "[00:10.00] line one\n[02:50.00] last",
                       "plain": "line one\nlast"}}


def test_synclrc_search_normalizes_records():
    """Search results nest lyrics under "lyrics" — the record should
    come out in the lrclib shape, tagged synclrc."""
    with patch.object(lyrics, "_http_json",
                      return_value={"results": [_fake_synclrc_hit()]}):
        recs = lyrics.synclrc_search("Queen Bicycle Race", duration=184)
    assert len(recs) == 1
    r = recs[0]
    assert r["source"] == "synclrc"
    assert r["track_name"] == "Bicycle Race"
    assert r["synced"] == [(10000, "line one"), (170000, "last")]
    assert r["plain"] == ["line one", "last"]


def test_synclrc_search_duration_filter():
    """A record 20s off the media duration is a different edit — skip."""
    hit = _fake_synclrc_hit()
    hit["duration"] = 160
    with patch.object(lyrics, "_http_json",
                      return_value={"results": [hit]}):
        assert lyrics.synclrc_search("x", duration=184) == []


def test_fetch_all_includes_synclrc_on_lrclib_miss():
    """lrclib empty + synclrc full → the synclrc record surfaces."""
    with patch.object(lyrics, "fetch", return_value=None), \
         patch.object(lyrics, "search", return_value=[]), \
         patch.object(lyrics, "synclrc_fetch", return_value=None), \
         patch.object(lyrics, "synclrc_search",
                      return_value=[lyrics._synclrc_record(
                          _fake_synclrc_hit())]), \
         patch.object(lyrics.time, "sleep"):
        recs = lyrics.fetch_all("Queen", "Bicycle Race", duration=184)
    assert recs and recs[0]["source"] == "synclrc"


# -- canonical provider metadata ----------------------------------------------

def _ytm_response(video_details):
    """Patch urllib.request.urlopen to serve this videoDetails payload."""
    import json
    from unittest.mock import MagicMock
    body = json.dumps({"videoDetails": video_details}).encode()
    resp = MagicMock()
    resp.__enter__.return_value.read.return_value = body
    resp.read.return_value = body
    return patch("urllib.request.urlopen", return_value=resp)


def test_music_meta_reads_tagged_fields():
    """An official upload's YTM videoDetails carry the canonical
    author/track — clean even when the display title is decorated."""
    from ultrapistar.providers import youtube
    with _ytm_response({"title": "Running Wild", "author": "Jin",
                        "musicVideoType": "MUSIC_VIDEO_TYPE_OMV"}):
        meta = youtube.YouTubeProvider("/tmp").music_meta("uoHol5Dr_go")
    assert meta == {"artist": "Jin", "track": "Running Wild",
                    "album": ""}


def test_music_meta_title_decoration_stripped():
    """YTM's title can still carry "(Official Video)" — cleaned."""
    from ultrapistar.providers import youtube
    with _ytm_response({"title": "Chandelier (Official Video)",
                        "author": "Sia",
                        "musicVideoType": "MUSIC_VIDEO_TYPE_OMV"}):
        meta = youtube.YouTubeProvider("/tmp").music_meta("x")
    assert meta["track"] == "Chandelier" and meta["artist"] == "Sia"


def test_music_meta_strips_artist_prefix_from_track():
    """Some YTM titles still carry "Artist - Track" — author prefix is
    redundant once it is the canonical artist."""
    from ultrapistar.providers import youtube
    with _ytm_response({"title": "Queen - Bicycle Race",
                        "author": "Queen",
                        "musicVideoType": "MUSIC_VIDEO_TYPE_OMV"}):
        meta = youtube.YouTubeProvider("/tmp").music_meta("x")
    assert meta["track"] == "Bicycle Race"


def test_music_meta_none_for_ugc():
    """User uploads (covers, lyric videos) report no official music
    type — no canonical metadata, heuristics take over."""
    from ultrapistar.providers import youtube
    with _ytm_response({"title": "my cover", "author": "FanChannel",
                        "musicVideoType": "MUSIC_VIDEO_TYPE_UGC"}):
        assert youtube.YouTubeProvider("/tmp").music_meta("x") is None
    with _ytm_response({"title": "t", "author": "a"}):
        assert youtube.YouTubeProvider("/tmp").music_meta("x") is None


def test_fetch_all_tries_seed_pairs_first():
    """Canonical (artist, track) seeds are exact-fetched before any
    title-parsed candidate."""
    calls = []

    def rec_fetch(a, t, duration=None):
        calls.append((a, t))
        return None

    with patch.object(lyrics, "fetch", side_effect=rec_fetch), \
         patch.object(lyrics, "synclrc_fetch", return_value=None), \
         patch.object(lyrics, "search", return_value=[]), \
         patch.object(lyrics, "synclrc_search", return_value=[]), \
         patch.object(lyrics.time, "sleep"):
        lyrics.fetch_all("HYBE LABELS", "some messy title",
                         seeds=[("Jin", "Running Wild")])
    assert calls[0] == ("Jin", "Running Wild")


def test_harvest_meta_persists_to_media_entry(db, tmp_config):
    """First worker pass writes tagged metadata onto media_entry and the
    live ent dict; later passes see meta_track and skip the network."""
    from ultrapistar.catalog import Catalog
    from ultrapistar.web.api import _harvest_meta

    class FakeProvider:
        calls = 0

        def music_meta(self, source_id):
            self.calls += 1
            return {"artist": "Jin", "track": "Running Wild",
                    "album": "Happy"}

    fake = FakeProvider()
    cat = Catalog(db, tmp_config.chart_library_dir)
    sid = cat.add_song("HYBE LABELS", "진 (Jin) 'Running Wild' Official MV")
    eid = cat.add_media_entry(sid, "youtube", "uoHol5Dr_go",
                              local_path="m.mp4")
    ctx = {"providers": {"youtube": fake}, "db": db}
    ent = cat.entry(eid)
    _harvest_meta(ctx, ent)
    assert ent["meta_artist"] == "Jin" and ent["meta_track"] == "Running Wild"
    row = db.query("SELECT meta_artist, meta_track FROM media_entry"
                   " WHERE id=?", (eid,))[0]
    assert (row["meta_artist"], row["meta_track"]) == ("Jin", "Running Wild")
    _harvest_meta(ctx, ent)                 # idempotent
    assert fake.calls == 1


def test_harvest_meta_no_provider_no_crash(db, tmp_config):
    """Local/imported entries have no music_meta — harvest is a no-op."""
    from ultrapistar.catalog import Catalog
    from ultrapistar.web.api import _harvest_meta
    cat = Catalog(db, tmp_config.chart_library_dir)
    sid = cat.add_song("A", "B")
    eid = cat.add_media_entry(sid, "local", "local:1", local_path="m.mp4")
    ctx = {"providers": {}, "db": db}
    ent = cat.entry(eid)
    _harvest_meta(ctx, ent)
    assert ent["meta_track"] == ""


def test_fetch_lyrics_seeds_canonical_pair():
    """An entry carrying harvested metadata seeds fetch_all with the
    canonical (artist, track) ahead of the raw song fields."""
    from unittest.mock import patch as _patch
    from ultrapistar.web import api
    ent = {"artist": "HYBE LABELS",
           "title": "진 (Jin) 'Running Wild' Official MV",
           "meta_artist": "Jin", "meta_track": "Running Wild",
           "duration_sec": 170}
    seen = {}

    def fake_fetch_all(artist, title, duration=None, seeds=None):
        seen["seeds"] = seeds
        return [{"id": 1}]

    with _patch("ultrapistar.lyrics.fetch_all",
                side_effect=fake_fetch_all):
        api._fetch_lyrics(ent)
    assert seen["seeds"][0] == ("Jin", "Running Wild")
