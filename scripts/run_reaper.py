#!/usr/bin/env python3
"""
run_reaper.py — convenience launcher for torbox-reaper.py (LOCAL dry-run).

Discovers Radarr/Sonarr API keys from the containers and reads TORBOX_API_KEY
from the repo-root .env, then runs the reaper with DRY_RUN=true against the
local library. Secrets are passed to the child as environment only — never
printed. This is the second half of the S1 check: verify the infohash mapping
and that TorBox's `size` field feeds the quota math sanely.

Usage:  python scripts/run_reaper.py
"""

import os
import re
import subprocess
import sys

import lib_env  # noqa: F401  (loads repo-root .env into os.environ on import)
from lib_env import env as cfg

COMPOSE_DIR = os.environ.get("COMPOSE_DIR", "local")
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def key(service):
    xml = lib_env.dc(["exec", "-T", service, "cat", "/config/config.xml"], COMPOSE_DIR).stdout
    return re.search(r"<ApiKey>([^<]+)</ApiKey>", xml).group(1).strip()


def main():
    child_env = dict(os.environ)
    child_env.update({
        "TORBOX_API_KEY": cfg("TORBOX_API_KEY", required=True),
        "RADARR_URL": "http://127.0.0.1:7878",
        "RADARR_API_KEY": key("radarr"),
        "SONARR_URL": "http://127.0.0.1:8989",
        "SONARR_API_KEY": key("sonarr"),
        # local library stands in for /mnt/box/media (host path, just needs to exist)
        "MEDIA_ROOT": os.path.join(REPO, "local", "library"),
        "DRY_RUN": "true",
    })
    print("[run_reaper] DRY_RUN=true, MEDIA_ROOT=", child_env["MEDIA_ROOT"])
    return subprocess.call([sys.executable, os.path.join(HERE, "torbox-reaper.py")],
                           env=child_env)


if __name__ == "__main__":
    sys.exit(main())
