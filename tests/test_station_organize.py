"""station.organize — flat prepared root → artist/title song folders."""
import json
from pathlib import Path

from station.organize import organize_root
from station.audit import _chart_problems, audit
from station.fixmeta import heal
from station.rechart import _meta_ok


class TestFixMeta:
    def test_channel_artist_video_title(self):
        assert heal({"artist": "RHINO",
                     "title": "Skid Row - 18 And Life "
                              "(Official Music Video)"}) == \
            ("Skid Row", "18 And Life")

    def test_dup_artist_prefix(self):
        assert heal({"artist": "Iron Maiden",
                     "title": "Iron Maiden - Run To The Hills "
                              "(Official Video)"}) == \
            ("Iron Maiden", "Run To The Hills")

    def test_bracketed_sep_not_a_split(self):
        assert heal({"artist": "Genesis",
                     "title": "Throwing It All Away (2007 - Remaster)"}) \
            is None

    def test_clean_record_untouched(self):
        assert heal({"artist": "Roxy Music", "title": "Avalon"}) is None

    def test_opaque_id_untouched(self):
        assert heal({"artist": "Unknown", "title": "yt-abCdEfGhIjK"}) \
            is None

    def test_tracknum_stripped(self):
        assert heal({"artist": "When In Rome", "title": "23.The Promise"}) \
            == ("When In Rome", "The Promise")

    def test_trailing_paren_residue(self):
        assert heal({"artist": "Skid Row", "title": "Monkey Business ("}) \
            == ("Skid Row", "Monkey Business")

    def test_song_by_artist_reprise(self):
        assert heal({"artist": "Nickelback",
                     "title": "Rockstar- Nickelback"}) == \
            ("Nickelback", "Rockstar")

    def test_swap_repair(self):
        assert heal({"artist": "Is This Love", "title": "Whitesnake"}) == \
            ("Whitesnake", "Is This Love")


class TestMetaOk:
    def test_channel_fix_accepted(self):
        assert _meta_ok("RHINO",
                        "White Lion - Radar Love (Official Music Video)",
                        "White Lion", "Radar Love")

    def test_same_title_different_song_rejected(self):
        assert not _meta_ok("Twisted Sister", "I Wanna Rock",
                            "Snoop Dogg", "I Wanna Rock")

    def test_channel_prefixed_record_rejected(self):
        assert not _meta_ok(
            "Mötley Crüe",
            "Mötley Crüe - Girls Girls Girls (Official Music Video)",
            "MotleyCrueVEVO", "Mötley Crüe - Girls Girls Girls")

    def test_single_char_artist_rejected(self):
        assert not _meta_ok("Phil Collins", "Both Sides of the Story",
                            "P", "Both Sides of the Story")

    def test_unknown_artist_trusts_record(self):
        assert _meta_ok("Unknown", "yt-abCdEfGhIjK",
                        "Real Artist", "Real Title")


class TestChartAudit:
    def _upsong(self, notes, source):
        return json.dumps({
            "format": "upsong", "version": 2, "headers": {},
            "source": source,
            "tracks": {"P1": notes}})

    def test_blank_lyrics_flagged(self, tmp_path):
        p = tmp_path / "x.upsong"
        p.write_text(self._upsong(
            [{"s": 0, "l": 1, "p": 60, "t": ""}] * 3,
            {"type": "generated", "lyrics": "lrc"}))
        assert "lyrics blank" in _chart_problems(p)

    def test_asr_lyrics_flagged(self, tmp_path):
        p = tmp_path / "x.upsong"
        p.write_text(self._upsong(
            [{"s": 0, "l": 1, "p": 60, "t": "yeah"}],
            {"type": "generated", "lyrics": None}))
        assert any("ASR" in i for i in _chart_problems(p))

    def test_lrc_chart_clean(self, tmp_path):
        p = tmp_path / "x.upsong"
        p.write_text(self._upsong(
            [{"s": 0, "l": 1, "p": 60, "t": "real words"}],
            {"type": "generated", "lyrics": "lrc"}))
        assert _chart_problems(p) == []


def _flat_root(tmp_path: Path) -> Path:
    root = tmp_path / "stick"
    root.mkdir()
    (root / "yt-song0001.mp4").write_bytes(b"v")
    (root / "yt-song0001.inst.mp4").write_bytes(b"b")
    (root / "yt-song0001.vocals.wav").write_bytes(b"s")
    (root / "yt-song0001.analysis.json").write_text("{}")
    (root / "yt-song0001.jpg").write_bytes(b"j")
    (root / "charts").mkdir()
    (root / "charts" / "David Bowie - Is There Life on Mars.upsong") \
        .write_text("{}")
    (root / "manifest.json").write_text(json.dumps({
        "format": 1, "songs": [{
            "artist": "David Bowie",
            "title": "Is There Life on Mars?",
            "provider": "youtube", "source_id": "song0001",
            "local_path": "yt-song0001.mp4",
            "inst_path": "yt-song0001.inst.mp4",
            "chart": "charts/David Bowie - Is There Life on Mars.upsong",
            "art": "yt-song0001.jpg"}]}))
    return root


def test_flat_root_reorganizes(tmp_path):
    root = _flat_root(tmp_path)
    out = organize_root(root)
    assert out["organized"] == 1
    d = root / "david_bowie" / "is_there_life_on_mars"
    for name in ("yt-song0001.mp4", "yt-song0001.inst.mp4",
                 "yt-song0001.vocals.wav", "yt-song0001.analysis.json",
                 "yt-song0001.jpg",
                 "David Bowie - Is There Life on Mars.upsong"):
        assert (d / name).is_file(), name
    man = json.loads((root / "manifest.json").read_text())
    rec = man["songs"][0]
    assert rec["local_path"] == \
        "david_bowie/is_there_life_on_mars/yt-song0001.mp4"
    assert rec["inst_path"].endswith("/yt-song0001.inst.mp4")
    assert rec["chart"].startswith("david_bowie/is_there_life_on_mars/")


def test_done_stray_cluster_adopted(tmp_path):
    """A fully-processed but un-manifested cluster (inst+vocals+
    analysis+chart) gets adopted into a real folder + manifest entry
    instead of _unsorted quarantine."""
    root = tmp_path / "stick"
    root.mkdir()
    (root / "yt-done00001.mp4").write_bytes(b"v")
    (root / "yt-done00001.inst.mp4").write_bytes(b"b")
    (root / "yt-done00001.vocals.wav").write_bytes(b"s")
    (root / "yt-done00001.analysis.json").write_text(json.dumps(
        {"lrc_chosen": ["Eurythmics", "Sweet Dreams"]}))
    (root / "charts").mkdir()
    (root / "charts" / "Eurythmics - Sweet Dreams.upsong").write_text("{}")
    (root / "manifest.json").write_text(
        json.dumps({"format": 1, "songs": []}))
    out = organize_root(root)
    assert out["organized"] == 1
    d = root / "eurythmics" / "sweet_dreams"
    assert (d / "yt-done00001.mp4").is_file()
    assert (d / "yt-done00001.inst.mp4").is_file()
    assert (d / "Eurythmics - Sweet Dreams.upsong").is_file()
    man = json.loads((root / "manifest.json").read_text())
    rec = man["songs"][0]
    assert rec["artist"] == "Eurythmics"
    assert rec["local_path"] == \
        "eurythmics/sweet_dreams/yt-done00001.mp4"


def test_incomplete_stray_still_quarantined(tmp_path):
    root = tmp_path / "stick"
    root.mkdir()
    (root / "yt-half00001.mp4").write_bytes(b"v")
    (root / "yt-half00001.vocals.wav").write_bytes(b"s")   # no inst/chart
    (root / "manifest.json").write_text(
        json.dumps({"format": 1, "songs": []}))
    out = organize_root(root)
    assert out["unsorted"] == 1
    assert (root / "_unsorted" / "yt-half00001"
            / "yt-half00001.mp4").is_file()


def test_idempotent_and_strays(tmp_path):
    root = _flat_root(tmp_path)
    organize_root(root)
    out = organize_root(root)          # second run is a no-op
    assert out["organized"] == 0 and out["skipped"] == 1
    # a stray un-manifested download groups under _unsorted/
    (root / "yt-stray001.mp4").write_bytes(b"v")
    out = organize_root(root)
    assert out["unsorted"] == 1
    assert (root / "_unsorted" / "yt-stray001"
            / "yt-stray001.mp4").is_file()
