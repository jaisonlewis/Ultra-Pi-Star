#!/usr/bin/env bash
# Fetch the community UltraStar chart library (razzertronic/usdx-songs,
# Unlicense) into the chart-library dir. UltraStar .txt files only — no
# audio/video; media comes from your own files or the YouTube provider.
set -euo pipefail

TARGET="${1:-$(dirname "$0")/../chart-library}/usdx-songs"
URL="https://github.com/razzertronic/usdx-songs/archive/refs/heads/master.tar.gz"

mkdir -p "$(dirname "$TARGET")"
echo "Fetching $URL -> $TARGET"
curl -fSL "$URL" | tar xz --strip-components=1 -C "$TARGET" --one-top-level="$(basename "$TARGET")" 2>/dev/null \
  || { mkdir -p "$TARGET"; curl -fSL "$URL" | tar xz --strip-components=1 -C "$TARGET"; }
echo "Done: $(find "$TARGET" -name '*.txt' | wc -l) charts indexed on next rescan"
