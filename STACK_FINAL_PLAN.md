# Build Plan: *arr Stack + Plex on Hetzner (Storage Box, TorBox debrid, Tailscale-only)

Single source of truth for this project. **Discarded:** mount architecture, DUMB,
NzbDAV, and Usenet. **Stack:** Decypharr(download mode)/TorBox, Prowlarr, Radarr,
Sonarr, Bazarr, Cleanuparr, Seerr, Plex on a Hetzner VPS with a **Storage Box**
for the library, **Tailscale** as the only network boundary.

Research dated 2026-09, labelled fact / inference / assumption.

---

## Why this shape

Real files on real storage — Decypharr downloads a **complete file to local
disk**, Radarr/Sonarr copy it into the Storage Box library, Plex serves it, the
iPhone downloads it for offline the normal way. No virtual filesystem, no alpha
software, no "does offline work" question.

**One source: TorBox debrid via Decypharr (download mode)** — chosen on **cost
and simplicity**. At $3/mo it's the cheapest content source, and a single debrid
client means no Usenet provider + indexer + retention math to manage. We watch
mainstream English releases; **Hebrew (and any other) subtitles are handled by
Bazarr/OpenSubtitles independently of the source**, so content coverage isn't the
deciding factor.

**TorBox, not Real-Debrid:** as a bonus, TorBox avoids RD's May-2026 keyword
filter (blocks 50-70% of cached releases) and RD's single-IP enforcement — TorBox
is multi-IP friendly ([RD vs TorBox 2026](https://iptvranking.com/real-debrid-vs-torbox/),
[ElfHosted filter-gate](https://store.elfhosted.com/blog/2026/05/12/real-debrid-filtering-may-2026/)).
Caveat: TorBox tightened its ToS ~2026-08 (session-replay telemetry, indefinite
retention, indemnification, explicit cross-user cache) ([Troypoint](https://troypoint.com/torbox-changes-their-terms-of-service/)).

---

## Architecture

| Service | Role |
|---|---|
| **Decypharr** (`download` mode) | The single download client. Fake-qBittorrent API to the arrs; sends releases to **TorBox**, pulls the cached file to **local disk** (not a mount, not a symlink). |
| **Prowlarr** | Manages **general-purpose torrent indexers**; feeds Radarr/Sonarr. |
| **Radarr / Sonarr** | Pick releases, import into the library, retry. Queue depth tuned to TorBox's 3 slots. |
| **Cleanuparr** | **Required.** Detects stalled/dead (seederless) items and triggers a re-search — with one source and no fallback, a stuck item otherwise blocks the request entirely. |
| **Bazarr** | Subtitles (incl. Hebrew) via OpenSubtitles; writes `.srt` sidecars beside media. Independent of the download source. |
| **Seerr** (Overseerr) | Watches both Plex watchlists; turns adds into requests. |
| **Plex** | The only user-facing app. Library, playback, offline downloads. |
| **Tailscale** | The network boundary (host + all clients). |

### Storage & path layout (the part that needs care)

**Fact:** a Storage Box is network-attached (SMB/CIFS, SSHFS/SFTP, WebDAV,
rclone) and **does not support hardlinks or symlinks** ([Hetzner CIFS notes](https://gist.github.com/x-yuri/ce43d0781ae28864cbcce8033a0b98a8);
[TRaSH hardlinks](https://github.com/trash-guides/guides/blob/master/docs/File-and-Folder-Structure/Hardlinks-and-Instant-Moves.md)).
So the "one filesystem, instant atomic moves" arr layout does **not** apply:

```
LOCAL VPS DISK (fast, high-churn)
  /opt/appdata/<service>     app configs + databases
  /data/downloads            Decypharr downloads/unpacks HERE

STORAGE BOX (mounted once on the host, bind-mounted into containers)
  /mnt/box/media/movies      Radarr library
  /mnt/box/media/tv          Sonarr library
```

- **Decypharr downloads on local disk** — debrid pulls are high-churn and
  unsuitable over a network mount.
- Radarr/Sonarr import = **copy** from `/data/downloads` to `/mnt/box/media`
  (cross-filesystem; copy+delete). Fine — nothing seeds.
- **Bazarr** writes `.srt` beside the video on the box (small CIFS writes).
- **Plex** reads the library from `/mnt/box/media`.

**Storage Box facts** ([overview](https://docs.hetzner.com/storage/storage-box/general/),
[SMB/CIFS access](https://docs.hetzner.com/storage/storage-box/access/access-samba-cifs/)):
- **10 concurrent connections.** Mount **once on the host** (CIFS) and
  **bind-mount** into containers so the stack shares one mount. This cap is the
  main reason for the Plex hardening below.
- ~70+ MB/s; put the Storage Box in the **same region as the VPS** (Germany).
  1080p direct play (~10-25 Mbps) is trivial for two users.
- SMB/CIFS must be **enabled in the Hetzner console first**; share name is
  `backup` (main account), port 445.

### TorBox Essential tier ($3) — constraints to configure around

- **3 concurrent download slots.** Cap Radarr/Sonarr simultaneous grabs / queue
  depth so the arrs don't over-queue against TorBox and stall.
- **300 GB permanent storage on TorBox's side — and Decypharr does NOT auto-clean
  it.** Verified against Decypharr's source/docs: its qBittorrent delete path
  deliberately keeps files, the Arr `cleanup` flag only tidies Decypharr's own
  queue, and the repair worker only touches *broken* items — **none of these
  remove completed torrents from your TorBox account** ([qBit mock](https://deepwiki.com/sirrobot01/decypharr/4.4-qbittorrent-api-mock),
  [config](https://deepwiki.com/sirrobot01/decypharr/5-configuration)). So the
  300 GB **will** fill as torrents accumulate. **Alternative (adopted): a
  scheduled TorBox-API reaper** (`scripts/torbox-reaper.py`) — a host
  systemd-timer that lists torrents (`GET /v1/api/torrents/mylist`) and deletes
  one (`POST /v1/api/torrents/controltorrent`, `{"torrent_id":N,"operation":"delete","all":false}`,
  Bearer) **only once Radarr/Sonarr report it `downloadFolderImported` AND the
  imported file still exists at its final path under `/mnt/box/media`** — gated
  on the file existing, never on age, so it cannot race an in-progress import.
  It also logs a **usage warning at/above 70% of the 300 GB regardless of
  dry-run**, so a forgotten `DRY_RUN=true` surfaces before adds start failing.
  (Set Arr `cleanup: true` in Decypharr too, so its queue stays tidy.)
- **Uncached torrents still work** — TorBox fetches from the swarm to their
  servers, then serves it (slower, not blocked). The real failure mode is a
  **dead torrent with no seeders**, which never completes — that's what
  **Cleanuparr** catches and re-searches.

### Plex over CIFS — hardening (do this BEFORE the first library scan)

Scans, thumbnailing, and intro/credit detection all hammer the network mount, and
the 10-connection cap amplifies it. In Plex settings, **before adding libraries /
the first scan**:

- **Disable** intro detection (Skip Intro).
- **Disable** credit detection.
- **Disable** video preview thumbnails (Generate video preview thumbnails = never).
- **Disable** chapter thumbnails.
- **Scanning: scheduled, not continuous.** Turn off "Scan my library
  automatically" / "Run a partial scan when changes are detected"; use a nightly
  scheduled scan (Radarr/Sonarr can trigger a targeted scan on import instead).
- Also: "Empty trash automatically after every scan" off; analyze/loudness off.

Doing this after the first scan means the thrash already happened and artifacts
already generated — set it first.

### Sizing

**8 GB VPS** (decided): comfortable for Plex + the services + Decypharr
(a light Go binary), with headroom and **no transcoding** (direct play only). EU
region; LinuxServer.io images are multi-arch so ARM (`CAX`) is fine. Local disk
just needs the download working area (1080p cap → ~2-15 GB/file).

---

## Security model

- **Bind every service to the Tailscale interface only**, never `0.0.0.0`.
- **Hetzner Cloud Firewall:** drop all inbound except Tailscale UDP (41641);
  SSH via Tailscale SSH.
- **Plex remote access OFF.** LAN networks = `100.64.0.0/10` so tailnet clients
  count as local (also sidesteps the Plex-Pass remote-stream requirement).
- **Separate Plex accounts per person.**
- **Coolify dashboard tailnet-only.**
- Secrets in **Coolify env vars**, never git: torrent indexer keys, **TorBox API
  token**, Storage Box credentials, *arr API keys, Plex claim token.
- Container auto-updates + off-box weekly config backup + Storage Box snapshot.

### Deployment via Coolify

Preconfigured in **`stack/`** (`docker-compose.yml`, `.env.example`, `README.md`):
LinuxServer.io arrs/Bazarr, official Plex, Overseerr, Decypharr, Cleanuparr, with
**consistent paths** — every container sees `/data/downloads` and `/mnt/box/media`
at the same path (no remote-path mappings), PUID/PGID defined once in `.env` and
matched to the fstab `uid/gid`, and every published port bound to `${TS_IP}`.
Deploy as one Coolify Docker-Compose resource; do **not** route through Coolify's
Traefik — bind to the tailnet. Host-level: Tailscale, firewall, and the Storage
Box CIFS mount (fstab).

---

## Off-VPS subscriptions

- **TorBox Essential ($3/mo)** — the single content source (torrent cache).
- **General-purpose torrent indexers** in Prowlarr — feeds Decypharr/TorBox.
- **Plex Pass** — offline downloads + remote-as-local (lifetime ~$250 kills $7/mo).
- **OpenSubtitles account** — Bazarr subtitles incl. Hebrew (free tier rate-limited).

Rough cost: Hetzner VPS ~$7.5 + Storage Box (1 TB) ~$4 + **TorBox ~$3** (content)
+ Plex Pass ~$7 ≈ **~$21/mo** (less with annual/lifetime deals). Content itself is
**~$3/mo**.

---

## Spikes

- **S1 - offline + download-to-disk sanity check.** Confirm end-to-end once:
  Decypharr `download` mode truly writes the file to **local disk** (not a
  symlink) — load-bearing for the whole copy-to-box pipeline — and the resulting
  library file downloads to the iPhone Plex app for offline. Also confirm the
  TorBox reaper deletes an imported torrent from the account (run it with
  `DRY_RUN=true` first and read the log) so the 300 GB doesn't fill.

---

## Build order

### Phase 1 - Foundation
Hetzner VPS (**8 GB**, EU) + **Storage Box (1 TB, same region, CIFS)**; Tailscale
on host + both iPhones; Hetzner Cloud Firewall (inbound = Tailscale UDP only);
Docker; Coolify (dashboard tailnet-only). Mount the Storage Box on the host at
`/mnt/box` via fstab. **Verify nothing answers on the public IP.** (Commands in
the appendix below.)

### Phase 2 - Core pipeline. VALUE CHECKPOINT.
Deploy the compose: Decypharr (`download` mode → TorBox, downloads to local
`/data`, Arr `cleanup: true`), Prowlarr (general torrent indexers), Radarr/Sonarr
(library on `/mnt/box/media`, capped 1080p H.264, simultaneous grabs ≤ TorBox's 3
slots), Cleanuparr (stalled/dead detection), Bazarr (OpenSubtitles, Hebrew),
Seerr on both watchlists, Plex. **Apply the Plex-over-CIFS hardening before the
first scan.** Plex: remote access OFF, LAN = `100.64.0.0/10`. Add the TorBox
reaper cron. From an iPhone: add **any mainstream title** to the Plex watchlist →
appears → plays over Tailscale → **Hebrew subtitles appear** → **downloads to the
device for offline.** Do not proceed until all four work. **Then run the
public-IP scan** (it's only meaningful now that all services are up):
`nmap -Pn -p 22,80,443,8000,32400,7878,8989,9696 <VPS_PUBLIC_IP>` — every port
must read filtered/closed from outside; the same services must answer only on the
`100.x` Tailscale address.

### Phase 3 - Hardening
Tune Cleanuparr thresholds against real stalls; verify the TorBox reaper keeps the
account under 300 GB; container auto-updates; off-box weekly config backup +
Storage Box snapshot; re-verify 1080p caps and that no Plex thumbnail/detection
jobs re-enabled themselves after updates.

### Phase 4 - Usenet (ONLY IF NEEDED)
If two weeks of real use shows TorBox missing titles often enough to matter, add
**SABnzbd + a Usenet provider + a Usenet indexer** as a **second download
client** (downloads/unpacks to local `/data`, then the same copy-to-box import).
Everything else stays identical — it's an added client, not a redesign. Set delay
profiles to prefer Usenet with debrid fallback. **Do not build or scaffold for
this now.**

### Clients
iPhones: Plex app + Tailscale. TV: Apple TV 4K (Ethernet, native tvOS Tailscale,
Infuse, auto refresh-rate). Avoid boxes without Ethernet + refresh-rate switching.
Add the Apple TV once Phase 2 passes.

---

## Appendix - Phase 1 commands

Assumes a fresh **Ubuntu 24.04** Hetzner Cloud VPS (create the 8 GB server + the
1 TB Storage Box in the Hetzner Console, same Germany region; enable **SMB/CIFS**
on the Storage Box under its settings). Run as root over the initial public SSH.

**Order matters: do NOT apply the firewall until you have proven Tailscale SSH
works, with the original public-SSH session still open.** Otherwise a broken
tailnet locks you out of your own box.

```bash
# 1. Base
apt update && apt -y upgrade
apt -y install curl cifs-utils nmap

# 2. Tailscale (host) — brings up Tailscale SSH so we can drop public SSH
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh --hostname=media-vps
tailscale ip -4                      # note the 100.x.y.z address
```

**>>> GATE: verify Tailscale SSH before touching the firewall. <<<**
Keep this first (public-SSH) session **open**. In a **second terminal** on your
laptop, confirm you can get in over the tailnet:

```bash
# second terminal, laptop (must succeed BEFORE you apply any firewall rule):
ssh root@<VPS_TAILSCALE_IP>
```

Only once that second session works do you continue.

```bash
# 3. Storage Box CIFS mount (main-account share is literally "backup", port 445).
#    No `seal`: SMB3 encryption burns shared-vCPU CPU on every byte Plex reads,
#    and traffic stays inside Hetzner's network. (If you ever add it, benchmark
#    read speed before building the library on top of it.)
#    Replace uXXXXXX + password with your Storage Box credentials.
printf 'username=uXXXXXX\npassword=YOUR_STORAGEBOX_PASSWORD\n' > /etc/box-credentials
chmod 600 /etc/box-credentials
mkdir -p /mnt/box
cat >> /etc/fstab <<'EOF'
//uXXXXXX.your-storagebox.de/backup /mnt/box cifs credentials=/etc/box-credentials,iocharset=utf8,rw,uid=1000,gid=1000,file_mode=0660,dir_mode=0770,_netdev,nofail 0 0
EOF
mount -a -v
mkdir -p /mnt/box/media/movies /mnt/box/media/tv   # library dirs on the box
mkdir -p /data/downloads /opt/appdata               # local working + config dirs
df -h /mnt/box                                       # confirm the box is mounted

# 4. Docker
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker

# 5. Coolify
curl -fsSL https://cdn.coollabs.io/coolify/install.sh | bash

# 6. Bind Coolify to the Tailscale interface EXPLICITLY (defense in depth — do
#    not rely on the firewall alone). Setting APP_PORT=IP:8000 in .env crashes
#    (the `expose:` needs a bare integer), so use an upgrade-safe override file.
TS_IP=$(tailscale ip -4 | head -n1)
cat > /data/coolify/source/docker-compose.custom.yml <<EOF
services:
  coolify:
    ports: !override
      - "${TS_IP}:8000:8080"
  soketi:
    ports: !override
      - "${TS_IP}:6001:6001"
      - "${TS_IP}:6002:6002"
EOF
cd /data/coolify/source && ./upgrade.sh   # re-render with the override applied
```

**Now (SSH-over-Tailscale confirmed) apply the Hetzner Cloud Firewall** — create
in the Console (or `hcloud`) and attach to the VPS. Inbound: **allow UDP 41641
from `0.0.0.0/0` + `::/0`** (Tailscale direct/DERP); **drop everything else** (no
public 22/80/443/8000). Outbound: allow all. Remove any inbound 22 rule — SSH is
now via Tailscale only.

```bash
# Optional: same firewall via hcloud CLI (run wherever hcloud is configured)
hcloud firewall create --name media-fw
hcloud firewall add-rule media-fw --direction in --protocol udp --port 41641 --source-ips 0.0.0.0/0 --source-ips ::/0
hcloud firewall apply-to-resource media-fw --type server --server media-vps
```

**Sanity check (Phase 1 level):** confirm Coolify answers on the tailnet and that
public SSH is now gone. (The full multi-service public-IP `nmap` scan belongs at
the **end of Phase 2**, once Plex/*arr actually exist — scanning their ports now
would prove nothing.)

```bash
curl -I http://<VPS_TAILSCALE_IP>:8000     # Coolify, reachable only over Tailscale
ssh -o ConnectTimeout=5 root@<VPS_PUBLIC_IP>   # should now time out / refuse
```

Then open Coolify at `http://<VPS_TAILSCALE_IP>:8000`, finish its setup, and move
to Phase 2 (deploy the compose).
