"""Audio output routing via PipeWire/WirePlumber (``wpctl``).

Both Chromium (music/video) and the mic monitor (``aplay -D default``)
route through PipeWire's default sink, so one control moves *all* sound —
TV HDMI, USB headset, whatever is connected.

Cards sitting on profile ``off`` (like vc4-hdmi on some TVs) expose no
sink; ``enable_card`` flips them to pro-audio so PipeWire creates one,
then the new sink becomes selectable.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

_ITEM = re.compile(r"([*│\s]*?)\s*(\d+)\.\s+(.*?)\s*(?:\[[^\]]*\])*\s*$")
_VOL = re.compile(r"\[vol:\s*([\d.]+)\]")


def _env() -> dict:
    env = dict(os.environ)
    # system service has no session bus — point at the user's PipeWire
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def _run(cmd: list[str], timeout: int = 8) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=_env()).stdout
    except Exception:
        return ""


def _ok(cmd: list[str], timeout: int = 8) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout,
                              env=_env()).returncode == 0
    except Exception:
        return False


def available() -> bool:
    return bool(shutil.which("wpctl"))


def _parse_status(text: str) -> dict[str, list[tuple[int, str, bool]]]:
    """Parse `wpctl status` into {section: [(id, name, is_default)]}."""
    sections: dict[str, list] = {}
    current = None
    for line in text.splitlines():
        m = re.search(r"├─\s+(\w[\w ]*):", line)
        if m:
            current = m.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current and ("│" in line or "*" in line):
            m = _ITEM.match(line)
            if m:
                sections[current].append(
                    (int(m.group(2)), m.group(3).strip(),
                     "*" in m.group(1)))
    return sections


def _sections() -> dict[str, list[tuple[int, str, bool]]]:
    return _parse_status(_run(["wpctl", "status"]))


def sinks() -> list[dict]:
    """PipeWire output sinks: id, name, node_name, default marker."""
    out = []
    node_names = {v: k for k, v in sink_names().items()}
    for sid, name, is_default in _sections().get("Sinks", []):
        out.append({"id": sid, "name": name, "default": is_default,
                    "node": node_names.get(sid, "")})
    return out


def playback_cards() -> list[dict]:
    """ALSA playback cards without a PipeWire sink yet (profile off).

    Labeled with the TV/monitor name from the EDID ELD when present.
    """
    out = []
    for line in _run(["aplay", "-l"]).splitlines():
        m = re.match(r"card (\d+): (\w+) \[(.*?)\], device (\d+):", line)
        if not m:
            continue
        card, short, name = m.group(1), m.group(2), m.group(3)
        mon, has_eld = "", False
        try:
            with open(f"/proc/asound/card{card}/eld#0") as f:
                has_eld = True
                for line in f:
                    mm = re.match(r"monitor_name\s+(.*)", line)
                    if mm:
                        mon = mm.group(1).strip()
                        break
        except OSError:
            pass
        # an hdmi eld with no monitor name = nothing plugged into that port
        if has_eld and not mon:
            continue
        out.append({"card": int(card), "id": f"hw:{card},0",
                    "name": mon or name or short})
    return out


def _alsa_card_for(device_id: int) -> str | None:
    """ALSA card index of a PipeWire device (via `wpctl inspect`)."""
    out = _run(["wpctl", "inspect", str(device_id)])
    m = re.search(r'alsa\.card\s*=\s*"(\d+)"', out)
    return m.group(1) if m else None


def _card_for_sink_name(node_name: str) -> int | None:
    """ALSA card index backing a saved sink node.name.

    Sink names embed the device: ``alsa_output.<dev>.<profile>`` where
    the PipeWire device is ``alsa_card.<dev>``. Used to resurrect a saved
    output whose card came up on profile ``off`` — e.g. the profile
    suffix changed (hdmi-stereo -> pro-output-0) or the card was never
    enabled this boot.
    """
    if not node_name.startswith("alsa_output."):
        return None
    want = "alsa_card." + node_name[len("alsa_output."):]
    for dev_id, _, _ in _sections().get("Devices", []):
        out = _run(["wpctl", "inspect", str(dev_id)])
        m = re.search(r'device\.name\s*=\s*"([^"]+)"', out)
        if m and want.startswith(m.group(1) + "."):
            card = _alsa_card_for(dev_id)
            return int(card) if card is not None else None
    return None


def _parse_pw_dump(text: str) -> list[dict]:
    import json
    try:
        objs = json.loads(text) if text else []
    except Exception:
        return []
    nodes = []
    for o in objs:
        if o.get("type") != "PipeWire:Interface:Node":
            continue
        props = o.get("info", {}).get("props", {})
        nodes.append({
            "id": o["id"],
            "serial": int(props.get("object.serial", 0) or 0),
            "name": props.get("node.name", ""),
            "desc": props.get("node.description") or props.get("node.nick")
                    or props.get("node.name", ""),
            "media_class": props.get("media.class", ""),
        })
    return nodes


def _pw_nodes() -> list[dict]:
    """pw-dump Node objects -> [{id, name, desc, media_class}]."""
    return _parse_pw_dump(_run(["pw-dump"]))


def sources() -> list[dict]:
    """PipeWire capture sources: [{id, name, desc}] (node.name is stable
    across restarts — use it as the pw-cat --target)."""
    return [{"id": n["id"], "name": n["name"], "desc": n["desc"]}
            for n in _pw_nodes() if n["media_class"] == "Audio/Source"]


def sink_names() -> dict[str, int]:
    """node.name -> current sink object serial (names survive restarts,
    serials don't). wpctl addresses objects by serial, not registry id."""
    names = {}
    for n in _pw_nodes():
        if n["media_class"] == "Audio/Sink":
            names[n["name"]] = n["serial"] or n["id"]
    return names


def node_id(name: str) -> int | None:
    """Registry id of any PipeWire node by its node.name.

    pw-cli addresses objects by registry id (wpctl's serials don't cover
    unregistered nodes like pw-loopback streams).
    """
    for n in _pw_nodes():
        if n["name"] == name:
            return n["id"]
    return None


def set_node_volume(nid: int, volume: float) -> bool:
    return _ok(["pw-cli", "set-param", str(nid), "Props",
                f"{{ volume: {volume:.3f} }}"])


def set_node_mute(nid: int, muted: bool) -> bool:
    return _ok(["pw-cli", "set-param", str(nid), "Props",
                "{ mute: true }" if muted else "{ mute: false }"])


def node_routed(node_name: str) -> bool | None:
    """Is any port of node ``node_name`` linked onward (``|->``) to a
    destination in ``pw-link -l``?

    ``True`` = routed, ``False`` = node has ports but they lead nowhere
    (a dead end — e.g. a pw-loopback that spawned while WirePlumber was
    down and never got its sink link), ``None`` = can't tell (pw-link
    missing, or no ports listed yet — still coming up).
    """
    if not shutil.which("pw-link"):
        return None
    lines = _run(["pw-link", "-l"]).splitlines()
    seen = False
    for i, line in enumerate(lines):
        if line[:1] in (" ", "\t", "|"):     # indented = link entry
            continue
        if not line.startswith(node_name + ":"):
            continue
        seen = True
        j = i + 1
        while j < len(lines) and lines[j].startswith((" ", "\t", "|")):
            if "|->" in lines[j]:
                return True
            j += 1
    return False if seen else None


def enable_card(alsa_card: int) -> bool:
    """Flip a card's profile off->pro-audio so its sink appears."""
    for dev_id, _, _ in _sections().get("Devices", []):
        if _alsa_card_for(dev_id) == str(alsa_card):
            return _ok(["wpctl", "set-profile", str(dev_id), "1"])
    return False


def set_default(sink_id: int) -> bool:
    return _ok(["wpctl", "set-default", str(sink_id)])


def _sink_device_ids() -> set[int]:
    """PipeWire device ids that back the current sinks."""
    ids = set()
    for s in sinks():
        m = re.search(r'device\.id\s*=\s*"?(\d+)"?',
                      _run(["wpctl", "inspect", str(s["id"])]))
        if m:
            ids.add(int(m.group(1)))
    return ids


def playback_targets() -> list[dict]:
    """Selectable outputs: live sinks + cards that need enabling."""
    targets = [{"kind": "sink", **s} for s in sinks()]
    # alsa card of each pw device that already exposes a sink
    covered = set()
    dev_cards = {}
    for dev_id, _, _ in _sections().get("Devices", []):
        c = _alsa_card_for(dev_id)
        if c:
            dev_cards[dev_id] = c
    for did in _sink_device_ids():
        if did in dev_cards:
            covered.add(int(dev_cards[did]))
    for card in playback_cards():
        if card["card"] not in covered:
            targets.append({"kind": "card", "id": None,
                            "card": card["card"], "name": card["name"],
                            "default": False, "needs_enable": True})
    return targets


def select_output(kind: str, ident) -> dict:
    """Route all audio to a sink (or enable a card then use its sink).

    kind "sink" takes a numeric wpctl id, "sink_name" a stable node.name
    (for restoring saved choices — ids change across restarts), "card" an
    ALSA index whose PipeWire profile gets enabled first.
    Returns {ok, name, node?, error?}.
    """
    if kind == "sink_name":
        sid = sink_names().get(str(ident))
        if sid is None:
            # saved sink gone — its card may be sitting on profile off
            # this boot, or the profile suffix changed; resurrect the
            # card and take whatever sink it exposes now
            card = _card_for_sink_name(str(ident))
            if card is None:
                return {"ok": False, "error": "saved sink not present"}
            kind, ident = "card", card
        else:
            kind, ident = "sink", sid
    if kind == "sink":
        if not set_default(int(ident)):
            return {"ok": False, "error": "wpctl set-default failed"}
        match = next((s for s in sinks() if s["id"] == int(ident)), None)
        return {"ok": True, "name": match["name"] if match else str(ident),
                "node": match["node"] if match else ""}
    if kind == "card":
        card = int(ident)
        before = {s["id"] for s in sinks()}
        if not enable_card(card):
            return {"ok": False, "error": "could not enable card"}
        import time
        for _ in range(10):          # sink node appears asynchronously
            time.sleep(0.5)
            for s in sinks():
                if s["id"] not in before:
                    set_default(s["id"])
                    return {"ok": True, "name": s["name"],
                            "node": s.get("node", "")}
        return {"ok": False, "error": "card enabled but no sink appeared"}
    return {"ok": False, "error": "unknown output kind"}
