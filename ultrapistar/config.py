"""Runtime configuration for Ultra Pi Star.

All values may be overridden with environment variables prefixed ``UPS_``.
The host key protects host-only routes and socket events; it defaults to a
random value generated per boot and printed on the console.
"""
from __future__ import annotations

import os
import secrets
import socket
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def detect_lan_ip() -> str:
    """Best-effort LAN IP discovery for the QR join code."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def find_usb_media_dir(bases: tuple = ()) -> Path | None:
    """Prepared removable storage — mounted USB sticks win over the SD
    card as the media write target.

    A stick is 'prepared' when it has an ``ultrapistar`` folder or a
    ``.upsmedia`` marker file at its root, so a random drive can't
    hijack the library. Pi OS desktop automounts under
    /media/<user>/<label> (or /run/media/<user>/<label>); manual
    mounts typically land in /mnt.
    """
    if not bases:
        bases = (Path("/media"), Path("/run/media"), Path("/mnt"))
    for base in bases:
        if not base.is_dir():
            continue
        mounts = []
        for user_dir in base.iterdir():
            mounts.append(user_dir)
            if user_dir.is_dir():
                try:
                    mounts.extend(user_dir.iterdir())
                except OSError:
                    pass
        for mount in mounts:
            try:
                if not mount.is_dir() or not os.access(mount, os.W_OK):
                    continue
                if (mount / ".upsmedia").exists():
                    (mount / "ultrapistar").mkdir(exist_ok=True)
                    return mount / "ultrapistar"
                if (mount / "ultrapistar").is_dir():
                    return mount / "ultrapistar"
            except OSError:
                continue
    return None


@dataclass
class Config:
    host: str = field(default_factory=lambda: os.environ.get("UPS_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.environ.get("UPS_PORT", "5000")))
    # 4-char room code — capitals + digits, no look-alikes (0/O, 1/I) so
    # it stays readable on a TV from across the room
    host_key: str = field(
        default_factory=lambda: os.environ.get("UPS_HOST_KEY")
        or "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
                   for _ in range(4))
    )
    data_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("UPS_DATA_DIR", BASE_DIR / "data"))
    )
    media_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("UPS_MEDIA_DIR")
            or ("" if os.environ.get("UPS_NO_USB") else find_usb_media_dir() or "")
            or BASE_DIR / "media")
    )
    # extra read roots — media on the SD card keeps resolving after a
    # prepared USB stick becomes the write target. UPS_MEDIA_DIRS adds
    # more (colon-separated).
    media_extra_dirs: tuple[Path, ...] = field(
        default_factory=lambda: tuple(
            Path(p) for p in
            os.environ.get("UPS_MEDIA_DIRS", "").split(":")
            if p.strip())
    )
    chart_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("UPS_CHART_DIR", BASE_DIR / "charts"))
    )
    chart_library_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("UPS_CHART_LIB", BASE_DIR / "chart-library"))
    )
    artwork_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("UPS_ARTWORK_DIR", BASE_DIR / "artwork"))
    )
    # separation + pitch models (spleeter int8 dir, rmvpe.onnx) — used by
    # the queue-time stem/pitch pipeline
    models_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("UPS_MODELS_DIR", BASE_DIR / "models"))
    )
    # play the vocal-reduced variant when an entry has one ("0" to disable)
    vocal_cut: bool = field(
        default_factory=lambda: os.environ.get("UPS_VOCAL_CUT", "1") != "0")
    # fraction of the vocal stem bled back into the karaoke backing —
    # restores music the separator ate and leaves a faint guide vocal
    # (0 = pure accompaniment, bass rescue still applies)
    karaoke_guide: float = field(
        default_factory=lambda: float(
            os.environ.get("UPS_KARAOKE_GUIDE", "0.15")))
    enabled_providers: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            p.strip()
            for p in os.environ.get("UPS_PROVIDERS", "local,youtube").split(",")
            if p.strip()
        )
    )
    mic_device: str = field(
        default_factory=lambda: os.environ.get("UPS_MIC_DEVICE", "default"))
    play_device: str = field(
        default_factory=lambda: os.environ.get("UPS_PLAY_DEVICE", "default"))
    # chart sources trusted to auto-verify on match (comma list of
    # source_repo tags, e.g. UPS_TRUST_SOURCES=usdx-songs)
    trusted_chart_sources: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            s.strip() for s in
            os.environ.get("UPS_TRUST_SOURCES", "").split(",") if s.strip()))
    # normalize every media file to the same integrated loudness
    # (EBU R128) so songs play at a consistent base volume
    audio_normalize: bool = field(
        default_factory=lambda: os.environ.get("UPS_NORMALIZE", "1") != "0")
    # integrated-loudness target in LUFS — -14 matches YouTube/Spotify;
    # raise toward -12 for a hotter room, lower toward -16 for headroom
    target_lufs: float = field(
        default_factory=lambda: float(
            os.environ.get("UPS_TARGET_LUFS", "-14")))
    # mic-handoff countdown before each song (0 = play immediately)
    intermission_sec: int = field(
        default_factory=lambda: int(os.environ.get("UPS_INTERMISSION_SEC", "30")))
    difficulty: int = field(
        default_factory=lambda: int(os.environ.get("UPS_DIFFICULTY", "0")))
    # user-editable YAML (see config.example.yaml) — env vars still win
    config_file: Path = field(
        default_factory=lambda: Path(
            os.environ.get("UPS_CONFIG_YAML", "")) if os.environ.get(
                "UPS_CONFIG_YAML") else None)
    db_path: Path = field(init=False)
    lan_ip: str = field(default_factory=detect_lan_ip)

    def __post_init__(self) -> None:
        self.db_path = self.data_dir / "ultrapistar.db"
        # when a USB stick (or an explicit UPS_MEDIA_DIR) is the write
        # target the SD media dir stays readable so the existing
        # library keeps playing
        default_media = BASE_DIR / "media"
        extras = list(self.media_extra_dirs)
        if self.media_dir != default_media and default_media.is_dir():
            extras.append(default_media)
        self.media_extra_dirs = tuple(
            dict.fromkeys(
                d for d in extras
                if d != self.media_dir and d.is_dir()))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.chart_dir.mkdir(parents=True, exist_ok=True)
        self.chart_library_dir.mkdir(parents=True, exist_ok=True)
        self.artwork_dir.mkdir(parents=True, exist_ok=True)
        if self.config_file is None:
            self.config_file = self.data_dir / "config.yaml"
        self._apply_config_file()

    def media_path(self, rel: str | Path) -> Path:
        """Resolve an entry-relative media path across all roots —
        the write target first, then extra roots (e.g. the SD card a
        USB stick replaced). Misses resolve into media_dir so writers
        always land on the primary root."""
        rel = Path(rel)
        for root in (self.media_dir, *self.media_extra_dirs):
            p = root / rel
            if p.exists():
                return p
        return self.media_dir / rel

    def media_roots(self) -> tuple[Path, ...]:
        """Every directory that may hold media files."""
        return (self.media_dir, *self.media_extra_dirs)

    def _apply_config_file(self) -> None:
        """Read the optional YAML config; env-set values take precedence."""
        if not self.config_file.is_file():
            return
        try:
            import yaml
            doc = yaml.safe_load(
                self.config_file.read_text(encoding="utf-8")) or {}
        except Exception:
            return  # unreadable config must never stop the party
        if ("UPS_KARAOKE_GUIDE" not in os.environ
                and doc.get("karaoke_guide") is not None):
            try:
                self.karaoke_guide = float(doc["karaoke_guide"])
            except (TypeError, ValueError):
                pass
