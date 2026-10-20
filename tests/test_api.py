"""HTTP API: join, search, queue permissions, host-only controls."""
import os
import re
import time


def test_host_key_is_four_char_room_code(monkeypatch):
    """Generated host codes: 4 chars, capitals + digits only."""
    from ultrapistar.config import Config
    monkeypatch.delenv("UPS_HOST_KEY", raising=False)
    key = Config().host_key
    assert re.fullmatch(r"[A-Z2-9]{4}", key)


def test_host_login_case_insensitive(client, tmp_config):
    """Phones may type the TV code in lowercase."""
    client.post("/api/join", json={"name": "Alex"})
    res = client.post("/api/host/login",
                      json={"key": tmp_config.host_key.lower()})
    assert res.status_code == 200


def test_join_requires_name(client):
    assert client.post("/api/join", json={}).status_code == 400
    res = client.post("/api/join", json={"name": "Alex"})
    assert res.status_code == 200
    assert res.get_json()["performer"]["name"] == "Alex"


def test_search_seeded_catalog(client):
    res = client.get("/api/search?q=signal")
    results = res.get_json()["results"]
    assert any(r["title"] == "Signal In The Noise" for r in results)
    charted = [r for r in results if r["title"] == "Signal In The Noise"][0]
    assert charted["scoring_available"] is True


def test_guest_cannot_use_host_controls(client):
    assert client.post("/api/queue/clear").status_code == 403
    assert client.post("/api/queue/1/move", json={"direction": "up"}).status_code == 403
    assert client.post("/api/playback/next").status_code == 403


def test_host_key_header_grants_access(client, tmp_config):
    res = client.post("/api/queue/clear",
                      headers={"X-Host-Key": tmp_config.host_key})
    assert res.status_code == 200


def test_queue_add_and_guest_remove_own(client):
    client.post("/api/join", json={"name": "Alex"})
    entry = client.get("/api/search?q=signal").get_json()["results"][0]
    res = client.post("/api/queue", json={"entry_id": entry["entry_id"], "name": "Alex"})
    assert res.status_code == 200
    item_id = res.get_json()["queue_item_id"]

    snap = client.get("/api/queue").get_json()
    # first item auto-promotes to playing when the player is idle
    assert snap["now_playing"]["id"] == item_id

    # a different guest (new session) cannot remove it
    other = client.application.test_client()
    assert other.post(f"/api/queue/{item_id}/remove").status_code == 403

    assert client.post(f"/api/queue/{item_id}/remove").status_code == 200


def test_host_login_and_me(client, tmp_config):
    """Phone-side host unlock via the printed host key."""
    assert client.get("/api/me").get_json()["is_host"] is False
    assert client.post("/api/host/login", json={"key": "nope"}).status_code == 403
    # host claim requires a joined performer
    assert client.post("/api/host/login",
                         json={"key": tmp_config.host_key}).status_code == 400
    client.post("/api/join", json={"name": "Alex"})
    res = client.post("/api/host/login", json={"key": tmp_config.host_key})
    assert res.status_code == 200
    assert client.get("/api/me").get_json()["is_host"] is True
    # host session can drive transport controls
    assert client.post("/api/playback/pause").status_code == 200
    assert client.post("/api/playback/resume").status_code == 200
    # a guest session cannot
    other = client.application.test_client()
    assert other.post("/api/playback/pause").status_code == 403


def test_host_claim_single_owner_and_release(client, tmp_config):
    """One room host at a time; releasing lets another player claim it."""
    client.post("/api/join", json={"name": "Alex"})
    assert client.post("/api/host/login",
                       json={"key": tmp_config.host_key}).status_code == 200

    other = client.application.test_client()
    other.post("/api/join", json={"name": "Sam"})
    res = other.post("/api/host/login", json={"key": tmp_config.host_key})
    assert res.status_code == 409
    assert "Alex" in res.get_json()["error"]

    # after release the other player can take over
    assert client.post("/api/host/release").status_code == 200
    assert client.get("/api/me").get_json()["is_host"] is False
    assert other.post("/api/host/login",
                      json={"key": tmp_config.host_key}).status_code == 200
    assert other.get("/api/me").get_json()["is_host"] is True


def test_queue_vocal_cut_selects_playback_media(client, app, tmp_config):
    """The singer's per-song choice picks the .inst.mp4 when one exists —
    and falls back to the original mix when it doesn't."""
    from ultrapistar.web.api import push_now_playing
    sock = app.extensions["ups"]["socketio"].test_client(app)
    ctx = app.extensions["ups"]
    client.post("/api/join", json={"name": "Alex"})
    entry = client.get("/api/search?q=groove").get_json()["results"][0]
    inst = tmp_config.media_dir / "demo-video-only-groove.inst.mp4"
    inst.write_bytes(b"x")
    ctx["db"].execute("UPDATE media_entry SET inst_path=? WHERE id=?",
                      (inst.name, entry["entry_id"]))

    res = client.post("/api/queue",
                      json={"entry_id": entry["entry_id"], "vocal_cut": 1})
    assert res.status_code == 200
    snap = res.get_json()["state"]
    assert snap["now_playing"]["vocal_cut"] == 1

    # loudness normalization runs on the queue-time worker; the TV is
    # held on "processing" until it finishes and re-pushes real playback
    last_uri = None
    deadline = time.time() + 30

    def _uri():
        nonlocal last_uri
        evs = [e["args"][0] for e in sock.get_received()
               if e["name"] == "now_playing" and e["args"][0]]
        if evs:
            last_uri = (evs[-1]["playback"] or {}).get("uri")
        return last_uri

    while _uri() is None and time.time() < deadline:
        time.sleep(0.05)

    assert _uri().endswith(".inst.mp4")          # karaoke backing chosen

    # same song queued "with vocals" → original mix
    ctx["db"].execute("UPDATE queue_item SET vocal_cut=0")
    push_now_playing(ctx)
    assert not _uri().endswith(".inst.mp4")

    # backing requested but the file isn't ready → safe fallback
    ctx["db"].execute("UPDATE queue_item SET vocal_cut=1")
    inst.unlink()
    push_now_playing(ctx)
    assert not _uri().endswith(".inst.mp4")


def test_now_playing_waits_while_chart_generates(client, app):
    """Media ready + generation inflight → TV holds on 'processing'
    instead of playing the raw video; when the worker finishes the
    re-push releases it (lyrics or not)."""
    from ultrapistar.web.api import push_now_playing
    sock = app.extensions["ups"]["socketio"].test_client(app)
    ctx = app.extensions["ups"]
    client.post("/api/join", json={"name": "Alex"})
    entry = client.get("/api/search?q=groove").get_json()["results"][0]
    ctx.setdefault("map_inflight", set()).add(entry["entry_id"])

    client.post("/api/queue", json={"entry_id": entry["entry_id"]})

    def _last():
        evs = [e["args"][0] for e in sock.get_received()
               if e["name"] == "now_playing" and e["args"][0]]
        return evs[-1]

    p = _last()
    assert p["waiting"] == "processing" and p["playback"] is None

    # worker finished without a chart — the wait releases into raw play
    ctx["map_inflight"].discard(entry["entry_id"])
    push_now_playing(ctx)
    p = _last()
    assert p["waiting"] is None and p["playback"]


def test_session_end_clears_queue_and_host(client, tmp_config):
    client.post("/api/join", json={"name": "Alex"})
    client.post("/api/host/login", json={"key": tmp_config.host_key})
    entry = client.get("/api/search?q=signal").get_json()["results"][0]
    client.post("/api/queue", json={"entry_id": entry["entry_id"]})

    # guests cannot end the session
    other = client.application.test_client()
    assert other.post("/api/session/end").status_code == 403

    assert client.post("/api/session/end").status_code == 200
    snap = client.get("/api/queue").get_json()
    assert snap["now_playing"] is None
    assert snap["queue"] == []
    assert client.get("/api/me").get_json()["is_host"] is False

    # a new player can claim host after the session ended
    other.post("/api/join", json={"name": "Sam"})
    assert other.post("/api/host/login",
                      json={"key": tmp_config.host_key}).status_code == 200


def test_duet_open_slot_join(client):
    """A queued duet with no P2 lets another player claim the second mic."""
    client.post("/api/join", json={"name": "Alex"})
    entry = [r for r in client.get("/api/search?q=voices").get_json()["results"]
             if r["is_duet"]][0]
    res = client.post("/api/queue", json={"entry_id": entry["entry_id"]})
    assert res.status_code == 200
    item_id = res.get_json()["queue_item_id"]

    # the owner cannot join their own P2 slot; a guest can
    assert client.post(f"/api/queue/{item_id}/join-duet").status_code == 409
    other = client.application.test_client()
    other.post("/api/join", json={"name": "Sam"})
    res = other.post(f"/api/queue/{item_id}/join-duet")
    assert res.status_code == 200
    assert res.get_json()["item"]["singer2"] == "Sam"
    # slot is now closed
    third = client.application.test_client()
    third.post("/api/join", json={"name": "Pat"})
    assert third.post(f"/api/queue/{item_id}/join-duet").status_code == 409


def test_scoreboard(client, app):
    """play_history scores surface as per-song records + singer standings."""
    client.post("/api/join", json={"name": "Alex"})
    entry = client.get("/api/search?q=signal").get_json()["results"][0]
    client.post("/api/queue", json={"entry_id": entry["entry_id"], "name": "Alex"})
    qm = app.extensions["ups"]["queue"]
    qm.finish_current("done", score=8500)

    board = client.get("/api/scores").get_json()
    assert board["singers"][0]["name"] == "Alex"
    assert board["singers"][0]["best"] == 8500
    assert board["songs"][0]["score"] == 8500
    assert board["songs"][0]["scorer"] == "Alex"

    row = [r for r in client.get("/api/search?q=signal").get_json()["results"]
           if r["title"] == "Signal In The Noise"][0]
    assert row["top_scorer"] == "Alex" and row["top_score"] == 8500

    # a higher score from another singer takes the record
    client.post("/api/queue",
                json={"entry_id": entry["entry_id"], "name": "Sam"})
    qm.finish_current("done", score=9100)
    board = client.get("/api/scores").get_json()
    assert board["songs"][0]["scorer"] == "Sam"
    assert board["singers"][0]["name"] == "Sam"  # sorted by total


def test_host_page_gate(client, tmp_config):
    assert client.get("/host").status_code == 403
    res = client.get(f"/host?key={tmp_config.host_key}")
    assert res.status_code in (200, 302)
    # wrong key still blocked
    other = client.application.test_client()
    assert other.get("/host?key=wrong").status_code == 403


def test_karaoke_title_reversed_fields(client, db):
    """'Bicycle Race - Queen | Karaoke Version | KaraFun' is Title-Artist
    order with channel tags — the library artist index must flip it."""
    db.execute(
        "INSERT INTO chart_library"
        " (file_path,artist,title,artist_norm,title_norm,is_duet,source_repo)"
        " VALUES ('q.txt','Queen','Some Song','queen','some song',0,'usdx')")
    client.post("/api/join", json={"name": "Alex"})
    client.post("/api/queue", json={
        "provider": "local", "source_id": "bike1",
        "title": "Bicycle Race - Queen | Karaoke Version | KaraFun",
        "artist": "KaraFun"})
    row = db.query("SELECT artist, title FROM song WHERE artist='Queen'")
    assert row and row[0]["title"] == "Bicycle Race"


def test_karaoke_title_normal_order(client, db):
    """Standard 'Artist - Title (Karaoke Version)' still parses."""
    client.post("/api/join", json={"name": "Alex"})
    client.post("/api/queue", json={
        "provider": "local", "source_id": "abba1",
        "title": "ABBA - Waterloo (Karaoke Version)",
        "artist": "KaraFun"})
    row = db.query("SELECT artist, title FROM song WHERE title='Waterloo'")
    assert row and row[0]["artist"] == "ABBA"


def test_song_level_chart_scores_every_version(client, db, tmp_config):
    """A verified chart with media_entry_id=NULL (ambiguous bind — the
    song has several versions) still unlocks scoring on every version."""
    from ultrapistar.catalog import Catalog
    cat = Catalog(db, tmp_config.chart_dir)
    sid = cat.add_song("Multi", "Versions")
    cat.add_media_entry(sid, "local", "v1", local_path="v1.mp4")
    e2 = cat.add_media_entry(sid, "local", "v2", local_path="v2.mp4")
    chart = tmp_config.chart_dir / "demo-signal-in-the-noise.txt"
    db.execute(
        "INSERT INTO chart (song_id,media_entry_id,source_repo,file_path,"
        " verified,confidence) VALUES (?,NULL,'usdb',?,1,1.0)",
        (sid, str(chart)))
    results = client.get("/api/search?q=multi").get_json()["results"]
    assert results and all(r["scoring_available"] for r in results)
    assert cat.chart_row_for_entry(e2) is not None
    assert cat.chart_payload(e2) is not None


def test_generated_chart_preferred_over_imported(db, tmp_config):
    """When an entry has both an imported (USDB) chart and our generated
    one, the generated chart wins — everywhere the winner is picked:
    chart_row_for_entry, chart_payload, and the search row."""
    from ultrapistar.catalog import Catalog
    cat = Catalog(db, tmp_config.chart_dir)
    sid = cat.add_song("Pref", "Generated Wins")
    eid = cat.add_media_entry(sid, "local", "v1", local_path="v1.mp4")
    chart = tmp_config.chart_dir / "demo-signal-in-the-noise.txt"
    usdb_id = db.execute(
        "INSERT INTO chart (song_id,media_entry_id,source_repo,file_path,"
        " verified,confidence) VALUES (?,?,'usdb',?,1,1.0)",
        (sid, eid, str(chart)))
    assert cat.chart_row_for_entry(eid)["source_repo"] == "usdb"
    gen_id = db.execute(
        "INSERT INTO chart (song_id,media_entry_id,source_repo,file_path,"
        " verified,confidence) VALUES (?,?,'generated',?,1,0.4)",
        (sid, eid, str(chart)))
    row = cat.chart_row_for_entry(eid)
    assert row["id"] == gen_id and row["source_repo"] == "generated"


def test_resync_endpoint_gated(client, host_client):
    """Resync needs host or the current singer; 409 when nothing scored
    is playing."""
    guest = client.application.test_client()
    assert guest.post("/api/playback/resync").status_code == 403
    assert host_client.post("/api/playback/resync").status_code == 409


def test_manage_songs_lists_versions(client):
    """The Songs tab feed: one row per media version with file size and
    chart/queue state."""
    rows = client.get("/api/manage/songs").get_json()["songs"]
    assert any(r["title"] == "Signal In The Noise" for r in rows)
    demo = [r for r in rows if r["title"] == "Signal In The Noise"][0]
    assert demo["size_bytes"] > 0
    assert demo["scored"] >= 1


def test_manage_delete_requires_host(client):
    assert client.post("/api/manage/song/1/delete").status_code == 403
    assert client.post("/api/manage/entry/1/delete").status_code == 403


def test_manage_delete_entry_removes_file_and_empty_song(
        host_client, db, tmp_config):
    """Deleting a version removes its file; a song with no versions left
    is removed as well."""
    row = db.query(
        "SELECT e.id AS eid, e.local_path, s.id AS sid"
        " FROM media_entry e JOIN song s ON s.id=e.song_id"
        " WHERE s.title='Video Only Groove'")[0]
    media = tmp_config.media_dir / row["local_path"]
    assert media.exists()
    res = host_client.post(f"/api/manage/entry/{row['eid']}/delete")
    assert res.status_code == 200
    assert not media.exists()
    assert not db.query("SELECT 1 FROM media_entry WHERE id=?",
                        (row["eid"],))
    # last version deleted -> the song row goes too
    assert not db.query("SELECT 1 FROM song WHERE id=?", (row["sid"],))


def test_manage_delete_keeps_scores_and_drops_queue(host_client, db):
    """FK-safe delete: queued items vanish but play_history survives with
    its queue link nulled."""
    db.execute(
        "INSERT INTO performer (name,color,created_at)"
        " VALUES ('Alex','#fff',1)")
    perf = db.query("SELECT id FROM performer WHERE name='Alex'")[0]["id"]
    song = db.query(
        "SELECT s.id AS sid, e.id AS eid FROM song s"
        " JOIN media_entry e ON e.song_id=s.id"
        " WHERE s.title='Signal In The Noise'")[0]
    qid = db.execute(
        "INSERT INTO queue_item"
        " (song_id,media_entry_id,performer_id,position,status,created_at)"
        " VALUES (?,?,?,1,'done',1)", (song["sid"], song["eid"], perf))
    db.execute(
        "INSERT INTO play_history"
        " (queue_item_id,song_id,performer_id,played_at,score)"
        " VALUES (?,?,?,1,9000)", (qid, song["sid"], perf))
    res = host_client.post(f"/api/manage/song/{song['sid']}/delete")
    assert res.status_code == 200
    assert not db.query("SELECT 1 FROM queue_item WHERE id=?", (qid,))
    assert not db.query("SELECT 1 FROM song WHERE id=?", (song["sid"],))
    hist = db.query("SELECT * FROM play_history WHERE song_id=?",
                    (song["sid"],))
    assert hist and hist[0]["score"] == 9000
    assert hist[0]["queue_item_id"] is None


def test_repair_requires_host(client):
    assert client.post("/api/manage/entry/1/repair").status_code == 403


def test_repair_deletes_generated_chart(
        host_client, db, tmp_config, monkeypatch):
    """Repair drops a generated chart row + file so the pipeline rebuilds
    it fresh (LRCLIB re-match, ASR timing, pitch)."""
    entry = db.query(
        "SELECT e.id AS eid FROM media_entry e JOIN song s ON s.id=e.song_id"
        " WHERE s.title='Signal In The Noise'")[0]
    chart_file = tmp_config.chart_library_dir / "gen" / "gen-test.upsong"
    chart_file.parent.mkdir(parents=True, exist_ok=True)
    chart_file.write_text("{}")
    db.execute(
        "INSERT INTO chart (song_id,media_entry_id,source_repo,file_path,"
        " verified,confidence) SELECT song_id,id,'generated',?,1,0.4"
        " FROM media_entry WHERE id=?", (str(chart_file), entry["eid"]))
    calls = []
    monkeypatch.setattr("ultrapistar.web.api.ensure_map",
                        lambda ctx, eid, item_id=None: calls.append(eid))
    res = host_client.post(f"/api/manage/entry/{entry['eid']}/repair")
    assert res.status_code == 200
    assert res.get_json()["regenerating"] is True
    assert calls == [entry["eid"]]
    assert not chart_file.exists()
    assert not db.query("SELECT 1 FROM chart WHERE file_path=?",
                        (str(chart_file),))


def test_repair_imported_chart_resets_warp(host_client, db, monkeypatch):
    """An imported (USDB) chart keeps its notes — repair retries our own
    generator first and clears the media warp as the fallback path."""
    entry = db.query(
        "SELECT e.id AS eid FROM media_entry e JOIN song s ON s.id=e.song_id"
        " WHERE s.title='Signal In The Noise'")[0]
    cid = db.execute(
        "INSERT INTO chart (song_id,media_entry_id,source_repo,file_path,"
        " verified,confidence,sync_warp) SELECT song_id,id,'usdb',"
        " '/nowhere/x.txt',1,1.0,'{""5"":{""marks"":[[1,2]]}}'"
        " FROM media_entry WHERE id=?", (entry["eid"],))
    calls = []
    monkeypatch.setattr("ultrapistar.web.api.ensure_map",
                        lambda ctx, eid, item_id=None: calls.append(eid))
    res = host_client.post(f"/api/manage/entry/{entry['eid']}/repair")
    assert res.status_code == 200
    assert res.get_json()["regenerating"] is True
    assert calls == [entry["eid"]]
    row = db.query("SELECT sync_warp FROM chart WHERE id=?", (cid,))[0]
    assert row["sync_warp"] == "{}"
    # the imported chart row itself survives — it stays as the fallback
    assert db.query("SELECT 1 FROM chart WHERE id=?", (cid,))


def test_pick_lrc_prefers_closest_length():
    """Two records with identical text: the one whose synced span best
    matches the heard-singing span wins (not necessarily the one whose
    declared duration is nearest)."""
    from ultrapistar.web.api import _pick_lrc
    words = "alpha bravo charlie delta echo foxtrot golf hotel".split()
    asr = [(w, 10000 + i * 24000) for i, w in enumerate(words)]  # ~170s
    texts = ["alpha bravo charlie", "delta echo foxtrot",
             "golf hotel india juliet"]
    # full edit: synced lines spread across ~175s, duration 200
    full = {"artist_name": "A", "track_name": "T", "duration": 200,
            "source": "synclrc",
            "synced": [(5000, texts[0]), (90000, texts[1]),
                       (175000, texts[2])],
            "plain": texts}
    # radio-edit edit: same text crammed into 14s, duration 15
    short = {"artist_name": "A", "track_name": "T", "duration": 15,
             "source": "lrclib",
             "synced": [(0, texts[0]), (7000, texts[1]), (14000, texts[2])],
             "plain": texts}
    rec, rows = _pick_lrc([short, full], asr, "T", media_dur=200)
    assert rec is full
    diffs = {r["src"]: r["dur_diff"] for r in rows}
    assert diffs["lrclib"] == 185.0 and diffs["synclrc"] == 0.0


def test_pick_lrc_partial_still_loses_to_fuller_farther_duration():
    """Bicycle Race: partial 26-line record has a *closer* declared
    duration (195s vs media 196) but stops at 45% coverage — the fuller
    record 12s off should still win."""
    from ultrapistar.web.api import _pick_lrc
    words = "bicycle race bicycle race bicycle race".split()
    asr = [(w, 5000 + i * 33000) for i, w in enumerate(words)]  # ~170s
    # full record: synced lines spread over ~175s (partial_cover ~0.95)
    full_lines = [(i * 5000, "bicycle race bicycle race")
                  for i in range(36)]
    full = {"artist_name": "Queen", "track_name": "Bicycle Race",
            "duration": 184, "source": "synclrc",
            "synced": full_lines,
            "plain": [t for _, t in full_lines]}
    # partial record: same lines but only first 16 (~75s span), dur 195
    partial = {"artist_name": "Queen", "track_name": "Bicycle Race",
               "duration": 195, "source": "lrclib",
               "synced": full_lines[:16],
               "plain": [t for _, t in full_lines[:16]]}
    rec, rows = _pick_lrc([partial, full], asr, "Bicycle Race",
                          media_dur=196)
    assert rec is full


def test_manage_delete_playing_blocked(host_client, client, db):
    """A song currently on the TV can't be deleted."""
    client.post("/api/join", json={"name": "Alex"})
    entry = client.get("/api/search?q=signal").get_json()["results"][0]
    client.post("/api/queue",
                json={"entry_id": entry["entry_id"], "name": "Alex"})
    # first queued item auto-promotes to 'playing'
    res = host_client.post(f"/api/manage/song/{entry['id']}/delete")
    assert res.status_code == 409
    res = host_client.post(
        f"/api/manage/entry/{entry['entry_id']}/delete")
    assert res.status_code == 409


def test_upload_mp3_creates_local_entry(client, tmp_config):
    """An uploaded mp3 lands in the media dir as a local-provider entry,
    with artist/title parsed from the 'Artist - Title' filename."""
    import io
    data = {"file": (io.BytesIO(b"ID3" + b"\x00" * 256),
                     "Test Artist - Test Song.mp3")}
    r = client.post("/api/upload", data=data,
                    content_type="multipart/form-data")
    assert r.status_code == 200
    ent = r.get_json()["entry"]
    assert ent["provider"] == "local"
    assert ent["artist"] == "Test Artist"
    assert ent["title"] == "Test Song"
    files = list(tmp_config.media_dir.glob("upload-*.mp3"))
    assert len(files) == 1


def test_upload_rejects_bad_extension(client, tmp_config):
    import io
    data = {"file": (io.BytesIO(b"x" * 32), "notes.exe")}
    r = client.post("/api/upload", data=data,
                    content_type="multipart/form-data")
    assert r.status_code == 400
    assert not list(tmp_config.media_dir.glob("upload-*"))


def test_uploaded_song_is_queueable(client, tmp_config):
    """Upload → entry → queue: the same confirm-sheet flow as any entry."""
    import io
    data = {"file": (io.BytesIO(b"fLaC" + b"\x00" * 256), "mine.flac"),
            "artist": "Uploader", "title": "My Tune"}
    r = client.post("/api/upload", data=data,
                    content_type="multipart/form-data")
    ent = r.get_json()["entry"]
    assert ent["status"] == "available"
    client.post("/api/join", json={"name": "Alex"})
    r = client.post("/api/queue",
                    json={"entry_id": ent["entry_id"], "name": "Alex"})
    assert r.status_code == 200, r.get_json()
