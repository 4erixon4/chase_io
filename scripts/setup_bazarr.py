#!/usr/bin/env python3
"""
setup_bazarr.py — configure Bazarr end-to-end via its settings API. No UI clicks.

- Connects Bazarr to Sonarr (sonarr:8989) and Radarr (radarr:7878).
- Adds the OpenSubtitles.com provider with your credentials.
- Creates a language profile (English + Hebrew) and sets it as the default for
  both series and movies, so every item Sonarr/Radarr add automatically gets
  subtitles searched.

Creds: OPENSUBTITLES_USER/PASS from the repo-root .env. Arr API keys and the
Bazarr API key are auto-discovered from the containers.

VPS parity: config isn't migrated, but running this on the VPS reproduces it
identically. Internal addresses (sonarr:8989 / radarr:7878) are the same there.

Usage:  python scripts/setup_bazarr.py
"""
import json, os, re, subprocess, sys, urllib.parse, urllib.request, urllib.error

import lib_env  # noqa: F401  (loads repo-root .env into os.environ on import)
from lib_env import env

BASE = os.environ.get("BAZARR_URL", "http://127.0.0.1:6767")
COMPOSE_DIR = os.environ.get("COMPOSE_DIR", "local")
HERE = os.path.dirname(os.path.abspath(__file__))
# EN + HE. Change here if you want different subtitle languages.
LANGS = [("en", "English"), ("he", "Hebrew")]
PROFILE_NAME = "EN+HE"


def log(m): print(f"[bazarr] {m}", flush=True)
def dc(a): return subprocess.run(["docker","compose"]+a, cwd=COMPOSE_DIR,
                                 capture_output=True, text=True)

def arr_key(s):
    xml = dc(["exec","-T",s,"cat","/config/config.xml"]).stdout
    return re.search(r"<ApiKey>([^<]+)</ApiKey>", xml).group(1).strip()

def bazarr_key():
    y = dc(["exec","-T","bazarr","cat","/config/config/config.yaml"]).stdout
    return re.search(r"apikey:\s*([0-9a-f]{32})", y).group(1)

def get(key):
    req=urllib.request.Request(BASE+"/api/system/settings",headers={"X-API-KEY":key})
    return json.loads(urllib.request.urlopen(req,timeout=20).read())

def post(key, fields):
    body=urllib.parse.urlencode(fields).encode()
    req=urllib.request.Request(BASE+"/api/system/settings", data=body, method="POST",
        headers={"X-API-KEY":key,"Content-Type":"application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req,timeout=60) as r:
            return r.status, r.read().decode()[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def profiles(bkey):
    req = urllib.request.Request(BASE+"/api/system/languages/profiles",
                                 headers={"X-API-KEY": bkey})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def main():
    bkey = bazarr_key()

    # Idempotent: reuse an existing "EN+HE" profile instead of recreating it
    # (re-POSTing a duplicate profileId makes Bazarr 500).
    existing = profiles(bkey)
    match = next((p for p in existing if p.get("name") == PROFILE_NAME), None)
    prof_id = match["profileId"] if match else \
        (max((p["profileId"] for p in existing), default=0) + 1)

    fields = [
        ("settings-general-use_sonarr", "true"),
        ("settings-general-use_radarr", "true"),
        ("settings-sonarr-ip", "sonarr"),
        ("settings-sonarr-port", "8989"),
        ("settings-sonarr-base_url", "/"),
        ("settings-sonarr-apikey", arr_key("sonarr")),
        ("settings-radarr-ip", "radarr"),
        ("settings-radarr-port", "7878"),
        ("settings-radarr-base_url", "/"),
        ("settings-radarr-apikey", arr_key("radarr")),
        ("settings-general-enabled_providers", "opensubtitlescom"),
        ("settings-opensubtitlescom-username", env("OPENSUBTITLES_USER","")),
        ("settings-opensubtitlescom-password", env("OPENSUBTITLES_PASS","")),
        ("settings-opensubtitlescom-use_hash", "true"),
        ("settings-general-serie_default_enabled", "true"),
        ("settings-general-serie_default_profile", str(prof_id)),
        ("settings-general-movie_default_enabled", "true"),
        ("settings-general-movie_default_profile", str(prof_id)),
    ]
    for code, _ in LANGS:
        fields.append(("languages-enabled", code))
    # Only create the profile when it doesn't exist yet; otherwise leave the
    # profiles untouched (omitting the key = no change).
    if match:
        log(f"language profile '{PROFILE_NAME}' exists (id {prof_id}) — reusing")
    else:
        newprof = {
            "profileId": prof_id, "name": PROFILE_NAME, "cutoff": None,
            "items": [
                {"id": i+1, "language": code, "audio_exclude": "False",
                 "hi": "False", "forced": "False"}
                for i, (code, _name) in enumerate(LANGS)
            ],
            "mustContain": [], "mustNotContain": [],
            "originalFormat": None, "tag": None,
        }
        fields.append(("languages-profiles", json.dumps(existing + [newprof])))

    code, resp = post(bkey, fields)
    log(f"settings POST -> {code} {resp}")

    # Verify
    s = get(bkey)
    g = s.get("general", {})
    prof = profiles(bkey)
    log(f"use_sonarr={g.get('use_sonarr')} use_radarr={g.get('use_radarr')} "
        f"providers={g.get('enabled_providers')}")
    log(f"sonarr ip={s['sonarr']['ip']}:{s['sonarr']['port']} keyset={bool(s['sonarr']['apikey'])}")
    log(f"radarr ip={s['radarr']['ip']}:{s['radarr']['port']} keyset={bool(s['radarr']['apikey'])}")
    log(f"language profiles: {[p.get('name') for p in prof]}")
    log(f"default serie profile={g.get('serie_default_profile')} "
        f"movie profile={g.get('movie_default_profile')}")
    log("DONE. Bazarr will pull the series/movies from the arrs and search subs "
        "on the default profile. First sync can take a minute.")


if __name__ == "__main__":
    sys.exit(main())
