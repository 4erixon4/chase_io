#!/usr/bin/env python3
"""
watchlist_sync.py — make the Plex watchlist the single source of truth.

The "seamless" model you asked for: your Plex **watchlist** is the remote
control for the whole library.

  * ADD to watchlist  -> Overseerr's watchlist sync requests it -> Radarr/Sonarr
    grab it -> it appears in Plex.  (already wired by setup_overseerr.py)
  * REMOVE from watchlist -> THIS script deletes it from Radarr/Sonarr *with its
    files* (so it leaves the Storage Box) and drops any leftover copy from your
    TorBox account.  (the missing half — that's what this adds)

So "if I delete something it's no longer on my watchlist and no longer on
TorBox" becomes literally true, driven by the one gesture you already use.

HOW IT DECIDES: it reads your account watchlist, resolves each item's external
IDs (tvdb/tmdb/imdb), then walks every Radarr movie and Sonarr series. Anything
NOT matched to a watchlist entry is a removal candidate.

SAFETY (this is destructive — it deletes media files):
  * DRY_RUN defaults to "true": it only logs "WOULD REMOVE ..." until you flip it.
  * If the watchlist fetch fails OR returns zero items, it ABORTS and deletes
    nothing — a transient Plex API blip must never nuke the whole library.
  * If MEDIA_ROOT (the Storage Box mount) is missing, it ABORTS — never delete
    DB entries while the files are merely unreachable.
  * Titles you add straight in Radarr/Sonarr (not via the watchlist) are treated
    as "not wanted" and would be removed. In this stack everything comes through
    the watchlist, so that's intended — but it's why this is opt-in via DRY_RUN.

Stdlib only. Run on the VPS host on a timer (see install_reaper.sh).

Required env:
  PLEX_TOKEN         owner account token (PlexOnlineToken from Preferences.xml)
  RADARR_URL, RADARR_API_KEY
  SONARR_URL, SONARR_API_KEY
Optional env:
  TORBOX_API_KEY     also drop matching leftovers from TorBox (best-effort)
  MEDIA_ROOT         default /mnt/box/media  (abort if not mounted)
  DRY_RUN            default "true"  (set "false" to actually delete)
  PLEX_CLIENT_ID     default "chaseio-watchlist-sync"
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DISCOVER = "https://discover.provider.plex.tv"
TORBOX_BASE = "https://api.torbox.app/v1/api"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def log(msg):
    print(f"[watchlist-sync] {msg}", flush=True)


def http_json(method, url, headers=None, body=None, timeout=30):
    data = None
    headers = dict(headers or {})
    headers.setdefault("User-Agent", UA)  # TorBox/Cloudflare rejects Python-urllib
    if body is not None:
        data = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {e.read().decode()[:200]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"{method} {url} -> {e.reason}")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"{method} {url} -> non-JSON: {raw[:200]}")


# ---- Plex watchlist -> external id sets ------------------------------------

def plex_headers(tok, cid):
    return {"Accept": "application/json", "X-Plex-Token": tok,
            "X-Plex-Client-Identifier": cid}


def fetch_watchlist(tok, cid):
    """Return list of {ratingKey, type, title, year}. Raises on failure so the
    caller can abort (never delete on a failed fetch)."""
    url = f"{DISCOVER}/library/sections/watchlist/all?includeExternalMedia=1"
    mc = http_json("GET", url, headers=plex_headers(tok, cid)).get("MediaContainer", {})
    items = []
    for it in mc.get("Metadata", []):
        guid = it.get("guid", "")                # e.g. plex://show/5d9c...
        rk = guid.rsplit("/", 1)[-1] if guid else it.get("ratingKey")
        items.append({"ratingKey": rk, "type": it.get("type"),
                      "title": it.get("title"), "year": it.get("year")})
    return items


def resolve_external_ids(rating_key, tok, cid):
    """tvdb/tmdb/imdb ids for one watchlist item (best-effort; [] on failure)."""
    if not rating_key:
        return []
    url = f"{DISCOVER}/library/metadata/{rating_key}?X-Plex-Token={urllib.parse.quote(tok)}"
    try:
        mc = http_json("GET", url, headers=plex_headers(tok, cid)).get("MediaContainer", {})
    except RuntimeError:
        return []
    out = []
    for md in mc.get("Metadata", []):
        for g in md.get("Guid", []):
            out.append(g.get("id", ""))          # "tvdb://1420", "tmdb://...", "imdb://tt..."
    return out


def build_wanted(tok, cid):
    """{'tvdb': set, 'tmdb': set, 'imdb': set, 'titles': set} of watchlisted items."""
    wl = fetch_watchlist(tok, cid)
    if not wl:
        raise RuntimeError("watchlist returned zero items — refusing to proceed "
                           "(set ALLOW_EMPTY_WATCHLIST=true only if you truly want "
                           "an empty watchlist to clear the whole library)")
    wanted = {"tvdb": set(), "tmdb": set(), "imdb": set(), "titles": set()}
    for it in wl:
        if it["title"]:
            wanted["titles"].add(norm_title(it["title"], it.get("year")))
        for gid in resolve_external_ids(it["ratingKey"], tok, cid):
            for pfx, key in (("tvdb://", "tvdb"), ("tmdb://", "tmdb"), ("imdb://", "imdb")):
                if gid.startswith(pfx):
                    wanted[key].add(gid[len(pfx):].split("?")[0])
    log(f"watchlist: {len(wl)} item(s) -> "
        f"tvdb={len(wanted['tvdb'])} tmdb={len(wanted['tmdb'])} imdb={len(wanted['imdb'])}")
    return wanted


def norm_title(title, year=None):
    t = re.sub(r"[^a-z0-9]+", "", (title or "").lower())
    return f"{t}:{year}" if year else t


# ---- Radarr / Sonarr -------------------------------------------------------

def arr_get(url, key, path):
    return http_json("GET", f"{url.rstrip('/')}{path}", headers={"X-Api-Key": key})


def arr_delete(url, key, path):
    http_json("DELETE", f"{url.rstrip('/')}{path}", headers={"X-Api-Key": key})


def is_wanted(imdb, ext_id, ext_key, title, year, wanted):
    if imdb and str(imdb).replace("tt", "tt") in wanted["imdb"]:
        return True
    if ext_id and str(ext_id) in wanted[ext_key]:
        return True
    return norm_title(title, year) in wanted["titles"]


def reap_arr(kind, url, key, wanted, dry_run):
    """kind: 'radarr' (movies, tmdb) or 'sonarr' (series, tvdb).
    Returns the list of raw titles removed (for the targeted TorBox prune)."""
    if not url or not key:
        log(f"{kind}: no URL/key, skipping")
        return []
    if kind == "radarr":
        items, ext_key, del_path = arr_get(url, key, "/api/v3/movie"), "tmdb", \
            "/api/v3/movie/{id}?deleteFiles=true&addImportListExclusion=false"
        ext_of = lambda x: x.get("tmdbId")
    else:
        items, ext_key, del_path = arr_get(url, key, "/api/v3/series"), "tvdb", \
            "/api/v3/series/{id}?deleteFiles=true&addImportListExclusion=false"
        ext_of = lambda x: x.get("tvdbId")

    removed = []
    for x in items:
        imdb = (x.get("imdbId") or "").strip()
        title, year = x.get("title"), x.get("year")
        if is_wanted(imdb, ext_of(x), ext_key, title, year, wanted):
            continue
        label = f"{title} ({year})"
        if dry_run:
            log(f"WOULD REMOVE ({kind}) + delete files: {label}")
        else:
            arr_delete(url, key, del_path.format(id=x["id"]))
            log(f"REMOVED ({kind}) + deleted files: {label}")
        removed.append(title)
    return removed


# ---- TorBox targeted cleanup ----------------------------------------------

def torbox_prune(api_key, removed_titles, dry_run):
    """Drop TorBox torrents belonging to titles we just removed from the arrs.

    Deliberately TARGETED, not "everything not on the watchlist": a brand-new
    watchlist item can sit on TorBox under its bare infohash (no readable name)
    while it's still downloading, and a blanket prune would kill that in-flight
    grab. So we only touch releases whose name contains a title we removed this
    run. (Steady-state TorBox space is freed by the reaper, gated on import.)"""
    if not api_key or not removed_titles:
        return 0
    norm_removed = [norm_title(t) for t in removed_titles if t]
    try:
        payload = http_json("GET", f"{TORBOX_BASE}/torrents/mylist?bypass_cache=true",
                            headers={"Authorization": f"Bearer {api_key}"})
    except RuntimeError as e:
        log(f"torbox: list failed ({e}); skipping torbox prune")
        return 0
    torrents = payload.get("data") if isinstance(payload.get("data"), list) else []
    pruned = 0
    for t in torrents:
        nt = norm_title(t.get("name", ""))
        if not any(rt and rt in nt for rt in norm_removed):
            continue  # not one of the titles we removed -> leave it alone
        name = t.get("name", "")
        if dry_run:
            log(f"WOULD DROP from TorBox (removed title): {name}")
        else:
            try:
                http_json("POST", f"{TORBOX_BASE}/torrents/controltorrent",
                          headers={"Authorization": f"Bearer {api_key}"},
                          body={"torrent_id": int(t["id"]), "operation": "delete",
                                "all": False})
                log(f"DROPPED from TorBox: {name}")
            except RuntimeError as e:
                log(f"torbox: delete '{name}' failed ({e})")
        pruned += 1
    return pruned


def main():
    tok = env("PLEX_TOKEN")
    if not tok:
        log("PLEX_TOKEN is required (owner PlexOnlineToken).")
        return 2
    cid = env("PLEX_CLIENT_ID", "chaseio-watchlist-sync")
    dry_run = env("DRY_RUN", "true").lower() != "false"
    media_root = os.path.abspath(env("MEDIA_ROOT", "/mnt/box/media"))
    allow_empty = env("ALLOW_EMPTY_WATCHLIST", "false").lower() == "true"

    # Never delete DB entries while the library files are merely unreachable.
    if not os.path.isdir(media_root):
        log(f"MEDIA_ROOT {media_root} not present (mount down?). Aborting, deleting nothing.")
        return 3

    try:
        wanted = build_wanted(tok, cid)
    except RuntimeError as e:
        if allow_empty and "zero items" in str(e):
            log("watchlist empty but ALLOW_EMPTY_WATCHLIST=true -> clearing everything")
            wanted = {"tvdb": set(), "tmdb": set(), "imdb": set(), "titles": set()}
        else:
            log(f"ABORT (no deletions): {e}")
            return 4

    log(f"mode: {'DRY RUN (nothing deleted)' if dry_run else 'LIVE (deleting)'}")
    removed = reap_arr("radarr", env("RADARR_URL"), env("RADARR_API_KEY"), wanted, dry_run)
    removed += reap_arr("sonarr", env("SONARR_URL"), env("SONARR_API_KEY"), wanted, dry_run)
    tb = torbox_prune(env("TORBOX_API_KEY"), removed, dry_run)
    verb = "would remove" if dry_run else "removed"
    log(f"Done. {verb}={len(removed)} arr title(s), {verb}={tb} TorBox item(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
