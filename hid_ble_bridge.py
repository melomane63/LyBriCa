#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BLE HID Bridge with verified keyboard and mouse state handling.
Run:  sudo -E python3 hid_ble_bridge.py --device-mac <MAC_ADDRESS>
 or:  sudo -E python3 hid_ble_bridge.py --device-name "HID Remote01"
"""

import asyncio
import signal
import argparse
import subprocess
import time
import os
import logging
from logging.handlers import RotatingFileHandler
from bleak import BleakClient, BleakScanner
from evdev import UInput, ecodes as e
from bleak.exc import BleakDeviceNotFoundError, BleakDBusError

# BLE UUIDs for HID Service and Characteristics
UUID_HID_SERVICE = "00001812-0000-1000-8000-00805f9b34fb"  # HID Service
UUID_HID_REPORT = "00002a4d-0000-1000-8000-00805f9b34fb"   # HID Report
UUID_HID_REPORT_MAP = "00002a4b-0000-1000-8000-00805f9b34fb"   # HID Report Map
REPORT_REFERENCE_UUID = "00002908-0000-1000-8000-00805f9b34fb"   # Report Reference

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

# Expanded media/consumer usage mappings (common HID Consumer Page codes)
MEDIA_USAGE_TO_EVKEY = {
    0x00B0: e.KEY_PLAY,           # Play
    0x00B1: e.KEY_PAUSE,          # Pause
    0x00B2: e.KEY_RECORD,         # Record
    0x00B3: e.KEY_FASTFORWARD,    # Fast Forward
    0x00B4: e.KEY_REWIND,         # Rewind
    0x00B5: e.KEY_NEXTSONG,       # Scan Next Track
    0x00B6: e.KEY_PREVIOUSSONG,   # Scan Previous Track
    0x00B7: e.KEY_STOP,           # Stop
    0x00B8: e.KEY_EJECTCD,        # Eject
    0x00CD: e.KEY_PLAYPAUSE,      # Play/Pause toggle
    0x00E2: e.KEY_MUTE,           # Mute
    0x00E9: e.KEY_VOLUMEUP,       # Volume Up
    0x00EA: e.KEY_VOLUMEDOWN,     # Volume Down
    0x0183: e.KEY_CONFIG,         # Consumer Control Configuration
    0x018A: e.KEY_MAIL,           # AL Email Reader
    0x0192: e.KEY_CALC,           # AL Calculator
    0x0194: e.KEY_FILE,           # AL Local Machine Browser
    0x0223: e.KEY_HOMEPAGE,       # AC Home
    0x0224: e.KEY_BACK,           # AC Back
    0x0225: e.KEY_FORWARD,        # AC Forward
    0x0226: e.KEY_STOP,           # AC Stop
    0x0227: e.KEY_REFRESH,        # AC Refresh
    0x022A: e.KEY_BOOKMARKS,      # AC Bookmarks
    0x0030: e.KEY_POWER,          # Power Button
}

# System control (HID Usage Page 0x01, Usage 0x80, bits 0..2)
SYSTEM_BITS_TO_EVKEY = {
    0: e.KEY_POWER,    # System Power Down
    1: e.KEY_SLEEP,    # System Sleep
    2: e.KEY_WAKEUP,   # System Wake Up
}

MOD_BITS_TO_EVKEY = {
    0: e.KEY_LEFTCTRL, 1: e.KEY_LEFTSHIFT, 2: e.KEY_LEFTALT, 3: e.KEY_LEFTMETA,
    4: e.KEY_RIGHTCTRL, 5: e.KEY_RIGHTSHIFT, 6: e.KEY_RIGHTALT, 7: e.KEY_RIGHTMETA,
}

# Reverse lookup for readable key names (handles aliases safely)
KEYCODE_TO_NAME = {}
NAME_TO_KEYCODE = {}  # Reverse lookup for efficiency
for name, code in e.ecodes.items():
    if not name.startswith("KEY_"):
        continue
    if isinstance(code, int):
        KEYCODE_TO_NAME[code] = name
        NAME_TO_KEYCODE[name] = code
    elif isinstance(code, list):
        # For aliases, use the first code and store all names
        for c in code:
            KEYCODE_TO_NAME[c] = name
        # Store only first code for reverse lookup to avoid overwriting
        if name not in NAME_TO_KEYCODE:
            NAME_TO_KEYCODE[name] = code[0]

# Task management and key tracking
notification_tasks = []
key_states = set()
key_press_times = {}  # track when keys were pressed for hold duration
media_pressed_by_source = {}  # track currently pressed media keys per source
media_press_times = {}  # track when media keys were pressed for hold duration
system_pressed_by_source = {}  # track system control presses per source
system_press_times = {}  # track when system control keys were pressed for hold duration
current_modifiers = set()  # track currently active modifiers for trigger matching
stop_loop = False

# Report definitions parsed from HID Report Map
report_definitions = {}  # report_id -> {"type": str, "size_bytes": int, "usage_pairs": set}
report_ids_present = False  # set True if Report Map declares any Report IDs

# Minimum hold duration in seconds before value 2 (hold/repeat) events are triggered
MIN_HOLD_DURATION = 0.5  # 500ms - typical hold threshold

observed_mouse_lengths = {}  # source - set of seen mouse payload lengths
MOUSE_MIN_MOVEMENT_THRESHOLD = 2  # ignore tiny/noise reports for length detection
MIN_SAMPLES_FOR_CONFIDENCE = 3    # how many consistent lengths before locking in

# Logger will be initialized in main() after parsing command line arguments
logger = None

def setup_logging(device_identifier: str = None, log_file: str = None, verbosity: int = 0):
    """
    Set up logging with rotating file handler.
    
    Args:
        device_identifier: Device MAC address or None. If provided, included in default log filename.
                          If None, uses generic filename without MAC.
        log_file: Optional custom log file path (overrides default)
        verbosity: 0 = ERROR (default), 1 = INFO (-v), 2 = DEBUG (-vv)
    """
    global logger
    
    # Determine log level based on verbosity
    if verbosity >= 2:
        log_level = logging.DEBUG
    elif verbosity == 1:
        log_level = logging.INFO
    else:
        log_level = logging.ERROR
    
    # Determine log file path
    if log_file is None:
        if device_identifier:
            # Create filename with device MAC address
            log_file = f"/var/log/pcp_hidbridge-{device_identifier}.log"
            fallback_name = f"/tmp/pcp_hidbridge-{device_identifier}.log"
        else:
            # Generic filename when searching for device
            log_file = "/var/log/pcp_hidbridge.log"
            fallback_name = "/tmp/pcp_hidbridge.log"
    else:
        # Custom log file provided - extract fallback name
        fallback_name = log_file.split('/')[-1] if '/' in log_file else log_file
    
    # Create logger
    logger = logging.getLogger("hid_ble_bridge")
    logger.setLevel(log_level)
    
    # Remove any existing handlers (in case this function is called multiple times)
    # This ensures clean state if logging needs to be reconfigured
    logger.handlers.clear()
    
    # Create rotating file handler (100KB max, 1 backup)
    try:
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=100 * 1024,  # 100KB
            backupCount=1
        )
    except (PermissionError, OSError) as e:
        # If we can't write to specified location, fall back to current directory
        error_msg = f"Cannot write to {log_file}: {e}"
        print(f"Warning: {error_msg}")
        print(f"Falling back to {fallback_name}")
        file_handler = RotatingFileHandler(
            fallback_name,
            maxBytes=100 * 1024,  # 100KB
            backupCount=1
        )
        log_file = fallback_name
    
    # Set format: mm/dd hh:mm:ss [Log Level]:[Line number] [log message]
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s]:%(lineno)d %(message)s',
        datefmt='%m/%d %H:%M:%S'
    )
    file_handler.setFormatter(formatter)
    
    # Add handler to logger
    logger.addHandler(file_handler)
    
    # Log permission error if we had to fall back
    if 'error_msg' in locals():
        logger.warning(error_msg)
    
    # Log initialization at INFO level always, so user knows logging is working
    # Temporarily set level to INFO to ensure this message is logged
    original_level = logger.level
    logger.setLevel(logging.INFO)
    logger.info(f"Logging initialized: file={log_file}, level={logging.getLevelName(original_level)}")
    logger.setLevel(original_level)  # Restore original level
    
    return logger

# ==============================================================================
# Trigger configuration handling
# ==============================================================================

triggers = []  # List of (event_keys, event_value, command) tuples
MAX_LOG_COMMAND_LENGTH = 50  # Maximum length of command to log (for security)

# ==============================================================================
# Key remapping configuration handling
# ==============================================================================

key_remappings = {}  # Dictionary mapping (source_keycode, key_held) to (destination keycode, set of modifier keycodes) tuple
remapped_key_press_times = {}  # Track press times for remapped keys that have hold-time behavior
remapped_active_modifiers = {}  # Track which modifiers are currently pressed for each remapped key in pass-through mode

def parse_triggers_file(filepath: str) -> list:
    """
    Parse triggerhappy-style configuration file.
    Format: <event name>	<event value>	<command line>
    
    Returns list of tuples: (event_keys, event_value, command)
    where event_keys is a list of key names (first is main key, rest are modifiers)
    
    Note: Caller should verify file exists before calling this function.
    Note: Logger must be initialized before calling this function.
    """
    if logger is None:
        print("ERROR: Logger not initialized before parsing triggers file")
        return []
    
    parsed_triggers = []
    
    try:
        with open(filepath, 'r') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                # Skip empty lines and comments
                if not line or line.startswith('#'):
                    continue
                
                # Split on whitespace, limiting to 3 parts
                parts = line.split(None, 2)
                if len(parts) < 3:
                    logger.warning(f"Invalid trigger line {line_num}: {line}")
                    continue
                
                event_name, event_value_str, command = parts
                
                # Parse event value
                try:
                    event_value = int(event_value_str)
                except ValueError:
                    logger.warning(f"Invalid event value on line {line_num}: {event_value_str}")
                    continue
                
                # Parse event name (may include modifiers with +)
                event_keys = event_name.split('+')
                
                parsed_triggers.append((event_keys, event_value, command))
                # Log only first MAX_LOG_COMMAND_LENGTH chars to avoid exposing sensitive data
                safe_command = command[:MAX_LOG_COMMAND_LENGTH] + "..." if len(command) > MAX_LOG_COMMAND_LENGTH else command
                logger.debug(f"Loaded trigger: {event_keys} = {event_value} -> {safe_command}")
    
    except Exception as e:
        logger.error(f"Error reading trigger file {filepath}: {e}")
    
    return parsed_triggers

def parse_remapping_file(filepath: str) -> dict:
    """
    Parse key remapping configuration file.
    Format: <source key name>:<destination spec>:<key_held>
    
    Where:
    - destination spec can be a simple key name (e.g., KEY_C) or 
      modifiers+key (e.g., SHIFT+KEY_C or CTRL+ALT+KEY_C)
    - key_held is:
      - 0: Pass-through mode - send press/release as they occur
      - 1: Send momentary press+release on key release if held < 0.5s
      - 2: Send momentary press+release on key release if held >= 0.5s
    
    Returns dict mapping (source_keycode, key_held) to (destination keycode, set of modifier keycodes).
    
    Note: Caller should verify file exists before calling this function.
    Note: Logger must be initialized before calling this function.
    """
    if logger is None:
        print("ERROR: Logger not initialized before parsing remapping file")
        return {}
    
    remappings = {}
    # Track which source keys have which key_held values defined
    source_key_definitions = {}
    
    # Build a set of valid modifier names from MOD_BITS_TO_EVKEY
    valid_modifiers = set(MOD_BITS_TO_EVKEY.values())
    # Also include the string names for convenience
    valid_modifier_names = set()
    for keycode in valid_modifiers:
        key_name = KEYCODE_TO_NAME.get(keycode)
        if key_name:
            valid_modifier_names.add(key_name)
    
    try:
        with open(filepath, 'r') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                # Skip empty lines and comments
                if not line or line.startswith('#'):
                    continue
                
                # Split on colon, expecting exactly 3 parts
                parts = line.split(':')
                if len(parts) != 3:
                    logger.warning(f"Invalid remapping line {line_num}: {line} (expected 3 colon-separated fields)")
                    continue
                
                source_key_name, dest_spec, key_held_str = parts[0].strip(), parts[1].strip(), parts[2].strip()
                
                # Validate source key
                source_keycode = NAME_TO_KEYCODE.get(source_key_name)
                if source_keycode is None:
                    logger.warning(f"Unknown source key name '{source_key_name}' on line {line_num}")
                    continue
                
                # Parse destination spec - can be "KEY_C" or "SHIFT+KEY_C" or "CTRL+ALT+KEY_C"
                dest_parts = dest_spec.split('+')
                dest_modifiers = set()
                dest_keycode = None
                
                if len(dest_parts) == 1:
                    # Simple key without modifiers
                    dest_keycode = NAME_TO_KEYCODE.get(dest_parts[0])
                    if dest_keycode is None:
                        logger.warning(f"Unknown destination key name '{dest_parts[0]}' on line {line_num}")
                        continue
                else:
                    # Multiple parts - last is the key, rest are modifiers
                    final_key_name = dest_parts[-1]
                    modifier_names = dest_parts[:-1]
                    
                    # Validate the final key
                    dest_keycode = NAME_TO_KEYCODE.get(final_key_name)
                    if dest_keycode is None:
                        logger.warning(f"Unknown destination key name '{final_key_name}' on line {line_num}")
                        continue
                    
                    # Validate and collect modifiers
                    for mod_name_orig in modifier_names:
                        mod_name = mod_name_orig
                        # Try direct lookup first
                        mod_keycode = NAME_TO_KEYCODE.get(mod_name)
                        if mod_keycode is None:
                            # Try with KEY_ prefix if not present
                            if not mod_name.startswith('KEY_'):
                                mod_name = f"KEY_{mod_name}"
                                mod_keycode = NAME_TO_KEYCODE.get(mod_name)
                        
                        # If still not found, try common abbreviations with LEFT prefix
                        if mod_keycode is None:
                            common_mods = {'SHIFT': 'KEY_LEFTSHIFT', 'CTRL': 'KEY_LEFTCTRL', 
                                         'ALT': 'KEY_LEFTALT', 'META': 'KEY_LEFTMETA',
                                         'CONTROL': 'KEY_LEFTCTRL'}
                            if mod_name_orig.upper() in common_mods:
                                mod_name = common_mods[mod_name_orig.upper()]
                                mod_keycode = NAME_TO_KEYCODE.get(mod_name)
                        
                        if mod_keycode is None:
                            logger.warning(f"Unknown modifier key name '{mod_name_orig}' on line {line_num}")
                            dest_keycode = None  # Invalidate this entry
                            break
                        
                        # Check if this is actually a valid modifier key
                        if mod_keycode not in valid_modifiers:
                            logger.warning(f"'{mod_name_orig}' is not a valid modifier key on line {line_num}")
                            dest_keycode = None  # Invalidate this entry
                            break
                        
                        dest_modifiers.add(mod_keycode)
                    
                    # Skip this entry if validation failed
                    if dest_keycode is None:
                        continue
                
                # Validate key_held value
                try:
                    key_held = int(key_held_str)
                    if key_held not in (0, 1, 2):
                        logger.warning(f"Invalid key_held value '{key_held_str}' on line {line_num} (must be 0, 1, or 2)")
                        continue
                except ValueError:
                    logger.warning(f"Invalid key_held value '{key_held_str}' on line {line_num} (must be integer 0, 1, or 2)")
                    continue
                
                # Track definitions for validation
                if source_keycode not in source_key_definitions:
                    source_key_definitions[source_keycode] = set()
                source_key_definitions[source_keycode].add(key_held)
                
                # Store the mapping with tuple (dest_keycode, modifier_set)
                # Use tuple of (source_keycode, key_held) as key to allow multiple definitions per source key
                remappings[(source_keycode, key_held)] = (dest_keycode, dest_modifiers)
                
                # Build readable log message
                dest_key_name = KEYCODE_TO_NAME.get(dest_keycode, f'key_{dest_keycode}')
                if dest_modifiers:
                    mod_names = [KEYCODE_TO_NAME.get(m, f"mod_{m}") for m in sorted(dest_modifiers)]
                    dest_spec_str = '+'.join(mod_names) + '+' + dest_key_name
                else:
                    dest_spec_str = dest_key_name
                logger.debug(f"Loaded remapping: {source_key_name} -> {dest_spec_str} (key_held={key_held})")
    
    except Exception as e:
        logger.error(f"Error reading remapping file {filepath}: {e}")
    
    # Validate that if key_held 0 is defined, no 1 or 2 can be defined
    # Filter out invalid combinations
    valid_remappings = {}
    for source_keycode, key_held_values in source_key_definitions.items():
        if 0 in key_held_values and len(key_held_values) > 1:
            key_name_str = KEYCODE_TO_NAME.get(source_keycode, f"keycode_{source_keycode}")
            logger.error(f"Key {key_name_str} has key_held=0 defined along with other values. Ignoring all definitions for this key.")
            # Remove all mappings for this source key
            for kh in key_held_values:
                if (source_keycode, kh) in remappings:
                    del remappings[(source_keycode, kh)]
        else:
            # Valid configuration - keep these mappings
            for kh in key_held_values:
                if (source_keycode, kh) in remappings:
                    valid_remappings[(source_keycode, kh)] = remappings[(source_keycode, kh)]
    
    return valid_remappings

def match_trigger(keycode: int, value: int, active_modifiers: set) -> str:
    """
    Check if a key event matches any configured trigger.
    
    Args:
        keycode: The evdev keycode that was pressed/released
        value: Event value (0=release, 1=press, 2=hold/repeat)
        active_modifiers: Set of currently active modifier keycodes
    
    Returns:
        Command string to execute, or None if no match
    """
    global triggers
    
    # Get the key name for the keycode
    key_name = KEYCODE_TO_NAME.get(keycode)
    if not key_name:
        return None
    
    # Find all matching triggers and select the most specific one
    matches = []
    
    for trigger_keys, trigger_value, command in triggers:
        # Check if event value matches
        if trigger_value != value:
            continue
        
        # First key in trigger_keys is the main key
        main_key = trigger_keys[0]
        modifier_keys = set(trigger_keys[1:])
        
        # Check if main key matches
        if main_key != key_name:
            continue
        
        # Check if all required modifiers are active (use NAME_TO_KEYCODE for efficiency)
        required_modifier_codes = set()
        for mod_name in modifier_keys:
            mod_code = NAME_TO_KEYCODE.get(mod_name)
            if mod_code is not None:
                required_modifier_codes.add(mod_code)
            else:
                # Log warning for unknown modifier names
                logger.warning(f"Unknown modifier key name '{mod_name}' in trigger configuration")
        
        # Check if required modifiers are a subset of active modifiers
        # This allows additional modifiers to be pressed (standard triggerhappy behavior)
        if required_modifier_codes.issubset(active_modifiers):
            matches.append((len(required_modifier_codes), command))
    
    # Return the most specific match (most modifiers)
    if matches:
        matches.sort(reverse=True)  # Sort by number of modifiers, descending
        return matches[0][1]
    
    return None

async def execute_trigger_command(command: str):
    """
    Execute a trigger command asynchronously in the background.
    Uses shell execution for compatibility with triggerhappy behavior.
    WARNING: Commands are executed via shell - only use trusted configuration files.
    """
    try:
        # Run command in background without waiting for completion
        # Note: Using shell=True for compatibility with triggerhappy format
        # Users should only use trusted trigger configuration files
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        # Log only first MAX_LOG_COMMAND_LENGTH chars to avoid exposing sensitive data
        safe_command = command[:MAX_LOG_COMMAND_LENGTH] + "..." if len(command) > MAX_LOG_COMMAND_LENGTH else command
        logger.info(f"Executed trigger: {safe_command}")
        # Note: We don't wait for the process to complete to avoid blocking
    except Exception as e:
        logger.error(f"Error executing trigger command: {e}")

async def handle_key_release_triggers(keycode: int, press_times: dict, active_modifiers: set, actions: list):
    """
    Handle trigger execution on key release based on hold duration.
    
    Args:
        keycode: The key that was released
        press_times: Dictionary tracking press times (either key_press_times or media_press_times)
        active_modifiers: Set of currently active modifier keycodes
        actions: List to append action descriptions to
    
    Returns:
        List of commands to execute (returned to allow logging after main HID report log)
    """
    commands_to_execute = []
    
    if keycode in press_times:
        press_time = press_times[keycode]
        current_time = time.time()
        hold_duration = current_time - press_time
        del press_times[keycode]
        
        # Check which trigger to execute based on hold duration
        if hold_duration >= MIN_HOLD_DURATION:
            # Key was held >= 0.5s, execute value 2 trigger
            actions.append(f"{key_name(keycode)} held for {hold_duration:.2f}s (value 2)")
            if command := match_trigger(keycode, 2, active_modifiers):
                commands_to_execute.append(command)
        else:
            # Key was held < 0.5s, execute value 1 trigger
            actions.append(f"{key_name(keycode)} held for {hold_duration:.2f}s (value 1)")
            if command := match_trigger(keycode, 1, active_modifiers):
                commands_to_execute.append(command)
        
        # Also check for release trigger (value 0)
        if command := match_trigger(keycode, 0, active_modifiers):
            commands_to_execute.append(command)
    
    return commands_to_execute

# ==============================================================================
# UInput device creation with retry logic
# ==============================================================================

def create_uinput_with_retry(capabilities, name, max_retries=5, initial_delay=0.5, max_delay=5.0):
    """
    Create a UInput device with retry logic to handle transient device errors.
    
    Args:
        capabilities: Device capabilities dict
        name: Device name string
        max_retries: Maximum number of retry attempts (default: 5)
        initial_delay: Initial delay between retries in seconds (default: 0.5)
        max_delay: Maximum delay between retries in seconds (default: 5.0)
    
    Returns:
        UInput device instance
    
    Raises:
        OSError: If all retry attempts fail
    """
    delay = initial_delay
    last_error = None
    
    for attempt in range(max_retries):
        try:
            ui = UInput(capabilities, name=name)
            if attempt > 0:
                logger.info(f"Successfully created {name} after {attempt + 1} attempts")
            return ui
        except OSError as e:
            last_error = e
            if attempt < max_retries - 1:
                logger.warning(f"Failed to create {name} (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {delay:g}s...")
                time.sleep(delay)
                delay = min(delay * 2, max_delay)  # Exponential backoff with cap
            else:
                logger.error(f"Failed to create {name} after {max_retries} attempts")
    
    # If we get here, all retries failed
    raise last_error

# ==============================================================================
# bluetoothctl helper functions for device preparation
# ==============================================================================

def run_bluetoothctl(*args: str) -> subprocess.CompletedProcess:
    cmd = ["bluetoothctl"] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10)


def get_controller_power():
    result = run_bluetoothctl("show")
    info = {"powered": False}
    if result.returncode != 0:
        return info
    for line in result.stdout.splitlines():
        line_lower = line.strip().lower()
        if line_lower.startswith("powered:"):
            info["powered"] = "yes" in line_lower
    return info

def get_device_info(mac_address: str) -> dict:
    result = run_bluetoothctl("info", mac_address)
    info = {"paired": False, "bonded": False, "trusted": False, "connected": False}
    if result.returncode != 0:
        return info

    for line in result.stdout.splitlines():
        line_lower = line.strip().lower()
        if line_lower.startswith("paired:"):
            info["paired"] = "yes" in line_lower
        elif line_lower.startswith("bonded:"):
            info["bonded"] = "yes" in line_lower
        elif line_lower.startswith("trusted:"):
            info["trusted"] = "yes" in line_lower
        elif line_lower.startswith("connected:"):
            info["connected"] = "yes" in line_lower
    return info


def get_paired_devices() -> dict:
    result = run_bluetoothctl("devices", "Paired")
    devices = {}
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith("Device "):
                parts = line.split(" ", 2)
                if len(parts) == 3:
                    mac = parts[1]
                    name = parts[2]
                    devices[name] = mac
    return devices


async def find_device_by_name(device_name: str, scan_timeout: float = 10.0) -> str:
    paired_devices = get_paired_devices()
    for name, mac in paired_devices.items():
        if device_name.lower() in name.lower():
            logger.info(f"Found paired device '{name}' with MAC address {mac}.")
            return mac

    logger.info(f"Device not paired. Scanning for '{device_name}'...")
    found_device = None

    def detection_callback(device, advertisement_data):
        nonlocal found_device
        if device.name and device_name.lower() in device.name.lower():
            found_device = device

    scanner = BleakScanner(detection_callback=detection_callback)
    try:
        await scanner.start()
        start_time = asyncio.get_event_loop().time()
        while found_device is None:
            if asyncio.get_event_loop().time() - start_time > scan_timeout:
                break
            await asyncio.sleep(0.1)
    except Exception as ex:
        logger.error(f"Error during scan: {ex}")
    finally:
        await scanner.stop()

    if found_device:
        logger.info(f"Found device '{found_device.name}' at {found_device.address}.")
        return found_device.address

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

def parse_hid_report_map(report_map: bytes) -> dict:
    """
    Parse HID Report Map (USB/BLE HID Report Descriptor) and build report definitions.
    
    Returns dict:
        report_id -> {
            "type": str,                # e.g. "mouse", "keyboard", "consumer", "system", etc.
            "direction": str,           # "input", "output", "feature" (most common: input)
            "size_bytes": int,
            "bits": int,
            "usage_pairs": set of tuples (usage_page, usage)
        }
    """
    report_data = {}          # report_id - {"bits": int, "usages": set, "directions": set}
    report_ids_present = False

    # Parser state
    usage_page = 0
    usage = 0
    report_id = 0
    report_size = 0
    report_count = 0
    collection_stack = []     # list of {"usage_page": int, "usage": int, "type": int}

    i = 0
    while i < len(report_map):
        prefix = report_map[i]
        i += 1

        # Long item (rare in practice)
        if prefix == 0xFE:
            if i + 1 >= len(report_map):
                break
            data_len = report_map[i]
            i += 1 + data_len
            continue

        # Short item
        size_code = prefix & 0x03
        item_size = 0 if size_code == 0 else (1 if size_code == 1 else (2 if size_code == 2 else 4))
        item_type = (prefix >> 2) & 0x03   # 0=main, 1=global, 2=local, 3=reserved
        tag       = (prefix >> 4) & 0x0F

        # Read data bytes
        if i + item_size > len(report_map):
            break
        data_bytes = report_map[i:i + item_size]
        value = int.from_bytes(data_bytes, "little") if item_size else 0
        i += item_size

        if item_type == 1:  # Global
            if tag == 0x0:   # Usage Page
                usage_page = value
            elif tag == 0x1: # Logical Minimum
                pass
            elif tag == 0x2: # Logical Maximum
                pass
            elif tag == 0x7: # Report Size
                report_size = value
            elif tag == 0x8: # Report ID
                report_id = value
                report_ids_present = True
            elif tag == 0x9: # Report Count
                report_count = value

        elif item_type == 2:  # Local
            if tag == 0x0:    # Usage
                usage = value
            # You could also track Usage Minimum/Maximum, but we keep it simple

        elif item_type == 0:  # Main
            if tag == 0xA:    # Collection
                collection_type = value & 0xFF  # 0x00=Physical, 0x01=Application, etc.
                collection_stack.append({
                    "usage_page": usage_page,
                    "usage": usage,
                    "type": collection_type
                })
                usage = 0  # reset local usage after starting collection

            elif tag == 0xC:  # End Collection
                if collection_stack:
                    collection_stack.pop()

            elif tag in (0x8, 0x9, 0xB):  # Input (0x8), Output (0x9), Feature (0xB)
                if report_size == 0 or report_count == 0:
                    continue

                bits = report_size * report_count
                direction = {0x8: "input", 0x9: "output", 0xB: "feature"}[tag]

                # Accumulate
                if report_id not in report_data:
                    report_data[report_id] = {
                        "bits": 0,
                        "usages": set(),
                        "directions": set()
                    }

                report_data[report_id]["bits"] += bits
                report_data[report_id]["directions"].add(direction)

                # Find the nearest Application collection for type hint
                app_usage = None
                for coll in reversed(collection_stack):
                    if coll["type"] == 0x01:  # Application
                        app_usage = (coll["usage_page"], coll["usage"])
                        break
                if not app_usage and collection_stack:
                    top = collection_stack[-1]
                    app_usage = (top["usage_page"], top["usage"])

                if app_usage:
                    report_data[report_id]["usages"].add(app_usage)

                # Reset consumed state (this was the main bug)
                report_size = 0
                report_count = 0

    # Build final result
    definitions = {}
    for rid, info in report_data.items():
        size_bytes = (info["bits"] + 7) // 8
        usage_pairs = info["usages"]
        directions = info["directions"]

        report_type = determine_report_type(usage_pairs)  # your existing function

        definitions[rid] = {
            "type": report_type,
            "direction": "input" if "input" in directions else list(directions)[0] if directions else "unknown",
            "size_bytes": size_bytes,
            "bits": info["bits"],
            "usage_pairs": usage_pairs,
        }

    return definitions

# ==============================================================================
# HID Report Map parsing
# ==============================================================================

def determine_report_type(usage_pairs: set) -> str:
    for usage_page, usage in usage_pairs:
        if usage_page == 0x01 and usage == 0x02:
            return "mouse"
        if usage_page == 0x01 and usage == 0x06:
            return "keyboard"
        if usage_page == 0x0C and usage == 0x01:
            return "consumer"
        if usage_page == 0x01 and usage == 0x80:
            return "system"
    return "unknown"

def resolve_report_definition(data: bytes):
    """
    Resolve report ID + payload based on report definitions.
    For devices where report IDs are not included in the data, match by payload length.
    Returns: (report_id, definition, payload, id_included, resolve_reason)
    """
    if not report_definitions:
        return None

    # Match by payload length only (report ID is NOT in the data)
    matches = []
    for rid, definition in report_definitions.items():
        if len(data) == definition.get("size_bytes", -1):
            matches.append((rid, definition))

    # Debug log
    # known_sizes = ", ".join(f"{rid}:{d['size_bytes']}" for rid, d in report_definitions.items())
    # printlog(
        # f"resolve_report_definition: len={len(data)} "
        # f"known_sizes={{{known_sizes}}} "
        # f"matches={[rid for rid, _ in matches]}"
    # )

    if len(matches) == 1:
        rid, definition = matches[0]
        return rid, definition, data, False, "length"

    return None
    
# ==============================================================================
# HID handling functions with enhanced logging
# ==============================================================================

def send_momentary_key(ui: UInput, keycode: int, modifiers: set = None):
    """
    Send a momentary key press followed immediately by a release.
    Used for hold-time-based remapping with KEY_HELD=1 or 2.
    
    Args:
        ui: UInput device
        keycode: The key to press
        modifiers: Set of modifier keycodes to press before the key
    """
    if ui is not None:
        if modifiers is None:
            modifiers = set()
        
        # Press all modifiers first
        for mod_keycode in sorted(modifiers):
            ui.write(e.EV_KEY, mod_keycode, 1)
            ui.syn()
        
        # Press the main key
        ui.write(e.EV_KEY, keycode, 1)
        ui.syn()
        
        # Release the main key
        ui.write(e.EV_KEY, keycode, 0)
        ui.syn()
        
        # Release all modifiers
        for mod_keycode in sorted(modifiers):
            ui.write(e.EV_KEY, mod_keycode, 0)
            ui.syn()


def press(ui: UInput, keycode: int):
    if ui is not None:
        # Check if this key has remapping with key_held=0 (pass-through mode)
        if (keycode, 0) in key_remappings:
            remapped_keycode, modifiers = key_remappings[(keycode, 0)]
            # Press all modifiers first
            for mod_keycode in sorted(modifiers):
                ui.write(e.EV_KEY, mod_keycode, 1)
                ui.syn()
            # Press the remapped key
            ui.write(e.EV_KEY, remapped_keycode, 1)
            ui.syn()
            # Track which modifiers are active for this remapped key
            remapped_active_modifiers[keycode] = modifiers
        # Check if key has hold-time-based remapping (key_held=1 or 2)
        elif (keycode, 1) in key_remappings or (keycode, 2) in key_remappings:
            # Track press time but don't inject yet - wait for release
            remapped_key_press_times[keycode] = time.time()
        else:
            # No remapping, send as-is
            ui.write(e.EV_KEY, keycode, 1)
            ui.syn()


def release(ui: UInput, keycode: int):
    if ui is not None:
        # Check if this key has remapping with key_held=0 (pass-through mode)
        if (keycode, 0) in key_remappings:
            remapped_keycode, modifiers = key_remappings[(keycode, 0)]
            # Release the remapped key
            ui.write(e.EV_KEY, remapped_keycode, 0)
            ui.syn()
            # Release all modifiers that were pressed for this key
            if keycode in remapped_active_modifiers:
                for mod_keycode in sorted(remapped_active_modifiers[keycode]):
                    ui.write(e.EV_KEY, mod_keycode, 0)
                    ui.syn()
                del remapped_active_modifiers[keycode]
        # Check if key has hold-time-based remapping
        elif keycode in remapped_key_press_times:
            press_time = remapped_key_press_times[keycode]
            current_time = time.time()
            hold_duration = current_time - press_time
            del remapped_key_press_times[keycode]
            
            # Determine which key_held value to use based on hold duration
            if hold_duration >= MIN_HOLD_DURATION:
                # Held >= 0.5s, use key_held=2
                if (keycode, 2) in key_remappings:
                    remapped_keycode, modifiers = key_remappings[(keycode, 2)]
                    send_momentary_key(ui, remapped_keycode, modifiers)
            else:
                # Held < 0.5s, use key_held=1
                if (keycode, 1) in key_remappings:
                    remapped_keycode, modifiers = key_remappings[(keycode, 1)]
                    send_momentary_key(ui, remapped_keycode, modifiers)
        else:
            # No remapping, send as-is
            ui.write(e.EV_KEY, keycode, 0)
            ui.syn()


def inject_mouse_event(ui: UInput, buttons, x, y, scroll):
    if ui is not None:
        for bit, button in enumerate([e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE]):
            if buttons & (1 << bit):
                ui.write(e.EV_KEY, button, 1)
            else:
                ui.write(e.EV_KEY, button, 0)

        ui.write(e.EV_REL, e.REL_X, x)
        ui.write(e.EV_REL, e.REL_Y, y)
        if scroll != 0:
            ui.write(e.EV_REL, e.REL_WHEEL, scroll)
        ui.syn()


def key_name(keycode: int) -> str:
    return KEYCODE_TO_NAME.get(keycode, f"KEY_{keycode}")

async def decode_hid_report_and_inject(ui_kb: UInput, ui_mouse: UInput, source: str, data: bytes, 
                                        explicit_report_type: str = None, explicit_size_bytes: int = None):
    """
    Decode HID report data and inject input events.
    
    Args:
        ui_kb: keyboard uinput device
        ui_mouse: mouse uinput device
        source: source identifier for tracking state
        data: raw HID report data
        explicit_report_type: explicit report type from report map (e.g., "keyboard", "mouse", "consumer")
        explicit_size_bytes: expected size in bytes from report map (reserved for future validation/verification)
    
    Note: explicit_size_bytes is currently reserved for future use. It could be used to validate
    incoming data length or warn about mismatches, but for now we rely on the report type alone.
    """
    global key_states, media_pressed_by_source, system_pressed_by_source, current_modifiers
    global key_press_times, media_press_times, system_press_times
    global observed_mouse_lengths

    actions = []
    commands_to_execute = []
    report_id = None
    report_type = explicit_report_type  # Use explicit type if provided
    payload = data
    id_included = False
    resolve_reason = "explicit" if explicit_report_type else "none"

    # Only use fallback heuristics if explicit type is not provided
    if report_type is None:
        # Try to resolve using report map / descriptor parser
        resolved = resolve_report_definition(data)
        if resolved:
            report_id, definition, payload, id_included, resolve_reason = resolved
            report_type = definition["type"]
        else:
            # Fallback heuristics if descriptor didn't resolve or is missing
            data_len = len(data)
            if data_len in (2, 3):
                report_type = "consumer"
            elif data_len in (8, 9):           # common keyboard sizes (8 + reserved/padding)
                report_type = "keyboard"
            elif data_len == 1:
                report_type = "system"
            elif data_len in (4, 5, 6, 7):     # mouse common sizes
                report_type = "mouse"
            else:
                report_type = "unknown"

    # ───────────────────────────────────────────────────────────────
    # Mouse report – dynamic length + standard field parsing
    # ───────────────────────────────────────────────────────────────
    if report_type == "mouse":
        current_len = len(payload)

        # Track observed lengths — only from meaningful movements
        observed_mouse_lengths.setdefault(source, set())

        # Extract movement early to decide if we should record this length
        x = 0
        y = 0
        if current_len >= 3:
            x = int.from_bytes(payload[1:3], "little", signed=True)  # usually byte 1
        if current_len >= 4:
            y = int.from_bytes(payload[2:4], "little", signed=True)  # usually byte 2

        meaningful_movement = abs(x) >= MOUSE_MIN_MOVEMENT_THRESHOLD or abs(y) >= MOUSE_MIN_MOVEMENT_THRESHOLD
        if meaningful_movement:
            observed_mouse_lengths[source].add(current_len)

        # Decide effective length for this report
        seen_lengths = observed_mouse_lengths[source]
        if len(seen_lengths) == 1 and min(seen_lengths) >= 4:
            # Only one consistent length observed - trust it
            effective_len = next(iter(seen_lengths))
        elif len(seen_lengths) >= MIN_SAMPLES_FOR_CONFIDENCE:
            # Multiple observations - most common (handles occasional outliers)
            from collections import Counter
            count = Counter(seen_lengths)
            effective_len = count.most_common(1)[0][0]
        else:
            # Not confident yet prefer observed if reasonable, else common fallback
            effective_len = current_len if current_len in (4, 5, 6) else 5

        # Parse fields using standard layout (descriptor-based offsets)
        buttons = payload[0] if effective_len >= 1 else 0
        # Fix bad button data (Any button higher than all 3 buttons pushed)
        buttons = 1 if payload[0] > 0x07 >= 3 else 0

        x_mov = int.from_bytes(payload[1:2], "little", signed=True) if effective_len >= 2 else 0
        y_mov = int.from_bytes(payload[2:3], "little", signed=True) if effective_len >= 3 else 0
        scroll = int.from_bytes(payload[3:4], "little", signed=True) if effective_len >= 4 else 0

        # If effective_len > 4 - extra bytes are padding/reserved - ignore

        inject_mouse_event(ui_mouse, buttons, x_mov, y_mov, scroll)

        actions.append(
            f"btn={buttons:02x} "
            f"x={x_mov:+3} y={y_mov:+3} wheel={scroll:+2} "
        )

    # ───────────────────────────────────────────────────────────────
    # Consumer / Media keys (2-byte usage usually)
    # ───────────────────────────────────────────────────────────────
    elif report_type == "consumer":
        if len(payload) >= 2:
            usage = int.from_bytes(payload[:2], "little")
            media_pressed_by_source.setdefault(source, set())

            if usage in MEDIA_USAGE_TO_EVKEY and usage != 0:
                keycode = MEDIA_USAGE_TO_EVKEY[usage]
                current_time = time.time()

                if keycode not in media_pressed_by_source[source]:
                    press(ui_kb, keycode)
                    media_pressed_by_source[source].add(keycode)
                    media_press_times[keycode] = current_time
                    actions.append(f"{key_name(keycode)} Pressed")

            elif usage == 0:
                to_release = list(media_pressed_by_source[source])
                for keycode in to_release:
                    release(ui_kb, keycode)
                    actions.append(f"{key_name(keycode)} Released")

                    cmds = await handle_key_release_triggers(keycode, media_press_times, current_modifiers, actions)
                    commands_to_execute.extend(cmds)

                media_pressed_by_source[source].clear()

            else:
                actions.append(f"Unknown media usage 0x{usage:04x}")
        else:
            actions.append("Consumer report too short")

# Flexible keyboard report handler
    # Supports:
    # - 6 bytes: pure key array (no modifiers/reserved) — like your device's Report ID 2
    # - 8 bytes: modifiers + reserved (ignored) + 6 keys — standard boot keyboard
    # - 9 bytes: Report ID + modifiers + reserved + 6 keys — if ID prepended
    # Assumes USAGE_TO_EVKEY maps HID usage (0x04–0xA4 etc.) to evdev keycodes
    elif report_type == "keyboard":
        # First, check and optionally strip Report ID if present (assuming it's the first byte)
        report_id = None
        if len(payload) == 9:
            report_id = payload[0]  # e.g., 0x02 for keyboard
            payload = payload[1:]   # now treat as 8-byte


        if len(payload) not in (6, 8):
            actions.append(f"Keyboard report unexpected length {len(payload)} (expected 6,8,9)")
        else:
            # Now payload is either 6 or 8 bytes
            modifiers_byte = 0  # default to no modifiers
            reserved_offset = 0
            keys_start = 0
            num_keys = 6

            if len(payload) == 8:
                # Standard: modifiers (0) + reserved (1) + keys (2:8)
                modifiers_byte = payload[0]
                reserved_offset = 1  # ignore payload[1]
                keys_start = 2
            elif len(payload) == 6:
                # Minimal: keys (0:6)
                keys_start = 0
            # else: already checked

            # Extract pressed usages (HID key codes, non-zero)
            pressed_usages = {k for k in payload[keys_start:keys_start + num_keys] if k != 0}

            current_time = time.time()

            # Handle modifiers if present
            current_modifiers.clear()
            if len(payload) >= 8:  # only for formats with modifiers
                for bit, keycode in MOD_BITS_TO_EVKEY.items():
                    if modifiers_byte & (1 << bit):
                        current_modifiers.add(keycode)
                        if keycode not in key_states:
                            press(ui_kb, keycode)
                            key_states.add(keycode)
                            actions.append(f"{key_name(keycode)} Pressed")
                    elif keycode in key_states:
                        release(ui_kb, keycode)
                        key_states.remove(keycode)
                        actions.append(f"{key_name(keycode)} Released")

            # Release keys no longer pressed
            for keycode in list(key_states):
                # Filter to normal keys (not modifiers, to avoid double-release)
                if keycode in USAGE_TO_EVKEY.values():  # assuming modifiers are separate in MOD_BITS_TO_EVKEY
                    usage = next((u for u, e in USAGE_TO_EVKEY.items() if e == keycode), None)
                    if usage not in pressed_usages:
                        release(ui_kb, keycode)
                        key_states.remove(keycode)
                        if keycode in key_press_times:
                            del key_press_times[keycode]
                        actions.append(f"{key_name(keycode)} Released")
                        cmds = await handle_key_release_triggers(keycode, key_press_times, current_modifiers, actions)
                        commands_to_execute.extend(cmds)

            # Press new keys
            for usage in pressed_usages:
                keycode = USAGE_TO_EVKEY.get(usage)
                if keycode:
                    if keycode not in key_states:
                        press(ui_kb, keycode)
                        key_states.add(keycode)
                        key_press_times[keycode] = current_time
                        actions.append(f"{key_name(keycode)} Pressed")
                else:
                    actions.append(f"Unknown keyboard usage 0x{usage:02x}")

    # ───────────────────────────────────────────────────────────────
    # System control (power/sleep/wake usually 1 byte bitfield)
    # ───────────────────────────────────────────────────────────────
    elif report_type == "system":
        value = payload[0] if len(payload) >= 1 else 0
        system_pressed_by_source.setdefault(source, set())
        current_time = time.time()

        for bit, keycode in SYSTEM_BITS_TO_EVKEY.items():
            if value & (1 << bit):
                if keycode not in system_pressed_by_source[source]:
                    press(ui_kb, keycode)
                    system_pressed_by_source[source].add(keycode)
                    system_press_times[keycode] = current_time
                    actions.append(f"{key_name(keycode)} Pressed")
            else:
                if keycode in system_pressed_by_source[source]:
                    release(ui_kb, keycode)
                    system_pressed_by_source[source].remove(keycode)
                    actions.append(f"{key_name(keycode)} Released")

                    cmds = await handle_key_release_triggers(keycode, system_press_times, current_modifiers, actions)
                    commands_to_execute.extend(cmds)

    else:
        actions.append(f"Unsupported report type={report_type} length={len(data)}")

    # ───────────────────────────────────────────────────────────────
    # Logging
    # ───────────────────────────────────────────────────────────────
    payload_hex = payload.hex() if payload is not None else ""
    action_str = "; ".join(actions) if actions else "No mapped actions"

    #If there are no actions and the payload is 0 (No key Pressed, just skip the logging)
    if actions or payload_hex.strip("0") != "":
        logger.debug(f"[{source}] Report type={report_type} payload={payload_hex} {action_str}")

    for command in commands_to_execute:
        await execute_trigger_command(command)

async def notification_handler(client: BleakClient, report_info: dict, ui_kb: UInput, ui_mouse: UInput):
    """
    Handle notifications for a specific HID report.
    
    Args:
        client: BLE client
        report_info: Dictionary containing:
            - handle: characteristic handle
            - report_id: report ID
            - report_type: report type ("keyboard", "mouse", "consumer", etc.) or None
            - size_bytes: expected size in bytes or None
        ui_kb: keyboard uinput device
        ui_mouse: mouse uinput device
    """
    handle = report_info["handle"]
    report_type = report_info.get("report_type")
    size_bytes = report_info.get("size_bytes")
    
    try:
        await client.start_notify(
            handle,
            lambda _, data: asyncio.create_task(
                decode_hid_report_and_inject(ui_kb, ui_mouse, f"HID-{handle}", data, report_type, size_bytes)
            ),
        )
        logger.debug(f"Started notifications for HID report (handle={handle}, type={report_type}, size={size_bytes}).")
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        logger.debug(f"Notification handler for handle {handle} canceled.")
    finally:
        try:
            await client.stop_notify(handle)
        except Exception as e:
            logger.error(f"Error stopping notifications: {e}")


async def cleanup(client, tasks):
    for task in tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except BleakDBusError:
            pass
    if client.is_connected:
        try:
            await client.disconnect()
        except EOFError:
            logger.debug("Disconnect interrupted (EOFError) during shutdown; ignoring.")
        except Exception as e:
            logger.error(f"Error during disconnect: {e}")
    
    # Clear task list to prevent memory leaks on reconnection
    global notification_tasks, key_states, media_pressed_by_source, system_pressed_by_source, current_modifiers, key_press_times, media_press_times, system_press_times
    notification_tasks.clear()
    
    # Reset key states to prevent stuck keys after disconnect
    key_states.clear()
    media_pressed_by_source.clear()
    system_pressed_by_source.clear()
    current_modifiers.clear()
    key_press_times.clear()
    media_press_times.clear()
    system_press_times.clear()


# ==============================================================================
# main
# ==============================================================================

async def main():
    parser = argparse.ArgumentParser(description="BLE HID Bridge with detailed feedback.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--device-mac", help="Bluetooth MAC address.")
    group.add_argument("--device-name", help="Bluetooth device name (e.g., 'HID Remote01').")
    parser.add_argument("--scan-timeout", type=float, default=10.0, help="Timeout in seconds for scanning by name (default: 10).")
    
    # Verbosity arguments (mutually exclusive with --debug for backwards compatibility)
    verbosity_group = parser.add_mutually_exclusive_group()
    verbosity_group.add_argument("-v", action="count", default=0, dest="verbosity", 
                                help="Increase verbosity: -v for INFO level, -vv for DEBUG level")
    verbosity_group.add_argument("--debug", action="store_const", const=2, dest="verbosity",
                                help="Enable debug logging (equivalent to -vv, for backwards compatibility)")
    
    parser.add_argument("--logfile", type=str, default=None,
                       help="Path to log file (default: /var/log/pcp_hidbridge-<device-mac>.log)")
    
    # Create mutually exclusive group for triggers and remapkeys
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--triggers", type=str, help="Path to triggerhappy-style configuration file for executing commands on key events.")
    mode_group.add_argument("--remapkeys", type=str, help="Path to key remapping configuration file. Format: SOURCE_KEY:DEST_KEY:KEY_HELD (one mapping per line).")
    args = parser.parse_args()

    global stop_loop, triggers, key_remappings, report_definitions, report_ids_present

    # Initialize logging right after parsing args
    # If device_mac is provided, use it in the filename; otherwise use generic filename
    device_identifier = args.device_mac if args.device_mac else None
    setup_logging(device_identifier, args.logfile, args.verbosity)

    # Wait for controller to be ready
    while True:
        info = get_controller_power()
        if info["powered"]:
            break
        else:
            logger.info("Controller is not ready, waiting...")
            await asyncio.sleep(2)

    # Resolve MAC address
    if args.device_name:
        device_mac = await find_device_by_name(args.device_name, args.scan_timeout)
        if device_mac is None:
            return
    else:
        device_mac = args.device_mac

    # Load trigger configuration if specified
    if args.triggers:
        if os.path.isfile(args.triggers):
            triggers = parse_triggers_file(args.triggers)
            logger.info(f"Loaded {len(triggers)} trigger(s) from {args.triggers}")
        else:
            logger.warning(f"Trigger file not found: {args.triggers}")
            logger.warning("Continuing without trigger handling...")
    
    # Load key remapping configuration if specified
    if args.remapkeys:
        if os.path.isfile(args.remapkeys):
            key_remappings = parse_remapping_file(args.remapkeys)
            logger.info(f"Loaded {len(key_remappings)} key remapping(s) from {args.remapkeys}")
        else:
            logger.warning(f"Remapping file not found: {args.remapkeys}")
            logger.warning("Continuing without key remapping...")

    # Prepare the device (disconnect if connected, check bonding, untrust)
    ready = await prepare_device_for_connection(device_mac)
    if not ready:
        return

    stop_event = asyncio.Event()
    
    # Define disconnect callback to detect when device disconnects
    def disconnected_callback(client):
        try:
            if not stop_event.is_set():
                logger.info("Device disconnected. Will attempt to reconnect...")
            stop_event.set()
        except Exception as e:
            logger.error(f"Error in disconnect callback: {e}")
    
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

    # Only create uinput devices if not using triggers-only mode
    if args.triggers:
        ui_kb = None
        ui_mouse = None
        logger.info("Triggers-only mode: uinput devices not created")
    else:
        kb_capabilities = {e.EV_KEY: set(USAGE_TO_EVKEY.values()) | set(MEDIA_USAGE_TO_EVKEY.values()) | set(SYSTEM_BITS_TO_EVKEY.values())}
        
        # Add remapped destination keys to capabilities
        if key_remappings:
            # key_remappings now maps (source_keycode, key_held) -> (dest_keycode, modifiers_set)
            dest_keycodes = set()
            modifier_keycodes = set()
            for (dest_keycode, modifiers) in key_remappings.values():
                dest_keycodes.add(dest_keycode)
                modifier_keycodes.update(modifiers)
            
            all_remapped_keys = dest_keycodes | modifier_keycodes
            kb_capabilities[e.EV_KEY] |= all_remapped_keys
            logger.debug(f"Added {len(dest_keycodes)} remapped destination key(s) and {len(modifier_keycodes)} modifier key(s) to keyboard capabilities")
        
        mouse_capabilities = {e.EV_KEY: {e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE}, e.EV_REL: {e.REL_X, e.REL_Y, e.REL_WHEEL}}
        
        ui_kb = create_uinput_with_retry(kb_capabilities, "pCP BLE HID Keyboard")
        ui_mouse = create_uinput_with_retry(mouse_capabilities, "pCP BLE HID Mouse")

        logger.info(f"Virtual keyboard created: {ui_kb.device}")
        logger.info(f"Virtual mouse created: {ui_mouse.device}")

    # This is to avoid spamming log during periods of sleeping.
    TimeoutError = False
    NotFoundError = False

    while not stop_loop:
        if not TimeoutError and not NotFoundError:
            logger.info(f"Connecting to: {device_mac}...")
        try:
            await client.connect()
            TimeoutError = False
            NotFoundError = False
            # Read and parse report map (best-effort)
            try:
                report_map = await client.read_gatt_char(UUID_HID_REPORT_MAP)
                logger.debug(f"HID Report Map: {report_map.hex()}")
                report_definitions = parse_hid_report_map(report_map)
                if report_definitions:
                    for rid, definition in report_definitions.items():
                        rid_label = f"ID {rid}" if rid != 0 else "no ID"
                        logger.debug(f"Report {rid_label}: type={definition['type']} size={definition['size_bytes']} bytes")
                    logger.debug(f"Report IDs present: {report_ids_present}")
                else:
                    logger.debug("Report map parsed but no report definitions found.")
            except Exception as err:
                report_definitions = {}
                report_ids_present = False
                logger.warning(f"Failed to read/parse Report Map: {err}")

            # Build comprehensive report info list that ties report IDs to handles
            report_info_list = []
            report_chars = []
            for service in client.services:
                for char in service.characteristics:
                    if char.uuid == UUID_HID_REPORT:
                        report_chars.append(char)

            for char in report_chars:
                # Read Report Reference descriptor to get report ID and type
                ref_desc = char.get_descriptor(REPORT_REFERENCE_UUID)
                if ref_desc:
                    try:
                        ref_value = await client.read_gatt_descriptor(ref_desc.handle)
                        report_id = ref_value[0]
                        report_type_val = ref_value[1]  # 1=Input, 2=Output, 3=Feature
                        
                        # Only process input reports that support notifications
                        if report_type_val == 1 and "notify" in char.properties:
                            # Generate type string for logging (refactored to avoid duplication)
                            type_str = {1: "Input", 2: "Output", 3: "Feature"}.get(report_type_val, f"Unknown({report_type_val})")
                            
                            # Get report definition from parsed report map
                            if report_id in report_definitions:
                                definition = report_definitions[report_id]
                                report_info = {
                                    "handle": char.handle,
                                    "report_id": report_id,
                                    "report_type": definition["type"],  # "keyboard", "mouse", "consumer", etc.
                                    "size_bytes": definition["size_bytes"]
                                }
                                report_info_list.append(report_info)
                                logger.debug(f"   Report ID {report_id} ({type_str}, {definition['type']}) handle={char.handle}, size={definition['size_bytes']} bytes")
                            else:
                                # No definition from report map, create basic info
                                logger.debug(f"   Report ID {report_id} handle={char.handle} skipped due to incomplete data")
                    except Exception as err:
                        logger.warning(f"Failed to read Report Reference for handle {char.handle}: {err}")

            # Create notification handlers with report info
            for report_info in report_info_list:
                task = asyncio.create_task(notification_handler(client, report_info, ui_kb, ui_mouse))
                notification_tasks.append(task)

            logger.info("Waiting for input events. Press Ctrl+C to quit.")
            await stop_event.wait()

        except (asyncio.TimeoutError, asyncio.CancelledError) as err:
            if not TimeoutError:
                logger.error(f"Connect failed: {err}. Will retry...")
                TimeoutError = True
        except BleakDBusError as err:
            logger.error(f"Bleak: {err}. Will retry...")
        except BleakDeviceNotFoundError as err:
            if not NotFoundError:
                logger.error(f"Bleak: {err}. Will retry...")
                NotFoundError = True
        except Exception as err:
            logger.error(f"Unexpected error during connect: {err}. Will retry...")

        finally:
            await cleanup(client, notification_tasks)
            await asyncio.sleep(3)
            stop_event.clear()

    logger.info("Cleaning up...")
    if ui_kb is not None:
        ui_kb.close()
        logger.info("Virtual keyboard stopped.")
    if ui_mouse is not None:
        ui_mouse.close()
        logger.info("Virtual mouse stopped.")


if __name__ == "__main__":
    asyncio.run(main())
