"""Media organizer — 'artist/title/' folders for browsable roots."""
from pathlib import Path
from types import SimpleNamespace

from ultrapistar.catalog import Catalog
from ultrapistar.mediastore import organize, song_folder, song_reldir


class TestSongFolder:
    def test_basic(self):
        assert song_folder("a-ha", "Take On Me") == "a-ha - Take On Me"

    def test_strips_illegal(self):
        assert "/" not in song_folder('AC/DC', 'Back <in> "Black"?')
        assert ":" not in song_folder("AC/DC", "Back: Black")

    def test_fallback_on_empty(self):
        assert song_folder("", "", "yt-abc123") == "yt-abc123"
        assert song_folder("", "") == "unknown"


class TestSongReldir:
    def test_basic(self):
        assert song_reldir("David Bowie", "Is There Life on Mars?") \
            == "david_bowie/is_there_life_on_mars"

    def test_strips_path_chars(self):
        out = song_reldir("AC/DC", "Back in Black")
        assert "/" not in out.split("/")[0]
        assert out == "ac_dc/back_in_black"

    def test_fallbacks(self):
        assert song_reldir("", "", "yt-abc") == "unknown/yt_abc"
        assert song_reldir("", "") == "unknown/unknown"


def _ctx(config, db):
    return {"config": config, "db": db,
            "catalog": Catalog(db, config.chart_dir, ()),
            "queue": SimpleNamespace(now_playing=lambda: None)}


class TestOrganize:
    def test_moves_into_folder(self, tmp_config, db):
        media = Path(tmp_config.media_dir)
        (media / "yt-abc123.mp4").write_bytes(b"v")
        (media / "yt-abc123.inst.mp4").write_bytes(b"b")
        (media / "yt-abc123.vocals.wav").write_bytes(b"s")
        (media / "yt-abc123.analysis.json").write_text("{}")
        (media / "yt-abc123.mp4.normed").write_text("x")
        cat = _ctx(tmp_config, db)["catalog"]
        sid = cat.add_song("Weezer", "Say It Ain't So")
        eid = cat.add_media_entry(
            sid, "youtube", "abc123", local_path="yt-abc123.mp4",
            status="available")
        db.execute("UPDATE media_entry SET inst_path=? WHERE id=?",
                   ("yt-abc123.inst.mp4", eid))
        out = organize(_ctx(tmp_config, db))
        assert out["moved"] == 1
        folder = media / "weezer" / "say_it_ain_t_so"
        assert (folder / "yt-abc123.mp4").is_file()
        assert (folder / "yt-abc123.inst.mp4").is_file()
        assert (folder / "yt-abc123.vocals.wav").is_file()
        assert (folder / "yt-abc123.analysis.json").is_file()
        e = db.query(
            "SELECT local_path, inst_path FROM media_entry WHERE id=?",
            (eid,))[0]
        assert e["local_path"] == "weezer/say_it_ain_t_so/yt-abc123.mp4"
        assert (e["inst_path"]
                == "weezer/say_it_ain_t_so/yt-abc123.inst.mp4")
        # resolution still finds the file through the folder path
        assert tmp_config.media_path(e["local_path"]).is_file()

    def test_idempotent_and_missing_ok(self, tmp_config, db):
        media = Path(tmp_config.media_dir)
        (media / "m.mp3").write_bytes(b"a")
        cat = _ctx(tmp_config, db)["catalog"]
        sid = cat.add_song("A", "B")
        cat.add_media_entry(sid, "local", "l1",
                            local_path="m.mp3", status="available")
        cat.add_media_entry(sid, "local", "l2",
                            local_path="gone.mp4", status="available")
        out = organize(_ctx(tmp_config, db))
        assert out["moved"] == 1 and out["skipped"] == 1
        out2 = organize(_ctx(tmp_config, db))
        assert out2["moved"] == 0          # second run is a no-op
        assert (media / "a" / "b" / "m.mp3").is_file()

    def test_skips_playing_entry(self, tmp_config, db):
        media = Path(tmp_config.media_dir)
        (media / "m.mp3").write_bytes(b"a")
        cat = _ctx(tmp_config, db)["catalog"]
        sid = cat.add_song("A", "B")
        eid = cat.add_media_entry(sid, "local", "l1",
                                  local_path="m.mp3",
                                  status="available")
        ctx = _ctx(tmp_config, db)
        ctx["queue"].now_playing = (
            lambda: {"id": 7, "entry_id": eid})
        ctx["np_pushed"] = (7,)   # marked as actually feeding the player
        out = organize(ctx)
        assert out["moved"] == 0 and out["skipped"] == 1
        assert (media / "m.mp3").is_file()
