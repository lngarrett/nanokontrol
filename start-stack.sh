#!/usr/bin/env bash
# Launch Frigate and Twitch in separate Chromium flatpaks.
# Different flatpak app IDs = separate sandboxes = separate PipeWire streams.

FRIGATE_URL="https://security.whisk.ee"
TWITCH_URL="https://twitch.tv"

echo "Starting Frigate (Chromium)..."
flatpak run org.chromium.Chromium --app="${FRIGATE_URL}" &
disown

sleep 2

echo "Starting Twitch (Ungoogled Chromium)..."
flatpak run io.github.ungoogled_software.ungoogled_chromium --app="${TWITCH_URL}" &
disown

echo "Stack launched."
