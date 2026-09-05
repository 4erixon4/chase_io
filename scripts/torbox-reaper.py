#!/usr/bin/env python3
"""
torbox-reaper.py — free TorBox account storage safely.

TorBox Essential gives ~300 GB of permanent account storage, and Decypharr does
NOT delete completed torrents from your TorBox account (its qBit-delete path
keeps files, the Arr `cleanup` flag only tidies Decypharr's own queue, and the
repair worker only touches broken items). Left alone, the 300 GB fills and new
adds fail.

This reaper deletes a torrent from TorBox ONLY once its content has actually
landed in the library — i.e. Radarr/Sonarr report a `downloadFolderImported`
event for that torrent's infohash AND the imported file still exists at its
final path under MEDIA_ROOT. Deletion is gated on that file existing, never on
age, so it can't race an in-progress import.

Run it on the VPS host (has /mnt/box mounted) on a schedule — see the systemd
timer at the bottom of this file. Stdlib only; no pip installs.

Required env:
  TORBOX_API_KEY     TorBox API token (Settings -> API)
Recommended env:
  RADARR_URL         e.g. http://127.0.0.1:7878   (tailnet/loopback is fine)
  RADARR_API_KEY
  SONARR_URL         e.g. http://127.0.0.1:8989
  SONARR_API_KEY
Optional env:
  MEDIA_ROOT         default /mnt/box/media  (imported path must live under here)
  DRY_RUN            default "true"          (set "false" to actually delete)
  MIN_GRACE_MINUTES  default 0               (extra safety: skip if the import
                                              event is younger than this; the
                                              file-existence check is the real
                                              gate, this is only belt-and-braces)
  HISTORY_MAX_PAGES  default 20              (how far back to read arr history)
  TORBOX_QUOTA_GB    default 300             (Essential tier permanent storage)
  WARN_PERCENT       default 70              (log a WARNING at/above this % of
                                              quota, even in DRY_RUN — so a
                                              forgotten DRY_RUN=true surfaces
                                              before TorBox adds start failing)
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TORBOX_BASE = "https://api.torbox.app/v1/api"


def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def log(msg):
    print(f"[torbox-reaper] {msg}", flush=True)


def http_json(method, url, headers=None, body=None, timeout=30):
    data = None
    headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    # TorBox sits behind Cloudflare, which rejects the default "Python-urllib"
    # User-Agent as a bot (HTTP 403, Cloudflare error 1010). Send a browser-like
    # UA so the API is reachable — matters on the VPS too, not just locally.
    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"{method} {url} -> {e.reason}")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"{method} {url} -> non-JSON response: {raw[:200]}")


# ---- Radarr / Sonarr: build {infohash(upper): (importedPath, event_time)} ----

def parse_time(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def collect_imports(arr_url, arr_key, max_pages):
    """Return {HASH: (importedPath, datetime)} from downloadFolderImported history."""
    if not arr_url or not arr_key:
        return {}
    arr_url = arr_url.rstrip("/")
    imported = {}
    for page in range(1, max_pages + 1):
        q = urllib.parse.urlencode({
            "page": page, "pageSize": 250,
            "sortKey": "date", "sortDirection": "descending",
        })
        url = f"{arr_url}/api/v3/history?{q}"
        payload = http_json("GET", url, headers={"X-Api-Key": arr_key})
        records = payload.get("records", [])
        if not records:
            break
        for r in records:
            if r.get("eventType") != "downloadFolderImported":
                continue
            dl_id = (r.get("downloadId") or "").strip().upper()
            if not dl_id:
                continue
            data = r.get("data") or {}
            path = data.get("importedPath") or ""
            when = parse_time(r.get("date"))
            # keep the most recent import event per hash
            if dl_id not in imported or (when and imported[dl_id][1] and when > imported[dl_id][1]):
                imported[dl_id] = (path, when)
    return imported


# ---- TorBox ----

def torbox_list(api_key):
    url = f"{TORBOX_BASE}/torrents/mylist?bypass_cache=true"
    payload = http_json("GET", url, headers={"Authorization": f"Bearer {api_key}"})
    data = payload.get("data")
    return data if isinstance(data, list) else []


def torbox_delete(api_key, torrent_id):
    url = f"{TORBOX_BASE}/torrents/controltorrent"
    body = {"torrent_id": int(torrent_id), "operation": "delete", "all": False}
    return http_json("POST", url, headers={"Authorization": f"Bearer {api_key}"}, body=body)


def main():
    api_key = env("TORBOX_API_KEY")
    if not api_key:
        log("TORBOX_API_KEY is required.")
        return 2

    media_root = os.path.abspath(env("MEDIA_ROOT", "/mnt/box/media"))
    dry_run = env("DRY_RUN", "true").lower() != "false"
    grace_min = int(env("MIN_GRACE_MINUTES", "0"))
    max_pages = int(env("HISTORY_MAX_PAGES", "20"))

    # Safety: if the library mount is missing, do nothing (never delete when the
    # only reason a file "doesn't exist" is that /mnt/box fell off).
    if not os.path.isdir(media_root):
        log(f"MEDIA_ROOT {media_root} not present (mount down?). Aborting, deleting nothing.")
        return 3

    imported = {}
    imported.update(collect_imports(env("RADARR_URL"), env("RADARR_API_KEY"), max_pages))
    imported.update(collect_imports(env("SONARR_URL"), env("SONARR_API_KEY"), max_pages))
    log(f"Found {len(imported)} imported download(s) in *arr history.")

    torrents = torbox_list(api_key)
    log(f"TorBox account holds {len(torrents)} torrent(s). dry_run={dry_run}")

    # Quota guard — runs regardless of dry_run so a forgotten DRY_RUN=true (or any
    # reaper failure) can't silently let the account fill until adds start failing.
    quota_gb = float(env("TORBOX_QUOTA_GB", "300"))
    warn_pct = float(env("WARN_PERCENT", "70"))
    # NOTE (verify in S1): this assumes mylist `size` is bytes for completed
    # items. TorBox may use a different unit or omit it for incomplete torrents.
    # Eyeball the "TorBox usage ~X GB" log line against the TorBox dashboard on
    # the first real run; if it's off, adjust the unit/field here.
    used_bytes = sum(int(t.get("size") or 0) for t in torrents)
    used_gb = used_bytes / (1024 ** 3)
    pct = (used_gb / quota_gb * 100) if quota_gb > 0 else 0
    usage_line = f"TorBox usage ~{used_gb:.1f} GB / {quota_gb:.0f} GB ({pct:.0f}%)"
    if pct >= warn_pct:
        log(f"WARNING: {usage_line} — at/above {warn_pct:.0f}%. "
            f"{'DRY_RUN is on, nothing is being reaped — flip DRY_RUN=false.' if dry_run else 'Reaping now.'}")
    else:
        log(usage_line)

    now = datetime.now(timezone.utc)
    deleted = kept = 0
    for t in torrents:
        tid = t.get("id")
        thash = (t.get("hash") or "").strip().upper()
        name = t.get("name", "?")
        if tid is None or not thash:
            log(f"SKIP (no id/hash): {name}")
            kept += 1
            continue

        entry = imported.get(thash)
        if not entry:
            log(f"KEEP (not imported yet): {name}")
            kept += 1
            continue

        path, when = entry

        # Gate 1 (the real one): the imported file must exist at its final path.
        if not path:
            log(f"KEEP (imported but no path recorded): {name}")
            kept += 1
            continue
        abspath = os.path.abspath(path)
        # commonpath raises ValueError on mixed absolute/relative paths (or, on
        # Windows, different drives). A malformed Radarr history record must skip
        # that item, not crash the whole loop — so treat any failure as KEEP.
        try:
            under_root = os.path.commonpath([abspath, media_root]) == media_root
        except ValueError:
            under_root = False
        if not under_root:
            log(f"KEEP (imported path not under {media_root} or malformed): {name} -> {path!r}")
            kept += 1
            continue
        if not os.path.exists(abspath):
            log(f"KEEP (imported record but file missing on box): {name} -> {abspath}")
            kept += 1
            continue

        # Gate 2 (optional belt-and-braces): skip very fresh imports.
        if grace_min and when and (now - when).total_seconds() < grace_min * 60:
            log(f"KEEP (within {grace_min}m grace): {name}")
            kept += 1
            continue

        if dry_run:
            log(f"WOULD DELETE: {name} (id={tid}) — file present at {abspath}")
            deleted += 1
            continue

        try:
            torbox_delete(api_key, tid)
            log(f"DELETED from TorBox: {name} (id={tid})")
            deleted += 1
        except RuntimeError as e:
            log(f"ERROR deleting {name} (id={tid}): {e}")
            kept += 1

    log(f"Done. {'would delete' if dry_run else 'deleted'}={deleted}, kept={kept}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# ---------------------------------------------------------------------------
# Schedule on the VPS host (files at /opt/appdata/torbox-reaper/):
#
#   /etc/systemd/system/torbox-reaper.service
#     [Unit]
#     Description=Reap imported torrents from TorBox
#     # systemd auto-generates a .mount unit from the fstab entry; its name is the
#     # escaped mount path (/mnt/box -> mnt-box.mount). CONFIRM the exact name
#     # before relying on it:  systemctl list-units --type=mount | grep box
#     After=network-online.target mnt-box.mount
#     Requires=mnt-box.mount
#     [Service]
#     Type=oneshot
#     EnvironmentFile=/opt/appdata/torbox-reaper/reaper.env
#     ExecStart=/usr/bin/python3 /opt/appdata/torbox-reaper/torbox-reaper.py
#
#   /etc/systemd/system/torbox-reaper.timer
#     [Unit]
#     Description=Run TorBox reaper hourly
#     [Timer]
#     OnCalendar=hourly
#     Persistent=true
#     [Install]
#     WantedBy=timers.target
#
#   reaper.env  (chmod 600 — holds secrets):
#     TORBOX_API_KEY=...
#     RADARR_URL=http://127.0.0.1:7878
#     RADARR_API_KEY=...
#     SONARR_URL=http://127.0.0.1:8989
#     SONARR_API_KEY=...
#     MEDIA_ROOT=/mnt/box/media
#     DRY_RUN=false        # leave "true" for the first few runs and read the log
#
#   systemctl daemon-reload && systemctl enable --now torbox-reaper.timer
# ---------------------------------------------------------------------------
