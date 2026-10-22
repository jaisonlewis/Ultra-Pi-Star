"""USB media detection + multi-root resolution."""
import shutil
import tempfile
from pathlib import Path

import pytest

from ultrapistar.app import create_app
from ultrapistar.config import Config, find_usb_media_dir
from ultrapistar.providers.local import LocalProvider


def _mount(base: Path, label: str) -> Path:
    mount = base / "user" / label
    mount.mkdir(parents=True)
    return mount


class TestFindUsbMediaDir:
    def test_prepared_dir(self, tmp_path):
        base = tmp_path / "media-root"
        mount = _mount(base, "KARAOKE")
        (mount / "ultrapistar").mkdir()
        assert find_usb_media_dir((base,)) == mount / "ultrapistar"

    def test_marker_file(self, tmp_path):
        base = tmp_path / "media-root"
        mount = _mount(base, "KARAOKE")
        (mount / ".upsmedia").touch()
        got = find_usb_media_dir((base,))
        assert got == mount / "ultrapistar"
        assert got.is_dir()  # marker bootstraps the folder

    def test_random_stick_ignored(self, tmp_path):
        base = tmp_path / "media-root"
        _mount(base, "PHOTOS")  # mounted, writable, no marker/folder
        assert find_usb_media_dir((base,)) is None

    def test_direct_mount_not_nested(self, tmp_path):
        base = tmp_path / "mnt"
        mount = base / "usb"
        (mount / "ultrapistar").mkdir(parents=True)
        assert find_usb_media_dir((base,)) == mount / "ultrapistar"


def _two_root_config(tmp_path):
    """media_dir=usb (empty write root), extra=sd (existing library)."""
    usb = tmp_path / "usb-media"
    sd = tmp_path / "sd-media"
    usb.mkdir()
    sd.mkdir()
    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.db_path = cfg.data_dir / "ultrapistar.db"
    cfg.media_dir = usb
    cfg.media_extra_dirs = (sd,)
    cfg.chart_dir = tmp_path / "charts"
    cfg.chart_dir.mkdir(exist_ok=True)
    cfg.chart_library_dir = tmp_path / "chart-library"
    cfg.chart_library_dir.mkdir(exist_ok=True)
    cfg.artwork_dir = tmp_path / "artwork"
    cfg.artwork_dir.mkdir(exist_ok=True)
    cfg.host_key = "test-host-key"
    cfg.enabled_providers = ("local",)
    cfg.config_file = cfg.data_dir / "config.yaml"
    return cfg, usb, sd


class TestMediaPath:
    def test_write_root_wins(self, tmp_path):
        cfg, usb, sd = _two_root_config(tmp_path)
        (usb / "song.mp4").write_bytes(b"new")
        (sd / "song.mp4").write_bytes(b"old")
        assert cfg.media_path("song.mp4") == usb / "song.mp4"

    def test_sd_fallback(self, tmp_path):
        cfg, usb, sd = _two_root_config(tmp_path)
        (sd / "old-song.mp4").write_bytes(b"old")
        assert cfg.media_path("old-song.mp4") == sd / "old-song.mp4"

    def test_miss_resolves_into_write_root(self, tmp_path):
        cfg, usb, sd = _two_root_config(tmp_path)
        assert cfg.media_path("brand-new.mp4") == usb / "brand-new.mp4"


class TestProviderFallback:
    def test_local_resolves_from_extra_root(self, tmp_path):
        cfg, usb, sd = _two_root_config(tmp_path)
        (sd / "legacy.mp4").write_bytes(b"vid")
        prov = LocalProvider(cfg.media_dir, cfg.media_extra_dirs)
        ref = prov.resolve({"local_path": "legacy.mp4"})
        assert ref is not None and ref.uri == "/media/legacy.mp4"

    def test_media_route_serves_extra_root(self, tmp_path):
        cfg, usb, sd = _two_root_config(tmp_path)
        (sd / "legacy.mp4").write_bytes(b"vid")
        app, _sio, _cfg = create_app(cfg, seed=False)
        resp = app.test_client().get("/media/legacy.mp4")
        assert resp.status_code == 200
        assert resp.data == b"vid"

    def test_sd_root_auto_added(self, tmp_path):
        """When media_dir points elsewhere, the repo's own media dir
        becomes a read fallback — the existing SD library survives
        USB adoption."""
        from ultrapistar.config import BASE_DIR
        usb = tmp_path / "usb-media"
        usb.mkdir()
        cfg = Config(media_dir=usb)
        assert usb == cfg.media_dir
        assert (BASE_DIR / "media") in cfg.media_extra_dirs
