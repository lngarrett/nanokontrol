#!/usr/bin/env python3
import json, subprocess
result = subprocess.run(["pw-dump"], capture_output=True, text=True)
for n in json.loads(result.stdout):
    if n.get("type") != "PipeWire:Interface:Client":
        continue
    p = n.get("info", {}).get("props", {})
    app = p.get("application.name", "")
    portal = p.get("pipewire.access.portal.app_id", "")
    if app or portal:
        print(f"client={n['id']:>4}  app={app!r:<20}  portal={portal!r}")
