"""EBU R128 loudness normalization.

Every playable file is remuxed once to the same integrated loudness so
songs don't jump between whisper-quiet and ear-splitting — singers never
reach for the volume slider mid-party. ``loudnorm`` runs two-pass
(measure, then linear apply); files already within tolerance are skipped
so normalization is idempotent. A ``<stem>.normed`` sidecar marks done
work — it survives re-imports and DB rebuilds, and records the measured
values for debugging.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

TARGET_I = -14.0        # LUFS integrated — streaming standard
                        # (YouTube/Spotify). Karaoke wants punchy.
TARGET_TP = -1.5        # dBTP true-peak ceiling
TARGET_LRA = 11.0       # loudness range ceiling
SKIP_TOLERANCE = 0.75   # within this of target → no re-encode

# audio codec per container when rewriting with normalized audio
_ACODEC = {
    ".mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    ".ogg": ["-c:a", "libvorbis", "-q:a", "5"],
    ".webm": ["-c:a", "libopus", "-b:a", "160k"],
    ".wav": ["-c:a", "pcm_s16le"],
    # mp4/m4a/mkv and anything else → aac
}
_AAC = ["-c:a", "aac", "-b:a", "192k"]


def _ffmpeg() -> str:
    import shutil
    return shutil.which("ffmpeg") or "ffmpeg"


def norm_marker(media: Path) -> Path:
    return media.parent / (media.name + ".normed")


def is_normed(media: Path, target_i: float = TARGET_I) -> bool:
    """True when the marker says the file was normalized to *this*
    target — a changed target re-normalizes on the next pass."""
    marker = norm_marker(media)
    if not marker.exists():
        return False
    try:
        return json.loads(marker.read_text()).get("target_i") == target_i
    except (OSError, ValueError, AttributeError):
        return False


def measure(media: Path, target_i: float = TARGET_I) -> dict | None:
    """First loudnorm pass — returns {input_i, input_tp, input_lra,
    input_thresh, target_offset} or None on failure/no audio."""
    try:
        proc = subprocess.run(
            [_ffmpeg(), "-hide_banner", "-nostats", "-i", str(media),
             "-vn", "-af",
             f"loudnorm=I={target_i}:TP={TARGET_TP}:LRA={TARGET_LRA}"
             ":print_format=json",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("loudness measure failed for %s: %s", media, exc)
        return None
    blocks = re.findall(r"\{[^{}]*\}", proc.stderr or "")
    for raw in reversed(blocks):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if "input_i" in data:
            return data
    if proc.returncode != 0:
        log.warning("loudness measure failed for %s (rc=%d)",
                    media, proc.returncode)
    return None


def normalize(media: Path, force: bool = False,
              target_i: float = TARGET_I) -> float | None:
    """In-place loudness normalization of one media file.

    Returns the measured integrated LUFS (pre-normalization), or None if
    the file couldn't be processed. Writes a ``.normed`` sidecar either
    way so the work isn't retried.
    """
    m = measure(media, target_i)
    if not m:
        return None
    try:
        in_i = float(m["input_i"])
        in_tp = float(m.get("input_tp", "0"))
        in_lra = float(m.get("input_lra", "0"))
        in_th = float(m.get("input_thresh", "0"))
        offset = float(m.get("target_offset", "0"))
    except (TypeError, ValueError):
        return None

    applied = False
    if force or abs(in_i - target_i) > SKIP_TOLERANCE:
        # second pass — linear=true keeps dynamics, applies pure gain
        # when headroom allows; loudnorm falls back to limiting when the
        # true peak would clip.
        af = (f"loudnorm=I={target_i}:TP={TARGET_TP}:LRA={TARGET_LRA}"
              f":measured_I={in_i}:measured_TP={in_tp}"
              f":measured_LRA={in_lra}:measured_thresh={in_th}"
              f":offset={offset}:linear=true:print_format=summary")
        fd, tmpname = tempfile.mkstemp(
            prefix=media.stem + ".", suffix=".norm" + media.suffix,
            dir=media.parent)
        os.close(fd)
        tmp = Path(tmpname)
        try:
            proc = subprocess.run(
                [_ffmpeg(), "-hide_banner", "-nostats", "-y",
                 "-i", str(media), "-map", "0", "-c:v", "copy",
                 "-af", af, "-ar", "48000",
                 *_ACODEC.get(media.suffix.lower(), _AAC),
                 str(tmp)],
                capture_output=True, text=True, timeout=900)
            if proc.returncode == 0 and tmp.stat().st_size > 0:
                os.replace(tmp, media)
                applied = True
            else:
                log.warning("loudnorm apply failed for %s (rc=%d): %s",
                            media, proc.returncode,
                            (proc.stderr or "")[-300:])
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("loudnorm apply failed for %s: %s", media, exc)
        finally:
            tmp.unlink(missing_ok=True)
        if not applied:
            return None

    norm_marker(media).write_text(json.dumps({
        "input_i": in_i, "input_tp": in_tp, "input_lra": in_lra,
        "target_i": target_i, "applied": applied}))
    log.info("loudness %s: %.1f LUFS -> %s", media.name, in_i,
             f"~{target_i}" if applied else "already on target")
    return in_i
