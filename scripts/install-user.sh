#!/usr/bin/env bash
# Ultra Pi Star — lightweight install (no appliance takeover).
#
# Use this when you want karaoke sessions ON DEMAND on a Pi you also use
# for other things, instead of a dedicated karaoke appliance.
#
#   curl -sSL https://raw.githubusercontent.com/jaisonlewis/Ultra-Pi-Star/AI/scripts/install-user.sh | bash
#   — or —  git clone <repo> && bash scripts/install-user.sh
#
# What this does NOT do (vs install-pi.sh, the appliance install):
#   - no system service at boot: a per-user systemd unit is installed but
#     left DISABLED — opt in with `systemctl --user enable ultrapistar`
#   - no Chromium kiosk, no desktop autostart changes, no rpi-connect
#   - app lives in ~/ultrapistar (override with UPS_APP_DIR), not /opt
set -euo pipefail

APP_DIR="${UPS_APP_DIR:-$HOME/ultrapistar}"
SERVICE=ultrapistar
PORT="${UPS_PORT:-5000}"
REPO_URL=https://github.com/jaisonlewis/Ultra-Pi-Star.git

# passworded sudoc when PI_PASS is provided (non-interactive installs)
sudoc() {
  if [ -n "${PI_PASS:-}" ]; then
    echo "$PI_PASS" | sudo -S "$@"
  else
    sudo "$@"
  fi
}

echo "==> Installing system packages (the only step needing sudo)"
sudoc apt-get update -qq
sudoc apt-get install -y -qq python3 python3-venv python3-pip ffmpeg alsa-utils

echo "==> Deploying app to $APP_DIR"
mkdir -p "$APP_DIR"
if [ -f "$(dirname "$0")/../run.py" ]; then
    SRC="$(cd "$(dirname "$0")/.." && pwd)"
elif [ ! -f "$APP_DIR/run.py" ]; then
    echo "==> Fetching source from GitHub"
    sudoc apt-get install -y -qq git
    CLONE="$(mktemp -d)"
    git clone --depth 1 "$REPO_URL" "$CLONE/repo"
    rsync -a "$CLONE/repo"/ "$APP_DIR"/ 2>/dev/null \
      || cp -r "$CLONE/repo"/. "$APP_DIR"/
    rm -rf "$CLONE"
    SRC="$APP_DIR"
else
    SRC="$APP_DIR"
fi
if [ "$SRC" != "$APP_DIR" ]; then
    rsync -a --delete \
        --exclude data --exclude '.git' \
        --exclude '.venv' --exclude '__pycache__' \
        --exclude 'chart-library' --exclude 'artwork' \
        --exclude 'media' --exclude 'charts' --exclude 'models' \
        --exclude 'research-worldparty' --exclude 'graphify-out' \
        --exclude '.pytest_cache' \
        "$SRC"/ "$APP_DIR"/ 2>/dev/null \
      || cp -r "$SRC"/. "$APP_DIR"/
fi
mkdir -p "$APP_DIR/media" "$APP_DIR/charts"
cp -n "$SRC"/media/demo-* "$APP_DIR/media/" 2>/dev/null || true
cp -n "$SRC"/charts/*.txt "$APP_DIR/charts/" 2>/dev/null || true

# cap the heavy steps at 3 of 4 cores — saturating all cores next to a
# running system was resetting the box (undervoltage/thermal). Harmless
# on boxes where ultrapistar isn't already installed.
CAP="taskset -c 0-2 nice"
command -v taskset >/dev/null || CAP="nice"

echo "==> Python environment"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
$CAP "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> ML models (separation + pitch)"
MODELS="$APP_DIR/models"
mkdir -p "$MODELS"
if [ ! -f "$MODELS/rmvpe.onnx" ]; then
    $CAP "$APP_DIR/.venv/bin/rmvpe-onnx" download --model "$MODELS/rmvpe.onnx" \
        || echo "  rmvpe download failed — pitch features disabled"
fi
if [ ! -f "$MODELS/sherpa-onnx-spleeter-2stems-int8/vocals.int8.onnx" ]; then
    curl -sL -o /tmp/spleeter.tar.bz2 \
      "https://github.com/k2-fsa/sherpa-onnx/releases/download/source-separation-models/sherpa-onnx-spleeter-2stems-int8.tar.bz2" \
      && $CAP tar xjf /tmp/spleeter.tar.bz2 -C "$MODELS" \
      || echo "  spleeter download failed — vocal cut disabled"
    rm -f /tmp/spleeter.tar.bz2
fi

# Remove a legacy WirePlumber rule that disabled ACP on vc4-hdmi cards —
# it leaves the HDMI card with no usable sink. Harmless if absent.
rm -f "$HOME/.config/wireplumber/wireplumber.conf.d/50-alsa-hdmi.conf"
systemctl --user restart wireplumber 2>/dev/null || true

echo "==> User service (installed, NOT enabled)"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/$SERVICE.service" <<EOF
[Unit]
Description=Ultra Pi Star karaoke (user service)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=UPS_PORT=$PORT
Environment=PYTHONUNBUFFERED=1
Environment=UPS_TRUST_SOURCES=${UPS_TRUST_SOURCES:-usdx-songs}
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/run.py
# cap the app at 3 of 4 cores: queue-time separation/ASR/pitch inference
# saturating all cores was resetting the box (undervoltage/thermal)
CPUAffinity=0-2
Nice=5
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload 2>/dev/null || true

IP=$(hostname -I | awk '{print $1}')
echo ""
echo "=============================================="
echo " Ultra Pi Star installed to $APP_DIR"
echo ""
echo "   Start now   : $APP_DIR/.venv/bin/python $APP_DIR/run.py"
echo "   As service  : systemctl --user start $SERVICE"
echo "   Stop        : systemctl --user stop $SERVICE"
echo "   Autostart   : systemctl --user enable $SERVICE"
echo "                 (add 'loginctl enable-linger \$USER' to run"
echo "                  at boot without logging in)"
echo ""
echo "   TV screen : open http://localhost:$PORT/tv in any browser"
echo "   Phone join: http://$IP:$PORT/"
echo "   Host key  : printed at startup, or"
echo "               'journalctl --user -u $SERVICE -n 15'"
echo "=============================================="
