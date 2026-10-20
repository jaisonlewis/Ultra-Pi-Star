# Ultra Pi Star — agent notes

- Run: `python run.py` (Flask + Flask-SocketIO, threading mode — no eventlet needed on Windows).
- Test: `python -m pytest tests -q`
- Deps: `pip install -r requirements.txt`
  - NOTE: this machine's global pip config has an unreachable `extra-index-url`
    (`pypi.ngc.nvidia.com`) that adds ~25s of retries per package.
    `PIP_CONFIG_FILE=<repo>\.pipconf\pip.ini pip install ...` bypasses it.
- Host key is printed to console at startup; pass `?key=` to `/host` or the
  `X-Host-Key` header for host-only API calls.
- sqlite3 `with conn:` only commits — always use `db.session()` which commits
  AND closes (open connections lock the DB file on Windows temp dirs).
- Chart scoring is gated on `chart.verified=1` + match to a specific
  `media_entry_id` — never auto-score on artist/title match alone.
- New media providers implement `ultrapistar/providers/base.py` and are
  enabled via `UPS_PROVIDERS`. The YouTube provider (yt-dlp) needs the
  `player_client=android` extractor arg — the default web client fails
  with "The page needs to be reloaded" (PO-token requirement).
- Keep yt-dlp current (`pip install -U yt-dlp`); YouTube breaks old versions.
- Remote results enter the catalog as `media_entry(status='downloading')`;
  `DownloadManager` flips them to `available`/`failed` and re-pushes
  `now_playing` so a TV waiting on the download starts automatically.
- Idle queue auto-starts: `queue_add` promotes the first item to `playing`
  when nothing is playing.
- Scoring (Phase 3): `pitch.py`/`scoring.py` are GPL-3.0 ports of WorldParty
  (`URecord.pas`/`UNote.pas`/`UScreenSingController.pas`); `sing.py`
  `SingSession` binds the TV's `player:position` clock to `Scorer` ticks and
  feeds `MicCapture` (arecord) pitch. Mic absent -> `mic_ok=False`, overlay
  shows "no mic", playback still works.
- Kiosk needs `--autoplay-policy=no-user-gesture-required` or Chromium
  blocks `video.play()` with audio (Pi: `~/.config/labwc/autostart`).
- Chart library: `.txt` drops in `charts/` + `chart-library/` (recursive)
  index into `chart_library` and auto-attach unverified to matching songs
  (normalized artist+title). Verify via host console or
  `POST /api/catalog/chart/<id>/verify`.
- `UPS_TRUST_SOURCES` (comma list, e.g. `usdx-songs`) marks chart sources
  trusted: matched charts attach already verified (Pi default in
  `scripts/install-pi.sh`). Charts can attach while media is still
  `downloading` — `DownloadManager.on_available` re-binds them once the
  file lands.
- Deploy rsync excludes `chart-library`, `artwork`, `media/yt-*`, `.venv`,
  `__pycache__` — without those, `--delete` wipes the Pi's chart library
  and downloads on every deploy.
- Artwork: iTunes→Deezer lookup cached in `artwork/`, served `/api/art/`;
  backfill via `POST /api/catalog/backfill-art` (host).
- Env: `UPS_MIC_DEVICE` (default `default`), `UPS_DIFFICULTY` (0..2),
  `UPS_CHART_LIB`, `UPS_ARTWORK_DIR`.
- Project license is GPL-3.0 (user decision — ports from GPL-3.0 WorldParty).
  Port inventory lives in `NOTICE.md`.
