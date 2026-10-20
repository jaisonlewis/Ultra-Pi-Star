"""Kiosk watchdog liveness signals: tv:ready, position heartbeats,
pause/resume flags, and disconnect marking."""

import time

from test_queue import _item


def _ctx(app):
    return app.extensions["ups"]


def _sock(app):
    return app.extensions["ups"]["socketio"].test_client(app)


def test_tv_ready_marks_liveness(app):
    sock = _sock(app)
    assert sock.is_connected()
    ctx = _ctx(app)
    assert ctx.get("tv_sid") is None
    sock.emit("tv:ready")
    assert ctx["tv_sid"] is not None
    assert ctx["tv_seen"] > 0


def test_disconnect_marks_dead(app):
    sock = _sock(app)
    sock.emit("tv:ready")
    ctx = _ctx(app)
    sock.disconnect()
    assert ctx["tv_seen"] == 0.0          # watchdog restart signal


def test_position_heartbeat_and_pause_resume(app, db, queue, host_client):
    _item(db, queue)
    item = queue.start_next()
    sock = _sock(app)
    sock.emit("tv:ready")
    ctx = _ctx(app)

    sock.emit("player:position", {"position_sec": 1.5})
    assert ctx["tv_pos_item"] == item["id"]
    assert ctx["tv_last_pos"] > 0

    r = host_client.post("/api/playback/pause")
    assert r.status_code == 200
    assert ctx["tv_paused"] == item["id"]

    r = host_client.post("/api/playback/resume")
    assert r.status_code == 200
    assert "tv_paused" not in ctx
    assert ctx["tv_expect_pos"] > 0

    sock.emit("player:position", {"position_sec": 2.0})
    assert "tv_expect_pos" not in ctx     # positions came back


def test_idle_tv_does_not_look_stalled(app):
    """An idle TV (connected, nothing playing) must not trigger the
    watchdog's position-stall path — silence is expected there."""
    sock = _sock(app)
    sock.emit("tv:ready")
    ctx = _ctx(app)
    assert _ctx(app)["queue"].now_playing() is None
    assert ctx.get("tv_last_pos") is None
    # socket stays connected -> tv_seen stays positive, watchdog stays quiet
    assert ctx["tv_seen"] > 0


def test_host_claim_state_broadcasts(app, client):
    """TV learns host claim on connect and on login/release transitions."""
    sock = _sock(app)
    events = {e["name"]: e["args"] for e in sock.get_received()}
    assert events["host:state"][0]["claimed"] is False

    client.post("/api/join", json={"name": "Alex"})
    r = client.post("/api/host/login", json={"key": "test-host-key"})
    assert r.status_code == 200
    events = {e["name"]: e["args"] for e in sock.get_received()}
    assert events["host:state"][0]["claimed"] is True

    client.post("/api/host/release")
    events = {e["name"]: e["args"] for e in sock.get_received()}
    assert events["host:state"][0]["claimed"] is False


def _playing_item(db, queue, performer_id):
    db.execute("INSERT OR IGNORE INTO song (artist,title) VALUES ('A','T')")
    sid = db.query("SELECT id FROM song LIMIT 1")[0]["id"]
    db.execute(
        "INSERT OR IGNORE INTO media_entry (song_id,provider,source_id)"
        " VALUES (?,?,?)", (sid, "local", f"mix-{time.time_ns()}"))
    eid = db.query(
        "SELECT id FROM media_entry ORDER BY id DESC LIMIT 1")[0]["id"]
    queue.add(sid, eid, performer_id)
    return queue.start_next()


def test_singer_can_mix(app, db, queue, client):
    """The performer whose song is playing may set volume without host."""
    res = client.post("/api/join", json={"name": "SingerSam"})
    pid = res.get_json()["performer"]["id"]

    # not singing yet -> no mix rights
    assert client.get("/api/mic").get_json()["can_mix"] is False
    assert client.post("/api/playback/volume",
                       json={"volume": 0.5}).status_code == 403

    _playing_item(db, queue, pid)

    # now singing -> mix rights
    assert client.get("/api/mic").get_json()["can_mix"] is True
    r = client.post("/api/playback/volume", json={"volume": 0.6})
    assert r.status_code == 200
    assert r.get_json()["music_volume"] == 0.6
    assert db.get_setting("music_volume") == "0.6"


def test_stranger_cannot_mix(app, db, queue, client):
    other = client.application.test_client()      # separate session
    res = client.post("/api/join", json={"name": "SingerSam"})
    _playing_item(db, queue, res.get_json()["performer"]["id"])

    other.post("/api/join", json={"name": "Watcher"})
    assert other.get("/api/mic").get_json()["can_mix"] is False
    assert other.post("/api/playback/volume",
                      json={"volume": 0.5}).status_code == 403
    assert other.post("/api/mic",
                      json={"volume": 0.5}).status_code == 403


def test_duet_second_singer_can_mix(app, db, queue, client):
    res = client.post("/api/join", json={"name": "Lead"})
    pid = res.get_json()["performer"]["id"]
    item = _playing_item(db, queue, pid)
    p2 = db.add_performer("Backup")["id"]
    db.execute("UPDATE queue_item SET performer2_id=? WHERE id=?",
               (p2, item["id"]))
    with client.session_transaction() as s:
        s["performer_id"] = p2                   # I'm P2 now
    assert client.get("/api/mic").get_json()["can_mix"] is True
    assert client.post("/api/playback/volume",
                       json={"volume": 0.8}).status_code == 200
