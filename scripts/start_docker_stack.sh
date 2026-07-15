#!/bin/bash
#
# Boot-time bring-up for the Postgres + Qdrant containers.
#
# Why this exists: after a full macOS reboot, Docker Desktop's containers
# were found (via a real reboot test) to NOT reliably come back on their
# own, even with `restart: unless-stopped` in docker-compose.yml — the VM
# teardown during shutdown leaves containers in an abnormal exited state
# that Docker Desktop doesn't always auto-resume after the next boot.
#
# This script is the explicit, provable fix: wait for the Docker daemon to
# actually be responsive (Docker Desktop itself can take a while to start
# after login), then bring the compose stack up. Safe to run repeatedly —
# `docker compose up -d` is a no-op if everything is already running.
#
# Run at every login via com.vcscouting.dockerstack.plist (RunAtLoad).

PROJECT_DIR="/Users/gthubaiuser/vc-scouting-system/vc-scouting-system"
LOG_FILE="$PROJECT_DIR/logs/docker_stack.log"
MAX_WAIT_SECONDS=180
WAITED=0

echo "[$(date '+%Y-%m-%d %H:%M:%S')] start_docker_stack.sh starting" >> "$LOG_FILE"

while ! docker info >/dev/null 2>&1; do
    if [ "$WAITED" -ge "$MAX_WAIT_SECONDS" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Docker daemon did not come up after ${MAX_WAIT_SECONDS}s — giving up" >> "$LOG_FILE"
        exit 1
    fi
    sleep 3
    WAITED=$((WAITED + 3))
done

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Docker daemon ready after ${WAITED}s — bringing up the stack" >> "$LOG_FILE"

cd "$PROJECT_DIR" || exit 1
/usr/local/bin/docker compose up -d >> "$LOG_FILE" 2>&1 || /opt/homebrew/bin/docker compose up -d >> "$LOG_FILE" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] docker compose up -d finished" >> "$LOG_FILE"
