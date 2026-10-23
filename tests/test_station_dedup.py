"""Station-level song dedup — the fuzzy artist+title check that catches
re-uploads whose YouTube source_id differs."""
from station.jobs import _is_known_song
from station.bundle import known_songs, append_song


PAIRS = [
    ("Sia", "Chandelier"),
    ("Queen", "Bicycle Race"),
    ("The Bluebells", "Young at Heart"),
]


def test_reupload_in_video_title():
    rec = {"title": "Sia - Chandelier (Official Video)",
           "artist": "SiaVEVO"}
    assert _is_known_song(rec, PAIRS)


def test_channel_name_supplies_artist():
    rec = {"title": "Chandelier", "artist": "SiaVEVO"}
    assert _is_known_song(rec, PAIRS)


def test_topic_channel():
    rec = {"title": "Bicycle Race (Remastered 2011)",
           "artist": "Queen - Topic"}
    assert _is_known_song(rec, PAIRS)


def test_different_song_same_artist():
    rec = {"title": "Queen - Don't Stop Me Now", "artist": "QueenVEVO"}
    assert not _is_known_song(rec, PAIRS)


def test_same_title_different_artist():
    # a cover of "Young at Heart" by somebody else is not a dupe
    rec = {"title": "Rod Stewart - Young at Heart", "artist": "RodStewartVEVO"}
    assert not _is_known_song(rec, PAIRS)


def test_partial_artist_token_no_match():
    # "Chic" must not match "Chicane"
    rec = {"title": "Chicane - Saltwater", "artist": "Chicane"}
    assert not _is_known_song(rec, [("Chic", "Saltwater")])


def test_no_title_never_matches():
    assert not _is_known_song({"title": "", "artist": "Sia"}, PAIRS)


def test_known_songs_roundtrip(tmp_path):
    append_song(tmp_path, {"source_id": "x1", "artist": "Sia",
                           "title": "Chandelier"})
    assert ("Sia", "Chandelier") in known_songs(tmp_path)
