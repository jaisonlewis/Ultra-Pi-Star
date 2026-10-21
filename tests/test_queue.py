"""Queue manager behavior: ordering, single-playing invariant, host actions."""
import time


def _item(db, queue, name="Singer"):
    pid = db.add_performer(name)["id"]
    db.execute("INSERT OR IGNORE INTO song (artist,title) VALUES ('A','T')")
    sid = db.query("SELECT id FROM song LIMIT 1")[0]["id"]
    db.execute(
        "INSERT OR IGNORE INTO media_entry (song_id,provider,source_id) VALUES (?,?,?)",
        (sid, "local", f"src-{time.time_ns()}"))
    eid = db.query("SELECT id FROM media_entry ORDER BY id DESC LIMIT 1")[0]["id"]
    return queue.add(sid, eid, pid)


def test_fifo_order(db, queue):
    a = _item(db, queue, "A")
    b = _item(db, queue, "B")
    ids = [i["id"] for i in queue.list()]
    assert ids == [a, b]


def test_move_up_down(db, queue):
    a = _item(db, queue, "A")
    b = _item(db, queue, "B")
    assert queue.move(b, "up")
    assert [i["id"] for i in queue.list()] == [b, a]
    assert not queue.move(b, "up")          # already first
    assert queue.move(b, "down")
    assert [i["id"] for i in queue.list()] == [a, b]


def test_single_playing_and_advance(db, queue):
    a = _item(db, queue, "A")
    b = _item(db, queue, "B")
    first = queue.start_next()
    assert first["id"] == a
    again = queue.start_next()
    assert again["id"] == a                  # still the same item playing
    nxt = queue.finish_current("done")
    assert nxt["id"] == b
    assert queue.finish_current("done") is None
    hist = db.query("SELECT * FROM play_history ORDER BY id")
    assert len(hist) == 2


def test_remove_and_clear(db, queue):
    a = _item(db, queue, "A")
    b = _item(db, queue, "B")
    assert queue.remove(a)
    assert [i["id"] for i in queue.list()] == [b]
    assert queue.clear() == 1
    assert queue.list() == []


def test_snapshot_shape(db, queue):
    _item(db, queue, "A")
    snap = queue.snapshot()
    assert snap["queue_count"] == 1
    assert snap["now_playing"] is None
    assert snap["queue"][0]["singer"] == "A"


def test_vocal_cut_per_item(db, queue):
    """The karaoke-backing choice is a queue-item property, not global."""
    pid = db.add_performer("S")["id"]
    db.execute("INSERT OR IGNORE INTO song (artist,title) VALUES ('A','T')")
    sid = db.query("SELECT id FROM song LIMIT 1")[0]["id"]
    for i in range(3):
        db.execute(
            "INSERT INTO media_entry (song_id,provider,source_id)"
            " VALUES (?,?,?)", (sid, "local", f"vc-{i}-{time.time_ns()}"))
    eids = [r["id"] for r in db.query("SELECT id FROM media_entry")]
    queue.add(sid, eids[0], pid)                    # default → NULL
    queue.add(sid, eids[1], pid, vocal_cut=1)       # wants karaoke backing
    queue.add(sid, eids[2], pid, vocal_cut=0)       # wants guide vocals
    cuts = [i["vocal_cut"] for i in queue.list()]
    assert cuts == [None, 1, 0]


def test_restart_skips_zombie_playing_item(tmp_config):
    """A 'playing' row left behind by a crash/restart would block every
    future advance() — app startup must terminal-ize it."""
    from ultrapistar.app import create_app
    from ultrapistar.db import Database
    db = Database(tmp_config.db_path)
    pid = db.add_performer("Ghost")["id"]
    db.execute("INSERT OR IGNORE INTO song (artist,title) VALUES ('A','T')")
    sid = db.query("SELECT id FROM song LIMIT 1")[0]["id"]
    db.execute(
        "INSERT INTO media_entry (song_id,provider,source_id,status)"
        " VALUES (?,?,?,'available')", (sid, "local", "src-z"))
    eid = db.query("SELECT id FROM media_entry LIMIT 1")[0]["id"]
    db.execute(
        "INSERT INTO queue_item (song_id,media_entry_id,performer_id,"
        " position,status,created_at) VALUES (?,?,?,1,'playing',0)",
        (sid, eid, pid))
    create_app(tmp_config, seed=True)
    statuses = [r["status"] for r in
                db.query("SELECT status FROM queue_item")]
    assert "playing" not in statuses and "skipped" in statuses


def test_skip_current_with_nothing_playing(queue):
    """Skip on an empty queue is a no-op, not an error."""
    assert queue.skip_current() is None


def test_stop_current_does_not_advance(db, queue):
    """Session end stops the playing item but must NOT promote the next
    one — the next session starts it fresh."""
    a = _item(db, queue, "A")
    _item(db, queue, "B")
    queue.start_next()
    assert queue.now_playing()["id"] == a
    queue.stop_current()
    assert queue.now_playing() is None
    # the queued B is still queued, untouched
    assert queue.list()[0]["status"] == "queued"


def test_join_duet_rejects_third_singer(db, queue):
    """P2 fills once — a second join_duet on a full slot returns None."""
    pid = db.add_performer("Lead")["id"]
    db.execute(
        "INSERT OR IGNORE INTO song (artist,title,is_duet) VALUES ('A','T',1)")
    sid = db.query("SELECT id FROM song WHERE is_duet=1 LIMIT 1")[0]["id"]
    db.execute(
        "INSERT OR IGNORE INTO media_entry (song_id,provider,source_id)"
        " VALUES (?,?,?)", (sid, "local", "src-duet"))
    eid = db.query("SELECT id FROM media_entry ORDER BY id DESC LIMIT 1")[0]["id"]
    item = queue.add(sid, eid, pid)
    other = db.add_performer("Backup")["id"]
    third = db.add_performer("Late")["id"]
    assert queue.join_duet(item, other) is not None
    assert queue.join_duet(item, third) is None
    # the lead can't duet with themselves either
    assert queue.join_duet(item, pid) is None


def test_finish_current_records_history_and_advances(db, queue):
    a = _item(db, queue, "A")
    b = _item(db, queue, "B")
    queue.start_next()
    nxt = queue.finish_current("done", score=87)
    assert nxt["id"] == b
    hist = db.query("SELECT * FROM play_history")
    assert len(hist) == 1 and hist[0]["score"] == 87


def test_remove_only_skips_queued(db, queue):
    """A playing item can't be removed mid-song — remove() only takes
    queued rows (the skip path owns 'playing')."""
    a = _item(db, queue, "A")
    queue.start_next()
    assert queue.remove(a) is False
    assert queue.now_playing() is not None


class _SlowProvider:
    """Fake provider whose download() takes long enough for a second
    call to overlap if the manager weren't serialized."""
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.done = []

    def download(self, entry, media_dir):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)
        self.active -= 1
        self.done.append(entry["source_id"])
        return entry["source_id"] + ".mp4"


class _FakeProviders:
    def __init__(self, prov):
        self._prov = prov

    def get(self, name):
        return self._prov


def test_downloads_run_one_at_a_time(db, tmp_config):
    """A bulk playlist drop must serialize — never N parallel yt-dlp."""
    from ultrapistar.downloads import DownloadManager
    prov = _SlowProvider()
    dm = DownloadManager(db, _FakeProviders(prov), tmp_config.media_dir,
                         lambda: None, lambda eid: None)
    for i in range(5):
        dm.ensure({"entry_id": i + 1, "provider": "yt",
                   "source_id": f"v{i}", "status": "queued",
                   "local_path": None})
    dm._q.join()
    assert prov.max_active == 1
    assert prov.done == ["v0", "v1", "v2", "v3", "v4"]   # FIFO order
