import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ultrapistar.app import create_app  # noqa: E402
from ultrapistar.config import Config  # noqa: E402
from ultrapistar.db import Database  # noqa: E402
from ultrapistar.queue_manager import QueueManager  # noqa: E402


@pytest.fixture()
def tmp_config():
    # ignore_cleanup_errors: Windows file-lock races (WAL db handles held
    # a beat too long by background threads) must not flake the suite
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        cfg = Config()
        cfg.data_dir = Path(d) / "data"
        cfg.media_dir = Path(d) / "media"
        cfg.chart_dir = Path(d) / "charts"
        cfg.chart_library_dir = Path(d) / "chart-library"
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cfg.media_dir.mkdir(parents=True, exist_ok=True)
        cfg.chart_dir.mkdir(parents=True, exist_ok=True)
        cfg.chart_library_dir.mkdir(parents=True, exist_ok=True)
        cfg.db_path = cfg.data_dir / "ultrapistar.db"
        cfg.host_key = "test-host-key"
        cfg.enabled_providers = ("local",)  # no network in tests
        cfg.config_file = cfg.data_dir / "config.yaml"
        # mirror the repo's bundled demo assets so seeding behaves identically
        root = Path(__file__).resolve().parent.parent
        for f in (root / "charts").glob("*.txt"):
            shutil.copy(f, cfg.chart_dir)
        for f in (root / "media").glob("*"):
            shutil.copy(f, cfg.media_dir)
        yield cfg


@pytest.fixture()
def db(tmp_config):
    return Database(tmp_config.db_path)


@pytest.fixture()
def queue(db):
    return QueueManager(db)


@pytest.fixture()
def app(tmp_config):
    app, socketio, cfg = create_app(tmp_config, seed=True)
    app.config["TESTING"] = True
    app.extensions["ups"]["socketio"] = socketio
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def host_client(client, tmp_config):
    with client.session_transaction() as s:
        s["is_host"] = True
    return client
