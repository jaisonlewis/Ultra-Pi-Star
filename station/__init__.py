"""Ultra Pi Star prep station — GPU-side media processing.

Run on a workstation to produce finished karaoke bundles the Pi only
has to play: download → lyrics → separation → ASR → pitch → chart →
normalize → manifest/bundle. Everything the Pi's queue-time pipeline
does, but with heavyweight models (demucs separation, faster-whisper
large-v3 multilingual ASR, RMVPE pitch).
"""
