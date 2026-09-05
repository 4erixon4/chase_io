#!/usr/bin/env python3
"""
setup_cleanuparr.py — configure Cleanuparr end-to-end via its API. No UI clicks.

Cleanuparr auto-recovers stuck/failed downloads: it strikes items that fail to
import or stall, removes them from the download client, blocklists the release,
and tells Sonarr/Radarr to grab a different one. With a single debrid source
that's not optional — a transient TorBox error (e.g. Cloudflare 524) would
otherwise wedge a request forever (exactly what happened to Sherlock S01/S03).

Fully scriptable, INCLUDING first-run account creation, so it reproduces
identically on the VPS: local config never migrates, but running this script on
the VPS rebuilds the same setup. Internal addresses (sonarr:8989 etc.) are the
same in both places.

Creds come from the repo-root .env (CLEANUPARR_USER/PASS). Arr API keys are
auto-discovered from the containers. Nothing secret is printed.

Usage:  python scripts/setup_cleanuparr.py
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

import lib_env  # noqa: F401  (loads repo-root .env into os.environ on import)
from lib_env import env

BASE = os.environ.get("CLEANUPARR_URL",
                      f"http://{os.environ.get('API_HOST', '127.0.0.1')}:11011")
COMPOSE_DIR = os.environ.get("COMPOSE_DIR", "local")
HERE = os.path.dirname(os.path.abspath(__file__))

ARRS = {
    "sonarr": {"name": "Sonarr", "url": "http://sonarr:8989"},
    "radarr": {"name": "Radarr", "url": "http://radarr:7878"},
}
DECYPHARR_HOST = "http://decypharr:8282"


def log(m): print(f"[cleanuparr] {m}", flush=True)


def dc(a): return lib_env.dc(a, COMPOSE_DIR)


def arr_key(s):
    xml = dc(["exec", "-T", s, "cat", "/config/config.xml"]).stdout
    return re.search(r"<ApiKey>([^<]+)</ApiKey>", xml).group(1).strip()


def call(path, method="GET", body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        return e.code, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw[:400])


def main():
    U = env("CLEANUPARR_USER", required=True)
    P = env("CLEANUPARR_PASS", required=True)

    # 1. First-run account (idempotent)
    st = call("/api/auth/status")[1]
    if isinstance(st, dict) and not st.get("setupCompleted"):
        c, r = call("/api/auth/setup/account", "POST",
                    {"username": U, "password": P, "confirmPassword": P})
        log(f"created admin account ({c})")
        call("/api/auth/setup/complete", "POST", {})
    else:
        log("account already set up (ok)")

    # 2. Login
    c, r = call("/api/auth/login", "POST", {"username": U, "password": P})
    if c != 200:
        log(f"login failed: {c} {r}"); return 1
    token = r["tokens"]["accessToken"]
    log("logged in")

    # 3. Sonarr / Radarr instances
    for svc, meta in ARRS.items():
        cur = call(f"/api/configuration/{svc}", token=token)[1]
        cur["instances"] = [{
            "name": meta["name"], "url": meta["url"],
            "apiKey": arr_key(svc), "enabled": True,
        }]
        c, r = call(f"/api/configuration/{svc}", "PUT", cur, token)
        log(f"{svc} instance -> {c} {r if isinstance(r, dict) else ''}")

    # 4. Download client (Decypharr, qBittorrent-compatible)
    dclients = call("/api/configuration/download_client", token=token)[1]
    have = any(x.get("host") == DECYPHARR_HOST
               for x in (dclients.get("clients", []) if isinstance(dclients, dict) else []))
    if have:
        log("download client already present (ok)")
    else:
        # Body is flat (no wrapper). "type" is the protocol enum (Torrent/Usenet),
        # NOT the software — the software goes in "typeName".
        c, r = call("/api/configuration/download_client", "POST", {
            "enabled": True, "name": "Decypharr",
            "type": "Torrent", "typeName": "qBittorrent",
            "host": DECYPHARR_HOST, "urlBase": "", "externalUrl": "",
            "username": env("DECYPHARR_USER", ""),
            "password": env("DECYPHARR_PASS", ""),
            "downloadDirectorySource": None, "downloadDirectoryTarget": None,
        }, token)
        log(f"download client -> {c} {r if isinstance(r, dict) else ''}")

    # 5. Enable the Queue Cleaner with strikes (removes+blocklists failed/stalled)
    qc = call("/api/configuration/queue_cleaner", token=token)[1]
    qc["enabled"] = True
    if isinstance(qc.get("failedImport"), dict):
        qc["failedImport"]["maxStrikes"] = 3
        # Exclude mode + empty patterns = strike ALL failed imports (Include with
        # no patterns is rejected by the API).
        qc["failedImport"]["patternMode"] = "Exclude"
        qc["failedImport"]["patterns"] = []
    qc["downloadingMetadataMaxStrikes"] = 3
    c, r = call("/api/configuration/queue_cleaner", "PUT", qc, token)
    log(f"queue_cleaner enabled -> {c} {r if isinstance(r, dict) else ''}")

    # 6. Show job schedule
    jobs = call("/api/jobs", token=token)[1]
    if isinstance(jobs, list):
        for j in jobs:
            if j.get("jobType") in ("QueueCleaner", "Seeker"):
                log(f"job {j['name']}: {j.get('status')} {j.get('schedule')}")
    log("DONE.")


if __name__ == "__main__":
    sys.exit(main())
