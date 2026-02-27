#!/usr/bin/env python3
import json, subprocess
result = subprocess.run(["pw-dump"], capture_output=True, text=True)
nodes = json.loads(result.stdout)

clients = {}
for n in nodes:
    if n.get("type") == "PipeWire:Interface:Client":
        props = n.get("info", {}).get("props", {})
        if "Firefox" in props.get("application.name", ""):
            cid = n["id"]
            pid = props.get("application.process.id", "?")
            clients[cid] = pid
            print(f"Client id={cid} pid={pid}")

print()
for n in nodes:
    if n.get("type") != "PipeWire:Interface:Node":
        continue
    props = n.get("info", {}).get("props", {})
    if props.get("media.class") != "Stream/Output/Audio":
        continue
    if "Firefox" not in props.get("application.name", ""):
        continue
    nid = n["id"]
    cid = props.get("client.id", "?")
    media = props.get("media.name", "")
    pid = clients.get(int(cid), "?") if cid != "?" else "?"
    print(f"Stream id={nid} client={cid} pid={pid} media={media!r}")
