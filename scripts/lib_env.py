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
import subprocess

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


def container_id(service):
    """Resolve a running container by its compose SERVICE label — works whether
    the container is named plainly (local `docker compose`) or mangled by
    Coolify (radarr-<hash>), and regardless of the compose project directory."""
    r = subprocess.run(
        ["docker", "ps", "-q", "-f", f"label=com.docker.compose.service={service}"],
        capture_output=True, text=True)
    ids = r.stdout.split()
    return ids[0] if ids else None


def dc(args, compose_dir=None, **kw):
    """docker-compose shim used by the setup scripts.

    `exec`/`restart` are resolved to `docker exec`/`docker restart` against the
    container found by compose service label, so they work on Coolify (where the
    build dir is deleted post-deploy and container_name is rewritten). Any other
    subcommand falls back to real `docker compose` in `compose_dir` (local dev).
    """
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    if args and args[0] in ("exec", "restart"):
        if args[0] == "exec":
            rest = [a for a in args[1:] if a != "-T"]
            svc, cmd = rest[0], rest[1:]
        else:
            svc, cmd = args[1], None
        cid = container_id(svc)
        if not cid:
            return subprocess.CompletedProcess(
                args, 1, "", f"[lib_env] no running container for service '{svc}'")
        base = ["docker", "exec", cid] + cmd if args[0] == "exec" \
            else ["docker", "restart", cid]
        return subprocess.run(base, **kw)
    return subprocess.run(["docker", "compose"] + args, cwd=compose_dir, **kw)
