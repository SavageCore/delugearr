#!/usr/bin/env bash
# Deploy delugearr to the seedbox and enable the service (dry-run ON by default).
# Also removes the legacy deluge-unregistered service/config.
#
# Usage:
#   DELUGE_PASSWORD='...' AUTH_PASSWORD='...' ./deploy.sh
#
# Config is (re)written only when at least one secret is provided; otherwise
# the existing /etc/delugearr/config is left untouched.
set -euo pipefail

SEEDBOX="${SEEDBOX:-root@seedbox.savagecore.uk}"
REMOTE_DIR=/opt/delugearr
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_PATH="${BASE_PATH:-/delugearr}"
PORT="${PORT:-11012}"
DRY_RUN="${DRY_RUN:-1}"
INTERVAL="${SCAN_INTERVAL_MINUTES:-30}"

echo "== Syncing code to ${SEEDBOX}:${REMOTE_DIR}"
ssh "$SEEDBOX" "mkdir -p ${REMOTE_DIR}"
rsync -az --delete \
  --exclude 'venv' --exclude '.venv' \
  --exclude '__pycache__' --exclude '*.pyc' --exclude '.git' \
  --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '.nicegui' \
  --exclude 'tests' \
  "$REPO_DIR/" "$SEEDBOX:${REMOTE_DIR}/"

echo "== Installing dependencies"
ssh "$SEEDBOX" "cd ${REMOTE_DIR} && (test -d venv || python3 -m venv venv) && venv/bin/pip install --quiet --upgrade pip && venv/bin/pip install --quiet ."

echo "== Config"
ssh "$SEEDBOX" "install -d -o savagecore -g savagecore /etc/delugearr"
if [ -n "${DELUGE_PASSWORD:-}" ] || [ -n "${AUTH_PASSWORD:-}" ]; then
  [ -n "${DELUGE_PASSWORD:-}" ] || echo "  WARNING: DELUGE_PASSWORD unset (Deluge access will fail)"
  [ -n "${AUTH_PASSWORD:-}" ] || echo "  WARNING: AUTH_PASSWORD unset (web login will fail)"
  echo "  writing /etc/delugearr/config"
  ssh "$SEEDBOX" "umask 177; cat > /etc/delugearr/config <<EOF
DELUGE_URL=${DELUGE_URL:-http://127.0.0.1:10376}
DELUGE_PASSWORD=${DELUGE_PASSWORD:-}
PORT=${PORT}
BASE_PATH=${BASE_PATH}
CONFIG_PATH=/etc/delugearr
DRY_RUN=${DRY_RUN}
SCAN_INTERVAL_MINUTES=${INTERVAL}
AUTH_USER=${AUTH_USER:-savagecore}
AUTH_PASSWORD=${AUTH_PASSWORD:-}
EOF"
  ssh "$SEEDBOX" "chown savagecore:savagecore /etc/delugearr/config && chmod 600 /etc/delugearr/config"
else
  echo "  no secrets provided - leaving existing config in place"
fi

echo "== Systemd unit"
ssh "$SEEDBOX" "cp ${REMOTE_DIR}/delugearr.service /etc/systemd/system/delugearr.service && systemctl daemon-reload && systemctl enable delugearr && systemctl restart delugearr"

echo "== Removing legacy deluge-unregistered"
ssh "$SEEDBOX" "systemctl disable --now deluge-unregistered 2>/dev/null || true; rm -f /etc/systemd/system/deluge-unregistered.service /etc/nginx/apps/deluge-unregistered.conf; rm -rf /opt/deluge-unregistered /etc/deluge-unregistered; systemctl daemon-reload || true"

echo "== nginx"
ssh "$SEEDBOX" "cp ${REMOTE_DIR}/nginx-delugearr.conf /etc/nginx/apps/delugearr.conf; nginx -t && systemctl reload nginx"

echo "== Health check"
sleep 3
curl -fsS "http://127.0.0.1:${PORT}${BASE_PATH}/api/status" || true
echo
echo "== Done. UI: https://seedbox.savagecore.uk${BASE_PATH}"
