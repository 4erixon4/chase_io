# Phase 2 stack — deploy notes

Deploy this as one Coolify Docker-Compose resource (or `docker compose up -d`)
**after Phase 1** (VPS + Tailscale + firewall + Storage Box mounted at `/mnt/box`
+ Coolify bound to the tailnet). See `STACK_FINAL_PLAN.md` for the full plan.

## Before you bring it up

1. **Config/secrets come from the repo-root [`.env`](../.env.example)** (single
   source). Deploying via **Coolify**: set the same vars in the resource's
   *Environment Variables*. Deploying by hand: `docker compose --env-file ../.env
   -f docker-compose.yml up -d`.
2. **PUID/PGID (`.env`) must equal the fstab `uid=1000,gid=1000`** on the Storage
   Box mount. This is the single place the value is defined; the compose and the
   mount both point at it. Mismatch = containers can't write to `/mnt/box`.
3. **`TS_IP`** = `tailscale ip -4` on the VPS. Every port binds to this address
   only; nothing listens on `0.0.0.0`.
4. **Plex claim token — time-sensitive.** Open <https://www.plex.tv/claim>, copy
   the `claim-…` token into `PLEX_CLAIM`, and run `docker compose up -d`
   **within 4 minutes** — the token expires that fast. If Plex comes up
   unclaimed, regenerate a fresh token and recreate the Plex container. On later
   runs `PLEX_CLAIM` can be blank.

## Image versions / pinning

Fast-moving and load-bearing images are pinned to exact versions (2026-09):
Decypharr `cy01/blackhole:v2.5`, Cleanuparr `ghcr.io/cleanuparr/cleanuparr:2.10.5`,
Plex `plexinc/pms-docker:1.43.3.10896-cb3ebc72d`, Overseerr `sctx/overseerr:1.35.0`.

The LinuxServer arrs (radarr/sonarr/prowlarr/bazarr) are left on `latest` because
LSIO doesn't publish a semver tag that's knowable in advance — you **freeze** them
to the static `version-<upstream_version>` tag after one pull, *before* S1, so the
spike tests a fixed target:

```bash
docker compose pull radarr sonarr prowlarr bazarr
for s in radarr sonarr prowlarr bazarr; do
  echo -n "$s build_version: "
  docker inspect -f '{{ index .Config.Labels "build_version" }}' \
    "lscr.io/linuxserver/$s:latest"
done
```

Each line prints something like `Linuxserver.io version: 5.26.2.10099-ls123 …`.
Take the version token (`5.26.2.10099-ls123`) and set that service's image to
`lscr.io/linuxserver/<svc>:version-5.26.2.10099-ls123`, then `docker compose up
-d`. Now every image is pinned and reproducible.

> **Overseerr is deprecated** (last release v1.35.0). It still works and is pinned;
> its successor is **Seerr**. If/when you migrate, swap only the `overseerr`
> service — the rest of the stack is unaffected.

## After it's up (order matters)

> **API-key dependency:** steps 3, 7, and 8 all consume Radarr/Sonarr **API keys**,
> which don't exist until Radarr and Sonarr have started once and generated them
> (Settings → General → API Key in each). So do step 3 first; Cleanuparr (7) and
> the reaper (8) both read those same keys. Prowlarr (4) also needs them to push
> indexer configs. Bring Radarr/Sonarr up and grab their keys before wiring the
> rest.

1. **Plex — apply the CIFS hardening BEFORE the first library scan** (see
   `STACK_FINAL_PLAN.md`): disable intro detection, credit detection, video
   preview thumbnails, chapter thumbnails; set scanning to scheduled, not
   continuous. Then add libraries pointing at `/mnt/box/media/movies` and
   `/mnt/box/media/tv`. Also: Settings → Remote Access **OFF**; Network → LAN
   Networks = `100.64.0.0/10` (tailnet counts as local).
2. **Decypharr — confirm DOWNLOAD mode** (real files to `/data/downloads`, not
   symlinks). This is load-bearing; the compose comment says so. Set
   `download_folder` to `/data/downloads` and Arr `cleanup: true`.
3. **Radarr/Sonarr** → add Decypharr as a **qBittorrent** download client
   (host `decypharr`, port `8282`, category `radarr` / `sonarr`). Root folders:
   `/mnt/box/media/movies` and `/mnt/box/media/tv`. Cap simultaneous grabs to
   **≤ 3** (TorBox Essential slots). Quality profiles capped at 1080p H.264.
4. **Prowlarr** → add general-purpose torrent indexers; sync to Radarr/Sonarr.
5. **Bazarr** → OpenSubtitles provider, Hebrew (+English) subtitle profiles.
6. **Overseerr** → connect Plex + both watchlists, wire to Radarr/Sonarr.
7. **Cleanuparr** → point at Radarr/Sonarr + Decypharr; enable stalled/seederless
   cleanup.
8. **TorBox reaper** → install `../scripts/torbox-reaper.py` as the systemd timer
   (see that file's footer). Leave `DRY_RUN=true` for the first few runs.

## S1 — verify before you trust any of it (first real download, DRY_RUN on)

The two unknowns both get answered on the first grab:

- **Does Decypharr actually write a real file?** After a grab completes, check
  `/data/downloads` on the host — it must contain the actual media file, not a
  symlink (`ls -l` shows a regular file, not `->`). Then confirm Radarr/Sonarr
  copied it into `/mnt/box/media` and Plex plays it and the iPhone downloads it
  offline.
- **Does the reaper's infohash match?** Run `torbox-reaper.py` with
  `DRY_RUN=true` and read the log: the imported item should show `WOULD DELETE …
  file present at /mnt/box/media/…`. If it instead says `KEEP (not imported
  yet)` for something you know imported, the TorBox↔*arr infohash mapping is off
  — fix that before ever setting `DRY_RUN=false`. The reaper fails safe (keeps
  everything) until then.

Only once both check out: set the reaper `DRY_RUN=false`.
