#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BLE HID Bridge (keyboard / consumer / system keys -> virtual uinput keyboard).

- Auto-repeat (value 2) is generated for held keys, like a real keyboard.

Run:  sudo -E python3 hid_ble_bridge.py --device-mac <MAC_ADDRESS>
 or:  sudo -E python3 hid_ble_bridge.py --device-name "HID Remote01"
"""

import asyncio
import signal
import argparse
import subprocess
import time
import logging
from logging.handlers import RotatingFileHandler
from bleak import BleakClient, BleakScanner
from evdev import UInput, ecodes as e
from bleak.exc import BleakDeviceNotFoundError, BleakDBusError

# BLE UUIDs for HID Service and Characteristics
UUID_HID_REPORT = "00002a4d-0000-1000-8000-00805f9b34fb"       # HID Report
UUID_HID_REPORT_MAP = "00002a4b-0000-1000-8000-00805f9b34fb"   # HID Report Map
REPORT_REFERENCE_UUID = "00002908-0000-1000-8000-00805f9b34fb" # Report Reference

# Key Mappings for HID Usages
USAGE_TO_EVKEY = {
    **{i: getattr(e, f"KEY_{chr(ord('A') + (i - 0x04))}") for i in range(0x04, 0x1e)},  # A-Z
    0x1e: e.KEY_1, 0x1f: e.KEY_2, 0x20: e.KEY_3, 0x21: e.KEY_4, 0x22: e.KEY_5,
    0x23: e.KEY_6, 0x24: e.KEY_7, 0x25: e.KEY_8, 0x26: e.KEY_9, 0x27: e.KEY_0,
    0x28: e.KEY_ENTER, 0x29: e.KEY_ESC, 0x2a: e.KEY_BACKSPACE, 0x2b: e.KEY_TAB,
    0x2c: e.KEY_SPACE, 0x2d: e.KEY_MINUS, 0x2e: e.KEY_EQUAL, 0x2f: e.KEY_LEFTBRACE,
    0x30: e.KEY_RIGHTBRACE, 0x31: e.KEY_BACKSLASH, 0x33: e.KEY_SEMICOLON,
    0x34: e.KEY_APOSTROPHE, 0x35: e.KEY_GRAVE, 0x36: e.KEY_COMMA,
    0x37: e.KEY_DOT, 0x38: e.KEY_SLASH, 0x39: e.KEY_CAPSLOCK,
    0x3a: e.KEY_F1, 0x3b: e.KEY_F2, 0x3c: e.KEY_F3, 0x3d: e.KEY_F4,
    0x3e: e.KEY_F5, 0x3f: e.KEY_F6, 0x40: e.KEY_F7, 0x41: e.KEY_F8,
    0x42: e.KEY_F9, 0x43: e.KEY_F10, 0x44: e.KEY_F11, 0x45: e.KEY_F12,
    0x4f: e.KEY_RIGHT, 0x50: e.KEY_LEFT, 0x51: e.KEY_DOWN, 0x52: e.KEY_UP,  # Arrow keys
}
EVKEY_TO_USAGE = {ev: usage for usage, ev in USAGE_TO_EVKEY.items()}

# Consumer page usages (common codes)
MEDIA_USAGE_TO_EVKEY = {
    0x00B0: e.KEY_PLAY,
    0x00B1: e.KEY_PAUSE,
    0x00B2: e.KEY_RECORD,
    0x00B3: e.KEY_FASTFORWARD,
    0x00B4: e.KEY_REWIND,
    0x00B5: e.KEY_NEXTSONG,
    0x00B6: e.KEY_PREVIOUSSONG,
    0x00B7: e.KEY_STOP,
    0x00B8: e.KEY_EJECTCD,
    0x00CD: e.KEY_PLAYPAUSE,
    0x00E2: e.KEY_MUTE,
    0x00E9: e.KEY_VOLUMEUP,
    0x00EA: e.KEY_VOLUMEDOWN,
    0x0183: e.KEY_CONFIG,
    0x018A: e.KEY_MAIL,
    0x0192: e.KEY_CALC,
    0x0194: e.KEY_FILE,
    0x0223: e.KEY_HOMEPAGE,
    0x0224: e.KEY_BACK,
    0x0225: e.KEY_FORWARD,
    0x0226: e.KEY_STOP,
    0x0227: e.KEY_REFRESH,
    0x022A: e.KEY_BOOKMARKS,
    0x0030: e.KEY_POWER,
}

# System control (HID Usage Page 0x01, Usage 0x80, bits 0..2)
SYSTEM_BITS_TO_EVKEY = {
    0: e.KEY_POWER,
    1: e.KEY_SLEEP,
    2: e.KEY_WAKEUP,
}

MOD_BITS_TO_EVKEY = {
    0: e.KEY_LEFTCTRL, 1: e.KEY_LEFTSHIFT, 2: e.KEY_LEFTALT, 3: e.KEY_LEFTMETA,
    4: e.KEY_RIGHTCTRL, 5: e.KEY_RIGHTSHIFT, 6: e.KEY_RIGHTALT, 7: e.KEY_RIGHTMETA,
}

# Report type lookup from application collection usage pairs
REPORT_TYPE_BY_USAGE = {
    (0x01, 0x02): "mouse",
    (0x01, 0x06): "keyboard",
    (0x0C, 0x01): "consumer",
    (0x01, 0x80): "system",
}

# evdev keycode -> name (for readable logging only)
KEYCODE_TO_NAME = {}
for _name, _code in e.ecodes.items():
    if not _name.startswith("KEY_"):
        continue
    if isinstance(_code, int):
        KEYCODE_TO_NAME[_code] = _name
    elif isinstance(_code, list):
        for _c in _code:
            KEYCODE_TO_NAME[_c] = _name

# Pressed-key tracking
key_states = set()               # keyboard keys + modifiers currently pressed
media_pressed_by_source = {}     # source -> set of pressed consumer keys
system_pressed_by_source = {}    # source -> set of pressed system keys
stop_loop = False

# Auto-repeat (generated here: the BLE remote does not send repeats)
REPEAT_DELAY = 0.5      # seconds before the first repeat
REPEAT_PERIOD = 0.04    # seconds between repeats (40 ms)

logger = None


def setup_logging(device_identifier: str = None, log_file: str = None, verbosity: int = 0):
    """Set up logging with a rotating file handler.
    verbosity: 0 = ERROR (default), 1 = INFO (-v), 2 = DEBUG (-vv)"""
    global logger

    if verbosity >= 2:
        log_level = logging.DEBUG
    elif verbosity == 1:
        log_level = logging.INFO
    else:
        log_level = logging.ERROR

    if log_file is None:
        if device_identifier:
            log_file = f"/var/log/pcp_hidbridge-{device_identifier}.log"
            fallback_name = f"/tmp/pcp_hidbridge-{device_identifier}.log"
        else:
            log_file = "/var/log/pcp_hidbridge.log"
            fallback_name = "/tmp/pcp_hidbridge.log"
    else:
        fallback_name = log_file.split('/')[-1] if '/' in log_file else log_file

    logger = logging.getLogger("hid_ble_bridge")
    logger.setLevel(log_level)
    logger.handlers.clear()

    error_msg = None
    try:
        file_handler = RotatingFileHandler(log_file, maxBytes=100 * 1024, backupCount=1)
    except (PermissionError, OSError) as err:
        error_msg = f"Cannot write to {log_file}: {err}"
        print(f"Warning: {error_msg}")
        print(f"Falling back to {fallback_name}")
        file_handler = RotatingFileHandler(fallback_name, maxBytes=100 * 1024, backupCount=1)
        log_file = fallback_name

    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s]:%(lineno)d %(message)s',
        datefmt='%m/%d %H:%M:%S'
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if error_msg:
        logger.warning(error_msg)

    # Always log initialization so the user knows logging works
    _prev_level = logger.level
    logger.setLevel(logging.INFO)
    logger.info(f"Logging initialized: file={log_file}, level={logging.getLevelName(_prev_level)}")
    logger.setLevel(_prev_level)

    return logger


# ==============================================================================
# UInput device creation with retry logic
# ==============================================================================

def create_uinput_with_retry(capabilities, name, max_retries=5, initial_delay=0.5, max_delay=5.0):
    """Create a UInput device, retrying with exponential backoff on OSError."""
    delay = initial_delay
    last_error = None

    for attempt in range(max_retries):
        try:
            ui = UInput(capabilities, name=name)
            if attempt > 0:
                logger.info(f"Successfully created {name} after {attempt + 1} attempts")
            return ui
        except OSError as err:
            last_error = err
            if attempt < max_retries - 1:
                logger.warning(f"Failed to create {name} (attempt {attempt + 1}/{max_retries}): {err}. Retrying in {delay:g}s...")
                time.sleep(delay)
                delay = min(delay * 2, max_delay)
            else:
                logger.error(f"Failed to create {name} after {max_retries} attempts")

    raise last_error


# ==============================================================================
# bluetoothctl helper functions for device preparation
# ==============================================================================

def run_bluetoothctl(*args: str) -> subprocess.CompletedProcess:
    cmd = ["bluetoothctl"] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10)


def controller_is_powered() -> bool:
    result = run_bluetoothctl("show")
    return result.returncode == 0 and "powered: yes" in result.stdout.lower()


def get_device_info(mac_address: str) -> dict:
    result = run_bluetoothctl("info", mac_address)
    info = {"paired": False, "bonded": False, "trusted": False, "connected": False}
    if result.returncode != 0:
        return info

    for line in result.stdout.splitlines():
        line_lower = line.strip().lower()
        for field in info:
            if line_lower.startswith(f"{field}:"):
                info[field] = "yes" in line_lower
    return info


def get_paired_devices() -> dict:
    result = run_bluetoothctl("devices", "Paired")
    devices = {}
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith("Device "):
                parts = line.split(" ", 2)
                if len(parts) == 3:
                    devices[parts[2]] = parts[1]   # name -> mac
    return devices


async def find_device_by_name(device_name: str, scan_timeout: float = 10.0) -> str:
    paired_devices = get_paired_devices()
    for name, mac in paired_devices.items():
        if device_name.lower() in name.lower():
            logger.info(f"Found paired device '{name}' with MAC address {mac}.")
            return mac

    logger.info(f"Device not paired. Scanning for '{device_name}'...")
    found_event = asyncio.Event()
    found_device = []

    def detection_callback(device, advertisement_data):
        if device.name and device_name.lower() in device.name.lower():
            if not found_device:
                found_device.append(device)
            found_event.set()

    scanner = BleakScanner(detection_callback=detection_callback)
    try:
        await scanner.start()
        try:
            await asyncio.wait_for(found_event.wait(), timeout=scan_timeout)
        except asyncio.TimeoutError:
            pass
    except Exception as err:
        logger.error(f"Error during scan: {err}")
    finally:
        await scanner.stop()

    if found_device:
        dev = found_device[0]
        logger.info(f"Found device '{dev.name}' at {dev.address}.")
        return dev.address

    logger.error(f"Could not find device named '{device_name}'.")
    return None


async def prepare_device_for_connection(mac_address: str) -> bool:
    info = get_device_info(mac_address)
    logger.info(f"Device state: Paired={info['paired']}, Bonded={info['bonded']}, "
                f"Trusted={info['trusted']}, Connected={info['connected']}")

    if info["connected"]:
        logger.info("Device is connected. Disconnecting...")
        run_bluetoothctl("disconnect", mac_address)
        await asyncio.sleep(1)
        info = get_device_info(mac_address)
        if info["connected"]:
            logger.error("Failed to disconnect device.")
            return False
        logger.info("Device disconnected.")

    if info["paired"] and not info["bonded"]:
        logger.error("Device is paired but not bonded. Please remove and re-pair the device.")
        return False

    if info["trusted"]:
        logger.info("Device is trusted. Removing trust to prevent auto-connect...")
        run_bluetoothctl("untrust", mac_address)
        await asyncio.sleep(0.5)
        logger.info("Device untrusted.")

    if not info["paired"]:
        logger.info("Device is not paired. Bleak will handle pairing automatically.")

    logger.info("Device is ready for connection.")
    return True


# ==============================================================================
# HID Report Map parsing
# ==============================================================================

def determine_report_type(usage_pairs: set) -> str:
    for pair in usage_pairs:
        if pair in REPORT_TYPE_BY_USAGE:
            return REPORT_TYPE_BY_USAGE[pair]
    return "unknown"


def parse_hid_report_map(report_map: bytes) -> dict:
    """
    Parse the HID Report Map (report descriptor).

    Returns dict:
        report_id -> {"type": str, "direction": str, "size_bytes": int,
                      "bits": int, "usage_pairs": set of (usage_page, usage)}
    """
    report_data = {}

    usage_page = 0
    usage = 0
    report_id = 0
    report_size = 0
    report_count = 0
    collection_stack = []

    i = 0
    while i < len(report_map):
        prefix = report_map[i]
        i += 1

        # Long item (rare)
        if prefix == 0xFE:
            if i + 1 >= len(report_map):
                break
            data_len = report_map[i]
            i += 1 + data_len
            continue

        size_code = prefix & 0x03
        item_size = 0 if size_code == 0 else (1 if size_code == 1 else (2 if size_code == 2 else 4))
        item_type = (prefix >> 2) & 0x03   # 0=main, 1=global, 2=local
        tag = (prefix >> 4) & 0x0F

        if i + item_size > len(report_map):
            break
        value = int.from_bytes(report_map[i:i + item_size], "little") if item_size else 0
        i += item_size

        if item_type == 1:  # Global
            if tag == 0x0:
                usage_page = value
            elif tag == 0x7:
                report_size = value
            elif tag == 0x8:
                report_id = value
            elif tag == 0x9:
                report_count = value

        elif item_type == 2:  # Local
            if tag == 0x0:
                usage = value

        elif item_type == 0:  # Main
            if tag == 0xA:    # Collection
                collection_stack.append({
                    "usage_page": usage_page,
                    "usage": usage,
                    "type": value & 0xFF,
                })
                usage = 0

            elif tag == 0xC:  # End Collection
                if collection_stack:
                    collection_stack.pop()

            elif tag in (0x8, 0x9, 0xB):  # Input / Output / Feature
                if report_size == 0 or report_count == 0:
                    continue

                bits = report_size * report_count
                direction = {0x8: "input", 0x9: "output", 0xB: "feature"}[tag]

                entry = report_data.setdefault(
                    report_id, {"bits": 0, "usages": set(), "directions": set()})
                entry["bits"] += bits
                entry["directions"].add(direction)

                # Nearest Application collection gives the type hint
                app_usage = None
                for coll in reversed(collection_stack):
                    if coll["type"] == 0x01:
                        app_usage = (coll["usage_page"], coll["usage"])
                        break
                if not app_usage and collection_stack:
                    top = collection_stack[-1]
                    app_usage = (top["usage_page"], top["usage"])
                if app_usage:
                    entry["usages"].add(app_usage)

                report_size = 0
                report_count = 0

    definitions = {}
    for rid, info in report_data.items():
        directions = info["directions"]
        definitions[rid] = {
            "type": determine_report_type(info["usages"]),
            "direction": "input" if "input" in directions else (list(directions)[0] if directions else "unknown"),
            "size_bytes": (info["bits"] + 7) // 8,
            "bits": info["bits"],
            "usage_pairs": info["usages"],
        }
    return definitions


# ==============================================================================
# Key repeat
# ==============================================================================

repeat_tasks = {}   # evdev keycode -> asyncio.Task


async def _repeat_key(ui: UInput, keycode: int):
    try:
        await asyncio.sleep(REPEAT_DELAY)
        while True:
            ui.write(e.EV_KEY, keycode, 2)
            ui.syn()
            await asyncio.sleep(REPEAT_PERIOD)
    except asyncio.CancelledError:
        pass


def start_repeat(ui: UInput, keycode: int):
    stop_repeat(keycode)
    repeat_tasks[keycode] = asyncio.get_running_loop().create_task(_repeat_key(ui, keycode))


def stop_repeat(keycode: int):
    task = repeat_tasks.pop(keycode, None)
    if task:
        task.cancel()


def stop_all_repeats():
    for keycode in list(repeat_tasks):
        stop_repeat(keycode)


# ==============================================================================
# Key injection
# ==============================================================================

def press(ui: UInput, keycode: int, repeat: bool = True):
    """Press a key. `repeat=False` for modifiers (a keyboard never repeats them)."""
    ui.write(e.EV_KEY, keycode, 1)
    ui.syn()
    if repeat:
        start_repeat(ui, keycode)


def release(ui: UInput, keycode: int):
    stop_repeat(keycode)
    ui.write(e.EV_KEY, keycode, 0)
    ui.syn()


def release_all_held(ui: UInput):
    """Release every key currently held (called on disconnect, avoids stuck keys)."""
    held = set(key_states)
    for pressed in media_pressed_by_source.values():
        held |= pressed
    for pressed in system_pressed_by_source.values():
        held |= pressed

    stop_all_repeats()
    for keycode in held:
        try:
            release(ui, keycode)
        except Exception as err:
            logger.error(f"Error releasing {key_name(keycode)}: {err}")


def key_name(keycode: int) -> str:
    return KEYCODE_TO_NAME.get(keycode, f"KEY_{keycode}")


# ==============================================================================
# HID report decoding
# ==============================================================================

def decode_hid_report_and_inject(ui_kb: UInput, source: str, data: bytes, report_type: str):
    """
    Decode a HID report and inject keyboard events.

    Args:
        ui_kb: keyboard uinput device
        source: source identifier for tracking state
        data: raw HID report data
        report_type: "keyboard", "consumer" or "system" (from the Report Map)
    """
    actions = []
    payload = data

    # ───────────────────────────────────────────────────────────────
    # Consumer / media keys (2-byte usage)
    # ───────────────────────────────────────────────────────────────
    if report_type == "consumer":
        if len(payload) >= 2:
            usage = int.from_bytes(payload[:2], "little")
            pressed = media_pressed_by_source.setdefault(source, set())

            if usage != 0 and usage in MEDIA_USAGE_TO_EVKEY:
                keycode = MEDIA_USAGE_TO_EVKEY[usage]
                if keycode not in pressed:
                    press(ui_kb, keycode)
                    pressed.add(keycode)
                    actions.append(f"{key_name(keycode)} Pressed")

            elif usage == 0:
                for keycode in list(pressed):
                    release(ui_kb, keycode)
                    actions.append(f"{key_name(keycode)} Released")
                pressed.clear()

            else:
                actions.append(f"Unknown media usage 0x{usage:04x}")
        else:
            actions.append("Consumer report too short")

    # ───────────────────────────────────────────────────────────────
    # Keyboard: 6 bytes (keys only), 8 bytes (modifiers + reserved + 6 keys),
    # 9 bytes (report ID + 8 bytes)
    # ───────────────────────────────────────────────────────────────
    elif report_type == "keyboard":
        if len(payload) == 9:
            payload = payload[1:]   # strip the report ID

        if len(payload) not in (6, 8):
            actions.append(f"Keyboard report unexpected length {len(payload)} (expected 6, 8, 9)")
        else:
            has_mod = len(payload) == 8
            modifiers_byte = payload[0] if has_mod else None
            keys = payload[2:8] if has_mod else payload[0:6]

            pressed_usages = {k for k in keys if k != 0}

            # Modifiers (no repeat)
            if modifiers_byte is not None:
                for bit, keycode in MOD_BITS_TO_EVKEY.items():
                    if modifiers_byte & (1 << bit):
                        if keycode not in key_states:
                            press(ui_kb, keycode, repeat=False)
                            key_states.add(keycode)
                            actions.append(f"{key_name(keycode)} Pressed")
                    elif keycode in key_states:
                        release(ui_kb, keycode)
                        key_states.remove(keycode)
                        actions.append(f"{key_name(keycode)} Released")

            # Keys no longer pressed
            for keycode in list(key_states):
                usage = EVKEY_TO_USAGE.get(keycode)
                if usage is not None and usage not in pressed_usages:
                    release(ui_kb, keycode)
                    key_states.remove(keycode)
                    actions.append(f"{key_name(keycode)} Released")

            # Newly pressed keys
            for usage in pressed_usages:
                keycode = USAGE_TO_EVKEY.get(usage)
                if keycode:
                    if keycode not in key_states:
                        press(ui_kb, keycode)
                        key_states.add(keycode)
                        actions.append(f"{key_name(keycode)} Pressed")
                else:
                    actions.append(f"Unknown keyboard usage 0x{usage:02x}")

    # ───────────────────────────────────────────────────────────────
    # System control (power / sleep / wake, 1-byte bitfield)
    # ───────────────────────────────────────────────────────────────
    elif report_type == "system":
        value = payload[0] if len(payload) >= 1 else 0
        pressed = system_pressed_by_source.setdefault(source, set())

        for bit, keycode in SYSTEM_BITS_TO_EVKEY.items():
            if value & (1 << bit):
                if keycode not in pressed:
                    press(ui_kb, keycode)
                    pressed.add(keycode)
                    actions.append(f"{key_name(keycode)} Pressed")
            elif keycode in pressed:
                release(ui_kb, keycode)
                pressed.remove(keycode)
                actions.append(f"{key_name(keycode)} Released")

    else:
        actions.append(f"Unsupported report type={report_type} length={len(data)}")

    # Logging (skip empty "nothing pressed" reports)
    payload_hex = payload.hex()
    if actions or payload_hex.strip("0") != "":
        action_str = "; ".join(actions) if actions else "No mapped actions"
        logger.debug(f"[{source}] Report type={report_type} payload={payload_hex} {action_str}")


async def notification_handler(client: BleakClient, report_info: dict, ui_kb: UInput):
    """Subscribe to the notifications of one HID report and decode them."""
    handle = report_info["handle"]
    report_type = report_info["report_type"]
    source = f"HID-{handle}"

    try:
        # Decoding is synchronous: reports are processed strictly in order
        await client.start_notify(
            handle,
            lambda _, data: decode_hid_report_and_inject(ui_kb, source, bytes(data), report_type),
        )
        logger.debug(f"Started notifications for HID report (handle={handle}, type={report_type}).")
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.debug(f"Notification handler for handle {handle} canceled.")
    finally:
        try:
            await client.stop_notify(handle)
        except Exception as err:
            logger.debug(f"Error stopping notifications: {err}")


async def cleanup(client, tasks, ui_kb):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    if client.is_connected:
        try:
            await client.disconnect()
        except EOFError:
            logger.debug("Disconnect interrupted (EOFError) during shutdown; ignoring.")
        except Exception as err:
            logger.error(f"Error during disconnect: {err}")

    tasks.clear()

    # Release held keys and stop repeats so nothing stays stuck after a disconnect
    release_all_held(ui_kb)
    key_states.clear()
    media_pressed_by_source.clear()
    system_pressed_by_source.clear()


# ==============================================================================
# main
# ==============================================================================

async def main():
    parser = argparse.ArgumentParser(description="BLE HID Bridge.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--device-mac", help="Bluetooth MAC address.")
    group.add_argument("--device-name", help="Bluetooth device name (e.g., 'HID Remote01').")
    parser.add_argument("--scan-timeout", type=float, default=10.0,
                        help="Timeout in seconds for scanning by name (default: 10).")

    verbosity_group = parser.add_mutually_exclusive_group()
    verbosity_group.add_argument("-v", action="count", default=0, dest="verbosity",
                                 help="-v for INFO level, -vv for DEBUG level")
    verbosity_group.add_argument("--debug", action="store_const", const=2, dest="verbosity",
                                 help="Enable debug logging (equivalent to -vv)")

    parser.add_argument("--logfile", type=str, default=None,
                        help="Path to log file (default: /var/log/pcp_hidbridge-<device-mac>.log)")
    args = parser.parse_args()

    global stop_loop

    setup_logging(args.device_mac, args.logfile, args.verbosity)

    # Wait for the controller to be powered
    while not controller_is_powered():
        logger.info("Controller is not ready, waiting...")
        await asyncio.sleep(2)

    # Resolve MAC address
    if args.device_name:
        device_mac = await find_device_by_name(args.device_name, args.scan_timeout)
        if device_mac is None:
            return
    else:
        device_mac = args.device_mac

    # Disconnect if connected, check bonding, untrust
    if not await prepare_device_for_connection(device_mac):
        return

    stop_event = asyncio.Event()

    def disconnected_callback(client):
        if not stop_event.is_set():
            logger.info("Device disconnected. Will attempt to reconnect...")
        stop_event.set()

    client = BleakClient(device_mac, disconnected_callback=disconnected_callback)

    def handle_sigint(signum, frame):
        global stop_loop
        stop_event.set()
        if signum != signal.SIGHUP:
            stop_loop = True

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)
    signal.signal(signal.SIGQUIT, handle_sigint)
    signal.signal(signal.SIGHUP, handle_sigint)

    # Virtual keyboard
    kb_keys = (set(USAGE_TO_EVKEY.values()) | set(MEDIA_USAGE_TO_EVKEY.values())
               | set(SYSTEM_BITS_TO_EVKEY.values()) | set(MOD_BITS_TO_EVKEY.values()))

    ui_kb = create_uinput_with_retry({e.EV_KEY: kb_keys}, "pCP BLE HID Keyboard")
    logger.info(f"Virtual keyboard created: {ui_kb.device}")

    notification_tasks = []      # persists across reconnect iterations
    logged_errors = set()        # avoid log spam while the device sleeps

    while not stop_loop:
        if not logged_errors:
            logger.info(f"Connecting to: {device_mac}...")
        try:
            await client.connect()
            logged_errors.clear()

            # Read and parse the Report Map (best effort)
            try:
                report_map = await client.read_gatt_char(UUID_HID_REPORT_MAP)
                logger.debug(f"HID Report Map: {report_map.hex()}")
                report_definitions = parse_hid_report_map(report_map)
                if report_definitions:
                    for rid, definition in report_definitions.items():
                        rid_label = f"ID {rid}" if rid != 0 else "no ID"
                        logger.debug(f"Report {rid_label}: type={definition['type']} size={definition['size_bytes']} bytes")
                else:
                    logger.debug("Report map parsed but no report definitions found.")
            except Exception as err:
                report_definitions = {}
                logger.warning(f"Failed to read/parse Report Map: {err}")

            # Tie report IDs to characteristic handles
            report_chars = [char for service in client.services
                            for char in service.characteristics
                            if char.uuid == UUID_HID_REPORT]

            report_info_list = []
            for char in report_chars:
                ref_desc = char.get_descriptor(REPORT_REFERENCE_UUID)
                if not ref_desc:
                    continue
                try:
                    ref_value = await client.read_gatt_descriptor(ref_desc.handle)
                    report_id = ref_value[0]
                    report_type_val = ref_value[1]  # 1=Input, 2=Output, 3=Feature

                    # Only input reports that support notifications
                    if report_type_val != 1 or "notify" not in char.properties:
                        continue

                    definition = report_definitions.get(report_id)
                    if definition is None:
                        logger.debug(f"   Report ID {report_id} handle={char.handle} skipped (not in Report Map)")
                        continue
                    if definition["type"] == "mouse":
                        logger.debug(f"   Report ID {report_id} (mouse) handle={char.handle} ignored")
                        continue

                    report_info_list.append({
                        "handle": char.handle,
                        "report_id": report_id,
                        "report_type": definition["type"],
                    })
                    logger.debug(f"   Report ID {report_id} (Input, {definition['type']}) "
                                 f"handle={char.handle}, size={definition['size_bytes']} bytes")
                except Exception as err:
                    logger.warning(f"Failed to read Report Reference for handle {char.handle}: {err}")

            for report_info in report_info_list:
                notification_tasks.append(
                    asyncio.create_task(notification_handler(client, report_info, ui_kb))
                )

            logger.info("Waiting for input events. Press Ctrl+C to quit.")
            await stop_event.wait()

        except (asyncio.TimeoutError, asyncio.CancelledError) as err:
            if "timeout" not in logged_errors:
                logger.error(f"Connect failed: {err}. Will retry...")
                logged_errors.add("timeout")
        except BleakDBusError as err:
            logger.error(f"Bleak: {err}. Will retry...")
        except BleakDeviceNotFoundError as err:
            if "notfound" not in logged_errors:
                logger.error(f"Bleak: {err}. Will retry...")
                logged_errors.add("notfound")
        except Exception as err:
            logger.error(f"Unexpected error during connect: {err}. Will retry...")

        finally:
            await cleanup(client, notification_tasks, ui_kb)
            await asyncio.sleep(3)
            stop_event.clear()

    logger.info("Cleaning up...")
    ui_kb.close()
    logger.info("Virtual keyboard stopped.")


if __name__ == "__main__":
    asyncio.run(main())
