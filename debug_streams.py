#!/usr/bin/env python3
import json, subprocess
result = subprocess.run(["pw-dump"], capture_output=True, text=True)
nodes = json.loads(result.stdout)
for n in nodes:
    if n.get("type") != "PipeWire:Interface:Node":
        continue
    props = n.get("info", {}).get("props", {})
    mc = props.get("media.class", "")
    if "Stream/Output/Audio" in mc or "Audio/Sink" in mc:
        nid = n["id"]
        app = props.get("application.name", "")
        media = props.get("media.name", "")
        desc = props.get("node.description", "")
        print(f"id={nid:>4}  class={mc:<25} app={app!r:<20} media={media!r:<40} desc={desc!r}")
