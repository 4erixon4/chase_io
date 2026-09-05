#!/usr/bin/env bash
# install_reaper.sh — install the TorBox reaper as a systemd timer on the VPS.
#
# Idempotent. Run as root from the repo root on the VPS:
#     sudo bash scripts/install_reaper.sh                 # live (DRY_RUN=false)
#     sudo DRY_RUN=true bash scripts/install_reaper.sh    # first-run safety
#
# It:
#   - reads TORBOX_API_KEY from the repo-root .env,
#   - auto-discovers Radarr/Sonarr API keys from their running containers,
#   - writes /opt/appdata/torbox-reaper/reaper.env (chmod 600, secrets),
#   - installs torbox-reaper.{service,timer} (hourly, gated on mnt-box.mount),
#   - enables the timer and runs the reaper once.
#
# The reaper only deletes a TorBox item once the *arr reports it imported AND the
# file exists under MEDIA_ROOT, so a live run can't race an in-progress import.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO/.env"
APPDIR="/opt/appdata/torbox-reaper"
UNIT_ENV="$APPDIR/reaper.env"

[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE"; exit 1; }

API_HOST="${API_HOST:-$(tailscale ip -4 2>/dev/null | head -n1)}"
[ -n "$API_HOST" ] || { echo "could not determine API_HOST (tailscale ip -4)"; exit 1; }
DRY_RUN="${DRY_RUN:-false}"
MOUNT_UNIT="$(systemctl list-units --type=mount --all --no-legend | awk '/\/mnt\/box/{print $1; exit}')"
MOUNT_UNIT="${MOUNT_UNIT:-mnt-box.mount}"

cid() { docker ps -q -f "label=com.docker.compose.service=$1"; }
arr_key() { docker exec "$(cid "$1")" sh -c 'grep -o "<ApiKey>[^<]*</ApiKey>" /config/config.xml | sed "s/<[^>]*>//g"'; }

TORBOX_API_KEY="$(grep -E '^TORBOX_API_KEY=' "$ENV_FILE" | cut -d= -f2-)"
RADARR_API_KEY="$(arr_key radarr)"
SONARR_API_KEY="$(arr_key sonarr)"
[ -n "$TORBOX_API_KEY" ] || { echo "TORBOX_API_KEY not set in .env"; exit 1; }

mkdir -p "$APPDIR"
umask 077
cat > "$UNIT_ENV" <<EOF
TORBOX_API_KEY=$TORBOX_API_KEY
RADARR_URL=http://$API_HOST:7878
RADARR_API_KEY=$RADARR_API_KEY
SONARR_URL=http://$API_HOST:8989
SONARR_API_KEY=$SONARR_API_KEY
MEDIA_ROOT=/mnt/box/media
MIN_GRACE_MINUTES=60
DRY_RUN=$DRY_RUN
EOF
chmod 600 "$UNIT_ENV"

cat > /etc/systemd/system/torbox-reaper.service <<EOF
[Unit]
Description=Reap imported torrents from TorBox
After=network-online.target $MOUNT_UNIT
Requires=$MOUNT_UNIT

[Service]
Type=oneshot
EnvironmentFile=$UNIT_ENV
ExecStart=/usr/bin/python3 $REPO/scripts/torbox-reaper.py
EOF

cat > /etc/systemd/system/torbox-reaper.timer <<'EOF'
[Unit]
Description=Run TorBox reaper hourly

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now torbox-reaper.timer
echo "installed: timer enabled, DRY_RUN=$DRY_RUN, mount=$MOUNT_UNIT, API_HOST=$API_HOST"
echo "--- running once now ---"
systemctl start torbox-reaper.service
journalctl -u torbox-reaper.service --no-pager -n 25 -o cat
