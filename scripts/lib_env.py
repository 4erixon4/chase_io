"""
lib_env.py — shared config/secrets loader for the setup scripts.

Single source of truth is the repo-root `.env` (see `.env.example`). This module
loads it into the process environment on import, so:

  * real environment variables (e.g. injected by Coolify, or `export`ed on the
    VPS host) ALWAYS win over the file, and
  * child `docker compose` calls inherit the vars, so ${PUID}/${TZ}/${TS_IP}
    interpolation works without a per-directory .env.

For local development we also overlay `local/.env` and `stack/.env` if present
(never committed). Usage:

    from lib_env import env
    token = env("TORBOX_API_KEY", required=True)
"""
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(path):
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            # Real env wins; file only fills gaps.
            if k and k not in os.environ:
                os.environ[k] = v


# Order: root .env is canonical; local/stack .env only fill remaining gaps.
_load(os.path.join(REPO_ROOT, ".env"))
_load(os.path.join(REPO_ROOT, "local", ".env"))
_load(os.path.join(REPO_ROOT, "stack", ".env"))


def env(key, default=None, required=False):
    val = os.environ.get(key, default)
    if required and (val is None or val == ""):
        raise SystemExit(
            f"[config] missing required variable {key!r}. Set it in the repo-root "
            f".env (copy .env.example) or export it / add it in Coolify."
        )
    return val
