"""station.organize — flat prepared root → artist/title song folders."""
import json
from pathlib import Path

from station.organize import organize_root
from station.audit import _chart_problems, audit
from station.fixmeta import heal
from station.rechart import _meta_ok
from station.prune import _reasons, prune
from station import identify


class TestIdentify:
    def _cluster(self, root, vid, size=2_000_000):
        d = root / "_unsorted" / f"yt-{vid}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"yt-{vid}.mp4").write_bytes(b"x" * size)
        return d

    def test_clusters_skips_fragments(self, tmp_path):
        self._cluster(tmp_path, "realsong123")
        self._cluster(tmp_path, "tinyfrag", size=100)
        vids = [v for v, _ in identify._clusters(tmp_path)]
        assert vids == ["realsong123"]

    def test_requeue_moves_media_and_writes_queue(self, tmp_path,
                                                monkeypatch):
        self._cluster(tmp_path, "abc123defg")
        monkeypatch.setattr(
            identify, "identify",
            lambda root, delay=0: {
                "abc123defg": {"dead": False, "author": "RHINO",
                               "title": "Dio - Holy Diver "
                                        "(Official Music Video)"}})
        stats = identify.requeue(tmp_path)
        assert stats["queued"] == 1
        recs = json.loads((tmp_path / ".station-queue.json")
                          .read_text())
        assert recs[0]["source_id"] == "abc123defg"
        assert recs[0]["artist"] == "Dio"
        assert recs[0]["title"] == "Holy Diver"
        # media drained out of _unsorted into incomplete/
        assert (tmp_path / "incomplete/yt-abc123defg.mp4").is_file()
        assert not (tmp_path / "_unsorted/yt-abc123defg").exists()

    def test_requeue_skips_dead_and_junk(self, tmp_path, monkeypatch):
        self._cluster(tmp_path, "deadvid1")
        self._cluster(tmp_path, "livevid2")
        monkeypatch.setattr(
            identify, "identify",
            lambda root, delay=0: {
                "deadvid1": {"dead": True, "author": "", "title": ""},
                "livevid2": {"dead": False, "author": "X",
                             "title": "Queen - Bohemian Rhapsody "
                                      "(Live At Wembley)"}})
        stats = identify.requeue(tmp_path)
        assert stats["dead"] == 1 and stats["junk"] == 1
        assert stats["queued"] == 0
        assert not (tmp_path / ".station-queue.json").exists()


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


class TestPrune:
    def _song(self, root, artist, title, dur=200, lyrics="lrc"):
        """A complete on-disk song cluster for _reasons to inspect."""
        d = root / "x" / "y"
        d.mkdir(parents=True, exist_ok=True)
        (d / "m.mp4").write_bytes(b"x")
        if lyrics is not None:
            src = {"type": "generated", "lyrics": lyrics}
            (d / "c.upsong").write_text(json.dumps(
                {"format": "upsong", "source": src,
                 "tracks": {"P1": [{"s": 0, "l": 1, "p": 60, "t": "hi"}]}}))
        return {"artist": artist, "title": title, "duration_sec": dur,
                "local_path": "x/y/m.mp4",
                "chart": "x/y/c.upsong" if lyrics is not None else ""}

    def test_full_album_flagged(self, tmp_path):
        s = self._song(tmp_path, "Dinosaur Jr.",
                       "Green Mind (Full Album)")
        assert "album/compilation" in _reasons(tmp_path, s, set())

    def test_long_duration_flagged(self, tmp_path):
        s = self._song(tmp_path, "X", "Y", dur=3600)
        assert "album/compilation" in _reasons(tmp_path, s, set())

    def test_live_footage_flagged(self, tmp_path):
        s = self._song(tmp_path, "Queen",
                       "Bohemian Rhapsody (Live At Wembley Stadium)")
        assert "live/non-official" in _reasons(tmp_path, s, set())

    def test_studio_song_named_live_kept(self, tmp_path):
        s = self._song(tmp_path, "Wings", "Live and Let Die")
        assert _reasons(tmp_path, s, set()) == []

    def test_best_of_me_is_a_song(self, tmp_path):
        s = self._song(tmp_path, "Ratt", "Best Of Me")
        assert _reasons(tmp_path, s, set()) == []

    def test_asr_chart_flagged(self, tmp_path):
        s = self._song(tmp_path, "Obscure Band", "Rare Song",
                       lyrics=None)
        # no chart file at all → no ASR reason (can't score anyway but
        # prune only pulls proven ASR/unknown entries)
        s2 = self._song(tmp_path, "Obscure Band", "Rare Song",
                        lyrics="")
        (tmp_path / "x/y/c.upsong").write_text(json.dumps(
            {"format": "upsong", "source": {"type": "generated"},
             "tracks": {"P1": [{"s": 0, "l": 1, "p": 60, "t": "hi"}]}}))
        assert "ASR-only lyrics" in _reasons(tmp_path, s2, set())

    def test_unknown_artist_flagged(self, tmp_path):
        s = self._song(tmp_path, "Unknown", "yt-abCdEfGhIjK")
        assert "no metadata" in _reasons(tmp_path, s, set())

    def test_prune_moves_dir_and_drops_rec(self, tmp_path):
        doc = {"songs": [
            self._song(tmp_path, "Dinosaur Jr.",
                       "Green Mind (Full Album)"),
            self._song(tmp_path, "Ratt", "Best Of Me")]}
        (tmp_path / "manifest.json").write_text(json.dumps(doc))
        out = prune(tmp_path)
        assert out["removed"] == 1
        kept = json.loads((tmp_path / "manifest.json").read_text())
        assert [s["title"] for s in kept["songs"]] == ["Best Of Me"]
        assert (tmp_path / "_removed/x/y/m.mp4").is_file()

    def _recognised(self, root, artist="Nirvana", track="About A Girl"):
        """Give the test song a sidecar with lrc_chosen — a provider
        holds a record for this exact recording."""
        (root / "x/y/m.analysis.json").write_text(json.dumps(
            {"lrc_chosen": [artist, track]}))

    def test_recognised_live_release_protected(self, tmp_path):
        """MTV Unplugged is an official release — LRCLIB carries it,
        so lrc_chosen exists and the live keyword can't prune it."""
        s = self._song(tmp_path, "Nirvana",
                       "About A Girl (Live On MTV Unplugged)")
        self._recognised(tmp_path)
        assert _reasons(tmp_path, s, set()) == []

    def test_unrecognised_live_footage_still_flagged(self, tmp_path):
        """Same live title with no provider record → still bootleg."""
        s = self._song(tmp_path, "Tool",
                       "Sober Live at Reading Festival 1993 (Pro Shot)")
        assert "live/non-official" in _reasons(tmp_path, s, set())

    def test_recognition_does_not_save_albums(self, tmp_path):
        """A 40-min compilation stays unchartable even if a provider
        recognises a track on it."""
        s = self._song(tmp_path, "Queen", "Greatest Hits", dur=2400)
        self._recognised(tmp_path)
        assert "album/compilation" in _reasons(tmp_path, s, set())

    def test_keep_file_protects(self, tmp_path):
        s = self._song(tmp_path, "Queen",
                       "Bohemian Rhapsody (Live At Wembley)")
        (tmp_path / "_keep.txt").write_text(
            "# my favourites\nwembley\n")
        assert _reasons(tmp_path, s, set(),
                        ["wembley"]) == []
        (tmp_path / "manifest.json").write_text(
            json.dumps({"songs": [s]}))
        out = prune(tmp_path)             # reads _keep.txt itself
        assert out["removed"] == 0


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
