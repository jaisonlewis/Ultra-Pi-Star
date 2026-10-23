"""Realtime channel (SocketIO).

Event map:
  client -> server
    state:pull            any client requests a fresh queue/now-playing snapshot
    tv:ready              TV player connected -> server sends current playback
    player:ended          TV finished the video -> advance queue
    player:error          TV could not play -> mark error, advance
    player:position       TV clock updates -> relayed for future scoring sync
  server -> clients
    state                 full queue + now-playing snapshot
    now_playing           playback descriptor for the TV (item/entry/playback)
"""
from __future__ import annotations

import json
import time

from flask import current_app, request
from flask_socketio import SocketIO

from ..catalog import Catalog
from ..config import Config
from ..db import Database
from ..providers import ProviderRegistry
from ..queue_manager import QueueManager

_queue: QueueManager
_catalog: Catalog
_providers: ProviderRegistry
_db: Database
_config: Config
_io: SocketIO


def init(socketio: SocketIO, queue: QueueManager, catalog: Catalog,
         providers: ProviderRegistry, db: Database, config: Config) -> None:
    global _queue, _catalog, _providers, _db, _config, _io
    _queue, _catalog, _providers, _db, _config, _io = (
        queue, catalog, providers, db, config, socketio)

    @socketio.on("connect")
    def on_connect():
        ctx = current_app.extensions["ups"]
        # version handshake — pages still running pre-deploy JS reload to
        # pick up the new code (the kiosk otherwise keeps stale JS forever
        # since socket.io reconnects without a page reload)
        socketio.emit("hello", {"v": ctx.get("build_v")}, to=request.sid)
        socketio.emit("state", _queue.snapshot(), to=request.sid)
        socketio.emit("host:state",
                      {"claimed": bool(ctx.get("host_performer_id")),
                       "key": _config.host_key},
                      to=request.sid)

    @socketio.on("state:pull")
    def on_pull():
        socketio.emit("state", _queue.snapshot(), to=request.sid)

    @socketio.on("tv:ready")
    def on_tv_ready():
        from .api import push_now_playing
        ctx = current_app.extensions["ups"]
        # kiosk liveness for the watchdog thread in app.py
        ctx["tv_sid"] = request.sid
        ctx["tv_seen"] = time.time()
        item = _queue.now_playing() or _queue.start_next()
        push_now_playing(ctx)

    @socketio.on("disconnect")
    def on_disconnect():
        ctx = current_app.extensions["ups"]
        if ctx.get("tv_sid") == request.sid:
            ctx["tv_seen"] = 0.0       # TV socket dead — watchdog may kick

    @socketio.on("player:ended")
    def on_player_ended(data=None):
        from .api import push_now_playing
        ctx = current_app.extensions["ups"]
        score = (data or {}).get("score") if isinstance(data, dict) else None
        sess = ctx.get("sing")
        if sess:
            final = sess.finish()
            ctx["sing"] = None
            score = final["score"]
            socketio.emit("sing:final", final)
        _queue.finish_current("done", score=score)
        push_now_playing(ctx)

    @socketio.on("player:stuck")
    def on_player_stuck(data=None):
        """TV reports the media element never un-paused after play() —
        usually a dead audio path (Chromium can't open a PipeWire stream
        so the media clock never starts). The item stays 'playing':
        bounce PipeWire and have the TV retry in place first, restart
        the kiosk on the second stall, give up on the third — same
        ladder the liveness watchdog climbs, just minutes faster."""
        from ..app import bounce_audio_stack, restart_kiosk
        ctx = current_app.extensions["ups"]
        item = _queue.now_playing()
        if not item or (data or {}).get("item") != item["id"]:
            return                        # stale report for a dead item
        tries = ctx.setdefault("stuck_tries", {})
        n = tries.get(item["id"], 0) + 1
        tries[item["id"]] = n
        current_app.logger.warning(
            "player:stuck item %s (attempt %d) — %s", item["id"], n,
            "bouncing audio" if n == 1 else
            "bouncing audio + kiosk" if n == 2 else "giving up")
        if n >= 3:
            tries.pop(item["id"], None)
            on_player_error({"reason": "stuck-gave-up"})
            return
        bounce_audio_stack(current_app.logger)
        if n >= 2:
            restart_kiosk(current_app.logger)   # fresh page re-pushes
        else:
            socketio.emit("player:retry")       # in-place play() retry

    @socketio.on("player:error")
    def on_player_error(data=None):
        from .api import push_now_playing
        ctx = current_app.extensions["ups"]
        if ctx.get("sing"):
            ctx["sing"].stop_mic()
            ctx["sing"] = None
        current_app.logger.warning("player:error %r", data)
        _queue.finish_current("error")
        socketio.emit("toast", {"level": "error",
                                "message": "Playback failed — skipping to next song"})
        push_now_playing(ctx)

    @socketio.on("player:position")
    def on_player_position(data):
        # authoritative media clock -> scoring ticks + clock relay
        ctx = current_app.extensions["ups"]
        now = time.time()
        ctx["tv_seen"] = now           # playing TV proves the page is alive
        item = _queue.now_playing()
        if not item or not isinstance(data, dict):
            return
        ctx["tv_last_pos"] = now       # heartbeat for the kiosk watchdog
        ctx["tv_pos_item"] = item["id"]
        ctx.pop("tv_expect_pos", None) # positions resumed after a pause
        pos_sec = data.get("position_sec")
        socketio.emit("clock", {"item_id": item["id"],
                                "position_sec": pos_sec})
        sess = ctx.get("sing")
        if sess and pos_sec is not None:
            # pitch arriving now was sung mic_delay_ms ago — score it
            # against the music position it was a response to
            pos_ms = max(0.0, float(pos_sec) * 1000
                         - ctx.get("mic_delay_ms", 0))
            update = sess.tick(pos_ms)
            socketio.emit("sing:update", update)
            mon = ctx.get("monitor")
            if mon is not None:
                # chart notes at this position steer mic autotune
                mon.set_targets(sess.targets)
            for lev in update.get("line_events", []):
                socketio.emit("sing:line", lev)
            # mic autosync can be host-disabled (setting autosync_mic=0)
            # — the deterministic media alignment still runs either way
            res = (None if ctx["db"].get_setting("autosync_mic", "1") == "0"
                   else sess.maybe_autosync())
            if res and res.get("offset_ms") is not None:
                # confident pitch-contour match — same effect as a host
                # sync nudge: persist on the chart row and retime the
                # live lyric/lane payload without touching playback
                new_off = res["offset_ms"]
                # learn it as a per-phrase warp entry too — a gap that
                # runs long/short here only re-times later phrases, and
                # replaying the song inherits the correction
                beat = res.get("warp_beat") or sess.warp_boundary(pos_ms)
                sess.learn_warp(beat, new_off)
                row = ctx["catalog"].chart_row_for_entry(item["entry_id"])
                if row:
                    try:
                        warp_map = json.loads(row["sync_warp"] or "{}")
                    except (TypeError, ValueError):
                        warp_map = {}
                    if not isinstance(warp_map, dict):
                        warp_map = {}
                    warp_map[str(item["entry_id"])] = {
                        "ratio": sess.tempo, "marks": sess._warp}
                    ctx["db"].execute(
                        "UPDATE chart SET sync_offset_ms=?, sync_warp=?"
                        " WHERE id=?",
                        (new_off, json.dumps(warp_map), row["id"]))
                sess.apply_sync_offset(new_off, pos_ms)
                socketio.emit("lyrics",
                              ctx["catalog"].chart_payload(
                                  item["entry_id"],
                                  rom=bool(item.get("lyrics_rom"))))
            if res:
                socketio.emit("sing:autosync", res)
