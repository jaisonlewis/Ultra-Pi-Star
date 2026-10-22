"""Application factory: wires config, db, catalog, providers, queue, web."""
from __future__ import annotations

from flask import Flask
from flask_socketio import SocketIO

from .artwork import ArtworkFetcher
from .catalog import Catalog
from .config import Config
from .db import Database
from .downloads import DownloadManager
from .providers import LocalProvider, ProviderRegistry
from .queue_manager import QueueManager


def create_app(config: Config | None = None, seed: bool = True,
               async_mode: str = "threading"
               ) -> tuple[Flask, SocketIO, Config]:
    config = config or Config()
    app = Flask(__name__, template_folder="web/templates",
                static_folder="web/static")
    # stable session secret: persisted so phone sessions survive restarts
    # (host_key regenerates each boot and must NOT be the secret — it would
    # log every guest out on every deploy)
    secret_file = config.data_dir / "session_secret"
    if secret_file.exists():
        app.config["SECRET_KEY"] = secret_file.read_text().strip()
    else:
        import secrets as _sec
        config.data_dir.mkdir(parents=True, exist_ok=True)
        app.config["SECRET_KEY"] = _sec.token_urlsafe(32)
        secret_file.write_text(app.config["SECRET_KEY"])
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
    # LAN app: always revalidate static so phones never run stale js
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    db = Database(config.db_path)
    catalog = Catalog(db, config.chart_dir, config.trusted_chart_sources)
    if seed:
        catalog.seed_if_empty(config.media_dir)
    # index dropped charts + any imported chart library (e.g. usdx-songs)
    catalog.rescan_charts([config.chart_library_dir])
    queue = QueueManager(db)

    providers = ProviderRegistry()
    if "local" in config.enabled_providers:
        providers.register(LocalProvider(config.media_dir,
                                         config.media_extra_dirs))
    if "youtube" in config.enabled_providers:
        try:
            from .providers.youtube import YouTubeProvider
            providers.register(YouTubeProvider(config.media_dir,
                                               config.media_extra_dirs))
        except ImportError:
            app.logger.warning("youtube provider enabled but yt-dlp not installed")

    socketio = SocketIO(app, async_mode=async_mode, cors_allowed_origins="*")
    if async_mode == "eventlet":
        # Emits from real worker threads (downloads, pipeline, watchdogs)
        # can't switch into the hub's greenlets — eventlet raises
        # "Cannot switch to a different thread" and the update is lost.
        # schedule_call_global is the one hub primitive that is safe to
        # call from any thread, so route every emit through it. Emits
        # inside socket handlers just queue one loop tick later — same
        # ordering, no behavioral change.
        import eventlet
        _hub = eventlet.hubs.get_hub()
        _emit = socketio.emit

        def _emit_threadsafe(*args, **kwargs):
            _hub.schedule_call_global(0, _emit, *args, **kwargs)

        socketio.emit = _emit_threadsafe

    def _emit_state():
        socketio.emit("state", queue.snapshot())

    def _repush(entry_id: int):
        # if the item waiting on the TV just became playable, push it again
        item = queue.now_playing()
        if item and item.get("entry_id") == entry_id:
            from .web.api import push_now_playing
            push_now_playing(app.extensions["ups"])

    def _on_available(entry_id):
        # media just landed — attach/rebind any library charts that matched
        # while this entry was still downloading, then build its timing
        # map immediately so it's ready long before the TV needs it
        rows = db.query("SELECT song_id FROM media_entry WHERE id=?", (entry_id,))
        if rows:
            catalog.attach_matching_charts(rows[0]["song_id"])
            catalog.rebind_song_charts(rows[0]["song_id"])
        ctx = app.extensions.get("ups")
        if ctx:
            from .web.api import ensure_map
            ensure_map(ctx, entry_id)

    downloads = DownloadManager(db, providers, config.media_dir,
                                _emit_state, _repush, _on_available,
                                media_extra_dirs=config.media_extra_dirs)
    if config.media_extra_dirs:
        app.logger.info(
            "media roots: write=%s read-fallback=%s",
            config.media_dir,
            ", ".join(str(d) for d in config.media_extra_dirs))

    # orphaned intermediates — a normalization pass or yt-dlp download
    # interrupted mid-write leaves *.norm.* temps and .part/.ytdl files
    # nothing retries; the 5-min grace keeps a just-written temp safe
    import time
    from pathlib import Path
    try:
        cutoff = time.time() - 300
        for f in (p for root in config.media_roots()
                  for p in root.iterdir() if root.is_dir()):
            n = f.name
            if (f.is_file() and f.stat().st_mtime < cutoff
                    and (".norm." in n or n.endswith(".part")
                         or n.endswith(".ytdl") or ".part-Frag" in n)):
                f.unlink(missing_ok=True)
    except Exception:
        pass

    # a download killed mid-flight by a restart would sit 'downloading'
    # forever — reset and retry so a waiting TV still gets its media
    for row in db.query(
            "SELECT id FROM media_entry WHERE status='downloading'"):
        db.execute("UPDATE media_entry SET status='failed' WHERE id=?",
                   (row["id"],))
        entry = catalog.entry(row["id"])
        if entry:
            downloads.ensure(entry)

    # downloads that already failed but still have a waiting queue item
    # (e.g. throttled mid-batch) get a retry too — the serial worker
    # drains them one at a time
    for row in db.query(
            "SELECT DISTINCT e.id FROM media_entry e"
            " JOIN queue_item q ON q.media_entry_id=e.id"
            " WHERE e.status='failed' AND q.status='queued'"):
        entry = catalog.entry(row["id"])
        if entry:
            downloads.ensure(entry)

    # same for a song that was 'playing' when the service stopped — the
    # video is gone with the process, so the zombie row would otherwise
    # block every future advance()
    db.execute("UPDATE queue_item SET status='skipped' WHERE status='playing'")

    app.extensions["ups"] = {
        # "app" lets worker threads use app.logger/app_context without
        # current_app, which raises outside a request context
        "app": app,
        "config": config, "db": db, "catalog": catalog,
        "queue": queue, "providers": providers, "socketio": socketio,
        "downloads": downloads,
        "artwork": ArtworkFetcher(db, config.artwork_dir),
        "mic": None,
        "monitor": None,
        # speaker->mic loop delay from /api/mic/calibrate — scoring shifts
        # the media position by this so pitch is judged against what the
        # singer was actually hearing
        "mic_delay_ms": int(float(
            db.get_setting("mic_delay_ms", "0") or 0)),
    }

    # shared mics: every detected capture device feeds pitch detection AND
    # the mixed speaker monitor (each ALSA device can only be opened once).
    # Host mic->singer assignments persist in the settings table.
    try:
        import json as _json
        from .mic import MicManager, MicMonitor
        monitor = MicMonitor(config.play_device)
        micman = MicManager(monitor)
        micman.on_change = lambda: db.set_setting(
            "mic_assign", _json.dumps(micman.assignments))
        try:
            saved = _json.loads(db.get_setting("mic_assign", "{}") or "{}")
            micman.assignments = {k: int(v) for k, v in saved.items()}
        except Exception:
            pass
        if config.mic_device == "default":
            found = micman.discover()
        else:  # explicit UPS_MIC_DEVICE pins a single capture device
            cap = micman.ensure(config.mic_device)
            found = [config.mic_device] if cap.start().available else []
            if cap.running:
                monitor.start()
        app.extensions["ups"]["micman"] = micman
        app.extensions["ups"]["monitor"] = monitor
        micman.start_supervisor()
        # back-compat: "mic" = first running capture for older call sites
        app.extensions["ups"]["mic"] = next(
            (c for c in micman.captures.values() if c.running), None)
        if found:
            app.logger.info("mic capture: %d device(s), monitor %s",
                            len(found),
                            "running" if monitor.running else "failed")
        else:
            app.logger.info("no capture devices found")
    except Exception as exc:
        app.logger.warning("mic setup failed: %s", exc)

    # re-apply the host's saved audio output target (PipeWire session may
    # not be up yet at boot — best effort; phone UI can re-apply anytime)
    try:
        import json as _json
        from . import audio as _pw
        saved_out = _json.loads(db.get_setting("output_target", "") or "null")
        if isinstance(saved_out, dict):
            for kind in ("sink_name", "sink", "card"):
                if kind in saved_out:
                    r = _pw.select_output(kind, saved_out[kind])
                    app.logger.info("audio output -> %s",
                                    r.get("name") or r.get("error"))
                    break
    except Exception as exc:
        app.logger.info("audio output restore skipped: %s", exc)

    # content fingerprint of the web assets — changes whenever a deploy
    # ships new JS/CSS/markup, so it doubles as cache-buster and as the
    # "reload me" version in the socket hello handshake
    import hashlib
    from pathlib import Path
    h = hashlib.sha1()
    webroot = Path(__file__).parent / "web"
    for f in sorted(webroot.rglob("*")):
        if f.is_file():
            st = f.stat()
            h.update(f"{f.relative_to(webroot)}:{st.st_size}:"
                     f"{st.st_mtime_ns}".encode())
    app.extensions["ups"]["build_v"] = h.hexdigest()[:12]
    app.context_processor(
        lambda: {"v": app.extensions["ups"]["build_v"]})

    import logging
    app.logger.setLevel(logging.INFO)  # watchdog/mic logs -> journal
    _start_kiosk_watchdog(app)

    from .web.api import bp as api_bp
    from .web.pages import bp as pages_bp
    from .web import sockets  # noqa: F401  (registers socket handlers)

    sockets.init(socketio, queue, catalog, providers, db, config)
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp, url_prefix="/api")
    return app, socketio, config


def _user_env() -> dict:
    import os
    env = dict(os.environ)
    uid = getattr(os, "getuid", lambda: 1000)()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS",
                   f"unix:path=/run/user/{uid}/bus")
    return env


def bounce_audio_stack(logger=None) -> bool:
    """Restart pipewire + wireplumber. Recovers the wedged-PCM state
    where the sink still lists but Chromium can't open a stream — the
    media element then stays paused forever with no error event."""
    import shutil
    import subprocess
    if not shutil.which("systemctl"):
        return False
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", "pipewire.service",
             "pipewire-pulse.service", "wireplumber.service"],
            capture_output=True, timeout=20, env=_user_env())
        return r.returncode == 0
    except Exception as exc:
        (logger or _log()).warning("audio stack restart failed: %s", exc)
        return False


def restart_kiosk(logger=None) -> bool:
    import shutil
    import subprocess
    if not shutil.which("systemctl"):
        return False
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart",
             "ultrapistar-kiosk.service"],
            capture_output=True, timeout=15, env=_user_env())
        return r.returncode == 0
    except Exception as exc:
        (logger or _log()).warning("kiosk restart failed: %s", exc)
        return False


def _log():
    import logging
    return logging.getLogger(__name__)


def _start_kiosk_watchdog(app) -> None:
    """Restart the supervised kiosk when the TV socket goes silent.

    The TV page proves life via tv:ready / player:position; a dead renderer
    (crash, GPU hang) stops those. systemd's Restart=always relaunches the
    browser, which re-announces tv:ready and gets the current song pushed.
    Disabled entirely when no ultrapistar-kiosk unit exists (dev machines).
    """
    import os
    import shutil
    import subprocess
    import threading
    import time

    if not shutil.which("systemctl"):
        return
    env = dict(os.environ)
    uid = getattr(os, "getuid", lambda: 1000)()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS",
                   f"unix:path=/run/user/{uid}/bus")
    try:
        out = subprocess.run(
            ["systemctl", "--user", "list-unit-files",
             "ultrapistar-kiosk.service"],
            capture_output=True, text=True, timeout=10, env=env).stdout
    except Exception:
        return
    if "ultrapistar-kiosk" not in out:
        return

    STALE_S, RESUME_GRACE_S, BOOT_GRACE_S = 90, 60, 300
    PAGE_READY_S = 45
    MAX_RESTARTS_PER_HR = 3
    # a "Preparing lyrics…" hold is only valid while the map worker is
    # actually running — cap it far above a normal pipeline pass so a
    # wedged worker degrades to raw playback instead of hanging the item
    MAP_WAIT_S = 900

    def loop():
        ctx = app.extensions["ups"]
        queue = ctx["queue"]
        booted = time.time()
        restarts: list[float] = []
        while True:
            time.sleep(30)
            now = time.time()
            item = queue.now_playing()
            item_id = item["id"] if item else None

            # A dead renderer stops socket.io heartbeats -> server-side
            # disconnect marks tv_seen=0. Covers idle, paused and playing.
            dead = ctx.get("tv_seen") == 0.0
            # Never announced itself after boot/restart.
            never_seen = ctx.get("tv_sid") is None and (
                now - booted) > BOOT_GRACE_S
            # The page was served but tv:ready never followed — the
            # renderer loaded the HTML then wedged before running the JS
            # (crashed render process can still commit the navigation).
            # Much tighter than BOOT_GRACE_S since JS init takes seconds.
            page_at = ctx.get("tv_page_at")
            page_wedged = (
                ctx.get("tv_sid") is None
                and page_at is not None
                and (now - page_at) > PAGE_READY_S)
            # Positions flowed for this item then went silent while it is
            # still now_playing and not paused -> wedged video pipeline.
            pos_stale = (
                item_id is not None
                and ctx.get("tv_pos_item") == item_id
                and ctx.get("tv_paused") != item_id
                and (now - ctx.get("tv_last_pos", 0)) > STALE_S)
            # A playable payload was pushed but positions never started
            # at all — renderer wedged before the first heartbeat; the
            # pos_stale check can't see it since tv_pos_item still holds
            # the previous song's id.
            pushed = ctx.get("np_pushed")
            no_positions = (
                item_id is not None
                and pushed and pushed[0] == item_id
                and ctx.get("tv_pos_item") != item_id
                and ctx.get("tv_paused") != item_id
                and (now - pushed[1]) > STALE_S)
            # Host resumed but positions never came back.
            expect_at = ctx.get("tv_expect_pos")
            resume_stuck = (
                item_id is not None
                and expect_at is not None
                and ctx.get("tv_pos_item") == item_id
                and (now - expect_at) > RESUME_GRACE_S
                and ctx.get("tv_last_pos", 0) < expect_at)
            # "Preparing lyrics…" outlived the whole pipeline budget —
            # the map worker is wedged; drop the inflight marker and let
            # the song play raw rather than hold the TV on a spinner.
            waiting = ctx.get("np_waiting")
            if (item_id is not None and waiting
                    and waiting[0] == item_id
                    and now - waiting[1] > MAP_WAIT_S):
                app.logger.warning(
                    "map wait exceeded %ds — releasing playback raw",
                    MAP_WAIT_S)
                ctx.get("map_inflight", set()).discard(item["entry_id"])
                ctx.pop("np_waiting", None)
                from .web.api import push_now_playing
                push_now_playing(ctx)
            if not (dead or never_seen or page_wedged or pos_stale
                    or no_positions or resume_stuck):
                continue
            restarts = [t for t in restarts if now - t < 3600]
            if len(restarts) >= MAX_RESTARTS_PER_HR:
                continue                  # deeper problem — stop hammering
            restarts.append(now)
            for key in ("tv_seen", "tv_sid", "tv_last_pos", "tv_pos_item",
                        "tv_paused", "tv_expect_pos", "np_pushed",
                        "np_waiting", "tv_page_at"):
                ctx.pop(key, None)
            booted = now               # fresh grace window for relaunch
            mem_mb = None
            try:
                for ln in open("/proc/meminfo"):
                    if ln.startswith("MemAvailable:"):
                        mem_mb = int(ln.split()[1]) // 1024
                        break
            except Exception:
                pass
            app.logger.warning(
                "TV liveness lost (%s) — restarting kiosk "
                "(attempt %d/hr, memavail=%sMB)",
                "socket-dead" if dead else
                "never-ready" if never_seen else
                "page-wedged" if page_wedged else
                "position-stalled" if pos_stale else
                "no-positions" if no_positions else "resume-stuck",
                len(restarts), mem_mb)
            # dead positions can also mean the PipeWire PCM is wedged —
            # the sink still lists in wpctl but snd_pcm_mmap_commit
            # broken-pipes, so Chromium can't open a stream and play()
            # never un-pauses. A kiosk restart can't fix that; audio is
            # already silent here, so bouncing the stack is free — do it
            # before relaunching the browser so the stream path exists.
            if pos_stale or no_positions or resume_stuck:
                bounce_audio_stack(app.logger)
            restart_kiosk(app.logger)

    threading.Thread(target=loop, daemon=True,
                     name="kiosk-watchdog").start()
    app.logger.info("kiosk watchdog armed")

    # audio twin of the renderer wedge: wireplumber can start while the
    # HDMI cards aren't probeable yet and leaves them profile-less —
    # everything plays into "Dummy Output" and the TV stays silent.
    if not shutil.which("wpctl"):
        return
    import re

    def sink_alive() -> bool:
        try:
            out = subprocess.run(
                ["wpctl", "status"], capture_output=True, text=True,
                timeout=8, env=env).stdout
            block = out.split("Sinks:", 1)[1].split("Sources:", 1)[0]
        except Exception:
            return True               # can't inspect → don't flap
        return any(re.search(r"\d+\.\s+\S", ln)
                   and "Dummy Output" not in ln
                   for ln in block.splitlines())

    def pcm_wedged() -> bool:
        """The HDMI PCM broken-pipe burst shows in pipewire's journal
        BEFORE a song ever tries to open the stream — catching it here
        turns the ~100s stuck-countdown failure into a pre-emptive
        bounce while the TV is idle or the song is still preparing."""
        try:
            out = subprocess.run(
                ["journalctl", "--user", "-u", "pipewire.service",
                 "--since", "-50 sec", "--no-pager", "-o", "cat"],
                capture_output=True, text=True, timeout=8,
                env=env).stdout
        except Exception:
            return False              # can't inspect → don't flap
        return out.count("Broken pipe") >= 4

    def audio_loop():
        ctx = app.extensions["ups"]
        last_fix = 0.0
        while True:
            time.sleep(45)
            now = time.time()
            try:
                # wedged PCM is worth fixing even while idle — the next
                # song would otherwise walk straight into the dead path
                if pcm_wedged() and now - last_fix >= 600:
                    last_fix = now
                    app.logger.warning(
                        "audio watchdog: PCM broken-pipe burst — "
                        "restarting audio stack")
                    bounce_audio_stack(app.logger)
                    continue
                if (ctx["queue"].now_playing() is None or sink_alive()
                        or now - last_fix < 600):
                    continue
                last_fix = now
                app.logger.warning(
                    "no audio sink while playing — restarting wireplumber")
                subprocess.run(
                    ["systemctl", "--user", "restart",
                     "wireplumber.service"],
                    capture_output=True, timeout=15, env=env)
            except Exception as exc:
                app.logger.warning("audio watchdog: %s", exc)

    threading.Thread(target=audio_loop, daemon=True,
                     name="audio-watchdog").start()
