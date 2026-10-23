"""Station-level song dedup — the fuzzy artist+title check that catches
re-uploads whose YouTube source_id differs."""
from station.jobs import _is_known_song, Station
from station.bundle import known_songs, append_song
from pathlib import Path


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


def _stub_song(rec, dest, models_dir, on_stage=None):
    if on_stage:
        on_stage("downloading")
    return {"artist": "A", "title": "T", "source_id": rec["source_id"],
            "local_path": rec.get("local_path", "x.mp4"), "inst_path": "",
            "chart": "", "art": "", "provenance": "station"}


def _station(tmp_path, monkeypatch):
    import station.pipeline
    monkeypatch.setattr(station.pipeline, "process_song", _stub_song)
    return Station(tmp_path / "out", tmp_path / "models",
                   on_event=lambda *a: None)


def test_remove_queued_job(tmp_path, monkeypatch):
    st = _station(tmp_path, monkeypatch)
    st._stop.set()                        # worker exits; job stays queued
    st._worker.join(5)
    jid = st.add({"source_id": "v1", "title": "Song"})
    assert st.remove(jid)
    assert st.jobs[jid]["status"] == "cancelled"


def test_remove_running_job_aborts_at_stage_boundary(tmp_path, monkeypatch):
    import threading
    gate = threading.Event()

    def blocking_song(rec, dest, models_dir, on_stage=None):
        if on_stage:
            on_stage("downloading")
        gate.wait(5)                      # job is running; cancel lands here
        return _stub_song(rec, dest, models_dir, on_stage)
    import station.pipeline
    monkeypatch.setattr(station.pipeline, "process_song", blocking_song)
    st = Station(tmp_path / "out", tmp_path / "models",
                 on_event=lambda *a: None)
    jid = st.add({"source_id": "v5", "title": "Song"})
    while st.jobs[jid]["status"] != "running":
        pass
    assert st.remove(jid)
    gate.set()
    st._q.join()
    assert st.jobs[jid]["status"] == "cancelled"
    st.stop()


def test_remove_finished_job_refused(tmp_path, monkeypatch):
    st = _station(tmp_path, monkeypatch)
    jid = st.add({"source_id": "v2", "title": "Song"})
    st._q.join()
    assert st.jobs[jid]["status"] == "done"
    assert not st.remove(jid)
    st.stop()


def test_queue_persists_across_restarts(tmp_path, monkeypatch):
    st = _station(tmp_path, monkeypatch)
    st._stop.set()                      # freeze the worker so it stays queued
    jid = st.add({"source_id": "v3", "title": "Song"})
    st._q.queue.clear()                 # and make sure it never runs it
    qf = tmp_path / "out" / ".station-queue.json"
    assert qf.is_file()
    st.stop()

    import station.pipeline
    monkeypatch.setattr(station.pipeline, "process_song", _stub_song)
    st2 = Station(tmp_path / "out", tmp_path / "models",
                  on_event=lambda *a: None)
    assert [j["rec"]["source_id"] for j in st2.jobs.values()] == ["v3"]
    st2._stop.set()
    st2._q.queue.clear()
    st2.stop()


def test_completed_job_leaves_persisted_queue(tmp_path, monkeypatch):
    st = _station(tmp_path, monkeypatch)
    st.add({"source_id": "v4", "title": "Song"})
    st._q.join()
    import json
    recs = json.loads((tmp_path / "out" / ".station-queue.json")
                      .read_text(encoding="utf-8"))
    assert recs == []
    st.stop()


def test_youtube_job_prefetches_then_processes(tmp_path, monkeypatch):
    from ultrapistar.providers import youtube
    seen = {}

    def fake_download(self, rec, dest_dir=None):
        seen["dl"] = rec["source_id"]
        f = Path(dest_dir) / "yt-v9.mp4"
        f.write_bytes(b"media")
        return f.name
    monkeypatch.setattr(youtube.YouTubeProvider, "download", fake_download)

    st = _station(tmp_path, monkeypatch)
    jid = st.add({"source_id": "v9", "title": "T", "provider": "youtube"})
    st._dl_q.join()
    st._q.join()
    assert seen["dl"] == "v9"
    assert st.jobs[jid]["status"] == "done", st.jobs[jid].get("error")
    st.stop()


def test_local_file_skips_download_pool(tmp_path, monkeypatch):
    media = tmp_path / "song.mp4"
    media.write_bytes(b"m")
    st = _station(tmp_path, monkeypatch)
    jid = st.add({"source_id": "v10", "title": "T", "provider": "local",
                  "local_file": str(media)})
    st._q.join()
    assert st.jobs[jid]["status"] == "done"
    assert st._dl_q.empty()
    st.stop()
