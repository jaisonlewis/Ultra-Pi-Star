"""Mic assignment/resolution + PipeWire status parsing."""
from ultrapistar.audio import _parse_status
from ultrapistar.mic import MicManager, MicMonitor


class FakeCapture:
    def __init__(self, running=True):
        self.running = running
        self.level = 0.0


def _manager(*devices):
    m = MicManager(MicMonitor())
    for d in devices:
        m.captures[d] = FakeCapture()
        m.names[d] = d
    return m


def test_resolve_assigned_devices():
    m = _manager("hw:2,0", "hw:3,0")
    m.assign("hw:2,0", 10)   # Logitech -> performer 10
    m.assign("hw:3,0", 20)   # PRO X -> performer 20
    out = m.resolve({"P1": 10, "P2": 20})
    assert out["P1"] is m.captures["hw:2,0"]
    assert out["P2"] is m.captures["hw:3,0"]


def test_resolve_unassigned_falls_back_to_free_capture():
    m = _manager("hw:2,0", "hw:3,0")
    m.assign("hw:2,0", 10)
    out = m.resolve({"P1": 10, "P2": 30})   # performer 30 has no assignment
    assert out["P1"] is m.captures["hw:2,0"]
    assert out["P2"] is m.captures["hw:3,0"]   # first free capture


def test_resolve_single_mic_shares_capture():
    m = _manager("hw:2,0")
    out = m.resolve({"P1": 10, "P2": 20})
    assert out["P1"] is out["P2"] is m.captures["hw:2,0"]


def test_unassign():
    m = _manager("hw:2,0")
    m.assign("hw:2,0", 10)
    m.assign("hw:2,0", None)
    assert m.device_for(10) is None


def test_parse_pw_dump_nodes():
    import json
    from ultrapistar.audio import _parse_pw_dump
    dump = json.dumps([
        {"id": 35, "type": "PipeWire:Interface:Node", "info": {"props": {
            "node.name": "alsa_input.usb-Logitech_Logitech_USB_Microphone-00.mono-fallback",
            "node.description": "Logitech USB Microphone Mono",
            "media.class": "Audio/Source"}}},
        {"id": 46, "type": "PipeWire:Interface:Node", "info": {"props": {
            "node.name": "alsa_output.platform-107c701400.hdmi.pro-output-0",
            "media.class": "Audio/Sink"}}},
        {"id": 48, "type": "PipeWire:Interface:Device", "info": {"props": {
            "device.name": "alsa_card.usb"}}},
    ])
    nodes = _parse_pw_dump(dump)
    assert len(nodes) == 2          # devices filtered out
    assert nodes[0]["name"].startswith("alsa_input.")
    assert nodes[0]["desc"] == "Logitech USB Microphone Mono"
    assert nodes[1]["media_class"] == "Audio/Sink"
    assert _parse_pw_dump("") == []
    assert _parse_pw_dump("not json") == []


def test_parse_wpctl_status():
    text = """Audio
 ├─ Devices:
 │      48. Logitech G PRO X Gaming Headset     [alsa]
 │      50. Built-in Audio                      [alsa]
 ├─ Sinks:
 │  *   58. Logitech G PRO X Gaming Headset Analog Stereo [vol: 0.40]
 │      61. Built-in Audio Pro                  [vol: 1.00]
 ├─ Sources:
 │      35. Logitech USB Microphone Mono        [vol: 1.00]
"""
    s = _parse_status(text)
    assert (48, "Logitech G PRO X Gaming Headset", False) in s["Devices"]
    assert (58, "Logitech G PRO X Gaming Headset Analog Stereo", True) in s["Sinks"]
    assert (61, "Built-in Audio Pro", False) in s["Sinks"]
    assert s["Sources"][0] == (35, "Logitech USB Microphone Mono", False)


def test_mic_assign_endpoint(client, host_client, app):
    micman = app.extensions["ups"].get("micman")
    if micman is None:
        import pytest
        pytest.skip("no mic manager in test env")
    micman.captures["hw:9,0"] = FakeCapture()
    micman.names["hw:9,0"] = "Test Mic"
    host_client.post("/api/join", json={"name": "Alex"})
    pid = host_client.get("/api/me").get_json()["performer"]["id"]
    res = host_client.post("/api/mic/assign",
                           json={"device": "hw:9,0", "performer_id": pid})
    assert res.status_code == 200
    assert micman.device_for(pid) == "hw:9,0"
    # guests can't assign (fresh client — host_client shares its session)
    guest = client.application.test_client()
    assert guest.post("/api/mic/assign",
                      json={"device": "hw:9,0"}).status_code == 403


class _FakeStdin:
    def __init__(self):
        self.buf = b""

    def write(self, b):
        self.buf += b


class _FakeProc:
    def __init__(self):
        self.stdin = _FakeStdin()

    def poll(self):
        return None


def _monitor_with_proc(passthrough):
    m = MicMonitor()
    m.passthrough = passthrough
    m._proc = _FakeProc()
    m.running = True
    return m


def test_monitor_passthrough_writes_wet_only():
    """pw-loopback carries dry voice — Python must only write the echo
    tail, never the dry signal. With echo off it writes nothing at all
    (the wet stream would just emit buffered silence/noise)."""
    m = _monitor_with_proc(passthrough=True)
    m.echo_wet = 0                      # echo off -> nothing wet to send
    chunk = b"\x01\x40" * 2048          # non-zero PCM
    m._write([chunk])
    assert m._proc.stdin.buf == b""       # dry did not leak through
    m.echo_wet = 0.4                    # echo on -> only the wet tail goes out
    m._write([chunk])
    assert len(m._proc.stdin.buf) == len(chunk)
    assert set(m._proc.stdin.buf) == {0}  # first tail: ring still empty


def test_monitor_fallback_writes_dry():
    """Without pw-loopback the Python path must carry the dry signal."""
    m = _monitor_with_proc(passthrough=False)
    m.echo_wet = 0
    chunk = b"\x01\x40" * 2048
    m._write([chunk])
    assert len(m._proc.stdin.buf) == len(chunk)
    assert set(m._proc.stdin.buf) != {0}   # dry signal present


def test_feed_spawns_loopback_in_passthrough(monkeypatch):
    import ultrapistar.mic as mic_mod
    spawned = []

    class _P:
        def poll(self):
            return None

    monkeypatch.setattr(mic_mod.subprocess, "Popen",
                        lambda cmd, **kw: spawned.append(cmd) or _P())
    monkeypatch.setattr(mic_mod.threading, "Thread",
                        lambda *a, **kw: type("T", (), {"start": lambda s: None})())
    m = _monitor_with_proc(passthrough=True)
    m.feed("alsa_input.test-mic", b"\x00" * 4096)
    cmd = next(c for c in spawned if c[0] == "pw-loopback")
    assert "alsa_input.test-mic" in cmd and "-l" in cmd


def test_calibrate_endpoint_auth_and_persistence(client, host_client, app):
    """Calibration is host-or-singer gated and stores the measured delay."""
    ctx = app.extensions["ups"]

    class FakeMicMan:
        def calibrate(self):
            return {"ok": True, "mic_delay_ms": 87.0,
                    "per_device": {"hw:2,0": 87.0}}

        def status(self):
            return []

    ctx["micman"] = FakeMicMan()
    guest = client.application.test_client()
    assert guest.post("/api/mic/calibrate").status_code == 403

    res = host_client.post("/api/mic/calibrate")
    assert res.status_code == 200
    assert ctx["mic_delay_ms"] == 87.0
    assert ctx["db"].get_setting("mic_delay_ms") == "87.0"
    assert client.get("/api/mic").get_json()["mic_delay_ms"] == 87.0


def test_calibrate_error_propagates(client, host_client, app):
    ctx = app.extensions["ups"]

    class DeafMicMan:
        def calibrate(self):
            return {"ok": False, "error": "no clicks heard"}

    ctx["micman"] = DeafMicMan()
    res = host_client.post("/api/mic/calibrate")
    assert res.status_code == 502
    assert "no clicks heard" in res.get_json()["error"]
    assert ctx.get("mic_delay_ms", 0) == 0   # unchanged on failure


# -- monitor FX: pitch shift + reverb -----------------------------------

def test_pitch_shifter_preserves_duration_and_shifts():
    import numpy as np
    from ultrapistar.mic import _PitchShifter
    rate = 44100
    sh = _PitchShifter(rate)
    t = np.arange(rate // 2) / rate                 # 0.5s of 220 Hz
    src = (np.sin(2 * np.pi * 220 * t) * 8000).astype(np.float32)
    out = sh.process(src, 2.0)                       # octave up
    # duration preserved to within a couple of grains
    assert abs(len(out) - len(src)) <= sh.G * 2
    # zero-crossing rate roughly doubles -> pitch actually moved
    def zcr(x):
        return float(np.sum(x[:-1] * x[1:] < 0)) / max(1, len(x))
    mid = out[len(out) // 4: 3 * len(out) // 4]
    assert zcr(mid) > zcr(src) * 1.5
    deep = sh.process(src, 0.5)
    dm = deep[len(deep) // 4: 3 * len(deep) // 4]
    assert zcr(dm) < zcr(src) * 0.8


def test_monitor_passthrough_pitch_carries_dry():
    """While pitch FX is engaged the loopback is muted — Python must
    write the shifted dry signal, not just tails."""
    m = _monitor_with_proc(passthrough=True)
    m.echo_wet = 0
    m.set(pitch=3)
    chunk = b"\x01\x40" * 2048
    for _ in range(6):                  # fill the shifter's first grains
        m._write([chunk])
    assert len(m._proc.stdin.buf) > 0


def test_monitor_reverb_tail():
    """Feedforward taps ring for tens of ms after the input stops."""
    m = _monitor_with_proc(passthrough=False)
    m.set(reverb=0.5)
    m._write([b"\x01\x40" * 2048])       # an impulse-ish chunk
    before = len(m._proc.stdin.buf)
    m._write([b"\x00" * 4096])           # silence — only the tail plays
    tail = m._proc.stdin.buf[before:]
    assert any(b != 0 for b in tail)


def test_monitor_reverb_is_sustained_decay_not_echo():
    """Comb reverb rings for many chunks after input stops — a smooth
    sustained decay, not a single discrete slap."""
    import numpy as np
    m = _monitor_with_proc(passthrough=False)
    m.reverb_wet = 0.7
    m._write([("mic", _sine(amp=12000))])
    rms = []
    for _ in range(8):                    # ~1.6s of silence after the hit
        m._proc.stdin.buf = b""
        m._write([("mic", b"\x00" * 8820)])
        tail = np.frombuffer(m._proc.stdin.buf, dtype=np.int16)
        rms.append(float(np.sqrt(np.mean(tail.astype(np.float64) ** 2))))
    assert all(r > 1 for r in rms[:5])    # sustained, not one reflection
    assert rms[-1] < rms[0]               # and it decays


def test_monitor_fx_clamps():
    m = _monitor_with_proc(passthrough=False)
    m.set(pitch=99, reverb=2)
    assert m.pitch == 6 and m.reverb_wet == 1
    m.set(pitch=-99, reverb=-1)
    assert m.pitch == -6 and m.reverb_wet == 0


def test_mic_fx_endpoint(client, host_client, app):
    import pytest
    mon = app.extensions["ups"].get("monitor")
    if mon is None:
        pytest.skip("no monitor in test env")
    res = host_client.post("/api/mic", json={"pitch": 3, "reverb": 0.4})
    assert res.status_code == 200
    assert mon.pitch == 3 and abs(mon.reverb_wet - 0.4) < 1e-6
    assert client.get("/api/mic").get_json()["pitch"] == 3


# -- monitor FX: autotune + studio polish --------------------------------

def _sine(freq=220.0, n=4410, rate=44100, amp=8000):
    import numpy as np
    t = np.arange(n) / rate
    return (np.sin(2 * np.pi * freq * t) * amp).astype("<i2").tobytes()


def _zcr(arr):
    import numpy as np
    x = np.asarray(arr)
    return float(np.sum(x[:-1] * x[1:] < 0)) / max(1, len(x))


def test_monitor_autotune_corrects_toward_chart_note():
    """Detected 33.5st + chart target 33 -> the sung voice shifts DOWN."""
    import numpy as np
    m = _monitor_with_proc(passthrough=False)
    m.autotune = 1.0
    m._tones["mic"] = 33.5
    m._targets["mic"] = 33
    chunk = _sine()
    for _ in range(8):
        m._write([("mic", chunk)])
    assert "mic" in m._sh                    # per-source shifter engaged
    out = np.frombuffer(m._proc.stdin.buf, dtype=np.int16)
    ref = np.frombuffer(chunk, dtype=np.int16)
    assert _zcr(out[len(out) // 2:]) < _zcr(ref)   # pitch moved down


def test_monitor_autotune_snaps_nearest_semitone_without_chart():
    """No chart target -> round() snaps to the nearest semitone."""
    m = _monitor_with_proc(passthrough=False)
    m.autotune = 1.0
    m._tones["mic"] = 33.0          # already on pitch -> ratio 1, no shift
    for _ in range(4):
        m._write([("mic", _sine())])
    assert m._sh == {}
    m._tones["mic"] = 33.4          # flat -> snap down toward 33
    m._write([("mic", _sine())])
    assert "mic" in m._sh


def test_monitor_autotune_unvoiced_is_untouched():
    """No detected tone (between phrases) -> no correction applied."""
    m = _monitor_with_proc(passthrough=False)
    m.autotune = 1.0
    m.pitch = 0
    for _ in range(4):
        m._write([("mic", _sine())])
    assert m._sh == {}


def test_polish_gate_compressor_and_boost():
    import numpy as np
    m = MicMonitor()
    m.polish = True
    # gate: a quiet room-noise-level signal decays to silence
    quiet = np.random.RandomState(1).randn(2048).astype(np.float32) * 150
    for _ in range(70):
        out = m._polish(quiet.copy())
    assert np.abs(out).max() < 15
    # compressor: a loud signal is pulled under its raw peak
    t = np.arange(4096) / 44100
    loud = np.sin(2 * np.pi * 440 * t).astype(np.float32) * 25000
    for _ in range(20):
        out = m._polish(loud.copy())
    assert np.abs(out).max() < 25000 * 0.95
    # makeup: a mid-level voice gets lifted
    mid = np.sin(2 * np.pi * 440 * t).astype(np.float32) * 2000
    for _ in range(20):
        out = m._polish(mid.copy())
    assert np.abs(out).max() > 2000 * 1.1


def test_polish_and_autotune_clamps_and_routing():
    m = _monitor_with_proc(passthrough=True)
    m.set(autotune=2, polish=1)
    assert m.autotune == 1 and m.polish is True
    # polish alone forces the dry voice through the Python path —
    # pw-loopback would bypass the compressor/gate
    chunk = _sine()
    for _ in range(4):
        m._write([("mic", chunk)])
    assert len(m._proc.stdin.buf) > 0


def test_mic_polish_autotune_endpoint(client, host_client, app):
    import pytest
    mon = app.extensions["ups"].get("monitor")
    if mon is None:
        pytest.skip("no monitor in test env")
    res = host_client.post("/api/mic", json={"autotune": 0.7, "polish": True})
    assert res.status_code == 200
    assert mon.autotune == 0.7 and mon.polish is True
    s = client.get("/api/mic").get_json()
    assert s["autotune"] == 0.7 and s["polish"] is True


# -- per-singer FX profiles ------------------------------------------------

def test_source_fx_overrides_only_that_source():
    """Per-mic overrides change routing for that source only — duet
    partners keep the global defaults."""
    m = _monitor_with_proc(passthrough=True)
    assert not m._needs_voice_fx("micA")
    m.set_source_fx("micA", pitch=-4)
    assert m._needs_voice_fx("micA") and not m._needs_voice_fx("micB")
    fx = m._fx_for("micA")
    assert fx["pitch"] == -4 and fx["reverb_wet"] == m.reverb_wet


def test_source_fx_independent_duet_chains():
    """Each mic gets its own shifter — A deep, B squeaky, independently."""
    m = _monitor_with_proc(passthrough=True)
    m.set_source_fx("micA", pitch=-4)
    m.set_source_fx("micB", pitch=4)
    chunk = _sine()
    for _ in range(6):
        m._write([("micA", chunk), ("micB", chunk)])
    assert "micA" in m._sh and "micB" in m._sh
    m.clear_source_fx()
    assert m._src_fx == {} and not m._needs_voice_fx("micA")


def test_performer_fx_roundtrip(db):
    pid = db.add_performer("Aria")["id"]
    assert db.performer_fx(pid) == {}
    db.set_performer_fx(pid, {"pitch": -2, "autotune": 0.6, "polish": True})
    fx = db.performer_fx(pid)
    assert fx["pitch"] == -2 and fx["autotune"] == 0.6 and fx["polish"] is True
    db.set_performer_fx(pid, {"pitch": -2})   # overwrite = the new profile
    assert db.performer_fx(pid) == {"pitch": -2}


def test_mic_post_saves_to_caller_performer(client, app):
    """An identified singer's FX posts persist to their profile and apply
    to the mic serving them — not to the room defaults."""
    ctx = app.extensions["ups"]
    mon = MicMonitor()
    ctx["monitor"] = mon

    class _MM:
        def device_for(self, pid):
            return "micA" if pid else None

        def status(self):
            return []

    ctx["micman"] = _MM()
    pid = ctx["db"].add_performer("Bea")["id"]
    with client.session_transaction() as s:
        s["is_host"] = True
        s["performer_id"] = pid
    res = client.post("/api/mic", json={"pitch": -3, "autotune": 0.5})
    assert res.status_code == 200
    prof = ctx["db"].performer_fx(pid)
    assert prof["pitch"] == -3 and prof["autotune"] == 0.5
    assert mon._src_fx["micA"]["pitch"] == -3
    assert mon.pitch == 0                 # room default untouched
    # their saved profile comes back on GET for their own sliders
    mine = client.get("/api/mic").get_json()["my_fx"]
    assert mine["pitch"] == -3


def test_mic_post_without_identity_sets_room_default(client, app):
    """A host session with no performer identity still sets the room
    defaults (pre-profile behavior)."""
    ctx = app.extensions["ups"]
    mon = MicMonitor()
    ctx["monitor"] = mon
    with client.session_transaction() as s:
        s["is_host"] = True
    res = client.post("/api/mic", json={"pitch": 2})
    assert res.status_code == 200
    assert mon.pitch == 2 and mon._src_fx == {}
    assert client.get("/api/mic").get_json()["my_fx"] is None


def test_karaoke_mix_rescues_bass_and_bleeds_guide():
    """Vocal stem = voice + leaked kick. The backing must return the
    kick at full level and keep ~guide fraction of the voice."""
    import numpy as np
    from ultrapistar import separate
    sr = separate.SR
    t = np.arange(sr) / sr
    voice = (np.sin(2 * np.pi * 1000 * t) * 0.3)[None, :].repeat(2, 0)
    kick = (np.sin(2 * np.pi * 90 * t) * 0.4)[None, :].repeat(2, 0)
    music = (np.sin(2 * np.pi * 440 * t) * 0.2)[None, :].repeat(2, 0)
    vocals, accomp = voice + kick, music.copy()

    def amp(sig, hz):
        return float(abs((sig * np.exp(-2j * np.pi * hz * t))
                         .sum(-1)).mean()) * 2 / sig.shape[-1]

    mix = separate.karaoke_mix(vocals, accomp, guide=0.2)
    assert abs(amp(mix, 90) - 0.4) < 0.05       # kick rescued wholesale
    assert abs(amp(mix, 1000) - 0.06) < 0.03    # voice at ~guide level
    assert abs(amp(mix, 440) - 0.2) < 0.03      # music untouched

    mix0 = separate.karaoke_mix(vocals, accomp, guide=0.0)
    assert amp(mix0, 1000) < 0.03               # voice gone at guide=0
    assert abs(amp(mix0, 90) - 0.4) < 0.05      # bass still rescued


def test_node_routed_true_when_linked(monkeypatch):
    """A loopback whose port shows a |-> link is routed."""
    from ultrapistar import audio
    out = (
        "output.ups-mon-a0c9:output_FL\n"
        "  |-> alsa_output.hdmi.playback.0.0:playback_FL\n"
        "output.ups-mon-a0c9:output_FR\n"
        "  |-> alsa_output.hdmi.playback.0.0:playback_FR\n")
    monkeypatch.setattr(audio.shutil, "which", lambda n: "/usr/bin/pw-link")
    monkeypatch.setattr(audio, "_run", lambda c, timeout=8: out)
    assert audio.node_routed("output.ups-mon-a0c9") is True


def test_node_routed_false_on_dead_end(monkeypatch):
    """Ports exist but nothing links onward — the post-bounce dead end
    that silenced the mic monitor."""
    from ultrapistar import audio
    out = (
        "output.ups-mon-a0c9:output_FL\n"
        "output.ups-mon-a0c9:output_FR\n")
    monkeypatch.setattr(audio.shutil, "which", lambda n: "/usr/bin/pw-link")
    monkeypatch.setattr(audio, "_run", lambda c, timeout=8: out)
    assert audio.node_routed("output.ups-mon-a0c9") is False


def test_node_routed_none_when_node_absent(monkeypatch):
    """No ports listed → indeterminate (still coming up), not dead."""
    from ultrapistar import audio
    monkeypatch.setattr(audio.shutil, "which", lambda n: "/usr/bin/pw-link")
    monkeypatch.setattr(audio, "_run", lambda c, timeout=8: "")
    assert audio.node_routed("output.ups-mon-a0c9") is None


def test_node_routed_none_without_pw_link(monkeypatch):
    from ultrapistar import audio
    monkeypatch.setattr(audio.shutil, "which", lambda n: None)
    assert audio.node_routed("x") is None
