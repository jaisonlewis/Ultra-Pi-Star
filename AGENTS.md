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
- Station staging: work happens in `<root>/incomplete/` (downloads
  resume there across restarts); only fully-processed songs land in
  the root's artist/title folders, so the finished tree is always
  safe to copy. `_unsorted/` quarantines un-manifested strays.
- Media layout: prepared roots organize per song —
  `<root>/<artist_slug>/<title_slug>/yt-<id>.mp4` (+ `.inst.mp4`,
  `.vocals.wav`, `.analysis.json`, `.jpg`, `<name>.upsong`).
  `mediastore.song_reldir()` is THE convention — station pipeline,
  `imports` (push bundles re-organize on receipt), boot-time
  `organize()`, and `station.organize` (one-shot migration of flat
  roots; also a GUI button) all share it. Manifest paths are
  root-relative so both Pi and Switch resolve them identically.
- Library hygiene tools: `python -m station.audit <root>` checks every
  folder for media/inst/vocals/analysis/art/chart plus nonblank lyrics
  and ASR-fallback provenance (`--json` for a report).
  `python -m station.rechart <root> --asr` retries only charts whose
  lyrics were ASR-generated; `--fix-meta` adopts the chosen lyric
  record's canonical artist/title (guarded by `_meta_ok` — rejects
  same-title-different-song picks and channel-prefixed records).
  `python -m station.fixmeta <root>` heals video-upload metadata
  offline (channel artist + "Artist - Title" in the title field).
  `python -m station.prune <root>` removes full albums, live/non-
  official footage, and ASR-only/obscure entries — folders move to
  `_removed/` (invisible to manifest + Switch scans, recoverable).
  After any metadata change run `station.organize` to rename dirs.
- Media roots: `media_dir` is the write target; `media_extra_dirs` are
  read fallbacks. `config.media_path(rel)` resolves across all roots —
  always use it for entry-relative reads (`local_path`, `inst_path`,
  `*.vocals.wav`), never `Path(config.media_dir) / rel`. A USB stick
  with an `ultrapistar/` dir or `.upsmedia` marker becomes the write
  root at boot (scanned under /media, /run/media, /mnt; `UPS_NO_USB=1`
  disables, `UPS_MEDIA_DIR` overrides, `UPS_MEDIA_DIRS` adds roots).
  Derived files (stems, karaoke mp4s) are written next to their source
  via `media.parent`, so they resolve on the same root as the original.
- Project license is GPL-3.0 (user decision — ports from GPL-3.0 WorldParty).
  Port inventory lives in `NOTICE.md`.
