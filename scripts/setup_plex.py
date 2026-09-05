#!/usr/bin/env python3
"""
setup_plex.py — apply CIFS-friendly Plex server settings and create the Movies /
TV libraries. No UI clicks (after the one-time claim + sign-in at first run).

Why: Plex over a network mount (the Hetzner Storage Box, CIFS, 10-connection
cap) is the sharpest performance edge in this design. Library scans, thumbnail
generation and analysis all hammer the mount. So we:
  - keep continuous filesystem watching OFF (FSEvent*), scan on a schedule;
  - turn OFF chapter thumbnails, loudness analysis, ad markers, video preview
    thumbnails (BIF) — all heavy on a network mount;
  - run the scanner at low priority.
Set these BEFORE the first big scan (this script sets prefs first, then creates
libraries so they inherit the behavior).

Reads the token from Plex's Preferences.xml (written after you sign in). Media
paths (/mnt/box/media/{movies,tv}) are identical locally and on the VPS, so this
reproduces the same setup there. Idempotent.

Usage:  python scripts/setup_plex.py
"""
import re, subprocess, sys, urllib.parse, urllib.request, urllib.error, os
import lib_env  # noqa: F401  (loads repo-root .env into os.environ on import)

COMPOSE_DIR = os.environ.get("COMPOSE_DIR", "local")
PLEX = os.environ.get("PLEX_URL", "http://127.0.0.1:32400")
PREFS_PATH = "/config/Library/Application Support/Plex Media Server/Preferences.xml"

# CIFS-friendly. Values are Plex "behavior" enums: never / scheduled / asap.
SERVER_PREFS = {
    "FSEventLibraryUpdatesEnabled": "0",     # no continuous FS watching over CIFS
    "FSEventLibraryPartialScanEnabled": "0",
    "ScheduledLibraryUpdatesEnabled": "1",   # scan on a schedule instead
    "ScheduledLibraryUpdateInterval": "3600",
    "ScannerLowPriority": "1",               # don't starve playback during scans
    "GenerateBIFBehavior": "never",          # video preview thumbnails
    "GenerateChapterThumbBehavior": "never", # chapter thumbnails
    "GenerateAdMarkerBehavior": "never",
    "LoudnessAnalysisBehavior": "never",
    # These two only exist with Plex Pass; set best-effort, ignored otherwise.
    "GenerateIntroMarkerBehavior": "never",
    "GenerateCreditsMarkerBehavior": "never",
}

LIBRARIES = [
    {"name": "Movies", "type": "movie", "agent": "tv.plex.agents.movie",
     "scanner": "Plex Movie", "location": "/mnt/box/media/movies"},
    {"name": "TV Shows", "type": "show", "agent": "tv.plex.agents.series",
     "scanner": "Plex TV Series", "location": "/mnt/box/media/tv"},
]


def log(m): print(f"[plex] {m}", flush=True)
def dc(a): return lib_env.dc(a, COMPOSE_DIR)


def token():
    xml = dc(["exec","-T","plex","cat", PREFS_PATH]).stdout
    m = re.search(r'PlexOnlineToken="([^"]+)"', xml)
    return m.group(1) if m else None


def req(method, path, tok, accept_xml=True):
    url = f"{PLEX}{path}"
    url += ("&" if "?" in url else "?") + "X-Plex-Token=" + urllib.parse.quote(tok)
    h = {"Accept": "application/xml"} if accept_xml else {}
    r = urllib.request.Request(url, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:300]


def main():
    tok = token()
    if not tok:
        log("Plex is not signed in yet (no PlexOnlineToken in Preferences.xml).")
        log("One-time manual step: open Plex, sign in / claim the server, then "
            "re-run this script. Everything after sign-in is scripted.")
        return 1
    log("token loaded")

    # 1. Server prefs (set before creating libraries)
    applied, skipped = [], []
    for k, v in SERVER_PREFS.items():
        code, _ = req("PUT", f"/:/prefs?{urllib.parse.urlencode({k: v})}", tok)
        (applied if code in (200, 204) else skipped).append(f"{k}={v}({code})")
    log("prefs applied: " + ", ".join(applied))
    if skipped:
        log("prefs not accepted (often Plex-Pass-only, safe to ignore): "
            + ", ".join(skipped))

    # 2. Libraries (idempotent by title)
    _, secs = req("GET", "/library/sections", tok)
    existing = set(re.findall(r'<Directory[^>]*?title="([^"]+)"', secs))
    for lib in LIBRARIES:
        if lib["name"] in existing:
            log(f"library '{lib['name']}' exists (ok)")
            continue
        q = urllib.parse.urlencode({
            "name": lib["name"], "type": lib["type"], "agent": lib["agent"],
            "scanner": lib["scanner"], "language": "en",
            "location": lib["location"],
        })
        code, resp = req("POST", f"/library/sections?{q}", tok)
        log(f"create library '{lib['name']}' -> {code}")

    log("DONE. Scans run on a schedule; heavy per-file analysis is off — right "
        "profile for a CIFS-mounted library.")


if __name__ == "__main__":
    sys.exit(main())
