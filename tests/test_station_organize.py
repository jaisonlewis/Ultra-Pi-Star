"""station.organize — flat prepared root → artist/title song folders."""
import json
from pathlib import Path

from station.organize import organize_root


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
