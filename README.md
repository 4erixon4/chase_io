# chase_io — self-hosted *arr + Plex media stack

A preconfigured, **Tailscale-only** media stack for a Hetzner VPS: Plex fed by
Radarr/Sonarr, content pulled from **TorBox** (debrid) via **Decypharr** in
download mode, with a **Hetzner Storage Box** for the library. Every service is
wired up by **idempotent Python scripts** rather than manual clicking, so the
same setup reproduces exactly on the VPS.

> Personal-use media automation. You are responsible for what you download and
> for complying with the terms of the services you use.

## What's in the box

| Service | Role |
|---|---|
| **Decypharr** (download mode) | Single download client; fake-qBittorrent API to the arrs; pulls TorBox files to **local disk** (real files, not symlinks). |
| **Prowlarr** | Torrent indexers → Radarr/Sonarr. |
| **Radarr / Sonarr** | Movies / TV; import (copy) into the Storage Box library. |
| **Cleanuparr** | Auto-recovery: strikes stalled/failed downloads, blocklists, re-searches. |
| **Bazarr** | Subtitles (EN/HE) via OpenSubtitles, independent of the source. |
| **Overseerr** | Requests + Plex Watchlist auto-request. |
| **Plex** | The only user-facing app; playback + offline. |
| **Tailscale** | The network boundary — nothing is exposed publicly. |

Full rationale, storage/path design, TorBox tier constraints, and security model:
**[`STACK_FINAL_PLAN.md`](STACK_FINAL_PLAN.md)** (single source of truth).

## Repo layout

```
.env.example          Single template for ALL config + secrets (copy to .env)
STACK_FINAL_PLAN.md   Architecture & decisions
DEPLOY.md             VPS deployment runbook (agent + user, step by step)
SETUP_RUNBOOK.md      Per-service configuration detail + the scripted run order
stack/                Production compose (deployed via Coolify on the VPS)
local/                Local dev compose (bind to 127.0.0.1) for learning/testing
scripts/              Idempotent setup + maintenance scripts (see below)
```

## Scripts

All read config/secrets from the repo-root **`.env`** (via `scripts/lib_env.py`),
auto-discover the arr/Prowlarr/Bazarr API keys from the containers, and are safe
to re-run. `COMPOSE_DIR` selects the compose (`local` default; set to the stack
dir on the VPS) and `API_HOST` the host (`127.0.0.1` local; the Tailscale IP on
the VPS).

| Script | Does |
|---|---|
| `setup_stack.py` | Decypharr download mode, Radarr/Sonarr root+client, Prowlarr apps+indexers |
| `setup_cleanuparr.py` | Creates Cleanuparr admin, wires arrs + Decypharr, enables queue cleaner |
| `setup_bazarr.py` | Wires arrs, OpenSubtitles provider, EN+HE default language profile |
| `setup_plex.py` | CIFS-friendly Plex prefs + creates Movies/TV libraries |
| `setup_overseerr.py` | Wires arrs, Plex Watchlist auto-request, scan-on-import |
| `verify.py` | Prints end-state of the stack (no secrets) |
| `torbox-reaper.py` / `run_reaper.py` | Keeps TorBox under its storage quota (dry-run launcher) |
| `s1_test.py` | The S1 spike: proves Decypharr writes real files, not symlinks |

## Secrets

**All secrets live in a single gitignored `.env`** at the repo root (see
`.env.example`). Nothing secret is committed. On the VPS you either keep the same
`.env` on the host or set the same variables in **Coolify's Environment
Variables** — the scripts read whichever is present (real env wins over the file).

## Getting started

- **Local (learn / test):** copy `.env.example` → `.env`, fill it in, then
  `docker compose --env-file .env -f local/docker-compose.yml up -d` and follow
  [`SETUP_RUNBOOK.md`](SETUP_RUNBOOK.md).
- **Production (VPS):** follow [`DEPLOY.md`](DEPLOY.md) top to bottom.
