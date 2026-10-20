# Ultra Pi Star — Phased Plan

**Status:** Planning only. No product code has been written.  
**Goal:** Define Ultra Pi Star, a fully open-source Raspberry Pi karaoke application that combines PiKaraoke’s room, queue and remote-control experience with UltraStar Deluxe’s chart-based singing game.

## Product decisions already established

- The deliverable is one new application, not PiKaraoke and UltraStar Deluxe running side by side.
- Raspberry Pi drives the display and audio/video playback.
- Phones join over the local network to search and queue songs. Phones are not microphones.
- Microphones connect directly to the Pi to keep scoring input local and responsive.
- The song’s video supplies the music and background. Where a verified UltraStar chart is available, the app overlays timed lyrics and pitch feedback and enables scoring. Without a verified chart, it plays the video as ordinary karaoke without scoring.
- Preserve multiplayer, duet and party-style play where the chart and chosen mode support them.
- Treat YouTube access as a replaceable provider. Use only the access and playback method cleared for the intended use.

## Phase 0 — Source baselining (complete)

Cloned both upstream repositories for read-only research. These are research checkouts, not a product repository:

| Project | Upstream | Audited snapshot |
|---|---|---|
| PiKaraoke | [vicwomg/pikaraoke](https://github.com/vicwomg/pikaraoke) | `e4b734c` |
| UltraStar Deluxe | [UltraStar-Deluxe/USDX](https://github.com/UltraStar-Deluxe/USDX) | `05a3cb3` |

The current clones are in the scratch workspace under `research-pikaraoke/` and `research-usdx/`. Record upstream commit IDs again whenever the source audit is refreshed.

## Phase 1 — Source and feature audit

**Purpose:** Identify which behaviors are worth adopting, which code could be reused, and what should be redesigned for a single integrated app.

1. Trace PiKaraoke’s phone-to-server flow: `routes/search_api.py`, `routes/queue_api.py`, `routes/socket_events.py`, and the search, queue and splash templates/scripts.
2. Trace its operational components: `lib/download_manager.py`, `lib/queue_manager.py`, `lib/playback_controller.py`, `lib/karaoke_database.py`, `lib/library_scanner.py`, `lib/song_manager.py`, and `lib/play_history_manager.py`.
3. Trace UltraStar’s singing path: `src/base/USong.pas` for chart parsing and duet tracks; `URecord.pas` and `UAudioInput_*.pas` for mic capture and pitch analysis; `USingNotes.pas` and `USingScores.pas` for note feedback and scoring; `UParty.pas` and `UPartyTournament.pas` for party play; `UBeatTimer.pas` for timing.
4. Create a feature-to-source matrix. For each feature, mark it as reuse candidate, behavior reference, redesign, or out of scope. Capture dependencies and platform assumptions.
5. Complete a file-level licensing and attribution inventory before copying code. The USDX repository has a GPLv2 license file and some source headers allow GPL version 2 or later; verify the terms for each component. PiKaraoke’s clone has a GPLv3 text at `docs/LICENSE`, but no root license file was observed in the checkout, so determine whether and how that license applies to the application source. Check bundled libraries and assets separately.

**Deliverable / exit gate:** Source audit with file paths, feature recommendations, license evidence and unresolved questions. No code copied before the license gate is resolved.

## Phase 2 — Product requirements and demo scope

Define the user journeys and the smallest convincing demonstration:

- Host starts the Pi, sees a ready screen and QR code, and connects mics.
- Singers scan the QR code, enter a display name, search the catalog and add a song to the shared queue.
- Host can reorder, skip or remove queue entries; the screen clearly shows the next singer/song.
- A charted song plays with synchronized words, pitch lane, per-player scoring and results.
- A duet chart assigns its two vocal tracks to two local mics; multiplayer and party rules are explicit.
- An uncharted song plays without scoring or a fabricated lyrics overlay.
- A song can be matched to a chart only when the match is verified; ambiguous or wrong-version matches require selection or remain unscored.

**Deliverable / exit gate:** Prioritized requirements, a screen inventory, mode rules and demo acceptance checklist. Keep the first demo small enough to show end-to-end behavior with a few cleared test songs.

## Phase 3 — UX/UI design

**Purpose:** Design the living-room experience and phone remote together, while giving each a clear role. This phase produces reviewable designs only; it does not build the interface.

### Screen map

- **TV idle / join screen:** App status, QR code and short join instructions; show the queue and next singer when songs are waiting.
- **Phone home:** Search, local library, queue and “now playing” entry points. Joining should work in a mobile browser without installing an app.
- **Search results:** Artist/title, available video, chart status (scoring available or video-only), duet/multiplayer indicator, artwork when available, and a clear add-to-queue action. Distinguish a verified match from a possible chart match.
- **Song confirmation:** Let the singer add their name and, for duet charts, choose or confirm a vocal part. Make the no-chart/video-only behavior clear before queueing.
- **Phone queue:** Current song, upcoming songs, singer names, position and status. Host controls for reordering, skipping or removing songs should be visibly separate from guest actions.
- **TV pre-song / singer handoff:** Show singer(s), song title, scoring mode, mic/track assignment and a short countdown; allow time to get to the microphones.
- **TV scoring play:** Keep the video visible as the main visual. Place timed lyrics in a stable lower-screen band, with the pitch target and singer feedback close enough to read together. Use separate, consistently colored lanes for duet parts and player scores.
- **TV video-only play:** Show the video and any lyrics already present in it. Do not display pitch lanes, scoreboards or game prompts when no verified chart is attached.
- **Results:** Present per-singer scores, duet/team outcome and a clear next action: return to queue/continue. Avoid blocking the queue for an unnecessarily long results screen.
- **Host setup:** Audio-device selection, mic-to-player assignment, calibration, display/audio sync, network/join information, storage and source settings. Keep setup controls off the normal guest path.

### Visual and interaction rules

- Design the TV screen for people standing several metres away: large type, strong contrast, restrained text and safe margins for overscan. Keep lyrics readable over varied video backgrounds using a backing panel or shadow treatment.
- Keep the video dominant. The lyric and scoring overlay should be visually coherent with the video, not cover faces or important action, and remain in fixed positions so singers can follow it.
- Give each player/duet part a persistent color plus a name or icon; never rely on color alone. Keep pitch feedback, lyric part and score identity consistent across play and results.
- Use distinct states for loading, buffering, download progress, missing chart, uncertain chart match, mic disconnected, calibration needed, playback error, pause and end-of-song. Explain what the singer or host can do next.
- Make mobile actions thumb-friendly, with clear feedback after queueing and no dependence on hover. Provide a host-only path for disruptive controls, with confirmation for clearing a queue or deleting library entries.
- Support scalable text, keyboard/controller navigation on the TV, reduced motion, localization and accessible contrast. Provide an option to hide score feedback while retaining lyrics if the group wants casual singing.

### UX/UI deliverables

1. Information architecture and navigation map for TV, phone and host setup.
2. Low-fidelity wireframes for idle/join, phone search/result, queue, pre-song, scored play, video-only play, results and mic calibration.
3. A visual system: typography sizes for TV distance and mobile, player/duet colors, overlay opacity, spacing, icons and error/status patterns.
4. Clickable or annotated screen flow showing guest and host paths, including chart/no-chart branching and duet assignment.
5. Short usability review with at least one first-time phone user and one singer viewing the TV screen from the intended distance.

**Deliverable / exit gate:** UX/UI specification and screen mockups reviewed against the demo checklist before implementation begins.

## Phase 4 — Architecture and risk decisions

Design the integrated app around a single local service and a TV player, with phone clients on the LAN. Keep the YouTube/catalog provider replaceable and the song queue, chart parser, player and scoring engine behind clear interfaces.

Resolve these design questions before implementation:

1. **Playback clock:** The scorer and lyric renderer must use the same authoritative media clock. Test whether a browser video player's playback timestamp can be reported reliably to the local scoring engine, or whether the kiosk player should own playback and expose its clock.
2. **Chart/video sync:** Store sync data against a specific video/source ID and chart revision, not merely artist/title. Support an adjustable offset and verify whether drift correction is needed. A chart made for a different recording may not stay synchronized with the selected video.
3. **Mic pipeline:** Decide supported USB/audio interfaces, input assignment per singer, pitch-detection method, calibration procedure and acceptable latency. Keep mic audio local to the Pi.
4. **Rendering:** Decide whether the full-screen experience is browser-based or native. Ensure the YouTube/local video, lyrics, pitch lanes, score and duet tracks can be composed without stutter on the target Pi.
5. **State and storage:** Define persistent records for catalog entries, queue items, performers, chart matches, source IDs, sync calibration, permissions/license notes, play history and scores.
6. **Resilience and access:** Plan for Wi-Fi dropouts, downloads that fail, corrupted charts, browser/player restarts, host controls and guest-versus-admin permissions.
7. **Open-source terms:** Select the project license after Phase 1. Keep notices, source offers, third-party attributions and asset licenses in the planned distribution.

**Deliverable / exit gate:** Architecture decision record, risk register and a small technical-spike plan. This phase is design only until implementation is separately authorized.

## Phase 5 — Song catalog, charts and artwork

Design ingestion around a canonical song record that can associate one or more video/source entries with optional charts and artwork. Index UltraStar `.txt` charts by artist, title, language, duet/player tracks and source. Keep source URL/repository, revision, license evidence and match confidence.

Artwork must be sourced from the song package or another permitted source and tracked independently. Chart-only repositories do not guarantee cover art or backgrounds. Do not treat a YouTube thumbnail or a repository license label as blanket permission for other material.

**Deliverable / exit gate:** Catalog schema, source allowlist, chart-match policy, artwork policy and a sample catalog using only material cleared for the demo.

## Phase 6 — Demo and product acceptance plan

Before building, define observable pass/fail checks for:

- QR join and phone queue management from at least two phones.
- Local mic capture and calibration for each singer.
- Charted single-player scoring, duet track assignment and agreed multiplayer/party modes.
- Stable lyric and score alignment through the whole song.
- Uncharted-video fallback with no score overlay.
- Queue persistence, download/error handling, and safe host controls.
- Target Raspberry Pi model, OS image, audio hardware, display resolution and startup time.

**Deliverable / exit gate:** Demo script, hardware bill of materials and acceptance checklist that can be shown to a reviewer before implementation begins.

## Future implementation phases (not started)

Implementation starts only after the planning gates are accepted. These are planned delivery stages, not authorization to begin product development. Every phase must end with a runnable demonstration on the target Raspberry Pi and a recorded pass/fail acceptance check before the next phase begins.

### Implementation Phase 1 — App shell, catalog and phone remote

Build the integrated app shell, persistent song/catalog database, TV idle/join screen, QR-based phone connection, mobile search/library views and shared queue. Use a small set of cleared sample records and local test media; no scoring is required yet.

**Runnable demo:** Start the app on the Pi, scan its QR code with two phones, search the seeded catalog, add songs with singer names, and manage the queue from the host screen.

**Acceptance checks:** Queue changes appear consistently on all clients; data survives app restart; guests cannot invoke host-only actions; phones are never used as microphones.

### Implementation Phase 2 — Cleared media provider, local library and player

Implement the selected, permitted media access method behind a provider interface. Add local media storage, source provenance, download/ingest status and retry behavior, then connect the shared queue to the full-screen player and phone now-playing state.

**Runnable demo:** Queue a cleared test video from a phone, show ingest or library status, play it full-screen on the Pi, and advance to the next queued item.

**Acceptance checks:** Only the approved provider path is enabled; a local item can be replayed without fetching it again; failures are visible and retryable; playback does not stutter when queue or phone clients update.

### Implementation Phase 3 — UltraStar charts, sync calibration and single-player scoring

Add UltraStar chart parsing and validation, verified chart-to-video association, per-video/chart sync calibration, local mic capture, pitch detection and the single-player lyrics/pitch/score overlay. Implement the video-only path for entries without a verified chart.

**Runnable demo:** Play a cleared charted test song with lyrics and scoring, adjust and save its sync offset, restart it to verify the offset persists, then play an uncharted test video without scoring elements.

**Acceptance checks:** Notes and lyrics remain aligned through the song; unverified/ambiguous chart matches are not scored automatically; the selected direct-connected mic is detected and calibrated; video-only playback shows no pitch lane or score.

### Implementation Phase 4 — Duet, multiple microphones and party modes

Add assignment of chart tracks to singers and microphones, simultaneous or turn-taking play as supported by the chart/mode, player-specific scoring, team rounds and the agreed party modes. Extend setup for mic assignment and per-input calibration.

**Runnable demo:** Complete a two-singer duet using two Pi-connected microphones, then play one agreed team/party mode with at least two teams and show the round and final results.

**Acceptance checks:** Each mic is routed to the intended player; duet lyrics and scoring stay on the correct track; one player’s input does not corrupt another’s score; party transitions, skips and result screens remain consistent.

### Implementation Phase 5 — Artwork/catalog polish, robustness and Raspberry Pi packaging

Complete permitted artwork and metadata handling, catalog matching and filtering, host/guest UX polish, storage management, error recovery, logs, startup behavior, configuration and install/upgrade packaging for the selected Pi OS and hardware.

**Runnable demo:** Install from a clean supported Pi image, boot into the karaoke experience, run a short mixed charted/video-only session, recover from a simulated network or media failure, and update the installation.

**Acceptance checks:** Artwork has recorded provenance and permission; catalog and queue remain usable after restart; the app recovers cleanly from the defined failures; package install, launch, update and uninstall steps work on the target Pi; the full demo acceptance checklist passes.

## Initial source notes

- PiKaraoke separates search/queue HTTP routes, real-time socket events, download management, playback control and persistent song/session data. Its current project uses Python, Flask-SocketIO, yt-dlp and FFmpeg. Good behaviors to study include QR-based phone joining, shared queue updates, download status/retries, fair queueing, host controls and session history.
- USDX has reusable conceptual boundaries around chart parsing, microphone input, scoring feedback, player tracks and party rounds. Its development notes describe SDL2 for window/input/audio devices, OpenGL rendering, FFmpeg media handling and SQLite song/score storage.
- The UltraStar file format is an interoperability asset in its own right. Prefer reading the documented format and preserving compatibility over inventing a proprietary chart format.
