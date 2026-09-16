#!/usr/bin/env bash
set -Eeuo pipefail
export GIT_SSH_COMMAND='ssh -o BatchMode=yes -o ConnectTimeout=15'
APP_DIR=/root/ssc-bot
STATE_DIR=/var/lib/ssc-bot-autodeploy
mkdir -p "$STATE_DIR"
exec 9>"$STATE_DIR/lock"
flock -n 9 || exit 0
cd "$APP_DIR"
[[ "$(git branch --show-current)" == main ]] || { echo 'Blocked: checkout is not main'; exit 1; }
git diff --quiet && git diff --cached --quiet || { echo 'Blocked: tracked files have local edits'; exit 1; }
git fetch origin main
target=$(git rev-parse origin/main)
deployed=$(<"$STATE_DIR/deployed")
force=${1:-}
if [[ "$target" == "$deployed" && "$force" != --force ]]; then exit 0; fi
if [[ -f "$STATE_DIR/failed" && "$force" != --force ]]; then
    failed=$(<"$STATE_DIR/failed")
    [[ "$failed" != "$target" ]] || exit 0
fi
git merge --ff-only origin/main
[[ "$(git rev-parse HEAD)" == "$target" ]] || { echo 'Blocked: local main is ahead of GitHub'; exit 1; }
trap 'printf "%s\n" "$target" > "$STATE_DIR/failed"; echo "Deployment failed for $target; inspect journal. Retry manually with --force."' ERR
echo "Deploying $target"
docker compose build ssc-bot
docker compose run --rm -v "$APP_DIR/tests:/app/tests:ro" ssc-bot python -m unittest discover -s tests
docker compose up -d --no-build --force-recreate ssc-bot
for attempt in {1..20}; do
    health=$(docker inspect --format '{{.State.Health.Status}}' ssc-telegram-bot)
    if [[ "$health" == healthy ]]; then
        printf '%s\n' "$target" > "$STATE_DIR/deployed"
        echo "Deployment healthy: $target"
        exit 0
    fi
    sleep 3
done
echo 'Container failed its health check; inspect docker logs.'
false
