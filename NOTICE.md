# Attribution & License Inventory

Ultra Pi Star is licensed **GPL-3.0** (see `LICENSE`).

## Ported code (derivative works)

The following modules are Python ports of GPL-3.0 code and carry the
upstream copyright of **UltraStar WorldParty** (by the UltraStar España
team, https://github.com/ultrastares/ultrastar-worldparty):

| Ultra Pi Star file | Ported from (WorldParty) | What |
|---|---|---|
| `ultrapistar/pitch.py` | `src/base/URecord.pas` (`TCaptureBuffer`) | autocorrelation pitch detector: 46-halftone C2–A5 scan, volume gate |
| `ultrapistar/scoring.py` | `src/base/UNote.pas` sing-update + `src/screens/controllers/UScreenSingController.pas` | per-beat note-hit logic, octave normalization, note-type score factors, golden-note complementary rounding, line bonus |

WorldParty research snapshot: `research-worldparty/` (read-only reference).

## Research-only references (no code copied)

| Directory | Upstream | License | Snapshot |
|---|---|---|---|
| `research-pikaraoke/` | vicwomg/pikaraoke | MIT | `e4b734c` |
| `research-usdx/` | UltraStar-Deluxe/USDX | GPL-2.0 | `05a3cb3` |
| `research-worldparty/` | ultrastares/ultrastar-worldparty | GPL-3.0 | `master` (shallow) |

## Bundled assets

- `charts/demo-*.txt`, `media/demo-*.mp4` — synthetic demo assets, CC0.
- `static/vendor/socket.io.min.js` — Socket.IO client, MIT.
- `chart-library/usdx-songs/` — razzertronic/usdx-songs chart library,
  Unlicense.

## Third-party media

Songs/charts fetched at runtime (YouTube provider, USDB, user drops) keep
their original copyrights; provenance is recorded per `media_entry` and
`chart` row in the catalog database.
