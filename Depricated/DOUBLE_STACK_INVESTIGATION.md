# Double Stack Investigation — Self-Hosted Stremio + Jellyfin/Infuse on Hetzner

> Status: SUPERSEDED by `STACK_FINAL.md` (Plex + Usenet via DUMB). Kept as the
> full record of the dual-path investigation (Stremio live streaming + Jellyfin
> owned library) that led to the simpler final decision. Not the plan we are
> building.

This was the design that explored running **two paths**: Path A, self-hosted
Stremio for instant live streaming of anything; Path B, a Jellyfin + Infuse
library of downloaded titles for offline viewing. Real-Debrid was the source,
Coolify + Traefik the platform, and the trust boundary combined mTLS (Stremio),
an Authentik OAuth portal (the download/admin stack), and Tailscale (Jellyfin,
Coolify). It reached rev 3 before the project pivoted to the simpler Plex+Usenet
stack.

## The two corrections to the original brief

- **Playback traverses the VPS (egress is real), by design.** Real-Debrid binds a resolved CDN link to the resolving IP and flags "resolve-here, stream-there" ([RD API docs](https://api.real-debrid.com/), 2026-09; [ElfHosted multi-IP guide](https://docs.elfhosted.com/guides/media/stream-from-real-debrid-with-stremio-from-with-multiple-ip-addresses-simultaneously/)). The Stremio Server resolves **and** proxies so RD sees one IP — which is also what makes iOS work. 20 TB egress ≈ 4,000 streams/mo, not a practical cap; still to be measured at the checkpoint.
- **Debrid HTTP-stream proxying needs Stremio Web v5**, not the v4.4 shell (same guide). Top risk to Path A; validated before the library work.

## Trust boundary — three zones with enforcement

The dividing line is the client type: a browser that can do OAuth, a browser that can present a client cert, or a native app that only speaks its service's protocol.

- **Public + OAuth (Authentik forwardAuth): the download/admin stack.** Radarr, Sonarr, Prowlarr, Bazarr, Decypharr UI, and the Authentik launcher itself. Reachable with no VPN; SSO enforced at Traefik on every subdomain, so the portal isn't bypassable.
- **Public + mTLS (Safari only): the Stremio origin.** The `tsaridas/stremio-docker` bundle serves the streaming server **and** Stremio Web v5 on one port = **one origin**; one Traefik router + one mTLS middleware protects both, and Safari presents the client cert on every request including the server's own API calls. OAuth is deliberately **not** in this path — it would break Stremio Web/Infuse's non-interactive fetches.
  - **Safari is a hard constraint.** Profile-installed client identities live in an Apple-only keychain access group; Chrome and all third-party iOS browsers cannot present them, the handshake fails silently ([Apple QA1745 / Dev Forums](https://developer.apple.com/forums/thread/707875); [Pinterest Engineering mTLS](https://medium.com/pinterest-engineering/employee-facing-mutual-tls-8643fe0cc0f9)). Open in Safari, add to Home Screen as a web-clip.
- **Tailnet-only: Jellyfin and Coolify.** Infuse speaks Jellyfin's own accounts (can't do OAuth); Jellyfin holds the actual files and has a history of unauthenticated API endpoints, so it stays off the public internet. Coolify has full Docker control of the host — tailnet-only.

### Enforcement (the load-bearing fix)

Coolify's Traefik binds `0.0.0.0:443` by default, so "public DNS doesn't point there" protects nothing. Controls:

1. **Two Traefik entrypoints bound to explicit addresses** (not `0.0.0.0`):
   - `websecure-public` → `<PUBLIC_IP>:443`: Stremio origin (mTLS) + forwardAuth-protected download/admin routers + Authentik.
   - `websecure-tailnet` → `<TAILSCALE_100.x>:443`: Jellyfin, Coolify.
   Routers pinned to one entrypoint, so tailnet services are unreachable on the public IP.
2. **Hetzner Cloud Firewall**: default-deny inbound; allow only TCP 443. SSH via Tailscale. DNS-01 ACME, so 80 stays closed.
3. **True wildcard cert** `*.example.com` via DNS-01, so per-host names never hit Certificate Transparency logs.
4. **Optional CrowdSec** on the public forwardAuth path.

## Phase 0 spikes (had to pass before building)

1. **Decypharr download-to-disk.** Confirm `download_action: download` pulls the RD CDN file to local disk (not a symlink into a mount). Config lists `symlink | download | strm | none` ([Decypharr docs](https://docs.decypharr.com/)), but its headline design is streaming/mount. If it only symlinks, the download-to-disk premise collapses.
2. **Stremio Web v5 + mTLS + CORS (Safari only).** Verify v5 proxies debrid HTTP streams; Safari presents the cert to same-origin API calls; AIOStreams calls succeed (mount under same origin); the cert works from the Home-Screen web-clip, not just a Safari tab.
3. **OpenSubtitles limits.** Free-tier daily caps are tight and consumed twice (addon + Bazarr). Either one paid VIP account, or split providers.

## Storage layout for hardlinks (100 GB, load-bearing)

```
/data                      (the 100GB volume, one filesystem)
  /data/downloads          Decypharr writes completed files here
  /data/media/movies       Radarr library (hardlink from downloads)
  /data/media/tv           Sonarr library (hardlink from downloads)
  /data/config/<service>   per-app config
```

Every arr + Decypharr + Jellyfin mounts `/data` at the **same path** so hardlinks resolve. Enable "Use Hardlinks instead of Copy" in Radarr/Sonarr. Bazarr writes `.srt` sidecars next to the video.

## Key research picks

- **Source addon: self-hosted AIOStreams**, RD-only + cached-only ([ElfHosted addon guide 2026](https://docs.elfhosted.com/stremio-addons/guide/recommended-addons/)).
- **Download client: Decypharr, `download` action** (pending spike #1). rdt-client effectively deprecated ([ElfHosted](https://docs.elfhosted.com/app/rdtclient/); [rdt-client #952](https://github.com/rogerfar/rdt-client/issues/952)).
- **OAuth: Authentik** (server + worker + Postgres + Redis) — chosen over Pocket ID for its **Application Launcher portal**. Coolify has a one-click Authentik template.
- **Sizing: 8 GB shared vCPU**, EU region (Nuremberg/Falkenstein). ARM `CAX21` if arm64 images check out, else x86 `CPX31`/`CX32`.
- **Transcoding: direct-play only.** No GPU on Hetzner cloud. Infuse on both phones; cap Radarr/Sonarr at 1080p H.264; cap Jellyfin transcode threads.

## Stremio accounts

**Two separate Stremio accounts**, one per phone, each pointed at the same server URL + AIOStreams + OpenSubtitles addon URLs, so continue-watching stays independent.

## DNS & domain plan

- Public A records: `stremio.` (mTLS), `auth.` (Authentik), `radarr / sonarr / prowlarr / bazarr / decypharr .example.com` (forwardAuth) → VPS public IP, pinned to `websecure-public`.
- Tailnet: `jellyfin`, `coolify` via Tailscale MagicDNS; no public DNS records.
- TLS: one DNS-01 true wildcard `*.example.com` (Cloudflare token).

## Secrets inventory

- Real-Debrid API token (AIOStreams, Decypharr) — Coolify env.
- mTLS: internal CA key + 2 per-device client certs + CA cert Traefik trusts; delivered via signed `.mobileconfig`; revoke per-device via CA/CRL.
- Authentik: secret key, bootstrap admin, Postgres/Redis creds, per-app OIDC client IDs/secrets.
- Cloudflare DNS-01 API token; Tailscale auth key; OpenSubtitles account(s); per-app API keys; Jellyfin admin + two users; Coolify dashboard creds.
- Git holds only compose with `${VAR}` placeholders; never secrets, never the CA key.

## Storage lifecycle

- Quality profiles capped 1080p H.264; 4K only per-title, manual.
- **Janitorr** (Jellyfin + arr aware): free-space floor (~15 GB free), delete oldest watched first, age backstop.
- **ntfy** push to both phones at 80%/90% volume use.

## Build plan (dual-path, checkpoint after Path A)

- **Phase 0 — Provision + enforce boundary + spikes.** 8 GB EU server + 100 GB single `/data` filesystem; Hetzner Cloud Firewall; Tailscale on host; Coolify; Traefik two per-IP entrypoints + DNS-01 wildcard cert; internal mTLS CA. Run spikes #1–#3.
- **Phase 1 — Path A (live streaming). VALUE CHECKPOINT.** tsaridas bundle (server + Web v5, same origin) on `websecure-public` with mTLS; AIOStreams (RD-only/cached, same-origin path) + OpenSubtitles. Client certs via `.mobileconfig`; Safari + Home-Screen web-clip. Confirm playback with no VPN; two Stremio accounts; measure egress.
- **Phase 2 — Auth portal.** Authentik (OIDC + Application Launcher). Two users. forwardAuth `@file` middleware. Optional CrowdSec.
- **Phase 3 — Path B (library).** Prowlarr, Radarr, Sonarr (1080p), Bazarr, Decypharr (download-to-disk) public behind forwardAuth; Jellyfin tailnet-only. Single `/data` filesystem. Infuse: verify direct play, download-to-device, `.srt` sidecars.
- **Phase 4 — Retention & ops.** Janitorr, ntfy, verify 1080p caps + hardlinking, secrets in Coolify env, backups.

## Why this was superseded

The dual-path design delivered a lot but carried real complexity: mTLS + CA lifecycle, an Authentik OAuth portal, a public attack surface, a 100 GB retention problem, and Real-Debrid's single-IP rule and May-2026 keyword filtering. `STACK_FINAL.md` trades the instant-play-anything of Stremio for a single-app (Plex), Tailscale-only, mount-based (nothing stored on the VPS) design that is simpler to run for two people. That is the direction now being built.
