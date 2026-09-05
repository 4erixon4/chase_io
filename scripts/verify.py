#!/usr/bin/env python3
"""
verify.py — print the end-state of the stack (no secrets). Safe to run anytime.

Shows Decypharr's download action, each arr's root folders / download clients /
indexers, and Prowlarr's indexers + linked apps. Use it after the setup scripts
to confirm everything wired up.

Env:  COMPOSE_DIR (default "local"; use "stack" or an absolute path on the VPS),
      API_HOST (default 127.0.0.1).
"""
import json, os, re, urllib.request
import lib_env

COMPOSE_DIR = os.environ.get("COMPOSE_DIR", "local")
API_HOST = os.environ.get("API_HOST", "127.0.0.1")


def key(svc):
    xml = lib_env.dc(["exec", "-T", svc, "cat", "/config/config.xml"], COMPOSE_DIR).stdout
    return re.search(r"<ApiKey>([^<]+)</ApiKey>", xml).group(1)


def get(port, path, k):
    req = urllib.request.Request(f"http://{API_HOST}:{port}{path}", headers={"X-Api-Key": k})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


dcfg = json.loads(lib_env.dc(["exec", "-T", "decypharr", "cat", "/app/config.json"],
                             COMPOSE_DIR).stdout)
print("Decypharr default_download_action:", dcfg.get("default_download_action"))

for svc, port in (("radarr", 7878), ("sonarr", 8989)):
    k = key(svc)
    rf = get(port, "/api/v3/rootfolder", k)
    dc = get(port, "/api/v3/downloadclient", k)
    idx = get(port, "/api/v3/indexer", k)
    print(f"{svc}: roots={[r['path'] for r in rf]} clients={[c['name'] for c in dc]} "
          f"indexers={[i['name'] for i in idx]}")

pk = key("prowlarr")
pidx = get(9696, "/api/v1/indexer", pk)
apps = get(9696, "/api/v1/applications", pk)
print("prowlarr indexers:", [i['name'] for i in pidx])
print("prowlarr apps:", [a['name'] for a in apps])
