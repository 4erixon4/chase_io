#!/usr/bin/env python3
"""
setup_stack.py — configure the media stack via API + config files (no clicking).

Idempotent: safe to re-run. Reads secrets from the repo-root .env; auto-discovers
Radarr/Sonarr/Prowlarr API keys from their containers so nothing secret is typed
or printed. Talks to the services over 127.0.0.1 (host), but writes INTERNAL
docker-network addresses (service names) into configs so containers reach each
other — identical on the VPS.

What it does:
  1. Decypharr: set default_download_action = "download" (CRITICAL — else it
     symlinks and the Storage-Box copy design fails), then restart it.
  2. Radarr / Sonarr: ensure root folder + a qBittorrent download client pointing
     at Decypharr (with Decypharr's auth + category).
  3. Prowlarr: add Radarr/Sonarr as applications, add a public torrent indexer,
     and sync.

Usage:  python scripts/setup_stack.py            (from repo root)
Env override: COMPOSE_DIR (default "local"), API_HOST (default 127.0.0.1).
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

import lib_env  # noqa: F401  (loads repo-root .env into os.environ on import)
from lib_env import env

COMPOSE_DIR = os.environ.get("COMPOSE_DIR", "local")
API_HOST = os.environ.get("API_HOST", "127.0.0.1")

# Ports on the host, and the INTERNAL docker service name + port for cross-container refs.
SVC = {
    "radarr":   {"port": 7878, "net": "http://radarr:7878"},
    "sonarr":   {"port": 8989, "net": "http://sonarr:8989"},
    "prowlarr": {"port": 9696, "net": "http://prowlarr:9696"},
    "decypharr": {"port": 8282, "net": "http://decypharr:8282"},
}
ROOTS = {"radarr": "/mnt/box/media/movies", "sonarr": "/mnt/box/media/tv"}
CATEGORY = {"radarr": "radarr", "sonarr": "sonarr"}


def log(m): print(f"[setup] {m}", flush=True)


def dc(args, **kw):
    """Run a docker compose command in COMPOSE_DIR."""
    return subprocess.run(["docker", "compose"] + args, cwd=COMPOSE_DIR,
                          capture_output=True, text=True, **kw)


def container_file(service, path):
    r = dc(["exec", "-T", service, "cat", path])
    if r.returncode != 0:
        raise RuntimeError(f"read {service}:{path} failed: {r.stderr[:200]}")
    return r.stdout


def arr_key(service):
    """Auto-discover an *arr/Prowlarr API key from its config.xml (never printed)."""
    xml = container_file(service, "/config/config.xml")
    m = re.search(r"<ApiKey>([^<]+)</ApiKey>", xml)
    if not m:
        raise RuntimeError(f"no ApiKey in {service} config.xml yet (started once?)")
    return m.group(1).strip()


def api(service, key, method, path, body=None):
    url = f"http://{API_HOST}:{SVC[service]['port']}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-Api-Key": key,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}")


# ---------------------------------------------------------------------------
# 1. Decypharr: force download mode
# ---------------------------------------------------------------------------
def fix_decypharr():
    cfg = json.loads(container_file("decypharr", "/app/config.json"))
    changed = False
    if cfg.get("default_download_action") != "download":
        cfg["default_download_action"] = "download"
        changed = True
        log("Decypharr: default_download_action symlink -> download")
    if not changed:
        log("Decypharr: already download mode (ok)")
        return
    payload = json.dumps(cfg, indent=2)
    w = dc(["exec", "-T", "decypharr", "sh", "-c", "cat > /app/config.json"],
           input=payload)
    if w.returncode != 0:
        raise RuntimeError(f"write config.json failed: {w.stderr[:200]}")
    dc(["restart", "decypharr"])
    log("Decypharr: restarted")
    time.sleep(5)


# ---------------------------------------------------------------------------
# 2. Radarr / Sonarr: root folder + download client
# ---------------------------------------------------------------------------
def ensure_root(service, key):
    existing = api(service, key, "GET", "/api/v3/rootfolder") or []
    if any(r.get("path") == ROOTS[service] for r in existing):
        log(f"{service}: root folder exists (ok)")
        return
    api(service, key, "POST", "/api/v3/rootfolder", {"path": ROOTS[service]})
    log(f"{service}: added root folder {ROOTS[service]}")


def ensure_download_client(service, key, dc_user, dc_pass):
    existing = api(service, key, "GET", "/api/v3/downloadclient") or []
    if any(c.get("name") == "Decypharr" for c in existing):
        log(f"{service}: download client 'Decypharr' exists (ok)")
        return
    schema_list = api(service, key, "GET", "/api/v3/downloadclient/schema") or []
    qbit = next((s for s in schema_list
                 if s.get("implementation", "").lower() == "qbittorrent"), None)
    if not qbit:
        raise RuntimeError(f"{service}: no qBittorrent schema found")
    fields = qbit.get("fields", [])
    want = {"host": "decypharr", "port": 8282, "username": dc_user,
            "password": dc_pass, "usessl": False}
    for fld in fields:
        n = fld.get("name", "")
        nl = n.lower()
        if nl in want:
            fld["value"] = want[nl]
        elif nl in ("category", "moviecategory", "tvcategory"):
            fld["value"] = CATEGORY[service]
    body = dict(qbit)
    body["name"] = "Decypharr"
    body["enable"] = True
    body["fields"] = fields
    api(service, key, "POST", "/api/v3/downloadclient?forceSave=true", body)
    log(f"{service}: added Decypharr qBittorrent client (category {CATEGORY[service]})")


# ---------------------------------------------------------------------------
# 3. Prowlarr: apps + indexer + sync
# ---------------------------------------------------------------------------
def ensure_prowlarr_app(pk, app, arr_url, arr_key_):
    existing = api("prowlarr", pk, "GET", "/api/v1/applications") or []
    if any(a.get("name", "").lower() == app for a in existing):
        log(f"prowlarr: app {app} exists (ok)")
        return
    schema_list = api("prowlarr", pk, "GET", "/api/v1/applications/schema") or []
    sch = next((s for s in schema_list
                if s.get("implementation", "").lower() == app), None)
    if not sch:
        raise RuntimeError(f"prowlarr: no {app} application schema")
    for fld in sch.get("fields", []):
        nl = fld.get("name", "").lower()
        if nl == "prowlarrurl":
            fld["value"] = SVC["prowlarr"]["net"]
        elif nl == "baseurl":
            fld["value"] = arr_url
        elif nl == "apikey":
            fld["value"] = arr_key_
    sch["name"] = app
    sch["syncLevel"] = "fullSync"
    api("prowlarr", pk, "POST", "/api/v1/applications?forceSave=true", sch)
    log(f"prowlarr: added application {app}")


def ensure_prowlarr_indexer(pk, definition):
    existing = api("prowlarr", pk, "GET", "/api/v1/indexer") or []
    if any(i.get("definitionName") == definition or i.get("name") == definition
           for i in existing):
        log(f"prowlarr: indexer {definition} exists (ok)")
        return True
    schema_list = api("prowlarr", pk, "GET", "/api/v1/indexer/schema") or []
    sch = next((s for s in schema_list if s.get("definitionName") == definition), None)
    if not sch:
        log(f"prowlarr: definition '{definition}' not in schema, skipping")
        return False
    profiles = api("prowlarr", pk, "GET", "/api/v1/appprofile") or []
    app_profile_id = profiles[0]["id"] if profiles else 1
    sch["name"] = definition
    sch["enable"] = True
    sch["appProfileId"] = app_profile_id
    try:
        api("prowlarr", pk, "POST", "/api/v1/indexer?forceSave=true", sch)
        log(f"prowlarr: added indexer {definition}")
        return True
    except RuntimeError as e:
        log(f"prowlarr: could not add {definition}: {e}")
        return False


def main():
    dc_user = env("DECYPHARR_USER", "")
    dc_pass = env("DECYPHARR_PASS", "")

    # discover keys (never printed)
    keys = {}
    for s in ("radarr", "sonarr", "prowlarr"):
        keys[s] = arr_key(s)
        log(f"{s}: API key discovered (len {len(keys[s])})")

    log("--- 1. Decypharr ---")
    fix_decypharr()

    log("--- 2. Radarr / Sonarr ---")
    for s in ("radarr", "sonarr"):
        ensure_root(s, keys[s])
        ensure_download_client(s, keys[s], dc_user, dc_pass)

    log("--- 3. Prowlarr ---")
    ensure_prowlarr_app(keys["prowlarr"], "radarr", SVC["radarr"]["net"], keys["radarr"])
    ensure_prowlarr_app(keys["prowlarr"], "sonarr", SVC["sonarr"]["net"], keys["sonarr"])
    # Best-effort: add several reliable API-based public indexers (no Cloudflare).
    # Prowlarr live-tests each on add; whichever connect stay. Broad + movie + TV.
    n = 0
    for d in ("TorrentsCSV", "thepiratebay", "yts", "eztv", "torrentdownloads"):
        if ensure_prowlarr_indexer(keys["prowlarr"], d):
            n += 1
    if n == 0:
        log("prowlarr: no indexer added automatically — add one in the UI "
            "(Indexers -> Add, pick a public one).")
    else:
        log(f"prowlarr: {n} indexer(s) active")
    # Force Prowlarr to push its indexers into Radarr/Sonarr now.
    try:
        api("prowlarr", keys["prowlarr"], "POST", "/api/v1/command",
            {"name": "ApplicationIndexerSync"})
        log("prowlarr: triggered ApplicationIndexerSync")
    except RuntimeError as e:
        log(f"prowlarr: sync trigger failed (harmless, runs on schedule): {e}")

    log("DONE.")


if __name__ == "__main__":
    sys.exit(main())
