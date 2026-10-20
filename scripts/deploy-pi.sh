#!/usr/bin/env bash
# Deploy Ultra Pi Star to a Raspberry Pi over SSH (Windows/git-bash friendly).
#
#   PI_PASS=<password> ./scripts/deploy-pi.sh [pi-hostname-or-ip] [pi-user]
#
# Uses OpenSSH with SSH_ASKPASS so password auth works non-interactively.
set -euo pipefail

PI_HOST="${1:-utra-pi-star.local}"
PI_USER="${2:-jaison}"
PI_PASS="${PI_PASS:?Set PI_PASS to the Pi account password}"
REMOTE_DIR=/tmp/ultrapistar-src
SRC="$(cd "$(dirname "$0")/.." && pwd)"
TARBALL=/tmp/ultrapistar.tar.gz

# askpass shim so `ssh`/`scp` can supply the password without a TTY
ASKPASS="$(mktemp /tmp/ups-askpass.XXXX.sh)"
printf '#!/bin/sh\necho "%s"\n' "$PI_PASS" > "$ASKPASS"
chmod +x "$ASKPASS"
trap 'rm -f "$ASKPASS" "$TARBALL"' EXIT
export SSH_ASKPASS="$ASKPASS" SSH_ASKPASS_REQUIRE=force
SSH_OPTS="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=5"

echo "==> Waiting for $PI_HOST"
until ping -n 1 -w 2000 "$PI_HOST" >/dev/null 2>&1; do sleep 3; done
echo "    host is up; waiting for sshd"
until ssh $SSH_OPTS "$PI_USER@$PI_HOST" "true" 2>/dev/null; do sleep 3; done
echo "    ssh ready"

echo "==> Packaging $SRC"
tar -czf "$TARBALL" -C "$SRC" \
    --exclude='.git' --exclude='data' --exclude='.pipconf' \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='media/yt-*' \
    --exclude='research-worldparty' --exclude='graphify-out' \
    --exclude='.pytest_cache' --exclude='models' .

echo "==> Uploading"
scp $SSH_OPTS "$TARBALL" "$PI_USER@$PI_HOST:/tmp/ultrapistar.tar.gz"

echo "==> Installing on the Pi"
ssh $SSH_OPTS "$PI_USER@$PI_HOST" "PI_PASS='$PI_PASS' bash -s" <<'EOS'
set -e
rm -rf /tmp/ultrapistar-src && mkdir -p /tmp/ultrapistar-src
tar -xzf /tmp/ultrapistar.tar.gz -C /tmp/ultrapistar-src
cd /tmp/ultrapistar-src
# normalize line endings in case the source came from a Windows checkout
find . -name '*.sh' -exec sed -i 's/\r$//' {} +
bash scripts/install-pi.sh
EOS

echo ""
echo "==> Done. Open http://$PI_HOST:5000/tv on the Pi display"
echo "    Phones join at http://$PI_HOST:5000/"
