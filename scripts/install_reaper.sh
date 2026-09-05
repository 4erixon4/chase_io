#!/usr/bin/env bash
# install_reaper.sh — install the background maintenance timers on the VPS:
#   1. torbox-reaper   — frees TorBox space by deleting imported torrents.
#   2. watchlist-sync  — makes the Plex watchlist the source of truth: removing a
#                        title from your watchlist deletes it from Radarr/Sonarr
#                        (with files) and drops its TorBox copy.
#
# Idempotent. Run as root from the repo root on the VPS:
#     sudo bash scripts/install_reaper.sh                 # reaper live
#     sudo DRY_RUN=true bash scripts/install_reaper.sh    # reaper dry-run
#
# It:
#   - reads TORBOX_API_KEY from the repo-root .env,
#   - auto-discovers Radarr/Sonarr API keys + the Plex owner token from the
#     running containers,
#   - writes /opt/appdata/torbox-reaper/{reaper.env,watchlist-sync.env} (0600),
#   - installs torbox-reaper.{service,timer} (hourly, gated on mnt-box.mount) and
#     watchlist-sync.{service,timer} (every 15 min),
#   - enables the timers and runs each once.
#
# The reaper only deletes a TorBox item once the *arr reports it imported AND the
# file exists under MEDIA_ROOT, so a live run can't race an in-progress import.
#
# watchlist-sync is DESTRUCTIVE (it deletes media). It installs in DRY_RUN mode
# by default — it only logs "WOULD REMOVE" until you flip it:
#     sudo WATCHLIST_DRY_RUN=false bash scripts/install_reaper.sh
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

plex_token() {
  docker exec "$(cid plex)" sh -c 'cat "/config/Library/Application Support/Plex Media Server/Preferences.xml"' \
    | tr ' ' '\n' | grep -i PlexOnlineToken | cut -d'"' -f2
}

TORBOX_API_KEY="$(grep -E '^TORBOX_API_KEY=' "$ENV_FILE" | cut -d= -f2-)"
RADARR_API_KEY="$(arr_key radarr)"
SONARR_API_KEY="$(arr_key sonarr)"
PLEX_TOKEN="$(plex_token)"
WATCHLIST_DRY_RUN="${WATCHLIST_DRY_RUN:-true}"   # destructive -> safe by default
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

# --- watchlist-sync env + units --------------------------------------------
WL_ENV="$APPDIR/watchlist-sync.env"
cat > "$WL_ENV" <<EOF
PLEX_TOKEN=$PLEX_TOKEN
RADARR_URL=http://$API_HOST:7878
RADARR_API_KEY=$RADARR_API_KEY
SONARR_URL=http://$API_HOST:8989
SONARR_API_KEY=$SONARR_API_KEY
TORBOX_API_KEY=$TORBOX_API_KEY
MEDIA_ROOT=/mnt/box/media
DRY_RUN=$WATCHLIST_DRY_RUN
EOF
chmod 600 "$WL_ENV"

cat > /etc/systemd/system/watchlist-sync.service <<EOF
[Unit]
Description=Reconcile library to the Plex watchlist (delete de-watchlisted titles)
After=network-online.target $MOUNT_UNIT
Requires=$MOUNT_UNIT

[Service]
Type=oneshot
EnvironmentFile=$WL_ENV
ExecStart=/usr/bin/python3 $REPO/scripts/watchlist_sync.py
EOF

cat > /etc/systemd/system/watchlist-sync.timer <<'EOF'
[Unit]
Description=Run watchlist reconcile every 15 minutes

[Timer]
OnCalendar=*:0/15
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now torbox-reaper.timer watchlist-sync.timer
echo "installed: reaper DRY_RUN=$DRY_RUN, watchlist DRY_RUN=$WATCHLIST_DRY_RUN, mount=$MOUNT_UNIT, API_HOST=$API_HOST"
echo "--- torbox-reaper run once ---"
systemctl start torbox-reaper.service
journalctl -u torbox-reaper.service --no-pager -n 20 -o cat
echo "--- watchlist-sync run once ---"
systemctl start watchlist-sync.service
journalctl -u watchlist-sync.service --no-pager -n 20 -o cat
