"""GPU model wrappers — demucs separation + faster-whisper ASR.

Models load lazily on first use and are cached for the process. Both
fall back to CPU when CUDA is unavailable (slow but functional)."""
from __future__ import annotations

import numpy as np

_sep = None
_whisper = None


def _cuda() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


def separate_stems(media_path) -> tuple[np.ndarray, np.ndarray]:
    """htdemucs → (vocals, accompaniment), each (2, n) float32 @44.1k —
    the same contract ultrapistar.separate.separate_stems honours, so
    karaoke_mix/karaoke_mp4/mono16 consume it unchanged."""
    global _sep
    import torch
    from demucs.apply import apply_model
    from demucs.audio import AudioFile
    from demucs.pretrained import get_model
    if _sep is None:
        _sep = get_model("htdemucs")
        if _cuda():
            _sep.cuda()
        _sep.eval()
    wav = AudioFile(str(media_path)).read(
        streams=0, samplerate=_sep.samplerate,
        channels=_sep.audio_channels)
    ref = wav.mean(0)
    wav = (wav - ref.mean()) / ref.std()
    with torch.no_grad():
        sources = apply_model(
            _sep, wav[None],
            device="cuda" if _cuda() else "cpu")[0]
    sources = sources * ref.std() + ref.mean()
    vidx = _sep.sources.index("vocals")
    vocals = sources[vidx]
    accomp = sources.sum(0) - vocals
    return (vocals.cpu().numpy().astype(np.float32),
            accomp.cpu().numpy().astype(np.float32))


def transcribe_vocals(wav_path) -> tuple[list[tuple[str, float]], str]:
    """faster-whisper large-v3 → ([(norm_word, start_ms)], language).

    Word-level timestamps on the isolated vocal stem; language is
    auto-detected (~99 languages incl. Hindi — whisper emits the
    native script, and lyrics.norm_word keeps it)."""
    global _whisper
    from faster_whisper import WhisperModel
    from ultrapistar.lyrics import norm_word
    if _whisper is None:
        dev = "cuda" if _cuda() else "cpu"
        _whisper = WhisperModel(
            "large-v3", device=dev,
            compute_type="float16" if dev == "cuda" else "int8")
    segments, info = _whisper.transcribe(
        str(wav_path), word_timestamps=True, vad_filter=True)
    out = []
    for seg in segments:
        for w in seg.words or []:
            t = norm_word(w.word)
            if t:
                out.append((t, float(w.start) * 1000.0))
    return out, getattr(info, "language", "") or ""
