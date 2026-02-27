# nanokontrol

Korg nanoKONTROL2 → PipeWire mixer daemon for Linux.

Maps physical faders and mute buttons to PipeWire sinks/sources with LED feedback. Built for [Bazzite](https://bazzite.gg/) (Fedora Atomic) with no compiled dependencies — just Python 3 and standard ALSA/PipeWire CLI tools.

## Features

- **8 channel fader control** — each fader maps to a PipeWire sink or source
- **Mute buttons with LEDs** — reads PipeWire state as source of truth, never drifts
- **Desync detection** — Solo (S) LEDs flash when fader position doesn't match PipeWire volume
- **Auto-resync** — automatically corrects PipeWire volume when devices reconnect (USB switch, PipeWire restart)
- **Auto-detect MIDI device** — no hardcoded `hw:X,0,0`, waits for controller to appear
- **Fader debounce** — 30ms batching so fast fader moves don't lag
- **Mute debounce** — prevents double-toggle from mechanical button bounce
- **Virtual audio sinks** — persistent PipeWire loopback modules for per-app routing
- **Per-browser routing** — separate Chromium flatpaks routed to dedicated sinks via `pulse.rules`

## Channel Layout

| Fader | Label | Target | Type |
|-------|-------|--------|------|
| 1 | General Mic | Shure MVX2U | Hardware source |
| 2 | Security Cam | Frigate Audio sink | Virtual sink |
| 3 | Twitch | Twitch Audio sink | Virtual sink |
| 4 | Firefox | Firefox Audio sink | Virtual sink |
| 5 | Unused | — | — |
| 6 | OS Volume | Desktop Audio sink | Virtual sink (default) |
| 7 | VOIP Speaker | VOIP Speaker sink | Virtual sink |
| 8 | Speakers | USB Audio Speakers | Hardware sink |

## Architecture

```
┌─────────────┐    pulse.rules     ┌──────────────────┐    loopback    ┌──────────────────┐
│  Chromium    │ ─────────────────→ │  Frigate Audio   │ ────────────→ │                  │
│  (Frigate)   │                    │  (virtual sink)  │               │                  │
├─────────────┤                    ├──────────────────┤               │   USB Audio      │
│  Ungoogled  │ ─────────────────→ │  Twitch Audio    │ ────────────→ │   Speakers       │
│  Chromium   │                    │  (virtual sink)  │               │   (hardware)     │
├─────────────┤                    ├──────────────────┤               │                  │
│  Firefox    │ ─────────────────→ │  Firefox Audio   │ ────────────→ │                  │
├─────────────┤                    ├──────────────────┤               │                  │
│  Games/Apps │ ─────────────────→ │  Desktop Audio   │ ────────────→ │                  │
│  (default)  │                    │  (default sink)  │               │                  │
├─────────────┤                    ├──────────────────┤               │                  │
│  Discord    │  (manual select)   │  VOIP Speaker    │ ────────────→ │                  │
│             │ ─────────────────→ │  (virtual sink)  │               │                  │
└─────────────┘                    └──────────────────┘               └──────────────────┘

                    ┌─────────────────┐
                    │  nanoKONTROL2   │
                    │  mixer.py       │
                    │                 │
                    │  Faders → wpctl │
                    │  Mutes → wpctl  │
                    │  LEDs ← amidi   │
                    └─────────────────┘
```

## Files

| File | Purpose |
|------|---------|
| `mixer.py` | Main daemon — MIDI input, volume/mute control, LED output |
| `start-stack.sh` | Launches Frigate + Twitch in separate browser flatpaks |
| `import-cert.sh` | One-time helper to re-encode client cert for Chromium NSS |

## PipeWire Config (separate from this repo)

These live in `~/.config/pipewire/` and define the virtual sinks and routing rules:

- `pipewire.conf.d/voip-virtual-devices.conf` — virtual sink definitions (Desktop Audio, VOIP Speaker, Frigate Audio, Twitch Audio, Firefox Audio)
- `pipewire-pulse.conf.d/50-browser-routing.conf` — routes Chromium/Firefox to their dedicated sinks

## systemd Services

```bash
# Mixer daemon — auto-starts at login, restarts on crash
systemctl --user enable --now nanokontrol.service

# Browser stack — launches Frigate + Twitch at login
systemctl --user enable --now nanokontrol-stack.service
```

## Dependencies

All available out of the box on Bazzite / Fedora:

- Python 3 (no pip packages)
- `aseqdump` (alsa-utils)
- `amidi` (alsa-utils)
- `wpctl` (wireplumber)
- `pw-dump` (pipewire)

## LED Behavior

- **M (Mute) LED on** = channel is muted in PipeWire
- **S (Sync) LED flashing** = fader position unknown (touch to sync) or volume can't be verified
- **S (Sync) LED off** = confirmed: fader matches PipeWire volume

## After Reboot

All services auto-start. Touch each fader once to sync positions (the controller doesn't report fader state on connect — hardware limitation). S LEDs flash until synced.
