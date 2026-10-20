"""Microphone capture on the Pi via ALSA ``arecord``.

Streams int16 mono PCM into ``PitchDetector`` on a reader thread and
invokes a callback with each analysis result (~every 46ms with a 2048
sample hop). Gracefully reports mic-unavailable instead of raising, so a
scored song without a mic simply shows "no mic" on the TV.
"""
from __future__ import annotations

import collections
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

from .pitch import PitchDetector, PitchResult

HOP_SAMPLES = 2048            # ~46ms at 44.1kHz
BYTES_PER_SAMPLE = 2          # S16_LE mono


def _pw_env() -> dict:
    """Systemd services lack a user session — point PipeWire clients at
    the user's runtime dir so pw-cat finds the daemon socket."""
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR",
                   f"/run/user/{getattr(os, 'getuid', lambda: 1000)()}")
    return env


@dataclass
class MicStatus:
    available: bool
    device: str = "default"
    reason: str = ""


def _pipewire() -> bool:
    """PipeWire-native tools present (pw-cat ships with pipewire-bin)."""
    return bool(shutil.which("pw-cat"))


class MicCapture:
    """Background capture feeding a pitch detector.

    Two backends: ``pw-cat`` (PipeWire — shares devices cleanly with the
    desktop audio graph, preferred) or raw ALSA ``arecord`` fallback.
    ``device`` is a PipeWire node name for the pw backend or an ALSA id
    like ``hw:2,0`` for the arecord fallback.
    """

    def __init__(self, device: str = "default", rate: int = 44100,
                 threshold_index: int = 2, backend: str = "auto"):
        self.device = device
        self.rate = rate
        self.backend = "pipewire" if backend == "auto" and _pipewire() \
            else backend if backend != "auto" else "alsa"
        self.detector = PitchDetector(rate, threshold_index)
        self.latest = PitchResult(valid=False)
        self.level = 0.0         # smoothed input level 0..1 (for UI meter)
        self.running = False
        self.error = ""
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._listeners: list = []
        self.monitor: MicMonitor | None = None
        self._calib: dict | None = None
        # rolling raw-audio window for speech-model lyric sync (Whistle)
        self._pcm_window: collections.deque = collections.deque()

    def _cmd(self) -> list[str]:
        if self.backend == "pipewire":
            return ["pw-cat", "-r", "--raw", "--format", "s16",
                    "--rate", str(self.rate), "--channels", "1",
                    "--latency", "256",
                    "--target", self.device, "-"]
        return ["arecord", "-D", self.device, "-f", "S16_LE",
                "-r", str(self.rate), "-c", "1", "-t", "raw", "-q"]

    # -- device discovery ---------------------------------------------------
    @staticmethod
    def list_devices() -> list[dict]:
        """Capture devices as [{name, id}] — PipeWire sources when pw-cat
        exists (id = stable node.name), else `arecord -l` hw ids."""
        if _pipewire():
            try:
                from .audio import sources
                srcs = sources()
                if srcs:
                    return [{"name": s["desc"], "id": s["name"]}
                            for s in srcs]
            except Exception:
                pass
        if not shutil.which("arecord"):
            return []
        try:
            out = subprocess.run(
                ["arecord", "-l"], capture_output=True, text=True,
                timeout=5).stdout
        except Exception:
            return []
        devs = []
        for line in out.splitlines():
            # "card 1: Device [USB Audio Device], device 0: ..."
            if line.startswith("card "):
                card, _, rest = line.partition(":")
                card_num = card.split()[1]
                name = rest.strip().split("[")[1].split("]")[0] if "[" in rest else rest.strip()
                sub = line.split("device ")[1].split(":")[0] if "device " in line else "0"
                devs.append({"name": name, "id": f"hw:{card_num},{sub}"})
        return devs

    PCM_WINDOW_SEC = 15

    def pcm_window(self) -> list:
        """[(monotonic_ts, s16_chunk)] covering the last ~15s of capture."""
        return list(self._pcm_window)

    def clear_pcm(self) -> None:
        """Drop the rolling window — a manual resync transcribes only
        audio captured *after* the button press."""
        self._pcm_window.clear()

    def on_pitch(self, fn) -> None:
        self._listeners.append(fn)

    def _emit(self, res: PitchResult) -> None:
        with self._lock:
            self.latest = res
        for fn in self._listeners:
            try:
                fn(res)
            except Exception:
                pass

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> MicStatus:
        if self.running:
            return MicStatus(True, self.device)
        tool = "pw-cat" if self.backend == "pipewire" else "arecord"
        if not shutil.which(tool):
            self.error = f"{tool} not installed"
            return MicStatus(False, self.device, self.error)
        try:
            self._proc = subprocess.Popen(
                self._cmd(), env=_pw_env(),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.error = str(exc)
            return MicStatus(False, self.device, self.error)
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return MicStatus(True, self.device)

    def _update_level(self, chunk: bytes) -> None:
        """Peak-follow RMS meter — fast attack, slow decay for a stable bar."""
        try:
            import numpy as np
            pcm = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(pcm * pcm)) / 32768.0) if len(pcm) else 0.0
            self.level = rms if rms > self.level else self.level * 0.85
        except Exception:
            pass

    def calib_start(self) -> None:
        """Begin latency calibration: buffer raw chunks with arrival times
        so the click pattern can be matched-filtered offline — weak mic
        pickup defeats plain energy thresholds but not correlation."""
        self._calib = {"chunks": []}

    def _loop(self) -> None:
        hop = HOP_SAMPLES * BYTES_PER_SAMPLE
        proc = self._proc
        try:
            while self.running and proc and proc.poll() is None:
                chunk = proc.stdout.read(hop) if proc.stdout else b""
                if not chunk:
                    time.sleep(0.01)
                    continue
                self._update_level(chunk)
                self._pcm_window.append((time.monotonic(), chunk))
                cutoff = time.monotonic() - self.PCM_WINDOW_SEC
                while self._pcm_window and self._pcm_window[0][0] < cutoff:
                    self._pcm_window.popleft()
                if self._calib is not None:
                    self._calib["chunks"].append((time.monotonic(), chunk))
                self.detector.feed(chunk)
                if self.monitor:
                    self.monitor.feed(
                        self.device, chunk,
                        self.latest.tone_abs if self.latest.valid else None)
                self._emit(self.detector.analyze())
        except Exception as exc:  # device unplugged etc.
            self.error = str(exc)
        finally:
            self.running = False

    def stop(self) -> None:
        self.running = False
        proc = self._proc
        self._proc = None
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


class _PitchShifter:
    """Streaming granular pitch shifter for the voice-FX path.

    Input grains (``G`` samples) are resampled by ``ratio`` and
    overlap-added on a half-grain hop with a Hann window — duration is
    preserved while pitch moves by the ratio. State carries between
    calls so arbitrary chunk boundaries don't seam. A fresh instance is
    created whenever the effect engages, so enabling mid-song starts
    clean."""
    def __init__(self, rate: int, grain_ms: int = 46):
        import numpy as np
        self.rate = rate
        self.G = max(64, int(rate * grain_ms / 1000) & ~1)
        self.H = self.G // 2
        self.win = np.hanning(self.G).astype(np.float32)
        self.inbuf = np.zeros(0, dtype=np.float32)
        self.tail = np.zeros(self.H, dtype=np.float32)
        self._pos = np.arange(self.G, dtype=np.float32)

    def process(self, pcm, ratio: float):
        import numpy as np
        self.inbuf = np.concatenate([self.inbuf, pcm])
        need = int(np.ceil(self.G * ratio)) + 2
        outs = []
        while len(self.inbuf) >= need:
            src = np.interp(self._pos * ratio,
                            np.arange(len(self.inbuf)), self.inbuf)
            g = src * self.win
            self.inbuf = self.inbuf[self.H:]
            seg = np.concatenate(
                [self.tail, np.zeros(self.H, dtype=np.float32)])
            seg += g
            outs.append(seg[:self.H])
            self.tail = seg[self.H:]
        if not outs:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(outs)


class MicMonitor:
    """Mic passthrough: singers hear themselves through the speakers.

    Two dry-monitor paths:
      * ``pw-loopback`` (preferred): in-daemon mic->sink at ~10ms — the
        Python pipeline's ~150ms round-trip made singers hear themselves
        as a distracting slapback.
      * Python mix -> ``pw-cat`` playback (fallback): used when pw-loopback
        is unavailable; bounded queues drop lagging audio instead of
        letting delay accumulate.

    Either way the Python path still computes the wet echo tail — latency
    on an already-delayed signal is invisible — so echo/reverb keeps
    working in both modes. Volume/mute ride on the loopback's stream
    controls (wpctl) in passthrough mode, or DSP gain in fallback.
    """

    MAX_QUEUED = 3               # ~140ms max queued audio per source
    # Schroeder reverb: parallel feedback combs, ~30-44ms delays with
    # ~0.73-0.80 feedback — a smooth decaying tail instead of discrete
    # early-reflection taps (which read as echo, not reverb)
    COMBS = ((0.0297, 0.773), (0.0371, 0.802), (0.0411, 0.753),
             (0.0437, 0.733))

    def __init__(self, play_device: str = "default", rate: int = 44100,
                 backend: str = "auto"):
        self.play_device = play_device
        self.rate = rate
        self.backend = "pipewire" if backend == "auto" and _pipewire() \
            else backend if backend != "auto" else "alsa"
        self.passthrough = bool(
            self.backend == "pipewire" and shutil.which("pw-loopback"))
        # live-tunable params (plain attrs — read per chunk, no locking needed)
        self.volume = 0.8          # 0.0-2.0 output gain
        self.echo_ms = 120         # delay tap length
        self.echo_feedback = 0.35  # repeat decay 0-0.9
        self.echo_wet = 0.0        # wet/dry mix 0-1 — off until asked for
        self.pitch = 0.0           # semitones, -6..+6 — voice FX
        self.reverb_wet = 0.0      # 0-1 early-reflection mix
        self.autotune = 0.0        # 0-1 correction strength (chart-guided)
        self.polish = False        # gate + compressor + rumble cut chain
        self.muted = False
        self.running = False
        self.error = ""
        self._proc: subprocess.Popen | None = None
        self._delay = None         # numpy ring buffer, lazily sized
        self._combs = None         # lazily-built reverb comb rings
        self._shifter = None       # created when pitch leaves 1.0
        self._q: dict[str, list] = {}   # source device -> queued chunks
        self._qlock = threading.Lock()
        self._loopbacks: dict[str, subprocess.Popen] = {}
        self._lb_names: dict[str, str] = {}
        self._lb_ids: dict[str, int] = {}
        self._lb_since: dict[str, float] = {}   # spawn time per source
        self._lb_check: dict[str, float] = {}   # last link audit per source
        self._restart_at = 0.0                  # throttle playback respawn
        self._sh: dict[str, _PitchShifter] = {}  # per-source pitch shift
        self._tones: dict[str, float] = {}       # detected pitch per mic
        self._targets: dict[str, float] = {}     # chart note per mic
        self._src_fx: dict[str, dict] = {}       # per-mic FX overrides
        self._pstate: dict[str, dict] = {}       # per-source polish state
    FX_KEYS = ("pitch", "autotune", "polish", "echo_wet", "reverb_wet")
    MA_LEN = 512                   # rumble-filter window (~12ms)
    GATE_RMS = 220                 # below this the mic bus fades silent
    COMP_THRESH = 9000             # int16 RMS where compression kicks in
    COMP_RATIO = 3.0
    MAKEUP = 1.35                  # post-compressor gain

    @property
    def pitch_ratio(self) -> float:
        return 2.0 ** (self.pitch / 12.0)

    def _cmd(self) -> list[str]:
        if self.backend == "pipewire":
            # no --target: links to the default sink, follows wpctl
            # set-default; --latency keeps the node buffer small
            return ["pw-cat", "-p", "--raw", "--format", "s16",
                    "--rate", str(self.rate), "--channels", "1",
                    "--latency", "256", "-"]
        return ["aplay", "-D", self.play_device, "-f", "S16_LE",
                "-r", str(self.rate), "-c", "1", "-t", "raw", "-q"]

    def start(self) -> bool:
        if self.running:
            return True
        tool = "pw-cat" if self.backend == "pipewire" else "aplay"
        if not shutil.which(tool):
            self.error = f"{tool} not installed"
            return False
        try:
            self._proc = subprocess.Popen(
                self._cmd(), env=_pw_env(),
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0)
        except OSError as exc:
            self.error = str(exc)
            return False
        self.running = True
        threading.Thread(target=self._writer, daemon=True).start()
        return True

    def set(self, volume=None, echo_ms=None, echo_wet=None, muted=None,
            pitch=None, reverb=None, autotune=None, polish=None):
        if volume is not None:
            self.volume = max(0.0, min(2.0, float(volume)))
        if echo_ms is not None:
            self.echo_ms = max(0, min(600, int(echo_ms)))
            self._delay = None  # resize on next chunk
        if echo_wet is not None:
            self.echo_wet = max(0.0, min(1.0, float(echo_wet)))
        if pitch is not None:
            self.pitch = max(-6.0, min(6.0, float(pitch)))
        if reverb is not None:
            self.reverb_wet = max(0.0, min(1.0, float(reverb)))
        if autotune is not None:
            self.autotune = max(0.0, min(1.0, float(autotune)))
        if polish is not None:
            self.polish = bool(polish)
        if muted is not None:
            self.muted = bool(muted)
        if (volume is not None or muted is not None or pitch is not None
                or autotune is not None or polish is not None):
            for src in list(self._loopbacks):
                self._refresh_loopback(src)

    # -- per-source FX (per-singer profiles) ----------------------------------
    # A source with no entry in _src_fx inherits the global monitor values;
    # the sing session loads each performer's saved FX onto their mic's
    # source when their song starts.

    def _fx_for(self, source) -> dict:
        """Effective FX params for one mic source: global defaults merged
        with that source's overrides."""
        fx = {"pitch": self.pitch, "autotune": self.autotune,
              "polish": self.polish, "echo_wet": self.echo_wet,
              "reverb_wet": self.reverb_wet}
        fx.update(self._src_fx.get(source) or {})
        fx["ratio"] = 2.0 ** (fx["pitch"] / 12.0)
        fx["voice"] = (abs(fx["ratio"] - 1.0) > 1e-3 or fx["autotune"] > 0
                       or fx["polish"])
        return fx

    def _needs_voice_fx(self, source) -> bool:
        """True when this source's dry signal must travel the Python path —
        the hardware loopback would bypass its shift/autotune/polish."""
        return self._fx_for(source)["voice"]

    def set_source_fx(self, source: str, **params) -> None:
        """Overlay per-mic FX onto one source (performer profiles)."""
        fx = self._src_fx.setdefault(source, {})
        for k in self.FX_KEYS:
            if params.get(k) is not None:
                fx[k] = params[k]
        self._refresh_loopback(source)

    def clear_source_fx(self) -> None:
        """Drop all per-source overrides (called on each new song before
        the current singers' profiles are loaded)."""
        self._src_fx.clear()
        self._pstate.clear()
        for src in list(self._loopbacks):
            self._refresh_loopback(src)

    def _refresh_loopback(self, source: str) -> None:
        if self.passthrough:
            threading.Thread(target=self._apply_loopback_state,
                             args=(source,), daemon=True).start()

    def set_targets(self, targets: dict) -> None:
        """Expected chart pitch (semitone) per source device — the sing
        session publishes this every tick for autotune correction."""
        self._targets = dict(targets)

    # -- pw-loopback dry path ------------------------------------------------

    def _loopback_cmd(self, name: str, source: str) -> list[str]:
        # -C links capture to our mic source; playback autoconnects to the
        # default sink so it follows the host's audio-out selection.
        # -l 10: request ~10ms graph latency for the loop.
        return ["pw-loopback", "-n", name, "-C", source,
                "-c", "1", "-l", "10"]

    LINK_CHECK_S = 3.0    # how often feed() audits loopback routing
    LINK_GRACE_S = 6.0    # node + WirePlumber links appear async post-spawn

    def _kill_loopback(self, source: str) -> None:
        proc = self._loopbacks.pop(source, None)
        self._lb_ids.pop(source, None)
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass

    def _ensure_loopback(self, source: str) -> None:
        proc = self._loopbacks.get(source)
        if proc and proc.poll() is None:
            # A live process isn't a live mix — after a PipeWire bounce
            # the output node can end up unlinked (or gone entirely)
            # while capture keeps scoring, so the singer is measured but
            # never heard. Audit occasionally and respawn dead ends so
            # WirePlumber rebuilds the sink link.
            now = time.monotonic()
            if (now - self._lb_check.get(source, 0.0) >= self.LINK_CHECK_S
                    and now - self._lb_since.get(source, 0.0)
                    > self.LINK_GRACE_S):
                self._lb_check[source] = now
                from . import audio as pw
                name = self._lb_names.get(source)
                nid = pw.node_id(f"output.{name}") if name else None
                if nid is None:
                    self._kill_loopback(source)      # node vanished
                else:
                    self._lb_ids[source] = nid       # ids churn on relaunch
                    if pw.node_routed(f"output.{name}") is False:
                        self._kill_loopback(source)  # routed nowhere
            return
        name = f"ups-mon-{abs(hash(source)) & 0xffff:x}"
        try:
            proc = subprocess.Popen(
                self._loopback_cmd(name, source), env=_pw_env(),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.error = str(exc)
            self.passthrough = False     # fall back to Python dry path
            return
        self._loopbacks[source] = proc
        self._lb_names[source] = name
        self._lb_ids.pop(source, None)   # new node id pending lookup
        self._lb_since[source] = time.monotonic()
        threading.Thread(target=self._apply_loopback_state,
                         args=(source,), daemon=True).start()

    def _apply_loopback_state(self, source: str) -> None:
        """Find the loopback's playback node and push volume/mute onto it.

        pw-loopback splits each instance into ``input.<name>`` (capture
        side) and ``output.<name>`` (playback side) — stream volume/mute
        live on the output node.
        """
        from . import audio as pw
        name = self._lb_names.get(source)
        nid = self._lb_ids.get(source)
        if nid is None and name:
            for _ in range(20):          # node appears asynchronously
                nid = pw.node_id(f"output.{name}")
                if nid is not None:
                    break
                time.sleep(0.1)
        if nid is None:
            return
        self._lb_ids[source] = nid
        pw.set_node_volume(nid, self.volume)
        # voice FX routes the dry voice through Python instead — the
        # loopback must not leak the unprocessed signal over it
        pw.set_node_mute(nid, self.muted or self._needs_voice_fx(source))

    def _ring(self, n_samples: int):
        import numpy as np
        need = int(self.rate * self.echo_ms / 1000)
        if self._delay is None or len(self._delay) != need + n_samples:
            self._delay = np.zeros(need + n_samples, dtype=np.int16)
        return self._delay

    def feed(self, source: str, chunk: bytes, tone=None) -> None:
        """Queue one PCM hop for DSP (non-blocking, drops on lag).

        ``tone`` is the source mic's latest detected pitch (semitone) —
        the autotune chain corrects against it; None while unvoiced."""
        if tone is not None:
            self._tones[source] = float(tone)
        else:
            self._tones.pop(source, None)
        if not self.running:
            # pw-cat -p died under us (a PipeWire bounce kills every
            # client) — _write just marked running=False and nothing else
            # restarts it, so the wet/FX path would stay silent while
            # scoring continues on its own capture stream. Respawn is
            # throttled: the daemon may still be coming back.
            if self._proc is not None:
                now = time.monotonic()
                if now - self._restart_at > 5:
                    self._restart_at = now
                    try:
                        self._proc.terminate()
                    except Exception:
                        pass
                    self._proc = None
                    self.start()
            return
        if self.passthrough:
            self._ensure_loopback(source)
        with self._qlock:
            q = self._q.setdefault(source, [])
            if len(q) < self.MAX_QUEUED:
                q.append(chunk)

    def _writer(self) -> None:
        while self.running:
            pairs = []
            with self._qlock:
                for src, q in self._q.items():
                    if q:
                        pairs.append((src, q.pop(0)))
            if not pairs:
                time.sleep(0.005)
                continue
            self._write(pairs)

    def _reverb(self, feed):
        """Schroeder comb bank: y[n] = x[n] + g·y[n-D] per comb, summed.

        Each comb's ring keeps its last D output samples; the recurrence
        is evaluated block-wise on [history | feed] so it stays vectorized
        (each D-block only needs the finalized block before it)."""
        import numpy as np
        n = len(feed)
        if self._combs is None:
            self._combs = [np.zeros(int(self.rate * ms), np.float32)
                           for ms, _g in self.COMBS]
        out = np.zeros(n, dtype=np.float32)
        for (ms, g), ring in zip(self.COMBS, self._combs):
            D = len(ring)
            buf = np.empty(D + n, np.float32)
            buf[:D] = ring
            buf[D:] = feed
            for off in range(D, D + n, D):
                m = min(D, D + n - off)
                buf[off:off + m] += g * buf[off - D:off - D + m]
            y = buf[D:]
            out += y
            if n >= D:
                ring[:] = y[-D:]
            else:
                ring[:D - n] = ring[n:]
                ring[D - n:] = y
        return out * 0.3          # comb sum is hot — keep headroom

    def _polish(self, x):
        """Polish chain on the shared/default state (tests, legacy callers)."""
        return self._polish_src(None, x)

    def _polish_src(self, name, x):
        """Rumble cut → noise gate → compressor/limiter on one source —
        state is per-source so duets polish independently."""
        import numpy as np
        st = self._pstate.setdefault(name, {"env": 1.0, "lp": None})
        n = len(x)
        # low-cut: subtract a long *centered* moving average — zero-phase,
        # so the response is 1-MAgain(f): ~0 below ~30Hz rumble, -4dB at
        # ~80Hz, unity through the vocal band. Costs a 256-sample delay.
        if st["lp"] is None:
            st["lp"] = np.zeros(self.MA_LEN, dtype=np.float32)
        hist = np.concatenate([st["lp"], x])
        cs = np.cumsum(np.concatenate(([0.0], hist)))
        x = (hist[self.MA_LEN // 2: self.MA_LEN // 2 + n]
             - (cs[self.MA_LEN:self.MA_LEN + n] - cs[:n]) / self.MA_LEN)
        st["lp"] = hist[-self.MA_LEN:]
        rms = float(np.sqrt(np.mean(x * x) + 1e-6))
        if rms < self.GATE_RMS:
            target = 0.0                       # room noise / TV bleed
        elif rms <= self.COMP_THRESH:
            target = self.MAKEUP
        else:
            target = self.MAKEUP * (
                self.COMP_THRESH / rms) ** (1.0 - 1.0 / self.COMP_RATIO)
        # fast open when singing starts, slow release for natural decay
        st["env"] += (target - st["env"]) * (
            0.5 if target > st["env"] else 0.08)
        return x * st["env"]

    def _write(self, pairs) -> None:
        proc = self._proc
        if not proc or proc.poll() is not None or proc.stdin is None:
            self.running = False
            return
        # accept legacy raw-chunk calls alongside (source, chunk) pairs
        pairs = [(None, c) if isinstance(c, (bytes, bytearray)) else c
                 for c in pairs]
        fxs = {s: self._fx_for(s) for s, _c in pairs}
        any_voice = any(f["voice"] for f in fxs.values())
        any_echo = any(f["echo_wet"] > 0 for f in fxs.values())
        any_rev = any(f["reverb_wet"] > 0 for f in fxs.values())
        dsp_needed = any_voice or any_echo or any_rev
        # pw-loopback carries the dry voice — when no DSP is needed there
        # is nothing for this stream to play; skipping also drains any
        # tail that was buffered while an effect was on
        if self.passthrough and not dsp_needed:
            return
        try:
            import numpy as np
            n_max = max(len(c) for _s, c in pairs) // BYTES_PER_SAMPLE
            dry_py = np.zeros(n_max, dtype=np.float32)  # FX'd sources only
            dry_all = np.zeros(n_max, dtype=np.float32)
            echo_feed = np.zeros(n_max, dtype=np.float32)
            rev_feed = np.zeros(n_max, dtype=np.float32)
            for src_name, c in pairs:
                fx = fxs[src_name]
                s = np.frombuffer(c, dtype=np.int16).astype(np.float32)
                ratio = fx["ratio"]
                if fx["autotune"] > 0:
                    det = self._tones.get(src_name)
                    tgt = self._targets.get(src_name)
                    if det is not None and det >= 0:
                        want = tgt if tgt is not None else round(det)
                        corr = max(-12.0, min(12.0,
                                   (want - det) * fx["autotune"]))
                        ratio *= 2.0 ** (corr / 12.0)
                if abs(ratio - 1.0) > 1e-3:
                    sh = self._sh.get(src_name)
                    if sh is None:
                        sh = self._sh[src_name] = _PitchShifter(self.rate)
                    s = sh.process(s, ratio)
                elif src_name in self._sh:
                    del self._sh[src_name]
                if fx["polish"]:
                    s = self._polish_src(src_name, s)
                k = min(len(s), n_max)   # shifted grains may run longer
                dry_all[:k] += s[:k]
                if fx["voice"]:
                    dry_py[:k] += s[:k]
                if fx["echo_wet"]:
                    echo_feed[:k] += s[:k] * fx["echo_wet"]
                if fx["reverb_wet"]:
                    rev_feed[:k] += s[:k] * fx["reverb_wet"]
            n = n_max
            # passthrough: each source's pw-loopback carries its dry voice
            # — except sources with voice FX, whose loopbacks are muted so
            # Python must carry them. Fallback mode sends it all.
            src = (dry_py if self.passthrough else dry_all) * self.volume
            wet = np.zeros(n, dtype=np.float32)
            # 'enabled' (not 'feed nonzero') gates the ring — a silent
            # chunk must still shift the ring so tails decay naturally
            if self.echo_ms > 0 and any_echo:
                delay = self._ring(n)
                wet = delay[:n].astype(np.float32)
                # shift buffer: delayed signal = wet feed + feedback echo
                delay[:len(delay) - n] = delay[n:]
                delay[-n:] = np.clip(
                    echo_feed * self.volume + wet * self.echo_feedback,
                    -32768, 32767)
            rev = (self._reverb(rev_feed * self.volume) if any_rev
                   else np.zeros(n, dtype=np.float32))
            out = src + wet + rev
            if self.muted:
                out[:] = 0
            proc.stdin.write(np.clip(out, -32768, 32767)
                             .astype(np.int16).tobytes())
        except (BrokenPipeError, OSError) as exc:
            self.error = str(exc)
            self.running = False
        except ImportError:
            try:
                proc.stdin.write(
                    b"" if self.muted
                    else max((c for _s, c in pairs), key=len))
            except (BrokenPipeError, OSError):
                self.running = False

    def stop(self) -> None:
        self.running = False
        for proc in self._loopbacks.values():
            try:
                proc.terminate()
            except Exception:
                pass
        self._loopbacks.clear()
        self._lb_ids.clear()
        self._lb_since.clear()
        self._lb_check.clear()
        proc = self._proc
        self._proc = None
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


class MicManager:
    """Every detected capture device + host's mic-to-singer assignments.

    All discovered mics run their own ``arecord`` (each feeds the shared
    ``MicMonitor`` so singers are audible regardless). Host assignments map
    ``device -> performer_id`` so a duet scores each track from the right
    mic; unassigned tracks fall back to any free running capture.
    """

    def __init__(self, monitor: MicMonitor, rate: int = 44100):
        self.monitor = monitor
        self.rate = rate
        self.captures: dict[str, MicCapture] = {}
        self.names: dict[str, str] = {}
        self.assignments: dict[str, int] = {}   # device -> performer_id
        self.on_change = None                   # optional persistence hook

    def discover(self) -> list[dict]:
        """Refresh device list and start capture on every found device."""
        found = MicCapture.list_devices()
        for d in found:
            self.names[d["id"]] = d["name"]
            cap = self.ensure(d["id"])
            if cap.start().available:
                self.monitor.start()
        return found

    # -- supervision ----------------------------------------------------------
    # pw clients die with the PipeWire daemon (bounce, crash, device
    # replug). Once the last capture exits, feed() stops flowing and the
    # monitor's own self-heal never runs — scoring AND speaker monitoring
    # stay dead until the next service restart. A light supervisor
    # restarts dead pieces; per-device backoff avoids churn while the
    # daemon or the hardware is still down.

    SUPERVISE_TICK_S = 5
    SUPERVISE_BACKOFF_S = 10

    def start_supervisor(self) -> None:
        if getattr(self, "_sv", False):
            return
        self._sv = True
        threading.Thread(target=self._supervise, daemon=True,
                         name="mic-supervisor").start()

    def _supervise(self) -> None:
        backoff: dict[str, float] = {}
        while True:
            time.sleep(self.SUPERVISE_TICK_S)
            now = time.monotonic()
            for dev, cap in list(self.captures.items()):
                if cap.running or now - backoff.get(dev, 0) < (
                        self.SUPERVISE_BACKOFF_S):
                    continue
                backoff[dev] = now
                try:
                    cap.start()
                except Exception:
                    pass
            if not self.monitor.running:
                try:
                    self.monitor.start()
                except Exception:
                    pass

    def ensure(self, device: str) -> MicCapture:
        cap = self.captures.get(device)
        if cap is None:
            cap = MicCapture(device, self.rate)
            cap.monitor = self.monitor
            self.captures[device] = cap
            self.names.setdefault(device, device)
        return cap

    def assign(self, device: str, performer_id: int | None) -> None:
        if performer_id is None:
            self.assignments.pop(device, None)
        else:
            self.assignments[device] = int(performer_id)
        if self.on_change:
            self.on_change()

    def device_for(self, performer_id: int | None) -> str | None:
        if performer_id is None:
            return None
        for dev, pid in self.assignments.items():
            if pid == performer_id:
                return dev
        return None

    def resolve(self, track_performers: dict) -> dict:
        """Map {track: performer_id} -> {track: MicCapture}.

        Assigned mics win; unassigned tracks take the first running capture
        not already claimed by another track (single-mic parties degrade to
        the shared-stream behavior from before).
        """
        out, used = {}, set()
        for track, pid in track_performers.items():
            dev = self.device_for(pid)
            cap = self.captures.get(dev) if dev else None
            if cap and cap.running:
                out[track] = cap
                used.add(dev)
        for track in track_performers:
            if track in out:
                continue
            dev = next((d for d, c in self.captures.items()
                        if c.running and d not in used), None) or next(
                (d for d, c in self.captures.items() if c.running), None)
            if dev:
                out[track] = self.captures[dev]
                used.add(dev)
        return out

    def status(self) -> list[dict]:
        return [{
            "device": dev, "name": self.names.get(dev, dev),
            "running": cap.running, "level": round(cap.level, 3),
            "assigned_to": self.assignments.get(dev),
        } for dev, cap in self.captures.items()]

    # -- latency calibration --------------------------------------------------
    # Loud clicks out through the sink, timed coming back through the mic.
    # The measured speaker->mic loop delay is subtracted from the media
    # position at scoring time so pitch is judged against the music the
    # singer was actually hearing (UltraStar/USDX do the same via their
    # per-device Latency config).

    @staticmethod
    def _find_peaks(corr, n: int, min_sep: int) -> list[int]:
        """Strongest correlation peaks, greedily separated by min_sep.

        Threshold: mean + 6σ — real clicks stand far above noise even at
        very low mic levels, so this survives weak room pickup where an
        energy threshold would never fire.
        """
        import numpy as np
        if not len(corr):
            return []
        thr = float(corr.mean() + 6 * corr.std())
        peaks = []
        for i in np.argsort(corr)[::-1]:
            if corr[i] < thr or len(peaks) == n:
                break
            if all(abs(int(i) - p) > min_sep for p in peaks):
                peaks.append(int(i))
        return sorted(peaks)

    def calibrate(self, n_clicks: int = 4, gap_ms: int = 350,
                  lead_ms: int = 500) -> dict:
        """Play clicks through the output; matched-filter each capture for
        them and measure the loop delay. Blocking ~2.5s — API endpoint."""
        if not _pipewire():
            return {"ok": False, "error": "calibration needs PipeWire"}
        caps = [(d, c) for d, c in self.captures.items() if c.running]
        if not caps:
            return {"ok": False, "error": "no running mics"}
        import numpy as np
        sr = self.rate
        click = (np.sin(2 * np.pi * 1000 *
                        np.arange(int(sr * 0.03)) / sr)
                 * 32767 * 0.9).astype(np.int16)
        parts, offsets, pos = [np.zeros(int(sr * lead_ms / 1000), np.int16)], [], 0
        pos = len(parts[0])
        gap = np.zeros(int(sr * gap_ms / 1000), np.int16)
        for _ in range(n_clicks):
            offsets.append(pos)
            parts += [click, gap]
            pos += len(click) + len(gap)
        buf = np.concatenate(parts).tobytes()
        for _, cap in caps:
            cap.calib_start()
        try:
            proc = subprocess.Popen(
                ["pw-cat", "-p", "--raw", "--format", "s16",
                 "--rate", str(sr), "--channels", "1",
                 "--latency", "256", "-"],
                env=_pw_env(), stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            t0 = time.monotonic()          # stream start ~= first drain
            proc.stdin.write(buf)
            proc.stdin.close()
        except OSError as exc:
            for _, cap in caps:
                cap._calib = None
            return {"ok": False, "error": str(exc)}
        emits = [t0 + off / sr for off in offsets]
        deadline = time.monotonic() + len(buf) / (2 * sr) + 0.6
        while time.monotonic() < deadline:
            time.sleep(0.05)
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()
        click_f = click.astype(np.float32)
        per_dev, delays = {}, []
        for dev, cap in caps:
            chunks = (cap._calib or {}).get("chunks", [])
            cap._calib = None
            if not chunks:
                continue
            # rebuild the capture timeline: last chunk's end time anchors
            # the whole buffer (chunks arrived contiguously)
            total = sum(len(c) for _, c in chunks) // BYTES_PER_SAMPLE
            t_end = chunks[-1][0]
            pcm = np.frombuffer(b"".join(c for _, c in chunks),
                                dtype=np.int16).astype(np.float32)
            if len(pcm) != total:
                continue
            corr = np.correlate(pcm[:total], click_f, "valid")
            peaks = self._find_peaks(corr, n_clicks, int(0.2 * sr))
            onset_t = [t_end - (total - i) / sr for i in peaks]
            ds = []
            for e in emits:
                hit = next((t for t in onset_t
                            if e <= t <= e + gap_ms / 1000), None)
                if hit is not None:
                    ds.append(hit - e)
            ds.sort()
            if ds:
                ms = ds[len(ds) // 2] * 1000
                per_dev[dev] = round(ms, 1)
                delays.append(ms)
        if not delays:
            return {"ok": False,
                    "error": "no clicks heard — is the volume up?"}
        delays.sort()
        return {"ok": True,
                "mic_delay_ms": round(delays[len(delays) // 2], 1),
                "per_device": per_dev}
