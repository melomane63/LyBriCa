#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BLE HID remote bridge -> "pCP BLE HID Keyboard" virtual keyboard.
Key repeat is always on, no mouse support.
Usage: hid_ble_bridge.py --device-mac XX:XX:XX:XX:XX:XX [-v | -vv]
Logging: no option = errors, -v = connections, -vv = full detail.
Other options (--repeat...) are accepted but ignored.
"""

import argparse
import asyncio
import logging
import signal
import subprocess
import time
from logging.handlers import RotatingFileHandler

from bleak import BleakClient
from evdev import UInput, ecodes as e

UUID_HID_REPORT = "00002a4d-0000-1000-8000-00805f9b34fb"
UUID_HID_REPORT_MAP = "00002a4b-0000-1000-8000-00805f9b34fb"
REPORT_REFERENCE_UUID = "00002908-0000-1000-8000-00805f9b34fb"

CONNECT_TIMEOUT = 60.0
RECONNECT_DELAY = 0.5
REPEAT_DELAY = 0.28
REPEAT_PERIOD = 0.04

# Keyboard usage page
KEYBOARD_KEYS = {0x04 + i: getattr(e, f"KEY_{c}") for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")}
KEYBOARD_KEYS.update({0x1E + i: getattr(e, f"KEY_{c}") for i, c in enumerate("1234567890")})
KEYBOARD_KEYS.update({0x3A + i: getattr(e, f"KEY_F{i + 1}") for i in range(12)})
KEYBOARD_KEYS.update({
    0x28: e.KEY_ENTER, 0x29: e.KEY_ESC, 0x2A: e.KEY_BACKSPACE, 0x2B: e.KEY_TAB,
    0x2C: e.KEY_SPACE, 0x2D: e.KEY_MINUS, 0x2E: e.KEY_EQUAL, 0x2F: e.KEY_LEFTBRACE,
    0x30: e.KEY_RIGHTBRACE, 0x31: e.KEY_BACKSLASH, 0x33: e.KEY_SEMICOLON,
    0x34: e.KEY_APOSTROPHE, 0x35: e.KEY_GRAVE, 0x36: e.KEY_COMMA, 0x37: e.KEY_DOT,
    0x38: e.KEY_SLASH, 0x39: e.KEY_CAPSLOCK,
    0x4F: e.KEY_RIGHT, 0x50: e.KEY_LEFT, 0x51: e.KEY_DOWN, 0x52: e.KEY_UP,
})

# Bits 0 to 7 of the modifier byte
MODIFIERS = (e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT, e.KEY_LEFTALT, e.KEY_LEFTMETA,
             e.KEY_RIGHTCTRL, e.KEY_RIGHTSHIFT, e.KEY_RIGHTALT, e.KEY_RIGHTMETA)

# Consumer usage page (media keys)
CONSUMER_KEYS = {
    0x0030: e.KEY_POWER, 0x00B0: e.KEY_PLAY, 0x00B1: e.KEY_PAUSE, 0x00B2: e.KEY_RECORD,
    0x00B3: e.KEY_FASTFORWARD, 0x00B4: e.KEY_REWIND, 0x00B5: e.KEY_NEXTSONG,
    0x00B6: e.KEY_PREVIOUSSONG, 0x00B7: e.KEY_STOP, 0x00B8: e.KEY_EJECTCD,
    0x00CD: e.KEY_PLAYPAUSE, 0x00E2: e.KEY_MUTE, 0x00E9: e.KEY_VOLUMEUP,
    0x00EA: e.KEY_VOLUMEDOWN, 0x0183: e.KEY_CONFIG, 0x018A: e.KEY_MAIL,
    0x0192: e.KEY_CALC, 0x0194: e.KEY_FILE, 0x0223: e.KEY_HOMEPAGE, 0x0224: e.KEY_BACK,
    0x0225: e.KEY_FORWARD, 0x0226: e.KEY_STOP, 0x0227: e.KEY_REFRESH,
    0x022A: e.KEY_BOOKMARKS,
}

SYSTEM_KEYS = (e.KEY_POWER, e.KEY_SLEEP, e.KEY_WAKEUP)   # bits 0 to 2

ALL_KEYS = set(KEYBOARD_KEYS.values()) | set(MODIFIERS) | set(CONSUMER_KEYS.values()) | set(SYSTEM_KEYS)

log = logging.getLogger("hid_ble_bridge")
pressed = {}         # source (handle) -> keys currently down
repeats = {}         # (source, key) -> repeat task


def send(ui, key, value):
    if value != 2:                          # repeats would flood the log
        log.debug("%s %s", e.KEY[key], "pressed" if value else "released")
    ui.write(e.EV_KEY, key, value)
    ui.syn()


async def repeat(ui, key):
    await asyncio.sleep(REPEAT_DELAY)
    while True:
        send(ui, key, 2)
        await asyncio.sleep(REPEAT_PERIOD)


def update(ui, source, keys):
    """Compare the keys down with the previous report from the same source."""
    state = pressed.setdefault(source, set())
    for key in list(state - set(keys)):
        state.discard(key)
        task = repeats.pop((source, key), None)
        if task:
            task.cancel()
        send(ui, key, 0)
    for key in keys:
        if key not in state:
            state.add(key)
            send(ui, key, 1)
            if key not in MODIFIERS:
                repeats[(source, key)] = asyncio.get_running_loop().create_task(repeat(ui, key))


def release_all(ui):
    """Avoid a stuck key if the link drops while a key is held."""
    for source in list(pressed):
        update(ui, source, [])


def decode(ui, source, kind, data):
    data = bytes(data)
    log.debug("[%s] %s: %s", source, kind, data.hex())
    if kind == "keyboard":
        if len(data) == 9:                  # leading Report ID
            data = data[1:]
        if len(data) == 8:                  # modifiers + reserved + 6 keys
            keys = [k for bit, k in enumerate(MODIFIERS) if data[0] & (1 << bit)]
            data = data[2:]
        elif len(data) == 6:                # 6 keys only
            keys = []
        else:
            log.warning("Unexpected keyboard report: %s", data.hex())
            return
        keys += [KEYBOARD_KEYS[u] for u in data if u in KEYBOARD_KEYS]
    elif kind == "consumer" and len(data) >= 2:
        usage = int.from_bytes(data[:2], "little")
        if usage and usage not in CONSUMER_KEYS:
            log.warning("Unknown consumer code: 0x%04x", usage)
        keys = [CONSUMER_KEYS[usage]] if usage in CONSUMER_KEYS else []
    elif kind == "system" and data:
        keys = [k for bit, k in enumerate(SYSTEM_KEYS) if data[0] & (1 << bit)]
    else:
        return
    update(ui, source, keys)


def parse_report_types(report_map):
    """Return {report_id: type} from each report's Application collection."""
    kinds = {(0x01, 0x06): "keyboard", (0x0C, 0x01): "consumer", (0x01, 0x80): "system"}
    types, stack = {}, []
    page = usage = report_id = 0
    i = 0
    while i < len(report_map):
        prefix = report_map[i]
        i += 1
        if prefix == 0xFE:                  # long item
            if i >= len(report_map):
                break
            i += 2 + report_map[i]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        value = int.from_bytes(report_map[i:i + size], "little")
        i += size
        item = prefix & 0xFC
        if item == 0x04:                    # Usage Page
            page = value
        elif item == 0x08:                  # Usage
            usage = value
        elif item == 0x84:                  # Report ID
            report_id = value
        elif item == 0xA0:                  # Collection
            stack.append(kinds.get((page, usage)) if value == 0x01 else None)
            usage = 0
        elif item == 0xC0:                  # End Collection
            if stack:
                stack.pop()
        elif item == 0x80:                  # Input
            kind = next((t for t in reversed(stack) if t), None)
            if kind:
                types[report_id] = kind
            usage = 0
    return types


async def read_reports(client):
    """Return {handle: type}. Any read error aborts (no partial result)."""
    types = parse_report_types(await client.read_gatt_char(UUID_HID_REPORT_MAP))
    reports = {}
    for service in client.services:
        for char in service.characteristics:
            if char.uuid != UUID_HID_REPORT or "notify" not in char.properties:
                continue
            desc = char.get_descriptor(REPORT_REFERENCE_UUID)
            if not desc:
                continue
            ref = await client.read_gatt_descriptor(desc.handle)
            if ref[1] == 1 and ref[0] in types:     # input reports, mouse excluded
                reports[char.handle] = types[ref[0]]
    return reports


async def session(client, ui, reports, lost):
    """One connection: subscribe, then wait for disconnection."""
    lost.clear()
    await client.connect()
    if not reports:                         # read on first connection only
        reports.update(await read_reports(client))
    if not reports:
        raise RuntimeError("no key report found")
    for handle, kind in reports.items():
        await client.start_notify(
            handle, lambda _, data, h=handle, k=kind: decode(ui, h, k, data))
    log.info("Connected, listening to %d report(s).", len(reports))
    await lost.wait()


def bluetoothctl(*args):
    try:
        return subprocess.run(["bluetoothctl", *args], capture_output=True,
                              text=True, timeout=10).stdout
    except subprocess.TimeoutExpired:
        log.warning("bluetoothctl is not responding: %s", " ".join(args))
        return ""


def device_flags(mac):
    lines = [line.strip().lower() for line in bluetoothctl("info", mac).splitlines()]
    return {k: f"{k}: yes" in lines for k in ("paired", "bonded", "trusted", "connected")}


async def prepare(mac):
    if device_flags(mac)["connected"]:
        bluetoothctl("disconnect", mac)
        await asyncio.sleep(1)
        if device_flags(mac)["connected"]:
            log.error("Unable to disconnect the device.")
            return False
    flags = device_flags(mac)
    if flags["paired"] and not flags["bonded"]:
        log.error("Paired but not bonded: remove the device and pair it again.")
        return False
    if flags["trusted"]:                    # prevents auto-connect
        bluetoothctl("untrust", mac)
        await asyncio.sleep(0.5)
    return True


def setup_logging(mac, verbosity):
    name = f"pcp_hidbridge-{mac}.log"
    for folder in ("/var/log", "/tmp"):
        try:
            handler = RotatingFileHandler(f"{folder}/{name}", maxBytes=100 * 1024, backupCount=1)
            break
        except OSError:
            handler = logging.StreamHandler()
    logging.basicConfig(handlers=[handler], datefmt="%m/%d %H:%M:%S",
                        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s")
    log.setLevel([logging.ERROR, logging.INFO, logging.DEBUG][min(verbosity, 2)])


def create_keyboard():
    for attempt in range(5):
        try:
            return UInput({e.EV_KEY: ALL_KEYS}, name="pCP BLE HID Keyboard")
        except OSError as err:
            if attempt == 4:
                log.error("Unable to create the virtual keyboard: %s", err)
                raise
            time.sleep(min(0.5 * 2 ** attempt, 5))


async def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--device-mac", required=True)
    parser.add_argument("-v", action="count", default=0, dest="verbosity")
    args, _ = parser.parse_known_args()
    mac = args.device_mac
    setup_logging(mac, args.verbosity)

    while "powered: yes" not in bluetoothctl("show").lower():
        log.info("Bluetooth controller not ready...")
        await asyncio.sleep(2)
    if not await prepare(mac):
        return

    ui = create_keyboard()
    stop, lost = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_stop():
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGQUIT):
        loop.add_signal_handler(sig, on_stop)
    loop.add_signal_handler(signal.SIGHUP, lost.set)        # force a reconnection

    def on_disconnect(_):
        log.info("Remote disconnected.")
        lost.set()

    # One client, reused as in the original script
    client = BleakClient(mac, disconnected_callback=on_disconnect, timeout=CONNECT_TIMEOUT)
    reports = {}
    last_error = None
    stopping = loop.create_task(stop.wait())
    try:
        while not stop.is_set():
            attempt = loop.create_task(session(client, ui, reports, lost))
            await asyncio.wait({attempt, stopping}, return_when=asyncio.FIRST_COMPLETED)
            attempt.cancel()                # no effect if already done
            try:
                await attempt
                last_error = None
            except asyncio.CancelledError:
                pass
            except Exception as err:
                message = f"{type(err).__name__}: {err}"
                if message != last_error:   # avoid flooding the log while asleep
                    log.error("Connection failed: %s", message)
                    last_error = message
            release_all(ui)
            try:
                await asyncio.wait_for(client.disconnect(), timeout=5)
            except Exception as err:
                log.warning("Error while disconnecting: %s", err)
            if not stop.is_set():
                await asyncio.wait({stopping}, timeout=RECONNECT_DELAY)
    finally:
        release_all(ui)
        ui.close()
        log.info("Bridge stopped.")


if __name__ == "__main__":
    asyncio.run(main())
