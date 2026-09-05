# S1 — Phase 2 spike checklist (the "do not trust it until proven" gate)

Run these on the **first real download**, in order, with the **reaper in
`DRY_RUN=true`**. Every box must be checked before Phase 3. Each item names what
you're de-risking and how to fail safe.

## A. Decypharr writes a REAL file (load-bearing)
- [ ] Grab one movie in Radarr → it goes to Decypharr → completes.
- [ ] On the host: `ls -l /data/downloads/...` shows a **regular file** (size in
      GB), **not** a symlink (`->`) and not a 0-byte/rclone placeholder.
      *If it's a symlink:* Decypharr is in symlink mode — switch its action to
      `download` and re-test. Nothing else works until this passes.

## B. Copy to the Storage Box
- [ ] Radarr imports it to `/mnt/box/media/movies/...` (a real file on the box:
      `ls -l /mnt/box/media/movies/...`).
- [ ] The source copy in `/data/downloads` is cleaned up (Arr `cleanup: true`),
      i.e. local disk isn't accumulating.
- [ ] Repeat once via Sonarr into `/mnt/box/media/tv/...`.

## C. Plex playback + offline (the whole point)
- [ ] Plex library scan finds it (with intro/credit/thumbnail jobs already
      disabled — confirm they're off *before* this scan).
- [ ] iPhone Plex (over Tailscale) direct-plays it — no transcode in Plex
      Dashboard.
- [ ] iPhone **downloads it for offline** and plays with the network off.

## D. Subtitles on CIFS
- [ ] Bazarr fetches a **Hebrew** subtitle and writes `movie.he.srt` **beside the
      media on `/mnt/box/media`** (confirm the file is actually there).
- [ ] The Hebrew subtitle shows and is selectable in the iPhone Plex player.

## E. Reaper — infohash match (dry-run)
- [ ] Run `torbox-reaper.py` with `DRY_RUN=true`; the imported item logs
      `WOULD DELETE … file present at /mnt/box/media/…`.
      *If it logs `KEEP (not imported yet)` for something you know imported,* the
      TorBox↔*arr infohash mapping is off — fix before ever setting
      `DRY_RUN=false`. (Reaper fails safe: it deletes nothing until then.)
- [ ] Delete gate proven: temporarily move/rename the imported file, re-run
      dry-run → it must switch to `KEEP (… file missing on box)`. Restore the file.

## F. Reaper — quota field sanity
- [ ] The `TorBox usage ~X GB / 300 GB (Y%)` log line **matches the TorBox
      dashboard** within reason. *If not,* mylist `size` is a different unit or
      omitted for incomplete items — fix the unit/field in the script before
      trusting the 70% warning.

## G. systemd mount-unit name
- [ ] `systemctl list-units --type=mount | grep box` prints the real unit name;
      confirm it equals `mnt-box.mount` (or update the reaper `.service`
      `After=`/`Requires=` to match).

## H. TorBox slot / uncached behavior
- [ ] With ≥4 grabs queued, Radarr/Sonarr respect **≤3 concurrent** (no stall
      spiral); the 4th waits.
- [ ] An **uncached** torrent still completes (TorBox swarm-fetches it, slower) —
      only truly **seederless/dead** ones fail, and Cleanuparr re-searches those.

## I. Perimeter (end of Phase 2, all services up)
- [ ] `nmap -Pn -p 22,80,443,8000,32400,7878,8989,9696 <VPS_PUBLIC_IP>` from your
      laptop → every port filtered/closed.
- [ ] The same services answer only on the `100.x` Tailscale address.

---
**Only once A–I pass:** set the reaper `DRY_RUN=false`, enable its timer, and
proceed to Phase 3 (freeze LSIO image tags first if you haven't).
