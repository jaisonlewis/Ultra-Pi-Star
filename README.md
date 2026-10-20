# Ultra Pi Star

Open-source Raspberry Pi karaoke appliance: boots straight into a TV
karaoke experience — no keyboard, no setup. Phones join over the LAN via
QR code or room code to search, queue, and sing. PiKaraoke-style remote +
UltraStar-style charted scoring, fully offline once media is cached.
Design history: `Raspberry_Pi_Karaoke_Phased_Plan (1).md`.

## What it does

**TV screen** (`/tv`, Chromium kiosk on the Pi's HDMI display)
- Idle screen with QR code + upcoming queue
- Full-screen `<video>` playback — the media clock drives lyrics, pitch
  lanes, and scoring
- Pre-song singer handoff with countdown, scored vs. video-only badges
- Audio-only songs (MP3 uploads) get a music-reactive psychedelic
  visualizer rendered on a WebAudio-driven canvas — lyrics and scoring
  still overlay it
- Holds on "Preparing lyrics…" while the chart pipeline finishes, then
  starts playback with everything attached

**Phone remote** (`/`)
- Display-name join, search, library browse, add-to-queue, duet partner +
  vocal-part selection, remove-own-song, per-song vocal-cut toggle
- **Upload your own songs** — MP4 video or MP3 (also m4a/ogg/wav/flac/
  webm/mkv) straight from the phone; artist/title parsed from
  `Artist - Title.ext` filenames, or entered manually

**Host console** (`/host?key=<HOST_KEY>`)
- Reorder/remove/clear queue, skip, pause/resume, volume, live chart
  sync nudge, library management (delete/repair entries), mic assignment
  and FX

**Song acquisition**
- YouTube provider (yt-dlp): search biased toward official/Vevo uploads,
  downloaded once to `media/` (≤1080p, merged mp4) and reused forever
- Uploads land in `media/` as first-class local entries
- Local `.txt` UltraStar chart drops (`charts/`, `chart-library/`) are
  indexed as fallback charts

**The native chart pipeline** (queue-time worker — runs while earlier
songs play)
1. Vocal separation (sherpa-onnx Spleeter int8) → vocal stem + a
   vocal-reduced `*.inst.mp4` karaoke backing
2. Lyrics: LRCLIB **and** SyncLRC fetched, best record picked by ASR
   word-match + duration/span proximity (incomplete records penalized)
3. ASR word timing (needle) anchors the lyric lines
4. RMVPE pitch extraction on the vocal stem → note chart
5. Result: a native `.upsong` chart timed to *that exact media file*
   (imported charts are only a fallback, warped to the media when the
   generator can't produce one)

**Loudness normalization** — every file (raw and karaoke backing) is
two-pass loudnorm'd to −16 LUFS at ingest, so base volume is consistent
across the library and toggling vocal-cut doesn't change the level.

**Microphones & scoring**
- ALSA/PipeWire capture, WorldParty-ported autocorrelation pitch
  detection + note-hit/line-bonus scoring (`pitch.py`, `scoring.py`)
- Live pitch lanes + sung-note trails on the TV, per-singer mic FX
  (pitch, autotune, echo, reverb), latency calibration
- Duets: two singers, per-track scoring

**Self-healing**
- Kiosk liveness watchdog restarts a wedged Chromium (stale positions,
  dead socket, stuck resume)
- "Preparing lyrics…" waits are time-boxed — a wedged pipeline degrades
  to raw playback instead of hanging the queue
- Audio watchdog restarts wireplumber when playing into "Dummy Output"
- Asset fingerprint handshake reloads stale kiosk pages after deploys
- systemd units (`ultrapistar`, `ultrapistar-kiosk`) restart on crash

## Run locally

```bash
pip install -r requirements.txt
python run.py
```

Console prints the TV URL, LAN join URL (encoded in the QR code) and
the host key. Open `/tv` for the TV; phones browse the join URL.
ffmpeg + ffprobe must be on PATH.

## Deploy to a Raspberry Pi

Flash Raspberry Pi OS (Lite or Desktop) with SSH enabled, then:

```bash
PI_PASS=<pi-password> ./scripts/deploy-pi.sh <pi-host-or-ip> <pi-user>
```

The installer sets up `/opt/ultrapistar` with a venv, downloads the
separation + pitch models, and registers:

- `ultrapistar.service` (system) — the app on port 5000, boots with the Pi
- `ultrapistar-kiosk.service` (user) — supervised Chromium kiosk → `/tv`
- labwc autostart → boots straight into karaoke on the HDMI display

Deploys are rsync-safe: `media/`, `charts/`, and `data/` are runtime
data and never wiped — uploaded songs, downloads, and stems persist.

## Layout

```
ultrapistar/
  app.py            Flask+SocketIO assembly, kiosk + audio watchdogs
  config.py         env/YAML config (UPS_*), LAN IP, host key
  db.py             SQLite schema + helpers
  catalog.py        song/media-entry/chart records, search, seeding
  queue_manager.py  queue state machine
  downloads.py      background provider ingest
  providers/        MediaProvider interface: local, youtube, registry
  separate.py       vocal/accompaniment separation, karaoke remux
  songmap.py        ASR transcript → word/line timing maps
  lyrics.py         LRCLIB + SyncLRC fetch, best-record selection
  melody.py         RMVPE pitch curve → notes
  genchart.py       native .upsong chart generation
  charts.py         UltraStar .txt parser (imported charts)
  align.py          chroma chart→media alignment (warp fallback)
  loudness.py       EBU R128 normalization (two-pass loudnorm)
  mic.py            mic capture, pitch gate, FX, calibration
  sing.py           live scoring session (media clock × mic pitch)
  pitch.py/scoring.py  WorldParty-ported pitch detector + scoring
  audio.py          PipeWire output routing
  artwork.py        album art lookup + cache
  web/
    pages.py        / /tv /host /media routes
    api.py          JSON API, uploads, manage endpoints, map worker
    sockets.py      realtime state/playback events
    templates/      phone, tv, host pages
    static/         css, js, vendored socket.io client
media/              local files: downloads, uploads (runtime data)
charts/             UltraStar .txt drop folder (rescanned on boot)
chart-library/      imported chart libraries
data/               sqlite db + config.yaml (runtime)
models/             spleeter int8 + rmvpe.onnx (downloaded by installer)
tests/              pytest suite
scripts/            install-pi.sh, deploy-pi.sh, probes
```

## Config (env vars)

| Var | Default | Purpose |
|---|---|---|
| `UPS_PORT` | 5000 | HTTP port |
| `UPS_HOST` | 0.0.0.0 | bind address |
| `UPS_HOST_KEY` | random per boot | host-console room code |
| `UPS_DATA_DIR` | `data/` | SQLite + config.yaml |
| `UPS_MEDIA_DIR` | `media/` | local media (downloads, uploads) |
| `UPS_CHART_DIR` | `charts/` | `.txt` chart drop folder |
| `UPS_CHART_LIB` | `chart-library/` | imported chart libraries |
| `UPS_MODELS_DIR` | `models/` | separation + pitch models |
| `UPS_PROVIDERS` | `local,youtube` | enabled providers (comma list) |
| `UPS_VOCAL_CUT` | 1 | default vocal-reduced playback when available |
| `UPS_KARAOKE_GUIDE` | 0.15 | vocal bleed-back into karaoke backing |
| `UPS_NORMALIZE` | 1 | loudness-normalize media at ingest |
| `UPS_INTERMISSION_SEC` | 30 | pre-song countdown |
| `UPS_DIFFICULTY` | 0 | scoring strictness |
| `UPS_MIC_DEVICE` / `UPS_PLAY_DEVICE` | default | ALSA devices |
| `UPS_TRUST_SOURCES` | — | chart source_repo tags to auto-verify |
| `UPS_CONFIG_YAML` | `data/config.yaml` | user-editable YAML config |

## Scoring

`ultrapistar/pitch.py` and `ultrapistar/scoring.py` are Python ports of
WorldParty's autocorrelation pitch detector and note-hit/line-bonus
scoring (`URecord.pas`, `UNote.pas`, `UScreenSingController.pas`).
Ported under GPL-3.0 with upstream copyright retained — see `NOTICE.md`.

## Tests

```bash
python -m pytest tests -q
```

## License

GPL-3.0 — see `LICENSE`. Third-party and ported components are
inventoried in `NOTICE.md`.
