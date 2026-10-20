import shutil
import subprocess
from pathlib import Path

import pytest

from ultrapistar import loudness

ffmpeg = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(not ffmpeg, reason="ffmpeg not installed")


def _make_sine(path: Path, seconds: float = 6, gain_db: float = 0.0):
    af = "sine=frequency=440:duration=%g" % seconds
    if gain_db:
        af += f",volume={gain_db}dB"
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", af,
         str(path)], check=True)


def test_measure_reports_integrated_loudness(tmp_path):
    f = tmp_path / "tone.mp3"
    _make_sine(f)
    m = loudness.measure(f)
    assert m is not None
    assert -35 < float(m["input_i"]) < -5   # sane LUFS for a test tone
    for k in ("input_tp", "input_lra", "input_thresh"):
        assert k in m


def test_normalize_brings_quiet_file_to_target(tmp_path):
    f = tmp_path / "quiet.mp3"
    _make_sine(f, gain_db=-14)              # make it audibly quiet
    pre = loudness.normalize(f)
    assert pre is not None
    assert loudness.is_normed(f)
    post = loudness.measure(f)
    assert abs(float(post["input_i"]) - loudness.TARGET_I) < 1.0


def test_normalize_is_idempotent(tmp_path):
    f = tmp_path / "tone.mp3"
    _make_sine(f)
    assert loudness.normalize(f) is not None
    marker = loudness.norm_marker(f)
    assert marker.exists()
    # second call measures but must not re-encode an on-target file
    size1, mtime1 = f.stat().st_size, f.stat().st_mtime
    again = loudness.normalize(f)
    assert again is not None
    assert abs(again - loudness.TARGET_I) < 1.0
    assert f.stat().st_mtime == mtime1     # file untouched


def test_normalize_no_audio_returns_none(tmp_path):
    # a file ffmpeg can't parse at all
    f = tmp_path / "broken.mp4"
    f.write_bytes(b"not media")
    assert loudness.normalize(f) is None


def test_normalize_video_keeps_video_track(tmp_path):
    f = tmp_path / "clip.mp4"
    subprocess.run(
        [ffmpeg, "-v", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
         "-f", "lavfi", "-i", "color=c=red:s=160x120:d=5",
         "-c:a", "aac", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-shortest", str(f)], check=True)
    assert loudness.normalize(f) is not None
    out = subprocess.run(
        [shutil.which("ffprobe") or "ffprobe", "-v", "error",
         "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(f)],
        capture_output=True, text=True, check=True).stdout
    assert "video" in out and "audio" in out


def test_manage_normalize_endpoint(host_client):
    r = host_client.post("/api/manage/normalize")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_manage_normalize_requires_host(client):
    assert client.post("/api/manage/normalize").status_code == 403
