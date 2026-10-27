"""Vocal separation — spleeter 2-stem via the sherpa-onnx C++ engine.

Turns a stereo mix into ``vocals`` + ``accompaniment`` stems. The vocal
stem feeds ASR (cleaner words than the full mix) and pitch extraction
(near-monophonic input); the accompaniment becomes the karaoke backing.

Uses sherpa-onnx's bundled spleeter int8 models — the C++ engine does
STFT + inference natively (~0.09x RTF on Pi 5, i.e. 11x faster than
realtime). Pure-Python MDX-Net was measured ~5x slower than realtime
and is not viable on the Pi.

Runs offline at queue/download time, never on the playback path.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

SR = 44100
MODEL_DIR = "sherpa-onnx-spleeter-2stems-int8"


def _separator(models_dir: Path):
    import sherpa_onnx
    cfg = sherpa_onnx.OfflineSourceSeparationConfig(
        model=sherpa_onnx.OfflineSourceSeparationModelConfig(
            spleeter=sherpa_onnx.OfflineSourceSeparationSpleeterModelConfig(
                vocals=str(models_dir / MODEL_DIR / "vocals.int8.onnx"),
                accompaniment=str(
                    models_dir / MODEL_DIR / "accompaniment.int8.onnx")),
            num_threads=3, provider="cpu"))
    if not cfg.validate():
        raise RuntimeError(f"bad separation config; models in {models_dir}?")
    return sherpa_onnx.OfflineSourceSeparation(cfg)


def decode_stereo(media_path, sr: int = SR) -> np.ndarray:
    """ffmpeg → stereo float32, shape (2, n)."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(media_path),
         "-ac", "2", "-ar", str(sr), "-f", "f32le", "-"],
        capture_output=True, check=True).stdout
    pcm = np.frombuffer(out, dtype=np.float32)
    return pcm.reshape(-1, 2).T.copy()


def separate_stems(media_path, models_dir) -> tuple[np.ndarray, np.ndarray]:
    """(vocals, accompaniment) — each (2, n) float32 @44.1k."""
    mix = decode_stereo(media_path)
    out = _separator(Path(models_dir)).process(SR, mix)
    stems = [np.asarray(s.data, dtype=np.float32) for s in out.stems]
    return stems[0], stems[1]


def mono16(samples: np.ndarray) -> np.ndarray:
    """(2, n) float32 @44.1k → 16k mono float32 for ASR/pitch input."""
    import librosa
    mono = samples.mean(axis=0)
    return librosa.resample(mono, orig_sr=SR, target_sr=16000)


def write_wav(path, samples: np.ndarray, sr: int = SR) -> Path:
    """(2, n) or (n,) float32 → 16-bit wav."""
    import soundfile as sf
    data = samples.T if samples.ndim == 2 else samples
    sf.write(str(path), data, sr, subtype="PCM_16")
    return Path(path)


def karaoke_mix(vocals: np.ndarray, accomp: np.ndarray,
                guide: float = 0.15, bass_hz: float = 150.0) -> np.ndarray:
    """accompaniment + rescued low end + a faint guide vocal.

    Spleeter int8 dumps center-panned music into the vocal stem —
    kick, bass, snare — so the raw accompaniment sounds gutted.
    Two rescues: everything below ~``bass_hz`` in the vocal stem is
    leaked instruments (no voice lives down there) and returns
    wholesale; ``guide`` fraction of the full vocal stem brings back
    the remaining leaked music plus a quiet guide vocal singers can
    lean on. ``guide=0`` still rescues the bass."""
    n = vocals.shape[-1]
    spec = np.fft.rfft(vocals, axis=-1)
    freqs = np.fft.rfftfreq(n, 1.0 / SR)
    # smooth rolloff — a brick-wall notch rings on transients
    spec *= np.clip((bass_hz * 1.4 - freqs) / (bass_hz * 0.8), 0.0, 1.0)
    low = np.fft.irfft(spec, n=n, axis=-1)
    mix = accomp + low + guide * (vocals - low)
    peak = float(np.abs(mix).max())
    return mix / peak if peak > 1.0 else mix


def has_real_video(media_path) -> bool:
    """True when the file carries a real video stream. An mp3's embedded
    cover art is an mjpeg *attached pic* — not a playable video track."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v",
             "-show_entries", "stream_disposition=attached_pic",
             "-of", "csv=p=0", str(media_path)],
            capture_output=True, text=True, timeout=20)
        # one line per video stream: 1 = attached pic, 0 = real video
        return any(line.strip() == "0" for line in out.stdout.splitlines())
    except Exception:
        return False


def _want_hevc() -> bool:
    """HEVC insts decode on the Pi's hardware block (~half the CPU of
    software h264). UPS_HEVC=0 opts back out to stream-copy."""
    import os
    return os.environ.get("UPS_HEVC", "1") != "0"


def _hevc_encoders() -> list:
    """HEVC encoders this ffmpeg build actually ships, best first."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=20)
        listed = out.stdout
    except Exception:
        listed = ""
    pref = ["hevc_nvenc", "hevc_qsv", "hevc_amf", "hevc_mf", "libx265"]
    return [e for e in pref if e in listed] or ["libx265"]


def karaoke_mp4(media_path, inst: np.ndarray, out_path) -> Path:
    """Remux/re-encode the original video with the accompaniment stem as
    audio — the karaoke-ready file the TV plays. With a real video track
    and UPS_HEVC (default) the stream is re-encoded to HEVC so the Pi can
    use its hardware decoder. Audio-only uploads (mp3 etc., including
    cover-art-only "video" tracks) get an audio-only container."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        write_wav(tmp_path, inst)
        if not has_real_video(media_path):
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y",
                 "-i", str(media_path), "-i", tmp_path,
                 "-map", "1:a", "-c:a", "aac", "-b:a", "192k",
                 str(out_path)],
                check=True)
        elif _want_hevc():
            maps = ["-map", "0:v", "-map", "1:a", "-shortest"]
            err = None
            for enc in _hevc_encoders():
                r = subprocess.run(
                    ["ffmpeg", "-v", "error", "-y",
                     "-i", str(media_path), "-i", tmp_path,
                     *maps, "-c:v", enc, "-tag:v", "hvc1",
                     "-b:v", "2M", "-maxrate", "4M", "-bufsize", "8M",
                     "-c:a", "aac", "-b:a", "192k", str(out_path)])
                if r.returncode == 0:
                    err = None
                    break
                err = r.returncode
            if err is not None:
                raise RuntimeError(f"all hevc encoders failed ({err})")
        else:
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y",
                 "-i", str(media_path), "-i", tmp_path,
                 "-map", "0:v", "-map", "1:a", "-shortest",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                 str(out_path)],
                check=True)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return Path(out_path)
