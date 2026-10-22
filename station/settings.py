"""Persisted station prefs (dest, Pi target, model tier)."""
from __future__ import annotations

import json
from pathlib import Path

_PATH = Path.home() / ".ultrapistar-station.json"


def load() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(prefs: dict) -> None:
    try:
        _PATH.write_text(json.dumps(prefs, indent=1), encoding="utf-8")
    except OSError:
        pass
