#!/usr/bin/env python3
"""
s1_test.py — automated S1 test: does Decypharr write a REAL FILE or a SYMLINK?

This is the whole reason for the local run. It:
  1. Makes sure Prowlarr has synced indexers into Radarr (retries past the 429).
  2. Adds a PUBLIC-DOMAIN movie ("Night of the Living Dead", 1968) to Radarr and
     forces a search — a legal, widely-seeded title so the pipeline is exercised
     without grabbing anything copyrighted.
  3. Polls Radarr's queue and inspects /data/downloads inside the Decypharr
     container, classifying what lands as a regular file vs a symlink.

Verdict:
  - regular file  -> download mode works, Storage-Box copy design is sound.
  - symlink       -> STOP; rethink the design before provisioning the VPS.

Usage:  python scripts/s1_test.py        (from repo root)
Env:    COMPOSE_DIR (default "local"), API_HOST (default 127.0.0.1),
        MAX_WAIT_SEC (default 360), MOVIE_TERM / MOVIE_YEAR to override the title.
"""

import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request

import lib_env  # noqa: F401  (loads repo-root .env into os.environ on import)

COMPOSE_DIR = os.environ.get("COMPOSE_DIR", "local")
API_HOST = os.environ.get("API_HOST", "127.0.0.1")
MAX_WAIT = int(os.environ.get("MAX_WAIT_SEC", "360"))
MOVIE_TERM = os.environ.get("MOVIE_TERM", "Night of the Living Dead")
MOVIE_YEAR = int(os.environ.get("MOVIE_YEAR", "1968"))
RADARR_PORT = 7878
ROOT = "/mnt/box/media/movies"


def log(m): print(f"[s1] {m}", flush=True)


def dc(args, **kw):
    return lib_env.dc(args, COMPOSE_DIR, **kw)


def key(service):
    xml = dc(["exec", "-T", service, "cat", "/config/config.xml"]).stdout
    return re.search(r"<ApiKey>([^<]+)</ApiKey>", xml).group(1).strip()


def api(port, k, method, path, body=None):
    url = f"http://{API_HOST}:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-Api-Key": k,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else None


def ensure_indexers(pk, rk):
    """Force sync until Radarr has >=1 indexer (works around Prowlarr 429s)."""
    for attempt in range(6):
        idx = api(RADARR_PORT, rk, "GET", "/api/v3/indexer") or []
        if idx:
            log(f"Radarr has {len(idx)} indexer(s): {[i['name'] for i in idx]}")
            return True
        log(f"Radarr has 0 indexers; triggering Prowlarr sync (try {attempt+1})")
        try:
            api(9696, pk, "POST", "/api/v1/command", {"name": "ApplicationIndexerSync"})
        except Exception as e:
            log(f"sync trigger error (harmless): {e}")
        time.sleep(15)
    return False


def add_and_search(rk):
    movies = api(RADARR_PORT, rk, "GET", "/api/v3/movie") or []
    existing = next((m for m in movies if m.get("year") == MOVIE_YEAR
                     and MOVIE_TERM.lower() in m.get("title", "").lower()), None)
    if existing:
        mid = existing["id"]
        log(f"Movie already added (id {mid}); forcing search")
        api(RADARR_PORT, rk, "POST", "/api/v3/command",
            {"name": "MoviesSearch", "movieIds": [mid]})
        return mid

    term = urllib.parse.quote(f"{MOVIE_TERM} {MOVIE_YEAR}")
    results = api(RADARR_PORT, rk, "GET", f"/api/v3/movie/lookup?term={term}") or []
    pick = next((r for r in results if r.get("year") == MOVIE_YEAR), None) \
        or (results[0] if results else None)
    if not pick:
        raise RuntimeError("movie lookup returned nothing")

    profiles = api(RADARR_PORT, rk, "GET", "/api/v3/qualityprofile") or []
    prof = next((p for p in profiles if p["name"].lower() == "any"), profiles[0])

    body = dict(pick)
    body.update({
        "qualityProfileId": prof["id"],
        "rootFolderPath": ROOT,
        "monitored": True,
        "minimumAvailability": "released",
        "addOptions": {"searchForMovie": True},
    })
    added = api(RADARR_PORT, rk, "POST", "/api/v3/movie", body)
    log(f"Added '{added['title']}' ({added['year']}) with profile "
        f"'{prof['name']}', search triggered")
    return added["id"]


def inspect_downloads():
    """Return (files, symlinks, listing) from /data/downloads inside Decypharr."""
    files = dc(["exec", "-T", "decypharr", "sh", "-c",
                "find /data/downloads -mindepth 1 -type f 2>/dev/null"]).stdout.strip()
    links = dc(["exec", "-T", "decypharr", "sh", "-c",
                "find /data/downloads -mindepth 1 -type l 2>/dev/null"]).stdout.strip()
    listing = dc(["exec", "-T", "decypharr", "sh", "-c",
                  "ls -laR /data/downloads 2>/dev/null | head -n 60"]).stdout
    f = [x for x in files.splitlines() if x]
    l = [x for x in links.splitlines() if x]
    return f, l, listing


def main():
    pk, rk = key("prowlarr"), key("radarr")
    ensure_indexers(pk, rk)
    add_and_search(rk)

    log(f"Polling up to {MAX_WAIT}s for a grab to land in /data/downloads ...")
    deadline = time.time() + MAX_WAIT
    last = ""
    while time.time() < deadline:
        q = api(RADARR_PORT, rk, "GET", "/api/v3/queue") or {}
        recs = q.get("records", q if isinstance(q, list) else [])
        if recs:
            r0 = recs[0]
            state = f"{r0.get('status')}/{r0.get('trackedDownloadState')}"
            if state != last:
                log(f"queue: {r0.get('title','?')[:50]} [{state}]")
                last = state
        files, links, _ = inspect_downloads()
        if files or links:
            break
        time.sleep(12)

    files, links, listing = inspect_downloads()
    print("\n" + "=" * 70)
    print("S1 RESULT")
    print("=" * 70)
    print(listing or "(nothing in /data/downloads yet)")
    print("-" * 70)
    print(f"regular files: {len(files)}   symlinks: {len(links)}")
    if links and not files:
        print("VERDICT: SYMLINK ONLY  ->  download mode NOT working. STOP and "
              "rethink the Storage-Box design before the VPS.")
    elif files:
        print("VERDICT: REAL FILE(S) PRESENT  ->  download mode works. "
              "Storage-Box copy design is sound. S1 (first half) PASSES.")
        for f in files[:5]:
            print("   file:", f)
    else:
        print("VERDICT: INCONCLUSIVE — nothing landed yet. TorBox may still be "
              "caching an uncached torrent, or no release was found. Re-run, or "
              "check manually:\n"
              "   docker compose -f local/docker-compose.yml exec decypharr "
              "sh -c 'ls -laR /data/downloads'")


if __name__ == "__main__":
    main()
