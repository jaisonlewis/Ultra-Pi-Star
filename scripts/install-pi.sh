#!/usr/bin/env bash
# Ultra Pi Star — Raspberry Pi installer.
# Run on the Pi:  curl -sSL <repo>/install-pi.sh | bash   (or copy + bash)
# Installs deps, deploys the app to /opt/ultrapistar, and registers a
# systemd service that boots straight into the karaoke experience.
set -euo pipefail

APP_DIR=/opt/ultrapistar
SERVICE=ultrapistar
PORT="${UPS_PORT:-5000}"

# passworded sudoc when PI_PASS is provided (non-interactive deploys)
sudoc() {
  if [ -n "${PI_PASS:-}" ]; then
    echo "$PI_PASS" | sudo -S "$@"
  else
    sudo "$@"
  fi
}

echo "==> Installing system packages"
sudoc apt-get update -qq
sudoc apt-get install -y -qq python3 python3-venv python3-pip ffmpeg alsa-utils

echo "==> Installing Raspberry Pi Connect (remote access)"
# full package on desktop images (screen share + shell); lite variant on
# Lite images (remote shell only) — apt picks whichever exists
sudoc apt-get install -y -qq rpi-connect 2>/dev/null \
  || sudoc apt-get install -y -qq rpi-connect-lite
sudoc rpi-connect on 2>/dev/null || true

echo "==> Deploying app to $APP_DIR"
sudoc mkdir -p "$APP_DIR"
sudoc chown "$USER:$USER" "$APP_DIR"

# If this script is run from inside a cloned/copied repo, use it in place;
# otherwise fetch from git remote.
if [ -f "$(dirname "$0")/../run.py" ]; then
    SRC="$(cd "$(dirname "$0")/.." && pwd)"
else
    SRC="$APP_DIR"
fi
rsync -a --delete \
    --exclude data --exclude '.git' \
    --exclude '.venv' --exclude '__pycache__' \
    --exclude 'chart-library' --exclude 'artwork' \
    --exclude 'media' --exclude 'charts' --exclude 'models' \
    --exclude 'research-worldparty' --exclude 'graphify-out' \
    --exclude '.pytest_cache' \
    "$SRC"/ "$APP_DIR"/ 2>/dev/null \
  || cp -r "$SRC"/. "$APP_DIR"/
# media/ and charts/ are runtime data (downloads, uploads, user drops) —
# a deploy must never delete them. The bundled demo assets are copied
# without -u so existing files are left alone.
mkdir -p "$APP_DIR/media" "$APP_DIR/charts"
cp -n "$SRC"/media/demo-* "$APP_DIR/media/" 2>/dev/null || true
cp -n "$SRC"/charts/*.txt "$APP_DIR/charts/" 2>/dev/null || true

echo "==> Python environment"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> ML models (separation + pitch)"
MODELS="$APP_DIR/models"
mkdir -p "$MODELS"
if [ ! -f "$MODELS/rmvpe.onnx" ]; then
    "$APP_DIR/.venv/bin/rmvpe-onnx" download --model "$MODELS/rmvpe.onnx" \
        || echo "  rmvpe download failed — pitch features disabled"
fi
if [ ! -f "$MODELS/sherpa-onnx-spleeter-2stems-int8/vocals.int8.onnx" ]; then
    curl -sL -o /tmp/spleeter.tar.bz2 \
      "https://github.com/k2-fsa/sherpa-onnx/releases/download/source-separation-models/sherpa-onnx-spleeter-2stems-int8.tar.bz2" \
      && tar xjf /tmp/spleeter.tar.bz2 -C "$MODELS" \
      || echo "  spleeter download failed — vocal cut disabled"
    rm -f /tmp/spleeter.tar.bz2
fi

echo "==> systemd service"
UNIT_TMP="$(mktemp)"
cat > "$UNIT_TMP" <<EOF
[Unit]
Description=Ultra Pi Star karaoke
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
Group=$USER
WorkingDirectory=$APP_DIR
Environment=UPS_PORT=$PORT
Environment=PYTHONUNBUFFERED=1
Environment=UPS_TRUST_SOURCES=${UPS_TRUST_SOURCES:-usdx-songs}
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/run.py
# cap the app at 3 of 4 cores: queue-time separation/ASR/pitch inference
# saturating all cores next to Chromium's video decode was resetting the
# box (undervoltage/thermal). One guaranteed-free core keeps the TV,
# kernel threads and sshd responsive through the heaviest worker phase.
CPUAffinity=0-2
Nice=5
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudoc install -m644 "$UNIT_TMP" /etc/systemd/system/$SERVICE.service
rm -f "$UNIT_TMP"

# service runs as $USER — runtime dirs must be writable by it even if a
# previous root-run service created them
sudoc chown -R "$USER:$USER" "$APP_DIR/data" "$APP_DIR/media" \
  "$APP_DIR/charts" "$APP_DIR/chart-library" 2>/dev/null || true

# Remove a legacy WirePlumber rule that disabled ACP on vc4-hdmi cards.
# It left the card with only off/pro-audio profiles — and the raw hw
# path on this driver is IEC958-locked, so no usable sink ever appeared.
# With ACP enabled the card gets output:hdmi-stereo, which uses the
# hdmi: plugin path that handles PCM correctly.
rm -f "$HOME/.config/wireplumber/wireplumber.conf.d/50-alsa-hdmi.conf"

sudoc systemctl daemon-reload
sudoc systemctl enable $SERVICE
# stop before the wireplumber restart: a running service may hold USB/HDMI
# devices exclusively, which would block WirePlumber's card re-probe
sudoc systemctl stop $SERVICE 2>/dev/null || true
systemctl --user restart wireplumber 2>/dev/null || true
sleep 2
sudoc systemctl start $SERVICE

# kiosk: a systemd USER service supervises Chromium — waits for the app
# before first launch and restarts automatically if the browser dies or
# hangs (the app also restarts it when the TV socket goes silent).
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/ultrapistar-kiosk.service" <<'EOF'
[Unit]
Description=Ultra Pi Star kiosk browser
After=graphical-session.target

[Service]
ExecStartPre=/bin/sh -c 'until curl -sf -o /dev/null http://localhost:5000/tv; do sleep 1; done'
ExecStart=/usr/bin/chromium --kiosk --noerrdialogs --disable-infobars --password-store=basic --autoplay-policy=no-user-gesture-required --start-fullscreen --disable-dev-shm-usage http://localhost:5000/tv
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload 2>/dev/null || true
systemctl --user enable ultrapistar-kiosk 2>/dev/null || true

# labwc autostart just kicks the supervised unit — the service owns the
# browser lifecycle from then on (survives crashes, app-driven restarts)
mkdir -p "$HOME/.config/labwc"
cat > "$HOME/.config/labwc/autostart" <<'EOF'
#!/bin/sh
systemctl --user start ultrapistar-kiosk
EOF
chmod +x "$HOME/.config/labwc/autostart"

IP=$(hostname -I | awk '{print $1}')
echo ""
echo "=============================================="
echo " Ultra Pi Star installed and running"
echo "   TV screen : http://localhost:$PORT/tv"
echo "   Phone join: http://$IP:$PORT/"
echo "   Host key  : check 'journalctl -u $SERVICE -n 15'"
echo "   Kiosk     : labwc autostart waits for the service, then opens"
echo "               chromium --kiosk http://localhost:$PORT/tv"
echo ""
echo " Remote access: run 'rpi-connect signin' and open the link it"
echo " prints to link this Pi to your Raspberry Pi ID, then reach it"
echo " from anywhere at https://connect.raspberrypi.com"
echo "=============================================="
