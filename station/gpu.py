"""GPU model wrappers — demucs separation + faster-whisper ASR.

Model tier is selectable: ``configure()`` picks the whisper size,
compute type and demucs model to match the card, or
``detect_tier()`` suggests one from the VRAM present. Models load
lazily on first use and are cached for the process."""
from __future__ import annotations

import numpy as np

_sep = None
_whisper = None

# VRAM tiers → model set. CPU is last-resort slow but functional.
TIERS = {
    "cpu": {"label": "CPU (no graphics card)",
            "whisper": "small", "compute": "int8",
            "demucs": "htdemucs"},
    "low": {"label": "GPU ≤ 6 GB VRAM",
            "whisper": "medium", "compute": "float16",
            "demucs": "htdemucs"},
    "mid": {"label": "GPU 8–16 GB VRAM",
            "whisper": "large-v3", "compute": "float16",
            "demucs": "htdemucs"},
    "max": {"label": "GPU 24 GB+ VRAM",
            "whisper": "large-v3", "compute": "float16",
            "demucs": "htdemucs_ft"},
}
_profile = "cpu"


def _cuda() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


def gpu_name() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except Exception:
        pass
    return ""


def gpu_vram_gb() -> float:
    try:
        import torch
        if torch.cuda.is_available():
            return (torch.cuda.get_device_properties(0).total_memory
                    / 2**30)
    except Exception:
        pass
    return 0.0


def detect_tier() -> str:
    """Suggest a tier from the installed card."""
    if not _cuda():
        return "cpu"
    gb = gpu_vram_gb()
    if gb >= 20:
        return "max"
    if gb >= 7:
        return "mid"
    return "low"


def configure(profile: str) -> None:
    """Select a TIERS key. Models load on next use."""
    global _profile, _sep, _whisper
    if profile not in TIERS:
        raise ValueError(f"unknown tier {profile!r}")
    _profile = profile
    _sep = _whisper = None            # force reload at the new tier


def current_tier() -> str:
    return _profile


def _device() -> str:
    return "cpu" if _profile == "cpu" or not _cuda() else "cuda"


def separate_stems(media_path) -> tuple[np.ndarray, np.ndarray]:
    """demucs → (vocals, accompaniment), each (2, n) float32 @44.1k —
    the same contract ultrapistar.separate.separate_stems honours, so
    karaoke_mix/karaoke_mp4/mono16 consume it unchanged."""
    global _sep
    import torch
    from demucs.apply import apply_model
    from demucs.audio import AudioFile
    from demucs.pretrained import get_model
    if _sep is None:
        _sep = get_model(TIERS[_profile]["demucs"])
        if _device() == "cuda":
            _sep.cuda()
        _sep.eval()
    wav = AudioFile(str(media_path)).read(
        streams=0, samplerate=_sep.samplerate,
        channels=_sep.audio_channels)
    ref = wav.mean(0)
    wav = (wav - ref.mean()) / ref.std()
    with torch.no_grad():
        sources = apply_model(_sep, wav[None], device=_device())[0]
    sources = sources * ref.std() + ref.mean()
    vidx = _sep.sources.index("vocals")
    vocals = sources[vidx]
    accomp = sources.sum(0) - vocals
    return (vocals.cpu().numpy().astype(np.float32),
            accomp.cpu().numpy().astype(np.float32))


def transcribe_vocals(wav_path) -> tuple[list[tuple[str, float]], str]:
    """faster-whisper → ([(norm_word, start_ms)], language).

    Word-level timestamps on the isolated vocal stem; language is
    auto-detected (~99 languages incl. Hindi — whisper emits the
    native script, and lyrics.norm_word keeps it)."""
    global _whisper
    from faster_whisper import WhisperModel
    from ultrapistar.lyrics import norm_word
    if _whisper is None:
        dev = _device()
        _whisper = WhisperModel(
            TIERS[_profile]["whisper"], device=dev,
            compute_type=(TIERS[_profile]["compute"]
                          if dev == "cuda" else "int8"))
    segments, info = _whisper.transcribe(
        str(wav_path), word_timestamps=True, vad_filter=True)
    out = []
    for seg in segments:
        for w in seg.words or []:
            t = norm_word(w.word)
            if t:
                out.append((t, float(w.start) * 1000.0))
    return out, getattr(info, "language", "") or ""
