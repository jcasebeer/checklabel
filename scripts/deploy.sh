#!/usr/bin/env bash
# Deploy to the VPS over SSH: rsync the source, rebuild, restart.
#
#   DEPLOY_HOST=deploy@1.2.3.4 ./scripts/deploy.sh
#
# Idempotent — run it for every release. Prerequisites on the server are a
# one-time setup (see deploy/README.md): Docker, /opt/label-check owned by the
# deploy user, and a filled-in deploy/.env (never overwritten by this script).
#
# Note: restarting the app forfeits the in-memory manifest state of any
# mode=queued batch still in flight (pollers get a 409 and must resubmit).
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${DEPLOY_HOST:?Set DEPLOY_HOST, e.g. DEPLOY_HOST=deploy@1.2.3.4}"
DIR="${DEPLOY_DIR:-/opt/label-check}"

echo "==> Syncing source to $HOST:$DIR"
rsync -az --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude 'tests/fixtures' \
  --exclude 'deploy/.env' \
  ./ "$HOST:$DIR/"

echo "==> Building and restarting"
ssh "$HOST" "set -e
  cd '$DIR/deploy'
  if [ ! -f .env ]; then
    echo 'ERROR: $DIR/deploy/.env is missing on the server.' >&2
    echo 'Copy .env.example to .env there and fill in the secrets.' >&2
    exit 1
  fi
  docker compose up -d --build
  docker compose ps"

echo "==> Verifying health"
ssh "$HOST" "cd '$DIR/deploy' && for i in \$(seq 1 12); do
    status=\$(docker compose ps --format '{{.Health}}' app 2>/dev/null || true)
    [ \"\$status\" = healthy ] && echo 'app: healthy' && exit 0
    sleep 5
  done
  echo 'app did not become healthy; recent logs:' >&2
  docker compose logs --tail 40 app >&2
  exit 1"

echo "==> Deployed."
