# Local stack — learning & S1 validation (NOT production)

Run the full eight-service stack on your own machine to learn the UIs and settle
the first half of S1 **for free**, before spending anything on Hetzner. Ports
bind to `127.0.0.1`; media lives in gitignored `./downloads` and `./library`.

> **This instance is for learning and validation only. We rebuild fresh on the
> VPS — configs are NOT migrated.** Don't invest in tuning it; its job is to
> answer questions, not to become the deployment.

## What this does NOT test (so a clean local run isn't mistaken for validation)

The three highest-risk parts of the production design are **all untested here**,
because local disk behaves differently from a Hetzner Storage Box:

1. **Copy-vs-hardlink import path.** Local disk supports hardlinks; the Storage
   Box (CIFS) does not. Locally Radarr/Sonarr may hardlink or atomic-move; on the
   VPS they must **copy** across filesystems. Different code path, different disk
   and time cost.
2. **The 10-connection cap.** No CIFS, no connection limit locally.
3. **Plex-over-CIFS scan performance.** Scans/thumbnails/detection over a network
   mount are the real pain point; locally they run against fast local disk.

Also untested locally:
- **Tailscale / security model.** Everything is on `127.0.0.1`; the interface
  binding, firewall, and tailnet-only access are not exercised.
- **The reaper's `mnt-box.mount` systemd dependency** — there's no such mount
  locally; the reaper is run by hand here (see below).

## Prerequisites

- Docker + Docker Compose.
- A **paid** TorBox tier. The **free tier has no API access**, so Decypharr
  cannot drive it — the download test below won't run without a paid token.
- A free OpenSubtitles account (for Bazarr).

## Key / credential order (several are self-generated; the order matters)

1. **Up front (external):**
   - **TorBox API token** — entered into **Decypharr** (paid tier required).
   - **OpenSubtitles** login — entered into **Bazarr**.
2. **Plex claim token** — from <https://www.plex.tv/claim>, **expires 4 minutes**
   after you generate it. Put it in `.env` right before `docker compose up -d`.
3. **Self-generated API keys** — **Radarr, Sonarr, and Prowlarr each mint their
   own API key on first run** (Settings → General → API Key). **Prowlarr,
   Cleanuparr, Bazarr, and the reaper all consume those keys**, so those steps
   cannot be done until the *arrs have started at least once.

## Run order

Config/secrets now live in the **repo-root `.env`** (single source — see
`../.env.example`), not a per-directory file. From the repo root:

```bash
cp .env.example .env          # fill in PLEX_CLAIM (fresh!), PUID/PGID, TZ, secrets
mkdir -p local/downloads local/library local/appdata
docker compose --env-file .env -f local/docker-compose.yml up -d
```

> Prefer the **scripted** setup (`scripts/setup_*.py`, see the root `README.md`
> and `SETUP_RUNBOOK.md` §0) over the manual steps below — the scripts read the
> same `.env`. The manual walkthrough here is kept only to learn the UIs.

Then, in order (manual reference):
1. **Radarr** (http://127.0.0.1:7878) and **Sonarr** (http://127.0.0.1:8989) —
   let them start, then copy each one's **API key** (Settings → General).
   Set root folders: Radarr `/mnt/box/media/movies`, Sonarr `/mnt/box/media/tv`.
2. **Decypharr** (http://127.0.0.1:8282) — add your **TorBox** token; set the
   action to **download** and `download_folder` to `/data/downloads`; add
   `radarr`/`sonarr` categories; set Arr `cleanup: true`.
3. In **Radarr/Sonarr** → add Decypharr as a **qBittorrent** download client
   (host `decypharr`, port `8282`, category `radarr`/`sonarr`).
4. **Prowlarr** (http://127.0.0.1:9696) — add a couple of general torrent
   indexers; add Radarr/Sonarr apps (needs their API keys from step 1).
5. **Bazarr** (http://127.0.0.1:6767) — OpenSubtitles provider, Hebrew+English.
6. **Overseerr** (http://127.0.0.1:5055) — optional locally; connect Plex + arrs.
7. **Cleanuparr** (http://127.0.0.1:11011) — point at Radarr/Sonarr + Decypharr
   (needs the API keys from step 1).
8. **Plex** (http://127.0.0.1:32400/web) — add libraries at
   `/mnt/box/media/movies` and `/mnt/box/media/tv`.

## S1 — the one question this local run must answer

**Does Decypharr in download mode write a regular file, or a symlink?**

Grab one movie in Radarr, let it complete, then on the host:

```bash
ls -l local/downloads        # look in the category/subfolders too
find local/downloads -maxdepth 3 -type l   # lists any SYMLINKS
```

- A **regular file** (real size, not listed by the `find … -type l`): download
  mode works — the Storage Box design is sound, proceed to provision the VPS.
- A **symlink** (`->` in `ls -l`, or shown by `find … -type l`): **stop.** The
  copy-to-Storage-Box pipeline cannot work as designed, and the **Storage Box
  design needs rethinking before we provision anything.**

## Reaper — verify infohash mapping + size field (dry-run, real TorBox account)

Run the reaper on the host against your real TorBox account, **`DRY_RUN=true`**:

```bash
cd ..
TORBOX_API_KEY=xxxx \
RADARR_URL=http://127.0.0.1:7878  RADARR_API_KEY=xxxx \
SONARR_URL=http://127.0.0.1:8989  SONARR_API_KEY=xxxx \
MEDIA_ROOT="$(pwd)/local/library" \
DRY_RUN=true \
python3 scripts/torbox-reaper.py
```

What to confirm from the log:
- **Infohash mapping:** the movie you just imported must be **matched to an
  import record** — you should NOT see `KEEP (not imported yet)` for it. (It will
  likely say `KEEP (… file missing on box)` because Radarr records the *container*
  path `/mnt/box/media/…` while the host folder is `./library`; that's expected
  locally and still proves the hash matched. The full delete gate is validated on
  the VPS, where host and container share `/mnt/box/media`.)
- **Size field:** the `TorBox usage ~X GB / 300 GB (Y%)` line must roughly
  **match the TorBox dashboard**. If it's wildly off, `mylist`'s `size` is a
  different unit / omitted for incomplete items — fix the unit/field in
  `scripts/torbox-reaper.py` before the quota warning can be trusted.

## Teardown

```bash
cd local
docker compose down            # keep ./appdata to resume, or:
docker compose down -v && rm -rf appdata downloads library   # wipe clean
```
