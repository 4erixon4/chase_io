# VPS Deployment Runbook (agent + user)

How we take this repo from nothing to a running stack on a Hetzner VPS. It is
written as a **checklist for the agent**, with every point where the agent must
**stop and hand off to you** called out explicitly.

Architecture and rationale live in [`STACK_FINAL_PLAN.md`](STACK_FINAL_PLAN.md).
Per-service configuration detail lives in [`SETUP_RUNBOOK.md`](SETUP_RUNBOOK.md).
This file is the *order of operations* for a real deploy.

## Legend

| Tag | Meaning |
|---|---|
| 🤖 **AGENT** | The agent runs this (over SSH on the VPS, or locally in the repo). |
| 👤 **USER** | Only you can do this (console, browser, account, physical device). |
| ⛔ **GATE** | Do **not** proceed past here until the check passes — lockout / data risk. |
| ❓ **ASK** | The agent must ask you for a value or a go-ahead before continuing. |

> **Golden rule for the agent:** never apply the firewall, never delete the
> public-SSH session, and never run a destructive command until the
> corresponding ⛔ GATE above it has been confirmed *in this session*.

---

## Prerequisites — accounts & tokens (👤 USER, before we start)

Gather these once and put the secret ones into the repo-root **`.env`** (copy
`.env.example`). The agent will also need them on the VPS — see Phase 4.

1. **Hetzner Cloud account** — to create the VPS + Storage Box.
2. **A domain** — optional; not required for a Tailscale-only deploy.
3. **Tailscale account** — free; this is the network boundary.
4. **TorBox Essential ($3/mo)** — API token from <https://torbox.app> → Settings
   → API. Paid tier required (free tier has no API). → `TORBOX_API_KEY`
5. **OpenSubtitles.com** account (free) — for Bazarr. → `OPENSUBTITLES_*`
6. **Plex account** (free) + **Plex Pass** (for offline downloads / remote-as-local).
7. **Plex claim token** — <https://www.plex.tv/claim>, **expires in 4 minutes**,
   so generate it *just before* first Plex start (Phase 5). → `PLEX_CLAIM`
8. Pick a **Decypharr** UI login and a **Cleanuparr** admin login (any
   values — the scripts create/consume them). → `DECYPHARR_*`, `CLEANUPARR_*`

❓ **ASK checkpoint:** before Phase 0, confirm you have items 1, 3, 4, 5, 6 in
hand. 7 and 8 are set later.

---

## Phase 0 — Provision the infrastructure (👤 USER, in the Hetzner Console)

The agent can't click the Hetzner console for you. Do this and report back the
values the agent asks for.

1. **Create the VPS:** Hetzner Cloud → new server, **Ubuntu 24.04**, **8 GB**
   RAM (e.g. `CPX31` x86, or `CAX21` ARM — images are multi-arch), **EU/Germany**
   region. Add your SSH public key. Note the **public IP**.
2. **Create the Storage Box:** Hetzner → Storage Box, **1 TB (BX11)**, **same
   Germany region** as the VPS (latency + no egress cost).
3. **Enable SMB/CIFS on the Storage Box:** in its settings, turn on **Samba/CIFS**
   (and Snapshots). The share name for the main account is literally **`backup`**,
   port **445**. Note the **username** (`uXXXXXX`) and set/note its **password**.

### How the Storage Box gets mounted (so this isn't a mystery later)

It is **not** block storage you attach in the console. It's a network share.
We mount it **once on the VPS host** over CIFS at `/mnt/box` via `/etc/fstab`
(Phase 2), then **bind-mount** `/mnt/box/media` into the containers. Every
container therefore sees the library at the *same* path, and the whole stack
shares Hetzner's **10-connection** cap through that single host mount (the reason
for the Plex hardening later). Decypharr downloads to fast **local** disk
(`/data/downloads`); Radarr/Sonarr then **copy** the finished file onto the box
(CIFS has no hardlinks, so it's copy-then-delete — fine, nothing seeds).

❓ **ASK checkpoint → give the agent:** VPS **public IP**, Storage Box **username
`uXXXXXX`** + **password**. (Paste the Storage Box password straight into the
VPS step, not into chat, if you prefer.)

---

## Phase 1 — Tailscale + SSH (🤖 AGENT install · 👤 USER meshnet · ⛔ GATE)

Goal: reach the VPS over the tailnet **before** the firewall closes public SSH.

1. 🤖 **AGENT** — SSH in over the **public IP** (initial session) and install base
   packages + Tailscale:
   ```bash
   apt update && apt -y upgrade
   apt -y install curl cifs-utils nmap
   curl -fsSL https://tailscale.com/install.sh | sh
   tailscale up --ssh --hostname=media-vps
   tailscale ip -4          # note the 100.x.y.z address
   ```
   `tailscale up` prints an auth URL.
2. 👤 **USER** — open that URL, **authenticate the VPS into your tailnet**
   (meshnet). Then **install Tailscale on this PC** and sign in, so your laptop
   is on the same tailnet. (Phones later, for Plex.)
3. ❓ **ASK** — tell the agent the VPS's **`100.x.y.z`** address (from step 1).
4. ⛔ **GATE — prove tailnet SSH before touching the firewall.** Keep the original
   **public-SSH session open**. In a *second* terminal on your PC:
   ```bash
   ssh root@<VPS_TAILSCALE_IP>       # must succeed
   ```
   Also confirm the difference on purpose: `ssh root@<VPS_PUBLIC_IP>` still works
   *now* (firewall not applied yet). Only once the tailnet SSH works do we
   continue. **If it fails, stop** — fixing the tailnet here is what prevents a
   lockout.

---

## Phase 2 — Host prep: Storage Box mount + Docker (🤖 AGENT)

Run on the VPS (either SSH session).

```bash
# Storage Box CIFS mount. uid/gid MUST equal PUID/PGID in .env (1000:1000).
# No `seal`: SMB3 encryption burns shared-vCPU CPU on every byte Plex reads.
printf 'username=uXXXXXX\npassword=YOUR_STORAGEBOX_PASSWORD\n' > /etc/box-credentials
chmod 600 /etc/box-credentials
mkdir -p /mnt/box
cat >> /etc/fstab <<'EOF'
//uXXXXXX.your-storagebox.de/backup /mnt/box cifs credentials=/etc/box-credentials,iocharset=utf8,rw,uid=1000,gid=1000,file_mode=0660,dir_mode=0770,_netdev,nofail 0 0
EOF
mount -a -v
mkdir -p /mnt/box/media/movies /mnt/box/media/tv    # library dirs on the box
mkdir -p /data/downloads /opt/appdata               # local working + config dirs
df -h /mnt/box                                       # confirm the box is mounted

# Docker
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
```

⛔ **GATE:** `df -h /mnt/box` must show the share mounted **before** any library
is built on top of it. If the mount is missing, the containers would write the
"library" to local disk and silently diverge from the box.

---

## Phase 3 — Coolify + tailnet binding (🤖 AGENT), then firewall (⛔ GATE → 👤/🤖)

```bash
# Install Coolify
curl -fsSL https://cdn.coollabs.io/coolify/install.sh | bash

# Bind Coolify (and soketi) to the Tailscale interface ONLY — defense in depth,
# not relying on the firewall alone.
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
cd /data/coolify/source && ./upgrade.sh
```

⛔ **GATE — now (and only now) apply the Hetzner Cloud Firewall.** Tailnet SSH is
proven (Phase 1) and Coolify is tailnet-bound.

- 👤 **USER** (Hetzner Console) *or* 🤖 **AGENT** (`hcloud` CLI): create a firewall
  that **allows inbound UDP 41641** from `0.0.0.0/0` + `::/0` (Tailscale) and
  **drops everything else** (no public 22/80/443/8000). Attach it to the VPS.
  ```bash
  # hcloud variant (wherever hcloud is configured):
  hcloud firewall create --name media-fw
  hcloud firewall add-rule media-fw --direction in --protocol udp --port 41641 --source-ips 0.0.0.0/0 --source-ips ::/0
  hcloud firewall apply-to-resource media-fw --type server --server media-vps
  ```

Then confirm the boundary:
```bash
curl -I http://<VPS_TAILSCALE_IP>:8000        # Coolify: reachable over tailnet
ssh -o ConnectTimeout=5 root@<VPS_PUBLIC_IP>  # should now TIME OUT / refuse
```

---

## Phase 4 — Deploy the stack via Coolify (👤 USER drives, 🤖 AGENT assists)

Coolify owns the compose lifecycle + CI/CD; the agent can't click its UI or hold
your git credentials.

1. 👤 **USER** — open `http://<VPS_TAILSCALE_IP>:8000`, finish Coolify's initial
   setup (admin account).
2. 👤 **USER** — **push this repo** to your git host (GitHub/GitLab), then in
   Coolify: **New Resource → Docker Compose → from your Git repo**, pointing at
   **`stack/docker-compose.yml`**. Enable **auto-deploy on push** for CI/CD.
3. 👤 **USER** — in the resource's **Environment Variables**, paste the same
   `KEY=VALUE` pairs from your `.env` (see the list in [`.env.example`](.env.example)):
   `PUID, PGID, TZ, TS_IP, PLEX_CLAIM, ...`. These feed the compose `${...}`
   interpolation. **Set `PLEX_CLAIM` right before deploying** (4-min expiry).
   > Secrets that only the *setup scripts* use (`TORBOX_API_KEY`, `DECYPHARR_*`,
   > `CLEANUPARR_*`, `OPENSUBTITLES_*`) don't have to go in Coolify — but it's
   > fine if they do. The agent needs them on the host in Phase 5 regardless.
4. 👤 **USER** — **Deploy**. Watch all containers come up (Radarr, Sonarr,
   Prowlarr, Decypharr, Cleanuparr, Bazarr, Overseerr, Plex).
5. 🤖 **AGENT** — get the repo + secrets onto the host for Phase 5:
   ```bash
   git clone <repo-url> /opt/chase_io          # or reuse Coolify's source clone
   cd /opt/chase_io
   cp .env.example .env    # then fill in — or scp your local .env up
   ```
   ❓ **ASK** the user to provide the `.env` values (or confirm Coolify env is set
   and export them into the shell).

⛔ **GATE:** `docker compose ps` (in the deployed stack dir) must show every
service **Up** before Phase 5. The setup scripts talk to these running
containers.

---

## Phase 5 — Configure the apps (🤖 AGENT scripts + a few 👤 USER sign-ins)

All scripts are **idempotent** and read secrets from `.env`. On the VPS, point
them at the running stack and the tailnet IP:

```bash
cd /opt/chase_io
export COMPOSE_DIR=<dir of the deployed stack compose>   # so `docker compose exec` finds the containers
export API_HOST=$(tailscale ip -4 | head -n1)            # ports bind to TS_IP, not 127.0.0.1
```
> The scripts resolve each container by its compose **service label**
> (`com.docker.compose.service=<svc>`) and `docker exec` into it directly, so they
> work under Coolify's mangled container names regardless of `COMPOSE_DIR`. All
> five setup scripts honour `API_HOST` for their host-port API calls.
>
> ⚠️ **Coolify auto-redeploy:** if the stack deploys on push, a `git push` to the
> tracked branch makes Coolify recreate the containers (brief downtime; app config
> persists in the `/opt/appdata/*` bind mounts). If a script hits "no running
> container" mid-setup, wait for the redeploy to finish (or `docker start` the app
> containers) and re-run — every script is idempotent.

Order (mirrors [`SETUP_RUNBOOK.md`](SETUP_RUNBOOK.md) §0):

1. 👤 **USER** — Decypharr first-run wizard once (`http://<TS_IP>:8282`): paste
   `TORBOX_API_KEY`, **Mount = None**. (The script then forces download mode.)
2. 🤖 **AGENT** — core wiring:
   ```bash
   python3 scripts/setup_stack.py        # Decypharr download mode, arrs, Prowlarr indexers
   python3 scripts/setup_cleanuparr.py   # creates its admin account, queue-cleaner rules
   python3 scripts/setup_bazarr.py       # arrs + OpenSubtitles + EN/HE profile
   ```
3. 👤 **USER** — get a fresh **4-min claim token** from <https://www.plex.tv/claim>
   (logged into your Plex account) and paste it to the agent. **Do not** use the
   web wizard over the tailnet — an *unclaimed* server returns "Not authorized" to
   a remote IP. 🤖 **AGENT** claims it from *inside* the container, then configures:
   ```bash
   p=$(docker ps -q -f label=com.docker.compose.service=plex)
   docker exec "$p" curl -s -X POST \
     "http://localhost:32400/myplex/claim?token=<CLAIM>&X-Plex-Product=Plex%20Media%20Server"
   python3 scripts/setup_plex.py         # CIFS-friendly prefs + Movies/TV libraries
   ```
   `setup_plex.py` routes admin writes through the container's localhost (Plex
   403s admin writes — prefs, library creation — from a remote IP; new agents also
   require locale `en-US`). Also set in the Plex UI once: **Remote Access OFF**,
   **Network → LAN Networks = `100.64.0.0/10`**.
4. 👤 **USER** — open Overseerr (`http://<TS_IP>:5055`), **sign in with Plex**,
   and on the Plex step **override Hostname to `plex`** (service name), port
   32400. Leave Radarr/Sonarr empty → Finish. Then:
   ```bash
   python3 scripts/setup_overseerr.py    # wires arrs, watchlist auto-request, scan-on-import
   ```
5. 🤖 **AGENT** — verify:
   ```bash
   python3 scripts/verify.py
   ```

---

## Phase 6 — Reaper timer + final security scan (🤖 AGENT + ⛔ GATE)

1. 🤖 **AGENT** — install the TorBox reaper as a systemd timer (keeps TorBox under
   its 300 GB). Confirm the mount unit name first:
   ```bash
   systemctl list-units --type=mount | grep box       # e.g. mnt-box.mount
   ```
   Run `scripts/torbox-reaper.py` with `DRY_RUN=true` **first** and read the log
   (verify infohash mapping + the quota warning fires correctly) before flipping
   it live. See [`STACK_FINAL_PLAN.md`](STACK_FINAL_PLAN.md) for the reaper design.
2. ⛔ **GATE — public-IP scan, only meaningful now that services exist:**
   ```bash
   nmap -Pn -p 22,80,443,8000,32400,7878,8989,9696 <VPS_PUBLIC_IP>
   ```
   Every port must read **filtered/closed** from the public IP; the same services
   must answer **only** on the `100.x` Tailscale address. If anything answers
   publicly, stop and fix the firewall / port binding before going further.

---

## Phase 7 — Value checkpoint (👤 USER)

From a phone (Plex app + Tailscale): add a **mainstream title** to the Plex
Watchlist → Overseerr requests it → Radarr/Sonarr grab via TorBox → it lands in
`/mnt/box/media` → appears in Plex → plays over Tailscale → **Hebrew subtitles
appear** → **downloads to the device for offline**. Don't consider the deploy
done until all of that works.

---

## Quick reference — what the agent needs from you

| When | The agent asks for | You provide |
|---|---|---|
| Phase 0 | Nothing yet | Create VPS + Storage Box; enable CIFS |
| Phase 1 | VPS public IP; then tailnet IP | Authenticate VPS + this PC into the tailnet |
| Phase 2 | Storage Box `uXXXXXX` + password | (paste into the VPS step) |
| Phase 4 | git repo URL; `.env` values | Push repo; set Coolify env; Deploy |
| Phase 5 | go-ahead per service | Decypharr wizard, Plex claim+sign-in, Overseerr Plex sign-in |
