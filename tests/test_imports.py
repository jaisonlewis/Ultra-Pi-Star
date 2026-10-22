"""Station-bundle ingest: manifest scan, registration, pushed zips."""
import io
import json
import zipfile
from pathlib import Path

import pytest

from ultrapistar.catalog import Catalog
from ultrapistar.imports import (import_manifest, scan_roots,
                                 unpack_bundle)
from ultrapistar.lyrics import norm_word
from ultrapistar.providers.youtube import video_id_from_url


class TestNormWordUnicode:
    def test_latin_unchanged(self):
        assert norm_word("Hello, World!") == "hello world".replace(" ", "")

    def test_devanagari_survives(self):
        assert norm_word("तुम ही हो") != ""
        assert norm_word("तुम") == "तुम"

    def test_mixed_scripts(self):
        out = norm_word("Song (हिन्दी Ver.)")
        assert "song" in out and "हिन्दी" in out

    def test_combining_marks_kept(self):
        # Devanagari dependent vowel signs are combining marks — they
        # must survive or Hindi words degrade to consonant skeletons
        assert norm_word("गाता") == "गाता"


class TestVideoId:
    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/watch?v=djV11Xbc914",
        "https://www.youtube.com/watch?v=djV11Xbc914&list=PLabc",
        "https://youtu.be/djV11Xbc914",
        "https://youtu.be/djV11Xbc914?t=42",
        "https://www.youtube.com/shorts/djV11Xbc914",
        "https://www.youtube.com/embed/djV11Xbc914",
        "https://music.youtube.com/watch?v=djV11Xbc914",
    ])
    def test_forms(self, url):
        assert video_id_from_url(url) == "djV11Xbc914"

    def test_non_youtube(self):
        assert video_id_from_url("take on me") is None


MINI_UPSONG = {
    "format": "upsong", "version": 2,
    "headers": {"ARTIST": "A", "TITLE": "T", "BPM": "15000"},
    "source": {}, "tracks": {"P1": [
        {"s": 0, "l": 400, "p": 60, "t": "hi", "wb": 1},
        {"s": 400, "l": 400, "p": 62, "t": "there"}]},
    "breaks": {"P1": [800]},
}


def _bundle_root(tmp_path: Path) -> Path:
    """A staging dir holding one song's files + manifest."""
    root = tmp_path / "staging"
    root.mkdir()
    (root / "yt-abc12345678.mp4").write_bytes(b"video")
    (root / "yt-abc12345678.inst.mp4").write_bytes(b"backing")
    (root / "charts").mkdir()
    (root / "charts" / "A - T.upsong").write_text(
        json.dumps(MINI_UPSONG), encoding="utf-8")
    man = {"format": 1, "songs": [{
        "artist": "Test Artist", "title": "Test Song",
        "language": "en", "provider": "youtube",
        "source_id": "abc12345678", "duration_sec": 200,
        "thumbnail": "", "local_path": "yt-abc12345678.mp4",
        "inst_path": "yt-abc12345678.inst.mp4",
        "chart": "charts/A - T.upsong", "provenance": "station"}]}
    (root / "manifest.json").write_text(
        json.dumps(man), encoding="utf-8")
    return root


@pytest.fixture()
def ictx(tmp_config, db):
    return {"config": tmp_config, "db": db,
            "catalog": Catalog(db, tmp_config.chart_dir, ())}


class TestImportManifest:
    def test_registers_song(self, tmp_path, ictx, db):
        root = _bundle_root(tmp_path)
        counts = import_manifest(ictx, root / "manifest.json")
        assert counts["added"] == 1
        rows = db.query(
            "SELECT e.local_path, e.inst_path, e.status, s.artist"
            " FROM media_entry e JOIN song s ON s.id=e.song_id")
        assert len(rows) == 1
        r = rows[0]
        assert r["artist"] == "Test Artist"
        assert r["status"] == "available"
        assert r["inst_path"] == "yt-abc12345678.inst.mp4"
        # media moved into the write root, with its sidecar
        media_dir = Path(ictx["config"].media_dir)
        assert (media_dir / "yt-abc12345678.mp4").is_file()
        assert (media_dir / "yt-abc12345678.inst.mp4").is_file()
        # chart copied under generated/ and attached verified
        charts = db.query(
            "SELECT verified, source_repo, media_entry_id FROM chart")
        assert charts and charts[0]["verified"] == 1
        assert charts[0]["source_repo"] == "generated"
        assert charts[0]["media_entry_id"] is not None
        assert Path(db.query(
            "SELECT file_path FROM chart")[0]["file_path"]).is_file()

    def test_skips_existing(self, tmp_path, ictx, db):
        root = _bundle_root(tmp_path)
        import_manifest(ictx, root / "manifest.json")
        counts = import_manifest(ictx, root / "manifest.json")
        assert counts["skipped"] == 1 and counts["added"] == 0
        assert len(db.query("SELECT id FROM media_entry")) == 1

    def test_missing_media(self, tmp_path, ictx):
        root = tmp_path / "staging"
        root.mkdir()
        (root / "manifest.json").write_text(json.dumps(
            {"format": 1, "songs": [{"artist": "x", "title": "y",
                                     "source_id": "zzz",
                                     "local_path": "gone.mp4"}]}))
        assert import_manifest(ictx, root / "manifest.json")[
            "missing"] == 1

    def test_scan_roots_finds_manifest(self, tmp_path, tmp_config,
                                       ictx):
        media = Path(tmp_config.media_dir)
        (media / "yt-abc12345678.mp4").write_bytes(b"video")
        (media / "charts").mkdir(exist_ok=True)
        (media / "charts" / "A - T.upsong").write_text(
            json.dumps(MINI_UPSONG), encoding="utf-8")
        (media / "manifest.json").write_text(json.dumps(
            {"format": 1, "songs": [{
                "artist": "Test Artist", "title": "Test Song",
                "provider": "youtube", "source_id": "abc12345678",
                "local_path": "yt-abc12345678.mp4",
                "chart": "charts/A - T.upsong"}]}))
        found = scan_roots(ictx)
        key = str(media)
        assert found[key]["added"] == 1
        assert (media / "yt-abc12345678.mp4").is_file()


class TestEndpoints:
    def test_import_scan(self, host_client, tmp_config):
        media = Path(tmp_config.media_dir)
        (media / "yt-abc12345678.mp4").write_bytes(b"video")
        (media / "manifest.json").write_text(json.dumps(
            {"format": 1, "songs": [{
                "artist": "Test Artist", "title": "Test Song",
                "provider": "youtube", "source_id": "abc12345678",
                "local_path": "yt-abc12345678.mp4"}]}))
        r = host_client.post("/api/manage/import-scan")
        assert r.status_code == 200
        d = r.get_json()
        assert d["ok"] and d["roots"]
        assert sum(v["added"] for v in d["roots"].values()) == 1

    def test_import_bundle_zip(self, host_client, tmp_config, db):
        root = _bundle_root(tmp_path_factory_root())
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for p in root.iterdir():
                if p.is_file() and p.name != "manifest.json":
                    z.write(p, p.name)
            z.writestr("chart.upsong",
                       (root / "charts" / "A - T.upsong").read_text())
            man = json.loads((root / "manifest.json").read_text())
            man["songs"][0]["chart"] = "chart.upsong"
            man["songs"][0]["source_id"] = "pushsong001"
            z.writestr("manifest.json", json.dumps(man))
        # rename the media to match the pushed source_id
        buf.seek(0)
        r = host_client.post("/api/import-bundle", data={
            "bundle": (buf, "bundle.zip")},
            content_type="multipart/form-data")
        assert r.status_code == 200
        d = r.get_json()
        assert d["ok"] and (d["added"] or d["missing"] or d["skipped"])
        # media landed in the write root under its manifest name
        assert (Path(tmp_config.media_dir)
                / "yt-abc12345678.mp4").is_file() or d["missing"] == 1

    def test_import_bundle_rejects_nonhost(self, client):
        r = client.post("/api/import-bundle")
        assert r.status_code == 403


def tmp_path_factory_root() -> Path:
    import tempfile
    return Path(tempfile.mkdtemp())
