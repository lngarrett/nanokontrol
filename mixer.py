#!/usr/bin/env python3
"""nanoKONTROL2 → PipeWire mixer daemon.

Reads MIDI CC via aseqdump, maps faders/mute buttons to PipeWire node volumes.
Sends MIDI CC back to the controller to drive mute button LEDs.
Flashes LEDs when volume is desynced or unknown.
No compiled dependencies — works on immutable OSes like Bazzite out of the box.
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------------------
# CC mapping (confirmed from aseqdump output)
# ---------------------------------------------------------------------------
FADER_CCS = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7}
MUTE_CCS = {48: 0, 49: 1, 50: 2, 51: 3, 52: 4, 53: 5, 54: 6, 55: 7}
MUTE_LED_CCS = {v: k for k, v in MUTE_CCS.items()}
# Solo (S) button LEDs for desync/sync indication
SYNC_LED_CCS = {0: 32, 1: 33, 2: 34, 3: 35, 4: 36, 5: 37, 6: 38, 7: 39}

MIDI_OUT_DEVICE: str | None = None  # auto-detected at startup

# Desync detection
DESYNC_THRESHOLD = 5      # out of 127 (~4%) — accounts for rounding
DESYNC_POLL_INTERVAL = 2  # seconds between PipeWire volume checks
FLASH_RATE = 0.3          # seconds between LED toggles when flashing

# ---------------------------------------------------------------------------
# Channel definitions
#   "match":
#     str          — "@DEFAULT_AUDIO_SINK@" or exact application.name
#     dict         — match against pw node properties (all must match)
#       "app": substring match on application.name
#       "class": exact match on media.class
#       "media": substring match on media.name
#       "desc": substring match on node.description
#       "exclude_media": list of substrings to skip
# ---------------------------------------------------------------------------
CHANNELS = {
    0: {"label": "General Mic",       "type": "source", "match": {"desc": "Shure MVX2U Mono"}},
    1: {"label": "Security Cam",      "type": "sink",   "match": {"desc": "Frigate Audio", "class": "Audio/Sink"}},
    2: {"label": "Twitch",            "type": "sink",   "match": {"desc": "Twitch Audio", "class": "Audio/Sink"}},
    3: {"label": "Firefox",           "type": "sink",   "match": {"desc": "Firefox Audio", "class": "Audio/Sink"}},
    4: {"label": "Unused",            "type": None,     "match": None},
    5: {"label": "OS Volume",         "type": "sink",   "match": {"desc": "Desktop Audio", "class": "Audio/Sink"}},
    6: {"label": "VOIP Speaker",      "type": "sink",   "match": {"desc": "VOIP Speaker", "class": "Audio/Sink"}},
    7: {"label": "Speakers",          "type": "sink",   "match": {"desc": "USB Audio Speakers"}},
}

# ---------------------------------------------------------------------------
# Shared state (protected by _lock)
# ---------------------------------------------------------------------------
_lock = threading.Lock()

# Last known fader position per channel. None = never touched (unknown).
_last_fader: dict[int, int | None] = {i: None for i in range(8)}

# Channels whose LEDs should flash (desynced or unknown).
_flash_set: set[int] = set()

# Flag to stop background threads on shutdown.
_shutdown = threading.Event()

_stream_cache_time: float = 0
STREAM_CACHE_TTL = 2.0

# Pending fader values for debounce. None = no pending change.
_pending_fader: dict[int, int | None] = {i: None for i in range(8)}
_fader_event = threading.Event()
FADER_DEBOUNCE = 0.03  # 30ms — fast enough to feel instant, batches rapid moves

CC_RE = re.compile(
    r"Control change\s+(\d+),\s+controller\s+(\d+),\s+value\s+(\d+)"
)

# Mute button debounce — ignore repeated presses within this window.
MUTE_DEBOUNCE = 0.3  # seconds
_last_mute_time: dict[int, float] = {}


def log(msg: str) -> None:
    print(msg, flush=True)


def find_midi_device() -> str | None:
    """Auto-detect nanoKONTROL2 MIDI output device (hw:X,0,0)."""
    try:
        result = subprocess.run(
            ["amidi", "-l"], capture_output=True, text=True, timeout=2,
        )
        for line in result.stdout.splitlines():
            if "nanoKONTROL2" in line:
                parts = line.split()
                for p in parts:
                    if p.startswith("hw:"):
                        return p
    except Exception:
        pass
    return None


def _send_midi_cc(cc: int, value: int) -> None:
    hex_msg = f"B0 {cc:02X} {value:02X}"
    try:
        subprocess.run(
            ["amidi", "-p", MIDI_OUT_DEVICE, "-S", hex_msg],
            capture_output=True, timeout=1,
        )
    except Exception:
        pass


def send_led(channel_idx: int, on: bool) -> None:
    """Set mute button LED."""
    cc = MUTE_LED_CCS.get(channel_idx)
    if cc is not None:
        _send_midi_cc(cc, 127 if on else 0)


def send_sync_led(channel_idx: int, on: bool) -> None:
    """Set sync (S) button LED."""
    cc = SYNC_LED_CCS.get(channel_idx)
    if cc is not None:
        _send_midi_cc(cc, 127 if on else 0)


def wpctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["wpctl", *args], capture_output=True, text=True, timeout=2)


def is_muted(target: str) -> bool:
    result = wpctl("get-volume", target)
    return "[MUTED]" in result.stdout


def get_volume(target: str) -> float | None:
    """Get current PipeWire volume as 0.0-1.0, or None on failure."""
    result = wpctl("get-volume", target)
    m = re.search(r"Volume:\s+([\d.]+)", result.stdout)
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Node discovery
# ---------------------------------------------------------------------------
class NodeInfo:
    __slots__ = ("node_id", "media_class", "app_name", "media_name", "description")
    def __init__(self, node_id: int, media_class: str, app_name: str, media_name: str, description: str):
        self.node_id = node_id
        self.media_class = media_class
        self.app_name = app_name
        self.media_name = media_name
        self.description = description


_node_list: list[NodeInfo] = []
_AUDIO_CLASSES = {"Stream/Output/Audio", "Stream/Input/Audio", "Audio/Sink", "Audio/Source"}


def refresh_nodes() -> None:
    global _node_list, _stream_cache_time
    now = time.monotonic()
    if now - _stream_cache_time < STREAM_CACHE_TTL:
        return
    _stream_cache_time = now
    _node_list.clear()
    try:
        result = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=3,
        )
        nodes = json.loads(result.stdout)
        for node in nodes:
            if node.get("type") != "PipeWire:Interface:Node":
                continue
            props = node.get("info", {}).get("props", {})
            media_class = props.get("media.class", "")
            if media_class not in _AUDIO_CLASSES:
                continue
            node_id = node.get("id")
            if node_id is not None:
                _node_list.append(NodeInfo(
                    node_id=node_id,
                    media_class=media_class,
                    app_name=props.get("application.name", ""),
                    media_name=props.get("media.name", ""),
                    description=props.get("node.description", ""),
                ))
    except Exception as e:
        log(f"  node lookup failed: {e}")


def resolve_targets(channel: int) -> list[str]:
    ch = CHANNELS.get(channel)
    if not ch or ch["type"] is None or ch["match"] is None:
        return []

    match = ch["match"]

    if isinstance(match, str) and match.startswith("@"):
        return [match]
    if isinstance(match, int):
        return [str(match)]
    if isinstance(match, str):
        refresh_nodes()
        return [str(n.node_id) for n in _node_list if n.app_name == match]
    if isinstance(match, dict):
        refresh_nodes()
        results = []
        for n in _node_list:
            if "desc" in match and match["desc"] not in n.description:
                continue
            if "app" in match and match["app"] not in n.app_name:
                continue
            if "class" in match and match["class"] != n.media_class:
                continue
            if "media" in match and match["media"] not in n.media_name:
                continue
            if "exclude_media" in match:
                if any(ex in n.media_name for ex in match["exclude_media"]):
                    continue
            results.append(str(n.node_id))
        return results
    return []


# ---------------------------------------------------------------------------
# Volume control
# ---------------------------------------------------------------------------
def set_volume(channel: int, midi_value: int) -> None:
    with _lock:
        _last_fader[channel] = midi_value
        _flash_set.discard(channel)
    targets = resolve_targets(channel)
    if not targets:
        return
    vol = f"{midi_value / 127.0:.3f}"
    for target in targets:
        wpctl("set-volume", target, vol)


def queue_volume(channel: int, midi_value: int) -> None:
    """Buffer a fader value for debounced application."""
    with _lock:
        _pending_fader[channel] = midi_value
    _fader_event.set()


def fader_worker() -> None:
    """Apply pending fader values at a throttled rate."""
    while not _shutdown.is_set():
        _fader_event.wait(timeout=1)
        _fader_event.clear()
        time.sleep(FADER_DEBOUNCE)
        # Drain all pending values
        with _lock:
            pending = {ch: val for ch, val in _pending_fader.items() if val is not None}
            for ch in pending:
                _pending_fader[ch] = None
        for ch, val in pending.items():
            set_volume(ch, val)


def toggle_mute(channel: int) -> None:
    targets = resolve_targets(channel)
    if not targets:
        return
    for target in targets:
        wpctl("set-mute", target, "toggle")
    # Read back actual state to set LED correctly
    now_muted = is_muted(targets[0])
    send_led(channel, now_muted)
    ch = CHANNELS[channel]
    state = "MUTED" if now_muted else "unmuted"
    log(f"  [{ch['label']}] {state}")


# ---------------------------------------------------------------------------
# Background threads
# ---------------------------------------------------------------------------
def desync_poller() -> None:
    """Poll PipeWire volumes and flag desynced channels."""
    while not _shutdown.is_set():
        for idx in range(8):
            if _shutdown.is_set():
                return
            ch = CHANNELS.get(idx)
            if not ch or ch["match"] is None:
                continue

            with _lock:
                last = _last_fader[idx]

            if last is None:
                # Never touched — unknown state, flash
                with _lock:
                    _flash_set.add(idx)
                continue

            targets = resolve_targets(idx)
            if not targets:
                # Can't find the node. For persistent sinks this means
                # pw-dump failed or PipeWire is restarting — keep current
                # flash state, don't assume anything.
                continue

            pw_vol = get_volume(targets[0])
            if pw_vol is None:
                # Can't read volume — don't clear flash, don't assume synced
                with _lock:
                    _flash_set.add(idx)
                continue

            actual_midi = round(pw_vol * 127)
            if abs(last - actual_midi) > DESYNC_THRESHOLD:
                # Auto-resync: apply saved fader position to PipeWire
                vol = f"{last / 127.0:.3f}"
                for t in targets:
                    wpctl("set-volume", t, vol)
                log(f"  [{CHANNELS[idx]['label']}] auto-resynced to {int(last / 127 * 100)}%")
                # Verify it actually took
                verify = get_volume(targets[0])
                if verify is not None:
                    verify_midi = round(verify * 127)
                    if abs(last - verify_midi) <= DESYNC_THRESHOLD:
                        with _lock:
                            _flash_set.discard(idx)
                    else:
                        with _lock:
                            _flash_set.add(idx)
                # If verify failed, leave flash state unchanged — next cycle retries
            else:
                # Confirmed in sync
                with _lock:
                    _flash_set.discard(idx)

        _shutdown.wait(DESYNC_POLL_INTERVAL)


def led_flasher() -> None:
    """Flash S (sync) LEDs for desynced channels."""
    flash_on = False
    while not _shutdown.is_set():
        flash_on = not flash_on
        with _lock:
            flashing = set(_flash_set)

        for idx in range(8):
            ch = CHANNELS.get(idx)
            if not ch or ch["match"] is None:
                continue

            if idx in flashing:
                send_sync_led(idx, flash_on)
            else:
                send_sync_led(idx, False)

        _shutdown.wait(FLASH_RATE)


def sync_leds() -> None:
    """Set non-flashing channel LEDs to actual PipeWire mute state."""
    for idx in range(8):
        with _lock:
            if idx in _flash_set:
                continue
        targets = resolve_targets(idx)
        if targets:
            muted = is_muted(targets[0])
            send_led(idx, muted)
        else:
            send_led(idx, False)
    log("  LEDs synced with PipeWire state")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    global MIDI_OUT_DEVICE
    log("nanokontrol mixer starting...")
    log("")

    for idx, ch in sorted(CHANNELS.items()):
        status = "ACTIVE" if ch["match"] is not None else "---"
        log(f"  Fader {idx + 1}: {ch['label']:<20} [{status}]")
    log("")

    # Auto-detect MIDI output device, wait if not plugged in yet
    log("  Waiting for nanoKONTROL2...")
    while not _shutdown.is_set():
        MIDI_OUT_DEVICE = find_midi_device()
        if MIDI_OUT_DEVICE:
            break
        time.sleep(2)
    if _shutdown.is_set():
        return
    log(f"  Found MIDI device: {MIDI_OUT_DEVICE}")

    # Mark all active channels as unknown (flash until touched)
    with _lock:
        for idx, ch in CHANNELS.items():
            if ch["match"] is not None:
                _flash_set.add(idx)

    # Start background threads
    poller = threading.Thread(target=desync_poller, daemon=True)
    flasher = threading.Thread(target=led_flasher, daemon=True)
    fworker = threading.Thread(target=fader_worker, daemon=True)
    poller.start()
    flasher.start()
    fworker.start()

    log("  LEDs flashing — touch each fader to sync")

    proc = subprocess.Popen(
        ["aseqdump", "-p", "nanoKONTROL2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def shutdown(signum, frame):
        log("\nShutting down...")
        _shutdown.set()
        for idx in range(8):
            send_led(idx, False)
            send_sync_led(idx, False)
        proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log("Listening on nanoKONTROL2...")
    log("")

    buf = b""
    fd = proc.stdout.fileno()
    while True:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line_bytes, buf = buf.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace")

            m = CC_RE.search(line)
            if not m:
                continue

            _ch, controller, value = int(m.group(1)), int(m.group(2)), int(m.group(3))

            if controller in FADER_CCS:
                fader_idx = FADER_CCS[controller]
                ch = CHANNELS.get(fader_idx)
                if ch and ch["match"] is not None:
                    queue_volume(fader_idx, value)
                    pct = int(value / 127 * 100)
                    log(f"  [{ch['label']}] {pct}%")

            elif controller in MUTE_CCS and value == 127:
                mute_idx = MUTE_CCS[controller]
                now = time.monotonic()
                if now - _last_mute_time.get(mute_idx, 0) >= MUTE_DEBOUNCE:
                    _last_mute_time[mute_idx] = now
                    toggle_mute(mute_idx)


if __name__ == "__main__":
    main()
