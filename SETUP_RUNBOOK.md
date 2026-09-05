# Setup Runbook — configure every service, page by page

Do the steps **in this order**. Each service lists: the **URL**, what you do on
that page, which **key** you grab or paste, and **where to get** anything
external. Written for the **local** run (`127.0.0.1`), with a **VPS** note where
it differs.

- **Local URL:** `http://127.0.0.1:<port>`
- **VPS URL:** `http://<TAILSCALE_IP>:<port>` (same ports; reachable only over Tailscale)
- **Inside the stack, services talk to each other by name, not 127.0.0.1** — e.g.
  Radarr reaches Decypharr at `http://decypharr:8282`. Use those internal
  addresses whenever a field asks for another service's URL. They're identical
  local and on the VPS.

Ports: Plex 32400 · Radarr 7878 · Sonarr 8989 · Prowlarr 9696 · Bazarr 6767 ·
Decypharr 8282 · Cleanuparr 11011 · Overseerr 5055.

---

## Accounts / secrets to get BEFORE you start (where to get each)

1. **TorBox API token** — sign in at <https://torbox.app> → **Settings → API**
   (or "API Keys") → create/copy the token. **Requires a paid tier** (Essential
   $3); the free tier has **no API access**, so Decypharr can't use it.
2. **OpenSubtitles account** — register free at <https://www.opensubtitles.com>
   (note: `.com`, the new site). You'll enter this username+password into Bazarr.
3. **Plex claim token** — <https://www.plex.tv/claim> → copy the `claim-…` string.
   **Expires 4 minutes** after you generate it. (Already used for the local run;
   you'll generate a fresh one on the VPS.)
4. **A Plex account** — free, <https://www.plex.tv> (used to sign into Plex and
   Overseerr).

Torrent **indexers** don't need a paid key — see the Prowlarr section.

---

## 0. Automated configuration — `scripts/setup_stack.py`  (do this first)

Sections **1–5 below are done for you by a script** so you don't click through
them. It's **idempotent** (safe to re-run) and API-driven, so it works the same
locally now and on the Linux VPS later.

**Prereqs:** the stack is up, and you've been through Decypharr's first-run
wizard once (TorBox token + Mount = None). The *arrs must have booted once so
they've generated their API keys.

**Secrets:** put non-discoverable secrets in the repo-root **`.env`** (gitignored;
copy `.env.example`). The scripts auto-load it via `scripts/lib_env.py`:

```
DECYPHARR_USER=<decypharr ui user>
DECYPHARR_PASS=<decypharr ui password>
TORBOX_API_KEY=<torbox token>
OPENSUBTITLES_USER=<opensubtitles user>
OPENSUBTITLES_PASS=<opensubtitles password>
CLEANUPARR_USER=<pick any — the script creates this Cleanuparr admin account>
CLEANUPARR_PASS=<pick any strong password>
```

The Radarr/Sonarr/Prowlarr API keys are **auto-discovered** from the containers'
`config.xml`, so you don't paste them anywhere.

**Run it:**

```bash
python scripts/setup_stack.py         # local (COMPOSE_DIR defaults to "local")
python scripts/verify.py              # prints end state, no secrets
```

**On the VPS:** `COMPOSE_DIR=/path/to/stack python3 scripts/setup_stack.py`
(uses the same internal `service:port` addresses, so nothing else changes).

**What it configures:**
1. **Decypharr** — sets `default_download_action = "download"` in
   `/app/config.json` (the load-bearing field — the wizard leaves it on
   `symlink`, which breaks the copy-to-Storage-Box design) and restarts it.
2. **Radarr / Sonarr** — root folder (`/mnt/box/media/movies|tv`) + a
   **qBittorrent download client "Decypharr"** (host `decypharr:8282`, category
   `radarr`/`sonarr`, using the Decypharr UI username/password since Decypharr
   has auth on).
3. **Prowlarr** — adds Radarr + Sonarr as applications, adds several reliable
   **public** torrent indexers (`TorrentsCSV`, `thepiratebay`, `yts`,
   `torrentdownloads`), and triggers an app-indexer sync.

**Notes / known-harmless warnings:**
- Some indexers (`1337x`, `eztv`) fail with a **Cloudflare** error — they need
  FlareSolverr (not in our stack). Skipped automatically; the others cover
  movies + TV.
- During bulk sync Prowlarr may log **`429 TooManyRequests`** while the arrs
  test each synced indexer's caps at once. That's rate-limiting, not a config
  error — re-run the script or wait for the scheduled sync and the rest land.

### The whole stack is scripted — run these in order

Only **two one-time manual sign-ins** remain (Plex claim + Overseerr's Plex
OAuth); everything else is API-driven and idempotent.

```bash
# 1. Bring the stack up, let every container boot once (arrs generate API keys).
docker compose up -d            # (local)  |  on VPS: in your stack dir

# 2. Decypharr first-run wizard once (TorBox token + Mount = None) — UI, one time.

# 3. Core arrs + Decypharr + Prowlarr
python scripts/setup_stack.py

# 4. Cleanuparr (creates its own admin account from .env)
python scripts/setup_cleanuparr.py

# 5. Bazarr (Sonarr/Radarr + OpenSubtitles + EN/HE language profile)
python scripts/setup_bazarr.py

# 6. Plex — sign in / claim the server in the UI ONCE, then:
python scripts/setup_plex.py     # CIFS-friendly prefs + Movies/TV libraries

# 7. Overseerr — sign in with Plex in the UI ONCE, then:
python scripts/setup_overseerr.py

# Verify end state (no secrets printed)
python scripts/verify.py
```

On the VPS it's identical, with `COMPOSE_DIR=/path/to/stack` in front of each
`python3 scripts/...` call. No config is migrated from local — these scripts
rebuild the same setup from scratch.

Sections 1–9 below are kept **as reference / fallback** — what each script set,
in case you want to check or tweak it by hand.

---

## 1. Radarr — movies  ·  http://127.0.0.1:7878   *(scripted — reference only)*

First run, no login by default.

1. **Settings → Media Management:** turn ON "Rename Movies" (optional but nice).
2. **Settings → Media Management → Root Folders → Add Root Folder:**
   `/mnt/box/media/movies`.
   - *(If it says "path does not exist," the folder isn't created yet — locally
     I made `library/movies`; on the VPS the Storage Box mount must be up first.)*
3. **Settings → General → Security → API Key:** copy this. Call it **RADARR_KEY**.
   You'll paste it into Prowlarr, Bazarr, Cleanuparr, Overseerr, and the reaper.
4. **Settings → Profiles:** set a Quality Profile capped at **1080p** (avoid 4K —
   no transcoding, keep files direct-play).

VPS note: root folder is the same path (`/mnt/box/media/movies`), backed by the
Storage Box mount instead of a local folder.

---

## 2. Sonarr — TV  ·  http://127.0.0.1:8989   *(scripted — reference only)*

Same as Radarr:

1. **Root Folder:** `/mnt/box/media/tv` (NOT `/movies`).
2. **Settings → General → API Key:** copy it → **SONARR_KEY**.
3. **Quality Profile:** cap at 1080p.

---

## 3. Decypharr — the download client (→ TorBox)  ·  http://127.0.0.1:8282   *(wizard by hand; download-mode set by script)*

This is the load-bearing one. It pretends to be qBittorrent for the *arrs and
pulls from TorBox.

> **Know this before you click:** Decypharr's *native* design is a **streaming
> bridge** — TorBox downloads on their servers and Decypharr **mounts** them so
> files appear locally as **symlinks** (nothing on your disk). Our design needs
> the opposite: **real files on disk** (download mode) so Radarr can copy them to
> the Storage Box. We force that by choosing **Mount System = None**. Whether a
> real file actually lands is the **S1 test** (step 10) — decided right here.

### Setup Wizard, step by step
1. **Authentication:** set a UI username/password (or the default). Locally it
   doesn't matter; on the VPS pick a real password.
2. **Debrid Account:** provider **TorBox**, paste your **TorBox API token**
   (torbox.app → Settings → API). If there's a **"Download Uncached"** toggle,
   turn it **ON** (lets TorBox swarm-fetch torrents it hasn't cached yet).
3. **Usenet Provider (Optional):** **Skip Usenet.** We're TorBox-only. (The
   yellow "either usenet or debrid must be configured" note is satisfied by
   step 2.)
4. **Download Folder:** `/data/downloads`.
5. **Mount System:** **None.** (No FUSE mount → it can't symlink a stream → it
   should download real files. Our container has no `/dev/fuse`/`SYS_ADMIN`, so
   mount modes wouldn't work anyway.)
6. **Overview:** finish.

### After the wizard — open Settings (gear icon)
Decypharr **2.5** tabs: **General · Providers · *Arrs · Mounts · Shares ·
Repair**. Under **General** there are sub-tabs: **General · Virtual Folders ·
Downloads · Auth · Notifications**. (Older docs call these "QBittorrent" /
"Categories" / "Debrid" — 2.5 renamed them.)

- **General → Downloads:** set **Download Folder = `/data/downloads`** (the
  load-bearing field — where real files should land).
- **General → Virtual Folders:** this is the **categories** feature. Ensure a
  **`radarr`** and a **`sonarr`** entry exist (add them if empty). Radarr/Sonarr's
  qBittorrent **Category** field must match these names exactly (step 4).
- **Providers:** confirm the **TorBox** provider + token (from the wizard); enable
  **Download Uncached** if offered.
- ***Arrs → "Add Arr Service":** add Radarr (Host `http://radarr:7878`, Token =
  RADARR_KEY, **cleanup** ON) and Sonarr (Host `http://sonarr:8989`, Token =
  SONARR_KEY, **cleanup** ON). Type is auto-detected.
- **Mounts:** ensure **no mount is enabled** (mode `none`) — we want
  download-to-disk, and the container has no FUSE.
- Click **Save Configuration** (top-right).
- **The download-vs-symlink action is now forced by `setup_stack.py`**, which
  sets `default_download_action = "download"` in `/app/config.json` (the wizard
  leaves it on `symlink`). You don't need to hunt for a toggle. If, after the
  first grab, `ls -l` *still* shows a **symlink**, that's the S1 "rethink the
  Storage Box design" signal.

Where the token comes from: TorBox site → Settings → API (see top of doc).

---

## 4. Back in Radarr & Sonarr — add Decypharr as the download client   *(scripted — reference only)*

In **each** app: **Settings → Download Clients → + → qBittorrent**:
- **Host:** `decypharr`   **Port:** `8282`   (internal name, not 127.0.0.1)
- **Category:** `radarr` (in Radarr) / `sonarr` (in Sonarr)
- **Username/Password:** the Decypharr UI username/password (auth is **on**).
- **Test** → **Save**.

---

## 5. Prowlarr — indexers  ·  http://127.0.0.1:9696   *(scripted — reference only)*

Prowlarr manages all your torrent indexers and **pushes them automatically** to
Radarr/Sonarr (you don't add indexers in Radarr/Sonarr directly).

1. **Settings → General → API Key:** copy it → **PROWLARR_KEY**.
2. **Indexers → Add Indexer:** search the built-in list and add **public torrent
   indexers** — these need **no account/key**, just add and Save. Good general
   ones: **1337x**, **The Pirate Bay**, **YTS**, **EZTV**, **TorrentGalaxy**,
   **LimeTorrents**. Add several for coverage.
   - Some sites sit behind Cloudflare and need a helper called **FlareSolverr**
     (not in our stack). If an indexer fails its test with a Cloudflare error,
     skip it for now — the ones above generally work without it.
   - *Private trackers* (if you ever join one) need your account cookie/API key
     from that tracker's site — out of scope for now.
3. **Settings → Apps → Add App → Radarr:**
   - **Prowlarr Server:** `http://prowlarr:9696`
   - **Radarr Server:** `http://radarr:7878`
   - **API Key:** RADARR_KEY → **Test → Save**.
4. **Add App → Sonarr:** Sonarr Server `http://sonarr:8989`, API Key SONARR_KEY.
5. **Sync App Indexers** (or it syncs automatically). Now Radarr/Sonarr can
   search all those indexers.

Why no API key for indexers here: public torrent indexers are open; Prowlarr
already ships their definitions. The "keys" that matter are the *arr keys, which
Prowlarr uses to push config to them.

---

## 6. Cleanuparr — unstick dead downloads  ·  http://127.0.0.1:11011   *(scripted)*

With one source and no fallback, a dead (seederless) torrent or a transient
TorBox/Cloudflare error (e.g. the 524 that stalled Sherlock S01/S03) would block
a request forever. Cleanuparr strikes those, removes them from Decypharr,
blocklists the release, and lets Radarr/Sonarr grab a different one.

```bash
python scripts/setup_cleanuparr.py
```

Fully scripted — **including first-run account creation**, so there is nothing
to click. It reads `CLEANUPARR_USER`/`CLEANUPARR_PASS` from the repo-root `.env`
and auto-discovers the Radarr/Sonarr API keys from the containers.

What it configures:
- Creates the admin account (idempotent) and logs in for a JWT.
- Adds **Sonarr** (`http://sonarr:8989`) and **Radarr** (`http://radarr:7878`).
- Adds **Decypharr** as a qBittorrent download client (`http://decypharr:8282`).
- Enables the **Queue Cleaner** (runs every 5 min): failed imports get 3 strikes
  (`patternMode=Exclude`, i.e. all failures count), then removal + blocklist +
  re-search. Metadata-stuck downloads get 3 strikes too.

**VPS parity:** the config itself is *not* migrated (we rebuild fresh), but this
script reproduces it identically — run the same command on the VPS and it
registers a fresh account from `.env` and rebuilds the same setup. The
internal addresses (`sonarr:8989`, `radarr:7878`, `decypharr:8282`) are the same
locally and on the VPS.

> Cleanuparr's API is undocumented; the script talks to it directly
> (`/api/auth/setup/account`, `/api/configuration/{sonarr,radarr,download_client,
> queue_cleaner}`). If a future image version changes those routes, this is where
> to look.

---

## 7. Bazarr — subtitles (incl. Hebrew)  ·  http://127.0.0.1:6767   *(scripted)*

```bash
python scripts/setup_bazarr.py
```

Fully scripted via Bazarr's settings API. Reads `OPENSUBTITLES_USER`/`_PASS`
from the repo-root `.env`; auto-discovers the arr keys and Bazarr's own API key from
the containers. It:
- Connects **Sonarr** (`sonarr:8989`) and **Radarr** (`radarr:7878`) and sets
  `use_sonarr`/`use_radarr`.
- Enables the **OpenSubtitles.com** provider with your credentials.
- Creates a language profile **EN+HE** and sets it as the **default** for both
  series and movies, so every arr item gets subtitles searched automatically.

Change the languages by editing `LANGS` at the top of the script. Bazarr writes
`.srt` files next to the video under `/mnt/box/media`. First sync (pulling the
series/movies from the arrs) can take a minute.

---

## 8. Overseerr — request UI  ·  http://127.0.0.1:5055   *(sign-in manual; rest scripted)*

**Manual (browser, only you can):**
1. **Sign in with Plex** (your Plex account).
2. **Configure Plex step:** click 🔄 to load servers and select yours, but then
   **override Hostname to `plex`** (the docker service name) + Port `32400`,
   SSL off → **Save Changes**. (Auto-detected `127.0.0.1` is unreachable from
   inside the Overseerr container — locally Plex even advertises `127.0.0.1`.)
   → **Sync Libraries** → enable **Movies** + **TV Shows** → run the one-time
   full **Manual Library Scan**.
3. **Configure Services step:** leave Radarr/Sonarr **empty** and click
   **Finish Setup** — the script fills them in.

**Then scripted:**
```bash
python scripts/setup_overseerr.py
```
Reads Overseerr's own API key + the arr keys, and:
- Adds **Radarr** (`radarr:7878`, root `/mnt/box/media/movies`) and **Sonarr**
  (`sonarr:8989`, root `/mnt/box/media/tv`) as default services.
- Enables **Plex Watchlist auto-request** for every Overseerr user
  (`watchlistSyncMovies`/`watchlistSyncTv`).
- Adds the built-in **Radarr/Sonarr → Plex Media Server** connection
  (`host plex`, token read from Plex's `Preferences.xml`, `updateLibrary` on) so
  Plex **scans on import** — new files appear in the library promptly instead of
  waiting for Plex's scheduled scan.

**Result (local, 2026-09-05):** both services added, watchlist sync on for 1
user, Plex scan-on-import wired for Radarr + Sonarr.

**The end-user flow this unlocks:** add a title to your **Plex Watchlist** →
Overseerr auto-requests it (polls every few minutes) → Radarr/Sonarr grab it via
TorBox → file lands in `/mnt/box/media` → Radarr/Sonarr ping Plex → it appears in
your library. Caveats: not instant, and only works if one of your indexers has
the title.

---

## 9. Plex — the player  ·  http://127.0.0.1:32400/web   *(sign-in manual; rest scripted)*

**Manual, once:** open Plex and **sign in / claim** the server (fresh claim
token from <https://www.plex.tv/claim>, expires in 4 min). That writes the
`PlexOnlineToken` the script needs.

**Then scripted:**
```bash
python scripts/setup_plex.py
```
Reads the token from Plex's `Preferences.xml` and:
- Applies **CIFS-friendly server prefs BEFORE creating libraries** (so they
  inherit them): continuous FS-watching **off** + **scheduled** scans instead;
  scanner at low priority; **off** = video preview thumbnails (BIF), chapter
  thumbnails, ad markers, loudness analysis, and (Plex-Pass-only, best-effort)
  intro/credit detection. This is the single sharpest perf edge in the design —
  Plex over the Storage Box (CIFS, 10-connection cap).
- Creates the **Movies** (`/mnt/box/media/movies`) and **TV Shows**
  (`/mnt/box/media/tv`) libraries if missing (idempotent by title).

Still do by hand (small, one-time UI toggles the API doesn't cover cleanly):
- **Settings → Remote Access:** leave **OFF**.
- **Settings → Network → LAN Networks:** `100.64.0.0/10` (so tailnet clients
  count as local — matters on the VPS).

---

## 10. The S1 test (why we're doing all this locally)  ·  **scripted**

```bash
python scripts/s1_test.py
```
This adds a **public-domain** movie ("Night of the Living Dead", 1968) to Radarr,
forces a search across the Prowlarr indexers, waits for the grab, then inspects
`/data/downloads` inside Decypharr and reports **real file vs symlink**.

- **Regular file** → download mode works → the Storage Box design is sound.
- **Symlink** → STOP; the copy-to-Storage-Box design must be rethought before
  provisioning the VPS.

**Result (local, 2026-09-05): PASS.** Decypharr wrote a real `.mkv` (grew
110 MB → 3.3 GB of actual bytes on disk), zero symlinks. Manual equivalent if
you want to eyeball it:
```bash
docker compose -f local/docker-compose.yml exec decypharr sh -c 'ls -laR /data/downloads'
```

## 11. The reaper (dry-run) — verify infohash mapping + quota field  ·  **scripted**

```bash
python scripts/run_reaper.py
```
This launcher reads `TORBOX_API_KEY` from the repo-root `.env`, auto-discovers
the Radarr/Sonarr keys, points `MEDIA_ROOT` at `local/library`, forces
`DRY_RUN=true`, and runs `scripts/torbox-reaper.py`. (Manual equivalent: set
those env vars yourself and call the reaper directly.)

- The `TorBox usage ~X GB / 300 GB` line must roughly match the TorBox dashboard
  — confirms the `size` field is bytes and the quota math is right.
- Before import: `KEEP (not imported yet)` — the **safety gate** correctly
  refuses to delete. After import it becomes a positive match; locally it then
  says `KEEP (… file missing on box)` because Radarr records the container path —
  expected, and it still proves the infohash matched.

**Result (local, 2026-09-05): PASS.** TorBox reachable, `usage ~4.4 GB / 300 GB`
read correctly, un-imported item correctly KEPT.

> **Fix that came out of this test:** TorBox is behind **Cloudflare**, which
> `403`s the default `Python-urllib` User-Agent (Cloudflare error 1010). The
> reaper now sends a browser-like User-Agent. This matters on the VPS too.

---

## Key directory (fill in as you go)

| What | Where you got it | Where it's used |
|---|---|---|
| RADARR_KEY | Radarr → Settings → General | Prowlarr, Bazarr, Cleanuparr, Overseerr, reaper |
| SONARR_KEY | Sonarr → Settings → General | Prowlarr, Bazarr, Cleanuparr, Overseerr, reaper |
| PROWLARR_KEY | Prowlarr → Settings → General | (Prowlarr internal / API use) |
| TorBox token | torbox.app → Settings → API | Decypharr, reaper |
| OpenSubtitles login | opensubtitles.com | Bazarr |
| Plex claim | plex.tv/claim (4-min expiry) | `.env` `PLEX_CLAIM` at first boot |

---

## What's DIFFERENT on the VPS (don't skip)

- **Host setup first** (see `STACK_FINAL_PLAN.md` appendix): Tailscale + verify
  SSH over the tailnet **before** the firewall; Hetzner firewall (inbound =
  Tailscale UDP only); mount the **Storage Box** at `/mnt/box` via CIFS/fstab
  (create `media/movies` + `media/tv` on it); bind Coolify to the tailnet.
- URLs become `http://<TAILSCALE_IP>:<port>` (internal `service:port` addresses
  above stay the same).
- Use `stack/.env` (has `TS_IP`) and a **fresh** Plex claim token.
- Do the **Plex CIFS hardening (step 9) BEFORE the first scan.**
- Install the **reaper systemd timer** (see `scripts/torbox-reaper.py` footer),
  leave `DRY_RUN=true` until S1 passes, then flip to `false`.
- **We rebuild fresh on the VPS — configs are not migrated from local.**
