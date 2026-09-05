#!/usr/bin/env python3
"""
setup_overseerr.py — wire Overseerr -> Radarr/Sonarr and enable Plex Watchlist
auto-request. Run AFTER Overseerr's manual "Sign in with Plex" + "Finish Setup"
(services can be left empty in the wizard; this fills them in).

Idempotent. Reads Overseerr's own API key from its settings.json and the arr
keys from the containers — nothing secret is printed. Uses internal docker
service names (radarr/sonarr), so it's identical on the VPS.

Usage:  python scripts/setup_overseerr.py
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

COMPOSE_DIR = os.environ.get("COMPOSE_DIR", "local")
API_HOST = os.environ.get("API_HOST", "127.0.0.1")
PORT = 5055

RADARR = {"name": "Radarr", "hostname": "radarr", "port": 7878,
          "dir": "/mnt/box/media/movies"}
SONARR = {"name": "Sonarr", "hostname": "sonarr", "port": 8989,
          "dir": "/mnt/box/media/tv"}


def log(m): print(f"[overseerr] {m}", flush=True)


def dc(args, **kw):
    return subprocess.run(["docker", "compose"] + args, cwd=COMPOSE_DIR,
                          capture_output=True, text=True, **kw)


def arr_key(service):
    xml = dc(["exec", "-T", service, "cat", "/config/config.xml"]).stdout
    return re.search(r"<ApiKey>([^<]+)</ApiKey>", xml).group(1).strip()


def overseerr_key():
    raw = dc(["exec", "-T", "overseerr", "cat", "/app/config/settings.json"]).stdout
    return json.loads(raw)["main"]["apiKey"]


def plex_token():
    prefs = dc(["exec", "-T", "plex", "cat",
                "/config/Library/Application Support/Plex Media Server/"
                "Preferences.xml"]).stdout
    m = re.search(r'PlexOnlineToken="([^"]+)"', prefs)
    return m.group(1) if m else None


def arr_api(port, key, method, path, body=None):
    url = f"http://{API_HOST}:{port}{path}"
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


def add_plex_notification(service, port, token):
    """Built-in Radarr/Sonarr 'Plex Media Server' connect -> library scan on import."""
    key = arr_key(service)
    existing = arr_api(port, key, "GET", "/api/v3/notification") or []
    if any(n.get("implementation") == "PlexServer" for n in existing):
        log(f"{service}: Plex connection exists (ok)")
        return
    schema = arr_api(port, key, "GET", "/api/v3/notification/schema") or []
    plex = next((s for s in schema if s.get("implementation") == "PlexServer"), None)
    if not plex:
        raise RuntimeError(f"{service}: no PlexServer notification schema")
    for f in plex.get("fields", []):
        n = f.get("name")
        if n == "host":
            f["value"] = "plex"
        elif n == "port":
            f["value"] = 32400
        elif n == "useSsl":
            f["value"] = False
        elif n == "authToken":
            f["value"] = token
        elif n == "updateLibrary":
            f["value"] = True
    body = dict(plex)
    body["name"] = "Plex Media Server"
    # fire on the events that actually add/replace files
    for flag in ("onDownload", "onUpgrade", "onRename"):
        if body.get("supportsOn" + flag[2:], True):
            body[flag] = True
    arr_api(port, key, "POST", "/api/v3/notification?forceSave=true", body)
    log(f"{service}: added Plex Media Server connection (scan on import)")


def api(method, path, key, body=None):
    url = f"http://{API_HOST}:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-Api-Key": key,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}")


def pick_profile(profiles):
    for pref in ("Any", "HD-1080p", "HD - 720p/1080p"):
        for p in profiles:
            if p["name"].lower() == pref.lower():
                return p
    return profiles[0]


def configure_arr(kind, spec, key, ok):
    """kind: 'radarr' or 'sonarr'."""
    existing = api("GET", f"/api/v1/settings/{kind}", ok) or []
    if any(s.get("hostname") == spec["hostname"] for s in existing):
        log(f"{kind}: already configured (ok)")
        return
    test_body = {"hostname": spec["hostname"], "port": spec["port"],
                 "apiKey": key, "useSsl": False, "baseUrl": ""}
    t = api("POST", f"/api/v1/settings/{kind}/test", ok, test_body)
    profiles = t.get("profiles", [])
    roots = [r["path"] for r in t.get("rootFolders", [])]
    if not profiles:
        raise RuntimeError(f"{kind}: test returned no quality profiles")
    prof = pick_profile(profiles)
    root = spec["dir"] if spec["dir"] in roots else (roots[0] if roots else spec["dir"])

    body = {
        "name": spec["name"], "hostname": spec["hostname"], "port": spec["port"],
        "apiKey": key, "useSsl": False, "baseUrl": "",
        "activeProfileId": prof["id"], "activeProfileName": prof["name"],
        "activeDirectory": root, "is4k": False, "isDefault": True,
        "externalUrl": "", "syncEnabled": True, "preventSearch": False,
        "tagRequests": False,
    }
    if kind == "radarr":
        body["minimumAvailability"] = "released"
    else:
        body["enableSeasonFolders"] = True
        langs = t.get("languageProfiles") or []
        if langs:
            body["activeLanguageProfileId"] = langs[0]["id"]
    api("POST", f"/api/v1/settings/{kind}", ok, body)
    log(f"{kind}: added ({spec['hostname']}:{spec['port']}, profile "
        f"'{prof['name']}', root '{root}')")


def enable_watchlist(ok):
    users = api("GET", "/api/v1/user?take=100", ok) or {}
    results = users.get("results", users if isinstance(users, list) else [])
    n = 0
    for u in results:
        uid = u["id"]
        try:
            api("POST", f"/api/v1/user/{uid}/settings/main", ok,
                {"watchlistSyncMovies": True, "watchlistSyncTv": True})
            n += 1
        except RuntimeError as e:
            log(f"watchlist sync for user {uid} failed: {e}")
    log(f"Plex Watchlist auto-request enabled for {n} user(s)")


def main():
    ok = overseerr_key()
    log("Overseerr API key loaded")
    configure_arr("radarr", RADARR, arr_key("radarr"), ok)
    configure_arr("sonarr", SONARR, arr_key("sonarr"), ok)
    enable_watchlist(ok)

    # Built-in Radarr/Sonarr -> Plex "scan on import" so new files appear promptly.
    token = plex_token()
    if token:
        add_plex_notification("radarr", RADARR["port"], token)
        add_plex_notification("sonarr", SONARR["port"], token)
    else:
        log("Plex token not found in Preferences.xml — skipping Plex scan hook "
            "(is Plex signed in?).")

    log("DONE. Add something to your Plex Watchlist and Overseerr will "
        "auto-request it within a few minutes; Radarr/Sonarr will tell Plex to "
        "scan on import.")


if __name__ == "__main__":
    sys.exit(main())
