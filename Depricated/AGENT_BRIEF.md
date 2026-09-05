# Project Brief: Self-Hosted Stremio + Media Library on Hetzner

## How to use this document

You are the engineering agent on this project. **Do not write any code, compose
files, or configuration until the design phase is complete and I have approved
the design document.**

Work in three phases:

1. **Research** — verify every assumption in this brief. Much of it came from a
   planning conversation and some of it is inference rather than confirmed fact.
   Flag anything you find to be wrong or outdated.
2. **Design** — produce the deliverables listed in "Design phase deliverables".
   Stop and wait for my approval.
3. **Build** — implement.

Rules for how you work:

- Cite sources with URLs and publication dates. Prefer official docs and project
  repos over blog posts and SEO content.
- Clearly separate **verified fact** from **inference** from **assumption**. Say
  which is which. Do not present unverified content as fact.
- If research contradicts a decision in this brief, say so directly rather than
  quietly working around it. Some decisions are fixed (see below) but a fixed
  decision built on a wrong fact should be challenged.
- Prefer native, first-party, well-maintained solutions over clever workarounds
  and over anything we would have to write ourselves.
- Give me a recommended pick with reasoning, not a menu of options.

---

## 1. Goal

Two users (me and my partner), both on iPhone only. We want:

- **Instant streaming** of movies and TV with no wait for torrent peers.
- **A small owned library** of selected titles, with subtitles, watchable on
  iPhone and downloadable to the phone for genuinely offline viewing.
- Access from anywhere, authenticated, for exactly two people.

Region: Israel.

---

## 2. Why the obvious answers don't work

Research should confirm each of these — they are the reasons the architecture
looks the way it does.

**No usable native Stremio on iOS.** The full Stremio app was removed from the
App Store; what remains there is a cut-down version (Stremio Lite / Organizer).
Stremio publishes official full-featured IPA files from their downloads page
(served from dl.strem.io) as of February 2026.

Sideloading those IPAs is *possible* here — AltStore **PAL** is EU/Japan/Brazil
only, but AltStore **Classic**, SideStore and Sideloadly work globally. It is
rejected anyway: a free Apple ID means 7-day re-signing and a 3-app limit, which
is unacceptable maintenance for two phones.

**Stremio Web alone doesn't fix it.** Stremio Web in Safari cannot do torrent
streaming because Stremio Service does not exist for iOS. **This is the single
most important insight in the whole design: running the Stremio streaming server
on the VPS instead of on the phone is what makes Stremio work on iOS.** Verify
this holds in practice.

**Stremio's own download feature is not a reliable answer.** An August 2026
changelog for the iOS/macOS app lists download-related items under a
"Supporters only" tier (download Live Activities, resume interrupted downloads,
opening the offline copy). Meanwhile Stremio's help center and site FAQ still
say offline viewing is not available. The evidence is contradictory and the app
is hard to install here regardless. Do not build on this.

---

## 3. Fixed decisions — do not relitigate without strong evidence

| Decision | Rationale |
|---|---|
| Hetzner Cloud VPS, ~100GB volume | ~$16/mo, target ~$20/mo end to end incl. debrid |
| Coolify for deployment | Already chosen |
| **Traefik** as proxy, not Caddy | Traefik is Coolify's default; Caddy support exists since beta.237 but is marked experimental and Coolify's docs recommend Traefik unless there's a specific reason |
| Real-Debrid as the only acquisition source | Kills peer wait; returns direct HTTPS links; keeps us out of swarms |
| **No torrent client anywhere in the stack** | Hetzner's abuse handling is aggressive about torrent traffic from their IPs. Verify their current AUP. |
| Download-to-disk, not a debrid FUSE mount | See section 5 |
| Jellyfin + Infuse, not WebDAV | Infuse has native Jellyfin support: metadata, artwork, resume sync, download-to-device |
| Tailscale for anything that can't do browser OAuth | See section 6 |

---

## 4. Architecture

All containers on one VPS, routed by Traefik on subdomains. Nothing binds a
host port; everything is reachable only through the proxy network or the
tailnet.

**Landing page.** A static `index.html` with two links, deployed as a Coolify
static site from a git repo. It is a file, not an application. It holds no
session, proxies nothing, and has no backend. Authentication lives in Traefik
in front of each destination, not here.

**Path A — live streaming.**
- `stremio/server` container (the streaming server).
- Stremio Web, self-hosted, configured to point at that server over HTTPS.
- Addons: a debrid-capable source addon configured **RD-only** so non-cached
  results never appear, plus OpenSubtitles for on-the-fly subtitles.
- Research the current best source addon. Torrentio is the widest-coverage
  default; AIOStreams aggregates Torrentio/Comet/MediaFusion into one
  deduplicated list; Comet is faster with thinner coverage. Public instances
  have uptime problems — evaluate self-hosting the addon too.

**Path B — the library.**
- Prowlarr (indexer management) + Radarr/Sonarr (search, selection, automation).
- **rdt-client** or **Decypharr**: presents a fake qBittorrent API to the *arrs,
  sends the release to Real-Debrid, then pulls the finished file to disk over
  HTTPS. This is the "download manager" — Radarr's UI is the search interface.
  **Do not build a custom download manager.**
- Bazarr writes sidecar `.srt` files next to the downloaded video files.
- Jellyfin indexes the library and serves it.
- Infuse on both phones connects to Jellyfin, direct-plays, and can download
  titles to the phone for offline viewing.

Note that OpenSubtitles appears in both paths doing different jobs: the Stremio
addon fetches subs on the fly for live playback; Bazarr writes files to disk for
the Jellyfin/Infuse path. Both are needed.

---

## 5. Why download-to-disk and not a debrid mount

The alternative considered was Zurg + rclone (or Decypharr's mount mode)
exposing the whole Real-Debrid library as a filesystem, with near-zero local
storage. Rejected because:

- Bazarr cannot write sidecar subtitle files into a read-only mount.
- Infuse's download-to-device is reported to be unreliable against FUSE mounts.
- Library scans over a large mount are heavy on a shared vCPU.

Verify both of the first two claims — if they turn out to be solvable, the mount
approach is worth reconsidering, since it would remove the storage constraint
entirely. Report back rather than switching unilaterally.

The chosen model: live streaming stores nothing, and the 100GB disk holds a
rotating shelf of 5–10 titles we specifically want offline.

---

## 6. Security requirements

Treat these as hard requirements, not suggestions. I work in application
security; a design that hand-waves the trust boundaries will be rejected.

**The Stremio streaming server has no authentication of any kind.** Exposed on
the public internet it is an open proxy that anyone can drive from our IP. It
goes on the tailnet only. If you believe there is a safe way to expose it
publicly, make the argument explicitly with evidence — do not assume an
obscure hostname is sufficient.

**The Coolify dashboard has full Docker control of the host.** Tailnet only,
not a public hostname with a password.

**Infuse and Stremio Web's calls to the streaming server cannot complete a
browser OAuth flow.** Any design that puts OIDC in front of them is broken.
Those paths are tailnet-only, or use the service's own credentials.

**Everything else goes behind Traefik forwardAuth middleware** with an OIDC
provider — Authentik, or Pocket ID if something smaller suffices for two users.
That covers Radarr, Sonarr, Prowlarr, Bazarr, rdt-client, and the Jellyfin
web UI if it is public at all.

Additional:

- Real-Debrid API keys and all other secrets live in Coolify environment
  variables, never in the git repo. Produce a secrets inventory.
- Decide and document whether my partner's phone runs Tailscale (simplest, but
  an always-on VPN profile) or whether Jellyfin is public behind its own
  accounts. Give me a recommendation.
- Consider CrowdSec on the Traefik path if anything is publicly exposed.

Produce an explicit **exposure classification** for every container:
`public` / `public + forwardAuth` / `tailnet-only` / `internal-only`.

---

## 7. Research questions to answer before designing

1. Current `stremio/server` image name, tag, and configuration. Can Stremio Web
   be self-hosted and pointed at a remote server over HTTPS? Any mixed-content,
   CORS, or CSP obstacles?
2. **Does Real-Debrid playback traverse the VPS, or does the player fetch
   directly from RD's edge?** The expectation is direct — RD returns an HTTPS
   URL and the Stremio server only handles addon calls. This determines whether
   the 20TB egress allowance matters at all. Verify, and plan to measure it
   with a traffic graph during a test playback.
3. Hetzner's current AUP on torrent traffic, and whether **ingress is unmetered**
   on the specific plan (Hetzner Cloud has historically billed egress only).
4. rdt-client vs Decypharr: current maintenance status, *arr compatibility,
   which one to pick.
5. Which of Jellyfin, Radarr, Sonarr, Prowlarr, Bazarr, Authentik have Coolify
   one-click service templates. Use templates where they exist rather than
   hand-writing compose files.
6. How to attach Traefik forwardAuth middleware in Coolify — custom labels on a
   resource vs. the dynamic config directory under `/data/coolify/proxy/`.
7. Infuse ↔ Jellyfin: confirm native integration, direct play, and
   download-to-device all work over Tailscale.
8. Transcoding. Shared vCPUs transcode badly. Infuse direct-plays nearly
   everything so my phone should never transcode, but Jellyfin's web player
   will. Confirm, and recommend a policy (e.g. cap Radarr's quality profile at
   1080p H.264, or put Infuse on both phones).
9. Real-Debrid rate limits, parallel download limits, and any ToS constraints on
   automated use.
10. Sizing: is 4GB RAM workable with Jellyfin + 4 *arr services + Authentik +
    rdt-client, or should I take 8GB?

---

## 8. Explicitly out of scope — do not build these

- A custom download manager UI. Radarr is the download manager.
- A custom application as the landing page, or any second reverse proxy in
  front of Traefik.
- Any torrent client (qBittorrent, Transmission, etc.).
- WebDAV. Jellyfin covers it better.
- **Do not attempt to place downloaded files into Stremio's cache directory to
  make them appear in Stremio.** The cache is info-hash keyed and Stremio will
  not enumerate arbitrary files placed there. If library-in-Stremio is wanted,
  the correct mechanism is a Jellyfin catalog addon — treat that as optional
  and out of the initial scope.

---

## 9. Operational constraints

**100GB is the binding constraint of the whole system**, not bandwidth. Design
retention from day one, not as an afterthought:

- Radarr/Sonarr quality profile capped at 1080p by default; 4K only by explicit
  per-title choice.
- Automated cleanup policy (age-based, watched-status-based, or size-ceiling
  based — recommend one). Do not rely on me deleting things by hand.
- Alerting or at least a visible indicator when the volume approaches full.

---

## 10. Design phase deliverables

Deliver these and stop:

1. **Research findings**, structured against section 7, with sources, dates, and
   an explicit fact/inference/assumption label on each finding. Call out
   anything in this brief that research contradicts.
2. **Architecture document**: container inventory, what each one does, image and
   version, and the data flows between them for both paths.
3. **Network and trust boundary diagram**, with the exposure classification from
   section 6 applied to every container.
4. **DNS and domain plan**: which subdomains are public, which resolve only on
   the tailnet.
5. **Secrets inventory**: every credential the system needs, where it is stored,
   and its rotation story.
6. **Storage lifecycle design** per section 9.
7. **Build plan**: ordered, with a checkpoint after Path A works end to end on
   an iPhone — that milestone alone delivers most of the value, and Path B
   should not begin until it is confirmed working.

One more note: the content sourcing here is public-tracker indexing via debrid.
Do not add commentary about it; do flag anything with a concrete operational
consequence, such as the Hetzner AUP question in item 3.
