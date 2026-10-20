"""UltraStar chart parser + catalog chart-match policy."""
from pathlib import Path

from ultrapistar.catalog import Catalog
from ultrapistar.charts import parse_chart

CHART = """#TITLE:Duet Song
#ARTIST:Band
#BPM:120
#GAP:0
P1
: 0 4 60 Hel
* 4 4 62 lo
P2
: 0 4 55 Hey
- 8
E
"""


def test_parse_duet(tmp_path):
    p = tmp_path / "duet.txt"
    p.write_text(CHART)
    chart = parse_chart(p)
    assert chart.is_duet
    assert chart.bpm == 120
    assert len(chart.tracks["P1"]) == 2
    assert chart.tracks["P1"][1].kind == "golden"
    assert chart.validate() == []


def test_word_boundaries_preserved(tmp_path):
    """Syllable edge-spaces mark word boundaries — leading space on the
    next syllable (usdx style) or trailing space (USDB style). Both are
    normalized into clean text + an explicit word_break flag."""
    p = tmp_path / "words.txt"
    p.write_text("#TITLE:T\n#ARTIST:A\n#BPM:120\n#GAP:0\n"
                 ": 0 4 60 Hel\n: 4 4 62  lo\n: 8 4 60  world\n- 12\nE\n")
    chart = parse_chart(p)
    texts = [n.text for n in chart.tracks["P1"]]
    wbs = [n.word_break for n in chart.tracks["P1"]]
    assert texts == ["Hel", "lo", "world"]
    assert wbs == [True, True, True]
    phrases = chart.phrases()
    assert phrases["P1"][0]["text"] == "Hel lo world"
    # syllable spans carry display text + word-break flags for karaoke fill
    syls = phrases["P1"][0]["syls"]
    assert [s["t"] for s in syls] == ["Hel", "lo", "world"]
    assert [s["wb"] for s in syls] == [True, True, True]

    p2 = tmp_path / "words2.txt"
    p2.write_text("#TITLE:T\n#ARTIST:A\n#BPM:120\n#GAP:0\n"
                  ": 0 8 60 Sum\n: 8 4 60 mer \n: 12 4 60 has \n"
                  ": 16 4 60 come \n- 20\nE\n")
    chart2 = parse_chart(p2)
    texts2 = [n.text for n in chart2.tracks["P1"]]
    wbs2 = [n.word_break for n in chart2.tracks["P1"]]
    assert texts2 == ["Sum", "mer", "has", "come"]
    assert wbs2 == [True, False, True, True]
    assert chart2.phrases()["P1"][0]["text"] == "Summer has come"


def test_tilde_joins_syllables(tmp_path):
    """'~' marks a syllable that continues the word (ne~ver -> never)."""
    p = tmp_path / "tilde.txt"
    p.write_text("#TITLE:T\n#ARTIST:A\n#BPM:120\n#GAP:0\n"
                 ": 0 4 60 Can \n: 4 4 62 ne\n: 8 4 60 ~ver \n"
                 ": 12 4 60 la\n: 16 4 60 ~~st,\n- 20\nE\n")
    chart = parse_chart(p)
    assert [n.text for n in chart.tracks["P1"]] == \
        ["Can", "ne", "ver", "la", "st,"]
    assert [n.word_break for n in chart.tracks["P1"]] == \
        [True, True, False, True, False]
    assert chart.phrases()["P1"][0]["text"] == "Can never last,"


def test_native_round_trip(tmp_path):
    """txt -> .upsong -> Chart preserves notes, breaks, headers."""
    from ultrapistar.charts import from_native, load_chart, to_native
    p = tmp_path / "song.txt"
    p.write_text(CHART)
    chart = parse_chart(p)
    chart.tracks["P1"][0].word_break = True
    d = to_native(chart, source={"kind": "usdb", "id": 1})
    back = from_native(d)
    assert back.artist == chart.artist and back.bpm == chart.bpm
    assert [(n.kind, n.start, n.length, n.pitch, n.text, n.word_break)
            for n in back.tracks["P1"]] == \
           [(n.kind, n.start, n.length, n.pitch, n.text, n.word_break)
            for n in chart.tracks["P1"]]
    assert back.tracks["P2"][0].text == "Hey"
    assert back.breaks["P2"] == [8]
    assert back.source == {"kind": "usdb", "id": 1}


def test_load_chart_prefers_native_and_migrates(tmp_path):
    """load_chart writes .upsong next to .txt, then reads it back; a
    hand-edited txt newer than its .upsong is re-imported."""
    import os
    from ultrapistar.charts import load_chart, native_path
    p = tmp_path / "song.txt"
    p.write_text(CHART)
    c1 = load_chart(p)
    assert c1.title == "Duet Song"
    nat = native_path(p)
    assert nat.exists()                     # lazy migration wrote it
    # corrupt the txt but keep it older than the .upsong -> native wins
    p.write_text("garbage")
    os.utime(p, (nat.stat().st_mtime - 10,) * 2)
    assert load_chart(p).title == "Duet Song"
    # hand-edit txt newer than .upsong -> re-imported
    p.write_text(CHART.replace("Duet Song", "New Title"))
    os.utime(p, (nat.stat().st_mtime + 10,) * 2)
    assert load_chart(p).title == "New Title"


def test_parse_missing_headers(tmp_path):
    p = tmp_path / "bad.txt"
    p.write_text(": 0 4 60 nope\nE\n")
    chart = parse_chart(p)
    assert "missing ARTIST/TITLE header" in chart.validate()


def test_unverified_chart_not_scored(db, tmp_path):
    cat = Catalog(db, tmp_path)
    sid = cat.add_song("Band", "Duet Song")
    eid = cat.add_media_entry(sid, "local", "x1")
    p = tmp_path / "duet.txt"
    p.write_text(CHART)
    cat.attach_chart(sid, p, media_entry_id=eid, verified=False, confidence=0.5)
    entry = cat.entry(eid)
    assert entry["scoring_available"] is False

    # verify it -> scoring enabled
    db.execute("UPDATE chart SET verified=1 WHERE media_entry_id=?", (eid,))
    assert cat.entry(eid)["scoring_available"] is True


def test_chart_library_matching(db, tmp_path):
    """Library charts index independently of songs, then auto-attach
    (unverified) when a matching song+media entry appears."""
    cat = Catalog(db, tmp_path / "dropzone")
    libdir = tmp_path / "chart-library" / "usdx-songs" / "A"
    libdir.mkdir(parents=True)
    (libdir / "Adele - Hello.txt").write_text(CHART.replace(
        "#TITLE:Duet Song", "#TITLE:Hello").replace("#ARTIST:Band", "#ARTIST:Adele"))

    assert cat.rescan_charts([libdir]) == 1
    # indexed in the library but no song yet -> no chart rows
    assert db.query("SELECT count(*) c FROM chart_library")[0]["c"] == 1
    assert db.query("SELECT count(*) c FROM chart")[0]["c"] == 0

    # a karaoke-video title arrives, cleaned to artist/title by the API path
    sid = cat.add_song("Adele", "Hello")
    eid = cat.add_media_entry(sid, "youtube", "vid123", status="downloading")
    assert cat.attach_matching_charts(sid) == 1

    pending = cat.pending_charts()
    assert len(pending) == 1 and pending[0]["media_entry_id"] == eid
    assert cat.entry(eid)["scoring_available"] is False  # not yet verified

    cat.verify_chart(pending[0]["id"], eid)
    payload = cat.chart_payload(eid)
    assert payload is not None and "P1" in payload["tracks"]


def test_relative_timing_unrolled(tmp_path):
    """#RELATIVE:YES: note beats count from their phrase's start and '-'
    carries (relative end, next-phrase start offset). USDX accumulates
    the second param per phrase — notes must land on absolute beats."""
    p = tmp_path / "rel.txt"
    p.write_text("#TITLE:T\n#ARTIST:A\n#BPM:120\n#GAP:0\n#RELATIVE:YES\n"
                 ": 0 4 60 one\n: 4 4 62 two\n- 8 10\n"
                 ": 0 4 63 three\n: 4 4 64 four\n- 8 20\n"
                 ": 0 4 65 five\nE\n")
    chart = parse_chart(p)
    starts = [n.start for n in chart.tracks["P1"]]
    # phrase 2's local beat 0 sits at +10, phrase 3's at +10+20
    assert starts == [0, 4, 10, 14, 30]
    assert chart.breaks["P1"] == [10, 30]
    lines = chart.lines("P1")
    assert len(lines) == 3

    # '-' with only the end param advances the phrase by that amount
    p2 = tmp_path / "rel2.txt"
    p2.write_text("#TITLE:T\n#ARTIST:A\n#BPM:120\n#GAP:0\n#RELATIVE:YES\n"
                  ": 0 4 60 a\n- 8\n: 0 4 61 b\nE\n")
    c2 = parse_chart(p2)
    assert [n.start for n in c2.tracks["P1"]] == [0, 8]


def test_stale_upsong_reimported(tmp_path):
    """A .upsong written by an older format version is rejected and
    re-imported from its .txt sibling."""
    import json
    from ultrapistar.charts import load_chart, native_path
    p = tmp_path / "rel.txt"
    p.write_text("#TITLE:T\n#ARTIST:A\n#BPM:120\n#GAP:0\n#RELATIVE:YES\n"
                 ": 0 4 60 a\n- 8 10\n: 0 4 61 b\nE\n")
    c1 = load_chart(p)
    assert [n.start for n in c1.tracks["P1"]] == [0, 10]
    # simulate a v1 cache (notes stored relative, unshifted)
    nat = native_path(p)
    nat.write_text(json.dumps({"format": "upsong", "version": 1,
                               "headers": {"TITLE": "T", "ARTIST": "A",
                                           "BPM": "120", "GAP": "0",
                                           "RELATIVE": "YES"},
                               "tracks": {"P1": [{"s": 0, "l": 4, "p": 60,
                                                  "t": "a"},
                                                 {"s": 0, "l": 4, "p": 61,
                                                  "t": "b"}]},
                               "breaks": {"P1": [8]}}))
    c2 = load_chart(nat)      # direct .upsong load must fall back to txt
    assert [n.start for n in c2.tracks["P1"]] == [0, 10]


def test_title_normalization():
    from ultrapistar.catalog import _norm
    assert _norm("Wake Me Up (Karaoke Version With Lyrics)") == "wake me up"
    assert _norm("Hello") == "hello"
    assert _norm("A-Ha") == "a ha"


def test_chart_payload_applies_sync_warp(tmp_path, db):
    """Learned per-phrase offsets retime phrases from their boundary
    onward — a shorter media gap steps the next line earlier while
    earlier phrases keep the global offset."""
    import json
    cat = Catalog(db, tmp_path / "dropzone")
    libdir = tmp_path / "chart-library" / "usdx-songs" / "B"
    libdir.mkdir(parents=True)
    (libdir / "Band - Duet Song.txt").write_text(
        "#TITLE:Warped\n#ARTIST:Band\n#BPM:120\n#GAP:0\n"
        ": 0 4 60 one\n- 8\n: 16 4 61 two\n- 24\n: 40 4 62 three\nE\n")
    assert cat.rescan_charts([libdir]) == 1
    sid = cat.add_song("Band", "Warped")
    eid = cat.add_media_entry(sid, "youtube", "v1", status="downloaded")
    cat.attach_matching_charts(sid)
    row = cat.pending_charts()[0]
    cat.verify_chart(row["id"], eid)
    beat_ms = 15000.0 / 120  # 125ms per beat

    base = cat.chart_payload(eid)
    starts = [p["start_ms"] for p in base["tracks"]["P1"]]
    assert starts == [0, 16 * beat_ms, 40 * beat_ms]

    # media's second gap ran 2s short -> phrase 3 lands at beat 40's time - 2s
    db.execute("UPDATE chart SET sync_warp=? WHERE id=?",
               (json.dumps({str(eid): [[40, -2000]]}), row["id"]))
    warped = cat.chart_payload(eid)
    starts2 = [p["start_ms"] for p in warped["tracks"]["P1"]]
    assert starts2[0] == starts[0] and starts2[1] == starts[1]
    assert starts2[2] == 40 * beat_ms - 2000
    # lanes follow the same segment
    lane3 = [n for n in warped["lanes"]["P1"] if n["text"] == "three"]
    assert lane3[0]["start_ms"] == 40 * beat_ms - 2000


def test_chart_payload_warp_is_media_specific(tmp_path, db):
    """Warp learned on one media entry must not leak onto another entry
    of the same song."""
    import json
    cat = Catalog(db, tmp_path / "dropzone")
    libdir = tmp_path / "chart-library" / "usdx-songs" / "B"
    libdir.mkdir(parents=True)
    (libdir / "Band - Duet Song.txt").write_text(
        "#TITLE:Warped\n#ARTIST:Band\n#BPM:120\n#GAP:0\n"
        ": 0 4 60 one\n- 8\n: 16 4 61 two\n- 24\n: 40 4 62 three\nE\n")
    cat.rescan_charts([libdir])
    sid = cat.add_song("Band", "Warped")
    e1 = cat.add_media_entry(sid, "youtube", "v1", status="downloaded")
    e2 = cat.add_media_entry(sid, "youtube", "v2", status="downloaded")
    cat.attach_matching_charts(sid)
    # two available entries -> one song-level chart row serves both;
    # verify in place without binding it to either entry
    rows = cat.pending_charts()
    assert len(rows) == 1 and rows[0]["media_entry_id"] is None
    db.execute("UPDATE chart SET verified=1, sync_warp=? WHERE id=?",
               (json.dumps({str(e1): [[40, -2000]]}), rows[0]["id"]))
    beat_ms = 15000.0 / 120
    p1 = cat.chart_payload(e1)
    p2 = cat.chart_payload(e2)
    assert p1["tracks"]["P1"][2]["start_ms"] == 40 * beat_ms - 2000
    assert p2["tracks"]["P1"][2]["start_ms"] == 40 * beat_ms


def test_clean_title_strips_uploader_decorations():
    from ultrapistar.catalog import clean_title
    assert clean_title(
        "Have You Ever Seen The Rain  with Lyrics On Screen"
    ) == "Have You Ever Seen The Rain"
    assert clean_title("Believer (Karaoke Version) HD") == "Believer"
    assert clean_title("Song lyric video") == "Song"
    assert clean_title("Song - Lyrics") == "Song"
    assert clean_title("Song Karaoke") == "Song"
    # real titles must survive
    assert clean_title("Who'll Stop The Rain") == "Who'll Stop The Rain"
    assert clean_title("Video Killed The Radio Star") == \
        "Video Killed The Radio Star"
    assert clean_title("Singin' in the Rain") == "Singin' in the Rain"




def test_parse_sync_warp_shapes():
    """Per-entry map, bare legacy list, and garbage all parse safely."""
    import json
    from ultrapistar.catalog import parse_sync_warp
    w = {"12": {"ratio": 1.05, "marks": [[0, -120], [64, 40]]},
         "7":  {"ratio": 0.9, "marks": [[0, 10]]}}
    assert parse_sync_warp(json.dumps(w), 12) == (1.05, [[0, -120], [64, 40]])
    assert parse_sync_warp(json.dumps(w), 7) == (0.9, [[0, 10]])
    # missing entry → defaults, not a crash
    assert parse_sync_warp(json.dumps(w), 99) == (1.0, [])
    # bare legacy list keeps ratio=1
    assert parse_sync_warp(json.dumps([[0, 5], [8, 9]]), 3) == \
        (1.0, [[0, 5], [8, 9]])
    assert parse_sync_warp("not json", 1) == (1.0, [])
    assert parse_sync_warp(None, 1) == (1.0, [])
