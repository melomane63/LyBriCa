import asyncio
import functools
import glob
import json
import logging
import math
import os
import re
import signal
import socket
import sys
import time
import unicodedata
from urllib.parse import quote, unquote
from camilladsp import CamillaClient


# =====================================================================
#  FEATURES - EDITABLE
#  Each block can be switched off. A disabled block is never started,
#  and the hardware libraries it needs are neither imported nor required.
# =====================================================================

ENABLE_LMS_SYNC       = True   # LMS <-> CamillaDSP volume bridge (asyncio task).
                               # Only the bridge: the LMS CLI is used anyway by the LMS keys
                               # (play / pause / seek) and by the TOSLINK monitor.
ENABLE_AUTO_POWEROFF  = True   # Power relay: off after silence, on with sound,
                               # halt after HALT_DELAY, relay toggle on POWER long press
ENABLE_REMOTE         = True   # Remote control (all keys: volume, mute, LMS, BACK,
                               # HOMEPAGE, POWER). If False, no key works at all.
ENABLE_DISPLAY        = True   # TM1637 display + ambient light sensor
                               # (the tone / tilt / loudness keys need it)
ENABLE_TOSLINK_SWITCH = True   # Auto config switching on the TOSLINK input.
                               # True : automatism ON  -> BACK cycles within the group
                               #                          (_ or |) of the active config;
                               #                          HOMEPAGE short press = tone / tilt /
                               #                          loudness summary.
                               #        automatism OFF -> BACK = next "_" config;
                               #                          HOMEPAGE short press = next "|" config.
                               #        HOMEPAGE long press (2 s) enables / disables
                               #        the automatism (any state).
                               # False: BACK = next "_" config,
                               #        HOMEPAGE = next "|" config.
                               # If no "_*" or no "|*" config file exists, the automatism
                               # cannot run: the keys then behave as with False.


# ---------------------------------------------------------------------
#  FEATURES - DERIVED VALUES - DO NOT EDIT
#  Computed from the switches above. Change the switches, not this.
# ---------------------------------------------------------------------

# At least one feature must be enabled, otherwise the script has nothing to do
if not (ENABLE_DISPLAY or ENABLE_REMOTE or ENABLE_AUTO_POWEROFF
        or ENABLE_TOSLINK_SWITCH or ENABLE_LMS_SYNC):
    raise ValueError("No feature enabled: turn on at least one option in the FEATURES block")

TONE_ACTIVE = ENABLE_REMOTE or ENABLE_TOSLINK_SWITCH   # tone is saved / restored per config
USE_GPIO    = ENABLE_AUTO_POWEROFF                     # TOSLINK detection needs no GPIO


# ---------------------------------------------------------------------
#  CONDITIONAL IMPORTS - DO NOT EDIT
#  These depend on the FEATURES block above. Disable a feature there,
#  not here.
# ---------------------------------------------------------------------

if ENABLE_REMOTE:
    import evdev
if ENABLE_DISPLAY:
    import serial
    from tm1637_lgpio import TM1637
if USE_GPIO:
    import lgpio
if ENABLE_TOSLINK_SWITCH:
    import alsaaudio
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # DeprecationWarning on Python 3.11 / 3.12
        import audioop                    # Python >= 3.13: install audioop-lts
                                          # (Debian: apt install python3-audioop-lts)


# ---------------------------------------------------------------------
#  LOGGING & ENVIRONMENT - DO NOT EDIT
# ---------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("remote")

# PiCorePlayer persistent partition: also tells us whether we run on pCP
PCP_DIR = "/mnt/mmcblk0p2/tce"


# =====================================================================
#  SETTINGS - EDITABLE
#  Tunables grouped by topic. Only change what you understand.
# =====================================================================

# ---------------------- CamillaDSP ----------------------
CAMILLA_ADDR = ("127.0.0.1", 1234)  # CamillaDSP on this Pi (must stay local: loopback audio)

# CamillaDSP polling: one shared reader (cdsp_poller) publishes into
# `vol_state`; display/resync, the TOSLINK monitor and the LMS bridge
# subscribe to it.
POLL_INTERVAL_S = 0.25      # One read cycle of CamillaDSP (volume + mute)
PATH_EVERY = 2              # The config file path is read every N cycles (2 -> every 0.5 s)
CDSP_RECONNECT_DELAY_S = 2.0    # Seconds to wait before trying to reconnect to CamillaDSP after an error


# ---------------------- Logitech Media Server ----------------------
# LMS CLI (Command Line Interface). Default: same Pi, port 9090 (not the
# web UI 9000, not SLIM 3483). Used by the volume bridge and by send_lms_command().
LMS_ADDR = ("127.0.0.1", 9090)

# LMS player used by the volume bridge AND by every LMS command (pause, stop,
# seek, play state). "" = auto (MAC of the Pi's default network interface,
# as Squeezelite announces it). Set manually if auto-detect picks the wrong
# interface or LMS uses a different MAC. Format: raw lowercase MAC.
LMS_PLAYER_MANUAL = ""

LMS_SYNC_DEBUG = False           # Log every volume/mute change and the raw LMS notifications


# ---------------------- LMS volume-sync bridge ----------------------
# Tunables for the LMS <-> CamillaDSP volume bridge. All three have sane
# defaults; only adjust if you see reconnect churn or a slow LMS host.

LMS_RESTORE_WINDOW_S = 0.5   # (default 0.5 s) after an unmute, LMS may briefly announce a wrong volume
                             # the bridge corrects it during this window
LMS_CONNECT_TIMEOUT_S = 5    # (default 5 s) TCP connect timeout to the LMS CLI
LMS_RETRY_S = 5              # (default 5 s) delay before the bridge restarts after an error


# ---------------------- Power ----------------------

POWER_GPIO = 4              # GPIO controlling the power relay
POWER_OFF_DELAY = 2         # Minutes without sound AND without key press before the power relay is switched off
                            # (needs ENABLE_AUTO_POWEROFF); also the delay before the display is blanked
                            # (needs ENABLE_DISPLAY). A key press restarts the countdown.
HALT_DELAY = 48             # Hours of continuous silence before the Raspberry Pi shuts down (needs ENABLE_AUTO_POWEROFF)


# ---------------------- Audio analysis ----------------------
# is_silent() feeds `silent_now`, used by the power relay (auto power-off and
# HALT_DELAY), by the display blanking and by the TOSLINK -> loopback return.
# It declares silence when the RMS level is below SILENCE_RMS_DB, or when the
# signal is a steady noise floor (crest below CREST_MIN_DB).
# CREST_MIN_DB is also used by the TOSLINK probe (_toslink_probe), which has its
# own level threshold (TOSLINK_ON_DB, see below).

SILENCE_RMS_DB = -80.0   # (default -80 dB) RMS below this -> silence
CREST_MIN_DB   = 2.0     # (default 2 dB)  peak - rms below this -> steady noise, not audio


# ---------------------- TOSLINK auto switching ----------------------
# Automatic switch between the streamer config ("_*") and the TV config
# ("|*"), based on what is actually playing:
#
#   "_*" active: TOSLINK audio detected -> switch to "|*"
#   "|*" active: LMS playing + TOSLINK silent -> switch back to "_*"
#                LMS playing while the TV plays   -> LMS is paused
#
# The automatism can be disabled / enabled at run time with a long press
# on HOMEPAGE (see HOLD_TOGGLE_AUTO_S). Enabled at every start.
#
# Capture params come from the first "|*" file: all files in that group must
# share the same capture input.

TOSLINK_ON_DB = -60.0   # (default -60 dBFS RMS) above which audio is considered present.
                        # Keep it ABOVE SILENCE_RMS_DB, otherwise a signal could be detected
                        # as TV and then count as silence.
TOSLINK_DEBUG = False   # Log every TOSLINK probe measurement (peak / rms), to calibrate TOSLINK_ON_DB

# The loop timing and backoff values are internal to monitor_tv_toslink()
# and are defined right there, next to the loop that uses them.


# ---------------------- Display ----------------------

CLK, DIO = 23, 24           # TM1637 display pins
SCREEN_RETURN_DELAY = 30    # Seconds without key press before returning to the volume screen
WAKE_DISPLAY_DELAY = 30     # Seconds the display stays on after a key press that woke it while the relay is
                            # already off (POWER SAVE done, or relay forced OFF). Independent of POWER_OFF_DELAY.
SCROLL_FRAME_DELAY = 0.35   # Seconds per step of a scrolling message (POWER OFF, CONF AUTO...)
SCROLL_END_PAUSE = 0.6      # Seconds the last frame of a scrolling message stays visible
VOLUME_PEEK_S = 0.5         # Seconds the volume level stays shown after a volume key is released while muted
                            # (the screen then goes back to "--")


# Ambient light sensor (ADC) -> display brightness
SERIAL_PORT = "/dev/serial0"
BAUD_RATE = 115200
ADC_LEVELS = [80, 1000, 2500, 3500]
BRIGHTNESS_LEVELS = [0, 1, 2, 3, 7]
HYSTERESIS_RATIO = 0.03     # 3 % hysteresis between two brightness levels


# ---------------------- Remote control ----------------------

# Name of the remote on Debian. Rewritten by the installer or set manually.
REMOTE_NAME = "HID Remote01 Keyboard"

# (auto-selection below - do not edit)
# On PiCorePlayer the name is fixed; the Debian value above
# is ignored on that platform.
REMOTE_NAME_PCP = "pCP BLE HID Keyboard"
if os.path.isdir(PCP_DIR):
    REMOTE_NAME = REMOTE_NAME_PCP

# Keys (all require ENABLE_REMOTE = True)
KEY_BINDINGS = {
    'VOLUMEDOWN':    'KEY_VOLUMEDOWN',   # Decrease volume
    'VOLUMEUP':      'KEY_VOLUMEUP',     # Increase volume
    'MUTE':          'KEY_MUTE',         # Mute / unmute audio
    'PLAYPAUSE':     'KEY_PLAYPAUSE',    # Pause (short press) / stop (long press) - LMS
    'PREVIOUSSONG':  'KEY_PREVIOUSSONG', # Previous song (short press) / rewind (long press) - LMS
    'NEXTSONG':      'KEY_NEXTSONG',     # Next song (short press) / fast-forward (long press) - LMS
    'UP':            'KEY_UP',           # Bass + / tilt + / loudness + (needs ENABLE_DISPLAY)
    'DOWN':          'KEY_DOWN',         # Bass - / tilt - / loudness -      "
    'LEFT':          'KEY_LEFT',         # Treble - / presence -             "
    'RIGHT':         'KEY_RIGHT',        # Treble + / presence +             "
    'POWER':         'KEY_POWER',        # Long press: toggle relay (needs ENABLE_AUTO_POWEROFF)
                                         # Very long press: shutdown (always)
    'ENTER':         'KEY_ENTER',        # Short: bass/treble/tilt/presence all 0 or restored
                                         #        (loudness screen: loudness on/off)
                                         # Long:  switch tone / tilt screen (needs ENABLE_DISPLAY)
    'BACK':          'KEY_BACK',         # TOSLINK switch ON, automatism ON : next config of the active group ("_" or "|")
                                         # TOSLINK switch ON, automatism OFF: next config prefixed "_"
                                         # TOSLINK switch OFF               : next config prefixed "_"
    'HOMEPAGE':      'KEY_HOMEPAGE',     # TOSLINK switch ON : long press = automatism on / off
                                         #                     short press, automatism ON : tone / tilt / loudness summary
                                         #                     short press, automatism OFF: next config prefixed "|"
                                         # TOSLINK switch OFF: next config prefixed "|"
}

# Hold durations, in key-repeat events (40ms each ) 
HOLD_TOGGLE_POWER = 2       # POWER: toggle the relay ~ 1s
HOLD_SHUTDOWN = 200         # POWER: shut the Raspberry Pi down - 8s
HOLD_STOP = 10              # PLAY/PAUSE: stop
HOLD_SEEK = 10              # NEXT/PREVIOUS: seeking starts
HOLD_SWITCH_TONE_TILT = 15  # ENTER: switch between the tone and tilt screens
SEEK_STEP_FORWARD_S = 30    # Seconds jumped forward at each seek (NEXTSONG)
SEEK_STEP_BACKWARD_S = 10   # Seconds jumped backward at each seek (PREVIOUSSONG)
SEEK_INTERVAL_S = 0.8       # Seconds between two seeks while the key is held (10 s / 0.5 s = 20x speed)

# Hold duration, in real seconds (independent of the remote's repeat rate)
HOLD_TOGGLE_AUTO_S = 2.0    # HOMEPAGE: long press to enable / disable the TOSLINK automatism
                            # (only when ENABLE_TOSLINK_SWITCH = True)

# VOLUME+ / VOLUME- held: apply a volume step only on every Nth repeat of the
# remote. 1 = every repeat (fastest), 2 = every other repeat, 3 = one in three,
# etc. The remote repeats every 40 ms (measured with evdev), so the real time
# to go from 0 to 100 is roughly N * 4 seconds.
#
#   value   steps/s    0 -> 100
#   1       ~25 /s     ~4 s     fastest possible (one step per repeat)
#   2       ~12.5 /s   ~8 s
#   3       ~8.3 /s    ~12 s
#   4       ~6.25 /s   ~16 s
#   5       ~5 /s      ~20 s
#   6       ~4.2 /s    ~24 s
#   7       ~3.6 /s    ~28 s
VOLUME_SLOWDOWN = {
    KEY_BINDINGS['VOLUMEUP']:   2,
    KEY_BINDINGS['VOLUMEDOWN']: 1,
}


# ---------------------- Tone and loudness ----------------------
# SAVE_MODE decides how the tone (bass, treble, tilt, presence) and the loudness
# are shared between configs when you switch config:
#
#   all_config       : each config keeps its own tone + loudness.
#                      (historical behaviour of the script)
#   all_group        : all configs of the same group ("_" or "|") share the same
#                      tone + loudness. Changing config within the same group copies
#                      the current values to the new config.
#   half             : tone per config, loudness per group. Changing config within
#                      the same group keeps the tone of the new config but copies the
#                      current loudness to the new config.
#   same_everywhere  : all configs share the same tone + loudness, regardless of the
#                      group. Changing config copies the current values to the new config.
#
# The copy is written into settings["configs"][new_config_key] before the config is
# applied, so apply_saved_values uses the copied values. The values survive restarts:
# they are saved per config in settings.json by save_audio_settings() at every config
# switch and at script exit. Coming back to a group needs no copy: the config you land
# on is the last one used in that group, and its values were saved when you left it.
SAVE_MODE = "half"   # "all_config" | "all_group" | "half" | "same_everywhere"

if SAVE_MODE not in ("all_config", "all_group", "half", "same_everywhere"):
    raise ValueError(f"Unknown SAVE_MODE {SAVE_MODE!r}")

# Adjustment ranges (TONE_MAX_DB, PRESENCE_MAX_DB and TILT_MAX_STEPS: 9 at most, the display has 2 digits)
TONE_MAX_DB = 9             # Bass / treble: -9 .. +9 dB
PRESENCE_MAX_DB = 3         # Presence: -3 .. +3 dB
TILT_MAX_STEPS = 3          # Tilt: -3 .. +3 displayed steps (TILT_DB_STEP dB each)
TILT_DB_STEP = 2            # Real dB applied per displayed tilt step
LOUDNESS_MIN, LOUDNESS_MAX = -50, -10   # Loudness reference level range in dB (stay above -100, which means "off")
LOUDNESS_STEP = 5           # Loudness: dB per key press
DEFAULT_LOUDNESS_REF = -20  # Loudness level restored by ENTER when no previous value is saved


# ---------------------------------------------------------------------
#  INTERNAL - DO NOT EDIT
#  Everything below is implementation detail. Changing it may break
#  things silently. Kept here only because it is shared across
#  multiple functions.
# ---------------------------------------------------------------------

TASK_RESTART_DELAY_S = 2.0  # Seconds before a background task that crashed is restarted

# CamillaDSP configs: PiCorePlayer's persistent partition (PCP_DIR), or ~ elsewhere.
BASE_DIR   = PCP_DIR if os.path.isdir(PCP_DIR) else os.path.expanduser("~")
CONFIG_DIR = BASE_DIR + "/camilladsp/configs/"

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

CONFIG_GROUPS = ("_", "|")  # Config file prefixes: "_" = stereo (BACK key), "|" = TV (HOMEPAGE key)

# Saved PER CONFIG. settings.json key -> (CamillaDSP filter, parameter)
# The loudness is handled separately (see LOUDNESS_FILTER): depending on SAVE_MODE,
# its value may be copied into other configs when switching.
TONE_PARAMS = {
    "bass_gain":     ("Bass", "gain"),
    "treble_gain":   ("Treble", "gain"),
    "tilt_gain":     ("Tilt", "gain"),
    "presence_gain": ("Presence", "gain"),
}

# The loudness filter (separate from the tone gains): (CamillaDSP filter, parameter)
LOUDNESS_FILTER = ("Loudness", "reference_level")

# The four gains driven by the tone / tilt screens and the ENTER key: settings key -> filter
GAIN_KEYS = {
    "bass_gain":     "Bass",
    "treble_gain":   "Treble",
    "tilt_gain":     "Tilt",
    "presence_gain": "Presence",
}

# Arrow keys on the tone / tilt screens: screen -> key -> (filter, step, limit)
ARROW_ADJUST = {
    "tone": {
        KEY_BINDINGS['UP']:    ("Bass",    1, TONE_MAX_DB),
        KEY_BINDINGS['DOWN']:  ("Bass",   -1, TONE_MAX_DB),
        KEY_BINDINGS['RIGHT']: ("Treble",  1, TONE_MAX_DB),
        KEY_BINDINGS['LEFT']:  ("Treble", -1, TONE_MAX_DB),
    },
    "tilt": {
        KEY_BINDINGS['UP']:    ("Tilt",  TILT_DB_STEP, TILT_MAX_STEPS * TILT_DB_STEP),
        KEY_BINDINGS['DOWN']:  ("Tilt", -TILT_DB_STEP, TILT_MAX_STEPS * TILT_DB_STEP),
        KEY_BINDINGS['RIGHT']: ("Presence",  1, PRESENCE_MAX_DB),
        KEY_BINDINGS['LEFT']:  ("Presence", -1, PRESENCE_MAX_DB),
    },
}


# =====================================================================
#  MAP OF THE FILE (below the SETTINGS)
# =====================================================================
#   1. CORE - shared by every feature
#   2. LMS - Logitech Media Server
#   3. POWER - relay, auto power-off, halt
#   4. DISPLAY - TM1637 and ambient light sensor
#   5. TOSLINK - automatic _ / | config switching
#   6. REMOTE - keys
#   7. STARTUP - module-level initialisation
#   8. SHUTDOWN / MAIN


# =====================================================================
#  CORE - shared by every feature
#  Not switchable: volume curve, the state published by cdsp_poller, activity /
#  silence clocks, filters + settings.json, CamillaDSP connection, config switching.
# =====================================================================


# ---------- Volume curve (level 0..100 <-> dB) ----------
# Level 0..100 (LMS scale) <-> dB (CamillaDSP).
#
# Based on the gain sent by LMS to Squeezelite (dB = 20 * log10(gain / 65536)),
# on a 0.5 dB grid:
#     25 .. 100 : 0.5 dB / level  (-37.5 .. 0 dB)
#      2 ..  24 : 1.5 dB / level  (-71 .. -38 dB)
#      1 : -75 dB, 0 : -100 dB (the real mute is handled separately)
# The curve is strictly increasing, which makes the inverse conversion exact.

def clamp(value, low, high):
    return max(low, min(high, value))


def pct_to_db(p):
    """Level 0..100 -> dB."""
    p = clamp(p, 0, 100)
    if p >= 25:
        return (p - 100) * 0.5          # 0.5 dB / level, up to 0 dB
    if p >= 2:
        return -71.0 + (p - 2) * 1.5    # 1.5 dB / level
    return -75.0 if p == 1 else -100.0  # levels 1 and 0: special cases


def db_to_pct(d):
    """dB -> level 0..100 (nearest integer level)."""
    if d >= -37.5:
        return clamp(round(100 + d * 2), 25, 100)
    if d >= -71.0:
        return clamp(round((d + 71.0) / 1.5) + 2, 2, 24)
    return min((0, 1, 2), key=lambda i: abs(pct_to_db(i) - d))


# ---------- Shared CamillaDSP state ----------
class VolumeState:
    """Last CamillaDSP reading, published by a single reader (cdsp_poller).

    `stamp` is the time (time.monotonic) at which the reading STARTED; it is
    used to discard stale readings. `config_path` is the path of the active
    config file (used by the TOSLINK monitor to notice external changes, and
    to know the active group). `config_data` is the parsed config published by
    change_config for an INTERNAL switch: volume_watcher_loop uses it instead
    of re-reading cdsp.config.active(), which can still return the previous
    config while CamillaDSP is applying the new one.

    `config_active` is the full content of cdsp.config.active(), published
    every poll by cdsp_poller. It is the change-detection source: comparing
    it to the last known content catches any config change, INTERNAL
    (change_config) or EXTERNAL (GUI), because it reflects what CamillaDSP
    actually plays, not what file_path() stores."""

    def __init__(self):
        self.db = None
        self.muted = None
        self.config_path = None
        self.config_data = None
        self.config_active = None      # full active() content, for change detection
        self.stamp = 0.0
        self._events = []

    def subscribe(self):
        ev = asyncio.Event()
        self._events.append(ev)
        return ev

    def unsubscribe(self, ev):
        try:
            self._events.remove(ev)
        except ValueError:
            pass

    def publish(self, db, muted, config_path=None, config_data=None,
                config_active=None, stamp=None):
        self.db = db
        self.muted = bool(muted)
        if config_path:                # an empty path (e.g. during a config reload) is ignored: the previous one is kept
            self.config_path = config_path
        if config_data is not None:    # kept until the watcher consumes it
            self.config_data = config_data
        if config_active is not None:  # overwritten every poll: it is the detection source
            self.config_active = config_active
        self.stamp = time.monotonic() if stamp is None else stamp
        for ev in self._events:
            ev.set()


vol_state = VolumeState()       # Last volume / mute / config path read from CamillaDSP (published by cdsp_poller)


# ---------- Activity and silence clocks ----------
silent_now = False              # True when CamillaDSP reports silence (updated once per second by silence_monitor)
silent_since = None             # time.monotonic() at which the current silence began (None while there is sound)
last_activity = time.monotonic()  # time.monotonic() of the last remote key event (touch_activity)
                                # Single source of truth for every "inactivity" delay: relay cut,
                                # display blanking and return to the volume screen.


# All inactivity delays are computed from timestamps, never from counters
# incremented by a loop tick: the loops do not run at an exact rate, and a counter
# reset from another task is easy to forget.

def touch_activity():
    """A remote key event restarts every inactivity delay."""
    global last_activity
    last_activity = time.monotonic()


def idle_age():
    """Seconds since the last remote key event."""
    return time.monotonic() - last_activity


def silence_age():
    """Seconds of continuous audio silence (0 while there is sound)."""
    if not silent_now or silent_since is None:
        return 0.0
    return time.monotonic() - silent_since


def quiet_age():
    """Seconds of silence AND without key press: common base of the relay cut
    and of the display blanking (0 while there is sound)."""
    if not silent_now:
        return 0.0
    return min(silence_age(), idle_age())


def is_silent():
    """Silence detection: RMS threshold, then crest factor (peak vs RMS).
    A steady noise floor has peak ≈ RMS; real audio does not."""
    rms = cdsp.levels.capture_rms()
    peak = cdsp.levels.capture_peak()
    rms_vals = list(rms.values()) if isinstance(rms, dict) else list(rms)
    peak_vals = list(peak.values()) if isinstance(peak, dict) else list(peak)
    if not rms_vals or not peak_vals:
        return True

    rms_db = max(float(v) for v in rms_vals)
    if rms_db <= SILENCE_RMS_DB:                    # below threshold: silence
        return True

    peak_db = max(float(v) for v in peak_vals)
    return (peak_db - rms_db) < CREST_MIN_DB        # steady noise: silence


async def silence_monitor():
    """Single reader of the CamillaDSP levels: refreshes `silent_now` once per
    second and records in `silent_since` when the current silence began.
    auto_poweroff, display_manager_loop and monitor_tv_toslink read these
    variables instead of each querying CamillaDSP themselves.
    On a read error the previous value is kept (no power cut because of a glitch)."""
    global silent_now, silent_since
    while True:
        try:
            now_silent = is_silent()
            if now_silent and not silent_now:
                silent_since = time.monotonic()     # silence just began
            elif not now_silent:
                silent_since = None
            silent_now = now_silent
        except Exception as e:
            log.warning("Silence monitor: cannot read levels (%s)", e)
        await asyncio.sleep(1)


# ---------- Filters and settings.json ----------
prev_values = {}                # ENTER-key restore values: keys of GAIN_KEYS + "loudness_ref" (loaded from the config's saved entry)
                                # (filled by load_prev_values)
current_config_key = None       # Settings key of the active config file
current_config_external = False # True while the active config was chosen OUTSIDE this script (CamillaDSP UI, another
                                # client): its tone / loudness are neither saved nor copied to the next config


def apply_filter_param(filters, filt, param, value):
    """Set a CamillaDSP filter parameter, silently ignoring filters that are
    missing from the active config (configs need not define every filter)."""
    f = filters.get(filt)
    if not isinstance(f, dict):
        return
    params = f.get("parameters")
    if not isinstance(params, dict):
        return
    params[param] = value


def active_filter_names(config):
    """Names of the filters really used by the pipeline. A filter defined in
    `filters:` but not listed in an (enabled) Filter step has no effect on the sound."""
    names = set()
    for step in config.get("pipeline") or []:
        if isinstance(step, dict) and step.get("type") == "Filter" and not step.get("bypassed"):
            step_names = step.get("names")
            if isinstance(step_names, list):
                names.update(n for n in step_names if isinstance(n, str))
    return names


def get_filter_params(config, filt):
    """Return the 'parameters' dict of a filter, or None if the filter is missing
    from the config or not used by its pipeline."""
    if filt not in active_filter_names(config):
        return None
    f = (config.get("filters") or {}).get(filt)
    params = f.get("parameters") if isinstance(f, dict) else None
    return params if isinstance(params, dict) else None


def get_filter_param(config, filt, param):
    """Return a filter parameter, or None if the filter/parameter is missing."""
    params = get_filter_params(config, filt)
    return params.get(param) if params is not None else None


def get_all_tone_params(config):
    """Return the four tone gains and the loudness reference level in a single
    pipeline traversal (active_filter_names is called once instead of five times).
    Each value is None if the filter is missing from the config or not used by
    its pipeline."""
    active = active_filter_names(config)
    filters = config.get("filters") or {}

    def value(filt, param):
        if filt not in active:
            return None
        f = filters.get(filt)
        p = f.get("parameters") if isinstance(f, dict) else None
        return p.get(param) if isinstance(p, dict) else None

    return {
        "bass":     value("Bass", "gain"),
        "treble":   value("Treble", "gain"),
        "tilt":     value("Tilt", "gain"),
        "presence": value("Presence", "gain"),
        "loudness": value("Loudness", "reference_level"),
    }


def config_key(path):
    """Settings key of a config file: its name without directory or extension."""
    return os.path.splitext(os.path.basename(path))[0]


def get_tone(config):
    """Current tone values of a config (only for filters that really exist)."""
    tone = {}
    for key, (filt, param) in TONE_PARAMS.items():
        value = get_filter_param(config, filt, param)
        if value is not None:
            tone[key] = value
    return tone


def apply_tone(filters, tone):
    """Apply a saved tone dict to the filters of a config."""
    for key, (filt, param) in TONE_PARAMS.items():
        if key in tone:
            apply_filter_param(filters, filt, param, tone[key])


def load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    configs = data.get("configs")
    last_config = data.get("last_config")
    return {
        "configs": configs if isinstance(configs, dict) else {},
        "last_config": last_config if isinstance(last_config, dict) else {},
        "last_tone_tilt": data.get("last_tone_tilt", "tone"),
    }


def write_settings():
    """Write settings.json atomically (no half-written file on power loss)."""
    tmp_path = SETTINGS_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(settings, f, indent=4, ensure_ascii=False)
    os.replace(tmp_path, SETTINGS_PATH)


def save_audio_settings(config):
    """Store the tone and the loudness of the active config, then write
    settings.json. Always per config: how the values are shared between
    configs is decided by SAVE_MODE in change_config, which copies them into
    the new config at switch time.
    A config chosen outside this script (current_config_external) is not stored."""
    if not TONE_ACTIVE:
        return
    if current_config_key and not current_config_external:
        entry = get_tone(config)
        entry["prev"] = {key: prev_values[key] for key in TONE_PARAMS}
        ref = get_filter_param(config, *LOUDNESS_FILTER)
        if ref is not None:
            entry["loudness_ref"] = ref
            entry["prev"]["loudness_ref"] = prev_values["loudness_ref"]
        settings["configs"][current_config_key] = entry
    settings["last_tone_tilt"] = last_tone_tilt
    write_settings()


def load_prev_values(saved=None):
    """Load the ENTER-key restore values (the four gains and the loudness) from
    the saved entry of a config (defaults if none)."""
    saved_prev = saved.get("prev") if saved else None
    if not isinstance(saved_prev, dict):
        saved_prev = {}
    for key in TONE_PARAMS:
        prev_values[key] = saved_prev.get(key, 0)
    prev_values["loudness_ref"] = saved_prev.get("loudness_ref", DEFAULT_LOUDNESS_REF)


def apply_saved_values(filters, saved):
    """Apply the saved tone and the saved loudness of a config to a `filters`
    dict (filters missing from the config are ignored). Which values are in
    `saved` is decided by SAVE_MODE in change_config, which may copy another
    config's values into it before this call."""
    if saved:
        apply_tone(filters, saved)
        if "loudness_ref" in saved:
            apply_filter_param(filters, *LOUDNESS_FILTER, saved["loudness_ref"])


def is_complete_config(cfg):
    """True si cfg a la section obligatoire 'devices' non vide.
    CamillaDSP rejette toute config sans 'devices'."""
    return (isinstance(cfg, dict)
            and isinstance(cfg.get("devices"), dict)
            and bool(cfg["devices"]))


def init_tone():
    """Apply the saved tone and loudness of the config CamillaDSP started with.
    The config is pushed to CamillaDSP even when nothing is saved: on this setup
    that push is what makes CamillaDSP start processing at boot (and the players
    that depend on it only start after that)."""
    global current_config_key
    path = cdsp.config.file_path()
    if not path:
        return
    current_config_key = config_key(path)
    saved = settings["configs"].get(current_config_key)
    load_prev_values(saved)
    if is_complete_config(config_active):
        filters = config_active.get("filters")
        apply_saved_values(filters if isinstance(filters, dict) else {}, saved)
        cdsp.config.set_active(config_active)


# ---------- CamillaDSP connection, polling and task supervision ----------
def reconnect_cdsp():
    """Close and reopen the main connection. It is the SAME `cdsp` object, so
    every existing call keeps working."""
    try:
        cdsp.disconnect()
    except Exception:
        pass
    cdsp.connect()


def resync_after_reconnect():
    """After a CamillaDSP restart: re-read the active config, re-apply the
    saved tone and loudness, and push the config back (same push as at boot).
    If CamillaDSP comes back with ANOTHER config (changed from outside, the GUI
    restarting CamillaDSP), it is an external change: the config we leave is
    saved, the new one is marked external and keeps its file values (the saved
    tone / loudness are not applied to an external config)."""
    global config_active, current_config_key, current_config_external
    new_active = cdsp.config.active() or {}
    path = cdsp.config.file_path()
    new_key = config_key(path) if path else None
    if new_key and current_config_key and new_key != current_config_key:
        # Before config_active is replaced (it still holds the config we leave)
        try:
            save_audio_settings(config_active)
        except Exception as e:
            log.warning("Resync: could not save the settings (%s)", e)
        log.info("[CONFIG] External change after a CamillaDSP restart: %s -> %s (%s)",
                 current_config_key, new_key,
                 "previous config not saved: it was already external"
                 if current_config_external else "previous config saved")
        current_config_external = True
    if isinstance(new_active, dict) and new_active:
        config_active = new_active
    current_config_key = new_key
    saved = settings["configs"].get(current_config_key) if current_config_key else None
    load_prev_values(saved)
    if TONE_ACTIVE and is_complete_config(config_active):
        filters = config_active.get("filters")
        if not current_config_external:
            apply_saved_values(filters if isinstance(filters, dict) else {}, saved)
        cdsp.config.set_active(config_active)


async def supervised(name, factory):
    """Restart a task that crashes, instead of bringing the whole script down."""
    while True:
        try:
            await factory()
            return                          # normal end (e.g. auto_poweroff after the shutdown, or TOSLINK detection disabled)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Task '%s' crashed, restarting in %g s", name, TASK_RESTART_DELAY_S)
            await asyncio.sleep(TASK_RESTART_DELAY_S)


async def cdsp_poller():
    """Periodic reader of the CamillaDSP volume / mute / config path.
    Publishes into vol_state; volume_watcher_loop, monitor_tv_toslink and the
    LMS bridge consume it.
    Also publishes the full content of cdsp.config.active(): it is the
    change-detection source that works even when the GUI changed the config
    without updating file_path() (CamillaDSP stores the path but does not
    touch it on SetConfig: only change_config calls set_file_path).
    On error: automatic reconnection, the script keeps running."""
    n = 0
    failures = 0
    while True:
        try:
            t0 = time.monotonic()           # start of the read (stale-reading guard)
            db = cdsp.volume.main_volume()
            muted = bool(cdsp.volume.main_mute())
 
            path = active = None
            if n % PATH_EVERY == 0:
                path = cdsp.config.file_path()
                # active() returns the whole serialized config: it is the
                # expensive call that blocks the event loop. Read it only
                # every PATH_EVERY cycles (0.5 s), and skip it entirely
                # while a volume key is held, otherwise the volume display
                # stutters on every key repeat (the remote handler already
                # refreshes the display directly during the hold).
                if not is_volume_key_held:
                    try:
                        active = cdsp.config.active() or {}
                    except Exception:
                        active = None
 
            n += 1
            vol_state.publish(db, muted, config_path=path,
                              config_active=active, stamp=t0)
            if failures:
                log.info("CamillaDSP reachable again")
                failures = 0
        except Exception as e:
            failures += 1
            if failures == 1 or failures % 20 == 0:
                log.warning("CamillaDSP unreachable (%s), reconnecting...", e)
            await asyncio.sleep(CDSP_RECONNECT_DELAY_S)
            try:
                reconnect_cdsp()
                resync_after_reconnect()
                log.info("CamillaDSP connection restored")
            except Exception as e2:
                log.warning("Reconnection failed (%s)", e2)
            continue
        await asyncio.sleep(POLL_INTERVAL_S)


# ---------- Config switching and resync after an external change ----------
def _prefix_of(path):
    """Series of a config: '|' if the file name starts with '|', '_' otherwise."""
    return '|' if os.path.basename(path or "").startswith('|') else '_'


def find_config_file(active_config):
    """Return the path of the YAML file whose content matches active_config,
    or None. Used ONLY when the GUI changed the config without updating
    file_path() (CamillaDSP stores the path but does not touch it on SetConfig:
    only change_config calls set_file_path). Stops at the first match. Only
    called on a GUI change, never on every poll."""
    if not isinstance(active_config, dict) or not active_config:
        return None
    for path in sorted(glob.glob(CONFIG_DIR + "*")):
        try:
            data = cdsp.config.read_and_parse_file(path)
        except Exception:
            continue
        if isinstance(data, dict) and data == active_config:
            return path
    return None


async def change_config(cdsp, config_pattern, cycle=True):
    """Switch config within the group of files matching the pattern.
    cycle=True : go to the next file of the group if the active config belongs to it.
    Otherwise (or if cycle=False): go to the last config used in this group.
    How the tone and the loudness are shared between configs is decided by
    SAVE_MODE (see the SETTINGS block). The values are always saved per config;
    SAVE_MODE only controls what is copied into the new config before it is applied.
    The new config is published into vol_state (path and the parsed config)
    so that the other tasks notice the change.
    Returns the title of the new config if a switch was performed, None otherwise."""
    global config_active, current_config_key, current_config_external

    group = os.path.basename(config_pattern)[:1]
    config_files = sorted(glob.glob(config_pattern))
    if not config_files:
        return None
    current_config = cdsp.config.file_path()
    if cycle and current_config in config_files:
        index = config_files.index(current_config)
        new_config_path = config_files[(index + 1) % len(config_files)]
    else:
        last_key = settings["last_config"].get(group)
        new_config_path = next(
            (p for p in config_files if config_key(p) == last_key),
            config_files[0],
        )
    if new_config_path == current_config:
        return None

    # Remember the tone and loudness of the config we are leaving (and write them to disk)
    if current_config_external:
        log.info("[CONFIG] Leaving the external config %s: its tone / loudness are not saved",
                 current_config_key)
    save_audio_settings(config_active)

    # The config we leave is, by definition, the last one used in its group: remember
    # it now, so that coming back to this group (TV on/off, BACK / HOMEPAGE) lands on
    # it and finds its latest values - even when it is the config the script started with.
    if current_config:
        settings["last_config"][_prefix_of(current_config)] = config_key(current_config)

    # Protection 3: abort if the new config cannot be read or does not parse to a dict
    try:
        new_config_data = cdsp.config.read_and_parse_file(new_config_path)
    except Exception as e:
        log.error("Cannot read %s: %s", new_config_path, e)
        return None
    if not isinstance(new_config_data, dict):
        log.error("Config %s is invalid, aborting switch", new_config_path)
        return None

    filters = new_config_data.get('filters')
    if not isinstance(filters, dict):
        filters = {}
        new_config_data['filters'] = filters

    new_key = config_key(new_config_path)
    new_series = _prefix_of(new_config_path)

    # SAVE_MODE: depending on the mode and on whether the new config is in the same
    # group as the one we are leaving, copy the values saved for the config we are
    # leaving into settings["configs"][new_key] BEFORE applying, so that
    # apply_saved_values below picks the copy up.
    #   all_config       : never copy, B keeps its own values.
    #   all_group        : same group -> copy tone + loudness.
    #   half             : same group -> copy the loudness only (the tone stays per config).
    #   same_everywhere  : always copy tone + loudness, whatever the group.
    # Nothing is copied from a config chosen outside this script (its saved values
    # are not the ones that were playing).
    if SAVE_MODE != "all_config" and not current_config_external:
        same_group = (new_series == _prefix_of(current_config))
        old_entry = settings["configs"].get(current_config_key)
        if isinstance(old_entry, dict):
            if SAVE_MODE == "all_group" and same_group:
                settings["configs"][new_key] = dict(old_entry)
                write_settings()
            elif SAVE_MODE == "half" and same_group:
                entry_b = settings["configs"].get(new_key)
                if not isinstance(entry_b, dict):
                    entry_b = {}
                if "loudness_ref" in old_entry:
                    entry_b["loudness_ref"] = old_entry["loudness_ref"]
                if isinstance(old_entry.get("prev"), dict):
                    entry_b.setdefault("prev", {})["loudness_ref"] = (
                        old_entry["prev"].get("loudness_ref", DEFAULT_LOUDNESS_REF))
                settings["configs"][new_key] = entry_b
                write_settings()
            elif SAVE_MODE == "same_everywhere":
                settings["configs"][new_key] = dict(old_entry)
                write_settings()

    # Saved values for this config (possibly overwritten by the copy above);
    # if none, the values from the .yml are kept
    saved = settings["configs"].get(new_key)
    apply_saved_values(filters, saved)

    # Make relative filter file names absolute
    config_dir = os.path.dirname(os.path.abspath(new_config_path))
    for f in filters.values():
        params = f.get("parameters") if isinstance(f, dict) else None
        filename = params.get("filename") if isinstance(params, dict) else None
        if isinstance(filename, str) and filename and not os.path.isabs(filename):
            params["filename"] = os.path.normpath(os.path.join(config_dir, filename))

    # Protection 4: only update the local state if CamillaDSP accepted the config
    try:
        cdsp.config.set_active(new_config_data)
        cdsp.config.set_file_path(new_config_path)
    except Exception as e:
        log.error("Failed to apply config %s: %s", new_config_path, e)
        return None

    config_active = new_config_data
    current_config_key = new_key
    current_config_external = False     # this config was chosen here

    load_prev_values(saved)

    # Remember this config as the last used of its group (survives TV on/off and restarts)
    if group in CONFIG_GROUPS:
        settings["last_config"][group] = new_key
        write_settings()

    # Let the other tasks know right away (path, parsed config) instead of
    # waiting for the next cdsp_poller cycle. The active group (_ or |) is derived
    # from the published path, there is no separate prefix variable to keep in sync.
    vol_state.publish(
        vol_state.db, vol_state.muted,
        config_path=new_config_path,
        config_data=new_config_data,
    )

    # Wait until CamillaDSP has finished loading the new config, so that
    # cdsp.config.title() returns the NEW title (or "" if it has none),
    # not the previous one.
    await asyncio.sleep(0.1)

    # A config switch is a visible event: it replaces any running message
    cancel_scroll()
    display_volume_info()
    return new_config_data.get("title")

async def volume_watcher_loop():
    """Detect config changes by comparing the full active config content
    (cdsp.config.active()) to the last known one. That content is the ONLY
    detection source: it changes on any config change, internal (change_config)
    or external (GUI), because it reflects what CamillaDSP actually plays.

    INTERNAL switch: change_config published config_data -> already handled,
    skip immediately.
    LOCAL change (parameter tweak by the remote, or the poller catching up
    with change_config): current_active == config_active -> nothing to do.
    EXTERNAL change (GUI): identify the file, save the config we leave,
    update state, reset prev_values, redraw tone/tilt/loudness. Do NOT restore
    the saved values of the external config, do NOT push it back to CamillaDSP."""
    global config_active, current_config_key, current_config_external

    ev = vol_state.subscribe()
    while vol_state.db is None or vol_state.config_active is None:
        await ev.wait()
        ev.clear()
    last_volume, last_mute = vol_state.db, vol_state.muted
    last_active = vol_state.config_active
    resync_retries = 0              # failed resyncs of the same change (the config can be unreadable for a moment)
    RESYNC_RETRIES = 5             # ~ 2,5 s of attempts, then the change is given up

    while True:
        await ev.wait()
        ev.clear()
        current_volume, current_mute = vol_state.db, vol_state.muted
        current_path = vol_state.config_path
        current_active = vol_state.config_active

        # DETECTION: compare the full active config content. It changes on
        # any config change, INTERNAL (change_config) or EXTERNAL (GUI),
        # because it reflects what CamillaDSP actually plays.
        config_changed = (current_active is not None
                          and current_active != last_active)

        if config_changed:
            # INTERNAL switch: change_config published the parsed config in
            # vol_state.config_data. Trust it and skip the rest: change_config
            # already did save, apply, set_active, set_file_path,
            # current_config_key, current_config_external = False, and display.
            published, vol_state.config_data = vol_state.config_data, None
            if published is not None:
                last_active = current_active
                resync_retries = 0
                continue

            # LOCAL change (parameter tweak by the remote, or the poller
            # catching up with an internal switch): nothing to do.
            if current_active == config_active:
                last_active = current_active
                resync_retries = 0
                continue

            # EXTERNAL change (GUI or another client). Give CamillaDSP time
            # to finish loading before reading.
            await asyncio.sleep(0.3)
            try:
                new_active = cdsp.config.active() or {}
            except Exception as e:
                log.warning("Volume watcher: could not fetch active config (%s)", e)
                new_active = None

            if is_complete_config(new_active):
                # Identify the file. The GUI does not call set_file_path, so
                # file_path() may still point to the PREVIOUS config. First
                # try the file it still points to (the GUI may have reloaded
                # the same file); if it does not match, scan the YAML files;
                # as a last resort, keep current_path.
                new_path = None
                if current_path:
                    try:
                        same_file = cdsp.config.read_and_parse_file(current_path)
                    except Exception:
                        same_file = None
                    if isinstance(same_file, dict) and same_file == new_active:
                        new_path = current_path
                if new_path is None:
                    new_path = find_config_file(new_active)
                if new_path is None:
                    new_path = current_path

                new_key = config_key(new_path) if new_path else None
                key_changed = new_key != current_config_key
                old_key, was_external = current_config_key, current_config_external

                # Keep the tone / loudness set on the config we leave (the last
                # one pushed internally), before config_active is replaced.
                # save_audio_settings itself skips a config that was already
                # external.
                if current_config_key and not current_config_external and key_changed:
                    try:
                        save_audio_settings(config_active)
                        log.info("[CONFIG] External change: %s -> %s (previous config saved)",
                                 old_key, new_key)
                    except Exception as e:
                        log.warning("Volume watcher: could not save the settings (%s)", e)
                elif key_changed:
                    log.info("[CONFIG] External change: %s -> %s (%s)", old_key, new_key,
                             "previous config not saved: it was already external"
                             if was_external else "same config reloaded")

                # Update local state, mark the config external. No saved values
                # are restored, the config plays its file values; nothing is
                # pushed back to CamillaDSP.
                config_active = new_active
                current_config_key = new_key
                current_config_external = True

                # Reset prev_values so ENTER does not restore the values of the
                # config we just left.
                load_prev_values(None)

                resync_retries = 0
                log.debug("External config change detected")

                # A tone / tilt / loudness screen still shows the previous config's values
                redraw = {"tone": display_tone_info, "tilt": display_tilt_info,
                          "loudness": display_loudness_info}.get(last_displayed)
                if redraw and not scroll_active() and not display_is_off:
                    redraw()

                last_active = current_active

            else:
                resync_retries += 1
                if resync_retries <= RESYNC_RETRIES:
                    # Do not update last_active: retry the same change at the next reading
                    pass
                else:
                    resync_retries = 0
                    log.warning("Config changed but active config is incomplete; keeping previous")
                    last_active = current_active

        if current_volume != last_volume or current_mute != last_mute or config_changed:
            last_volume = current_volume
            last_mute = current_mute
            # Redraw only if the volume screen is shown, no volume key is held and
            # no message is scrolling (the end of the scroll redraws the volume itself)
            if (last_displayed == "volume" and not is_volume_key_held
                    and not scroll_active() and not display_is_off):
                display_volume_info(current_volume, current_mute)


# =====================================================================
#  LMS - Logitech Media Server
#  The CLI helpers are used by the remote keys and by the TOSLINK monitor too;
#  the volume bridge itself needs ENABLE_LMS_SYNC.
# =====================================================================


# ---------- LMS CLI helpers ----------
@functools.lru_cache(maxsize=None)   # read once; a failure (exception) is not cached
def detect_player_mac():
    """MAC of the default network interface, raw format (dc:a6:32:3c:1c:21)."""
    with open("/proc/net/route") as f:
        next(f)  # header
        iface = next(
            (cols[0] for cols in map(str.split, f)
             if len(cols) >= 8 and cols[1] == cols[7] == "00000000"),
            None,
        )
    if not iface:
        raise RuntimeError("No default network interface found")
    with open(f"/sys/class/net/{iface}/address") as f:
        return f.read().strip().lower()


def player_id():
    """LMS player used everywhere (bridge and CLI commands): the manual value
    from the SETTINGS block, or the MAC of the default network interface."""
    return LMS_PLAYER_MANUAL or detect_player_mac()


def send_lms_command(command, quiet=False):
    """Send a command to Logitech Media Server, identifying the player with
    player_id().
    Uses its own socket (no shared state), so it is safe to run in a thread.
    The reply is read until LMS closes the connection (after our "exit").
    quiet=True: do not log a failure (for commands sent in a loop)."""
    try:
        mac = quote(player_id(), safe="")

        with socket.create_connection(LMS_ADDR, timeout=5) as sock:
            sock.sendall(f"{mac} {command}\r\nexit\r\n".encode("utf-8"))
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8").strip()

    except Exception as e:
        if not quiet:
            log.error("LMS command failed: %s", e)
        return None


_lms_unreachable = False        # True while LMS does not answer (to log the change of state only)


def lms_is_playing() -> bool:
    """True if LMS is in 'play' mode for this player.
    Called in a loop by monitor_tv_toslink: it only logs the changes of state
    (LMS unreachable / reachable again), never every failed request."""
    global _lms_unreachable
    response = send_lms_command("mode ?", quiet=True)
    if not response:
        if not _lms_unreachable:
            log.warning("[LMS] unreachable or empty reply, assuming not playing")
            _lms_unreachable = True
        return False
    if _lms_unreachable:
        log.info("[LMS] reachable again")
        _lms_unreachable = False
    parts = unquote(response).split()
    return bool(parts) and parts[-1] == "play"


# ---------- LMS <-> CamillaDSP volume bridge ----------
# LMS <-> CAMILLADSP VOLUME BRIDGE (asyncio, no thread)
# Inspired by the original work by mbrennwa, entirely reworked:
#     https://github.com/mbrennwa/camilla_LMS_volume_sync
#
#   - it reads the values published by ONE reader (cdsp_poller) in a VolumeState,
#     instead of querying CamillaDSP itself;
#   - the LMS connection uses asyncio streams;
#   - CamillaDSP readings that started before our last write are ignored
#     (otherwise a stale reading could undo a change that was just received
#     from LMS).

bridge_log = logging.getLogger("lms_bridge")


class LmsBridge:
    def __init__(self, player, state, set_volume_db, set_mute, lms_addr, *,
                 restore_window_s, connect_timeout_s,
                 debug=False):
        """
        player            : MAC address of the LMS player (raw format, lowercase)
        state             : VolumeState fed by the single CamillaDSP reader
        set_volume_db     : function(db) that writes the volume to CamillaDSP
        set_mute          : function(bool) that writes the mute to CamillaDSP
        lms_addr          : (host, port) of the LMS CLI
        restore_window_s  : after an unmute, LMS may announce a wrong volume
                            during this delay
        connect_timeout_s : timeout when connecting to the LMS CLI
        (the timings come from the SETTINGS block, see LMS_* constants)
        """
        self.pq = quote(player, safe="")
        self.state = state
        self.set_volume_db = set_volume_db
        self.set_mute = set_mute
        self.lms_addr = lms_addr
        self.debug = debug
        self.restore_window_s = restore_window_s
        self.connect_timeout_s = connect_timeout_s
        player_re = re.escape(player)
        self._re_vol = re.compile(
            rf"{player_re} prefset server volume (-?\d+(?:\.\d+)?)$", re.I)
        self._re_mute = re.compile(
            rf"{player_re} prefset server mute ([01])$", re.I)
        self._writer = None
        self._last_err = None      # last bridge error logged (one log line per distinct error)
        self._reset()

    # ---------- internal state ----------

    def _reset(self):
        self.pct = 0
        self.muted = False
        self.restore_until = 0.0   # deadline (time.monotonic) of the post-unmute window
        self.prev = None           # previous CamillaDSP reading (a change must be seen twice)
        self._written_at = 0.0     # time (time.monotonic) of our last write to CamillaDSP

    def _say(self, message):
        bridge_log.info("[LMS sync] %s", message)

    def _send(self, cmd):
        w = self._writer
        if w is None or w.is_closing():
            raise ConnectionError("LMS connection closed")
        w.write((cmd + "\n").encode())

    # ---------- writes to CamillaDSP ----------

    def _write_volume(self, p):
        self.prev = None   # a change already seen once is obsolete: we are writing
        try:
            self.set_volume_db(pct_to_db(p))
        except Exception as e:
            self._say(f"cannot write volume to CamillaDSP: {e!r}")
        self._written_at = time.monotonic()

    def _apply_mute_to_camilla(self, new):
        try:
            self.set_mute(new)
        finally:
            self._written_at = time.monotonic()

    # ---------- synchronization logic ----------

    def _set_mute(self, new, origin, apply):
        # After an unmute, LMS may announce a wrong volume (the +/- keys restart
        # from 0/5 %), or nothing at all (click on the mute icon). During the
        # restore window that starts here, a wrong volume announced by LMS is
        # corrected, and no volume is pushed from CamillaDSP to LMS.
        self.muted = new
        if self.debug:
            self._say(f"{origin} mute={new}")
        apply(new)
        self.restore_until = 0.0 if new else time.monotonic() + self.restore_window_s

    def _on_lms_volume(self, p):
        if time.monotonic() < self.restore_until:
            if p != self.pct:
                self._say(f"LMS unmute: restore LMS to {self.pct}%")
                self._send(f"{self.pq} mixer volume {self.pct}")
            else:
                self.restore_until = 0.0
            return
        if p != self.pct:   # real user action (an echo of our own push has p == pct)
            self.pct = p
            if self.debug:
                self._say(f"LMS {p}% -> Camilla {pct_to_db(p):.1f} dB")
            self._write_volume(p)

    def _on_line(self, line):
        if self.debug and "prefset server" in line:
            self._say(f"<< {line}")
        m = self._re_mute.match(line)
        if m:
            lms_muted = m[1] == "1"
            if lms_muted != self.muted:
                self._set_mute(lms_muted, "LMS -> Camilla",
                               self._apply_mute_to_camilla)
            return
        m = self._re_vol.match(line)
        if m and not self.muted:
            self._on_lms_volume(round(abs(float(m[1]))))

    def _on_camilla_reading(self, db, cam_muted):
        if cam_muted != self.muted:
            self._set_mute(cam_muted, "Camilla -> LMS",
                           lambda m: self._send(f"{self.pq} mixer muting {int(m)}"))
        if self.muted or time.monotonic() < self.restore_until:
            self.prev = None
            return
        p = db_to_pct(db)
        if p != self.pct and p == self.prev:   # seen on two consecutive readings
            self.pct, self.prev = p, None
            if self.debug:
                self._say(f"Camilla {p}% -> LMS")
            self._send(f"{self.pq} mixer volume {p}")
        else:
            self.prev = p

    # ---------- loops ----------

    async def _lms_loop(self, reader):
        while True:
            raw = await reader.readline()
            if not raw:
                raise ConnectionError("LMS closed the connection")
            self._on_line(unquote(raw.decode(errors="replace")).strip())

    async def _cam_loop(self, ev):
        while True:
            await ev.wait()
            ev.clear()
            st = self.state
            if st.db is None or st.stamp <= self._written_at:
                continue   # reading started before our last write: stale
            self._on_camilla_reading(st.db, bool(st.muted))

    async def run_session(self):
        """One bridge session. It only ends by raising an exception (connection
        lost...) or when cancelled."""
        self._reset()
        ev = self.state.subscribe()
        try:
            while self.state.db is None:       # wait for the first CamillaDSP reading
                await ev.wait()
                ev.clear()
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(*self.lms_addr, limit=1 << 20),
                timeout=self.connect_timeout_s)
            self._writer = writer
            self._say("Connected to LMS CLI")
            self._last_err = None

            # At startup, CamillaDSP wins: its values are pushed to LMS
            self.pct = db_to_pct(self.state.db)
            self.muted = bool(self.state.muted)
            self._send("listen 1")
            self._send(f"{self.pq} mixer volume {self.pct}")
            self._send(f"{self.pq} mixer muting {int(self.muted)}")
            self._say(f"Startup: Camilla -> LMS {self.pct}% muted={self.muted}")
            ev.clear()

            tasks = [asyncio.create_task(self._lms_loop(reader)),
                     asyncio.create_task(self._cam_loop(ev))]
            try:
                done, _ = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    t.result()                 # re-raises the exception, if any
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self.state.unsubscribe(ev)
            w, self._writer = self._writer, None
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass

    async def run_forever(self, *, retry_s):
        """Restart the bridge `retry_s` seconds after any error."""
        while True:
            try:
                await self.run_session()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if repr(e) != self._last_err:      # no log line at every retry
                    self._say(f"Bridge error: {e!r}, retrying every {retry_s:g}s")
                    self._last_err = repr(e)
            await asyncio.sleep(retry_s)


async def lms_bridge_task():
    """Run the LMS <-> CamillaDSP volume bridge (see LmsBridge above).
    Only the player detection is retried here (no network interface yet at
    boot); the bridge restarts itself after any error, and `supervised`
    restarts this task if it ever crashes."""
    player = None
    warned = False
    while player is None:
        try:
            player = player_id()
        except Exception as e:
            if not warned:             # one log line, not one every LMS_RETRY_S
                log.warning("[LMS sync] cannot find the player (%s), retrying every %s s",
                            e, LMS_RETRY_S)
                warned = True
            await asyncio.sleep(LMS_RETRY_S)

    bridge = LmsBridge(
        player, vol_state,
        set_volume_db=cdsp.volume.set_main_volume,
        set_mute=cdsp.volume.set_main_mute,
        lms_addr=LMS_ADDR, debug=LMS_SYNC_DEBUG,
        restore_window_s=LMS_RESTORE_WINDOW_S,
        connect_timeout_s=LMS_CONNECT_TIMEOUT_S)
    await bridge.run_forever(retry_s=LMS_RETRY_S)


# =====================================================================
#  POWER - relay, auto power-off, halt
#  Needs ENABLE_AUTO_POWEROFF.
# =====================================================================

auto_power_enabled = True
is_waiting_for_sound = False


def relay_is_off():
    """True when the power relay is already off: POWER SAVE done (waiting for
    sound) or relay forced OFF by the POWER key."""
    return ENABLE_AUTO_POWEROFF and (is_waiting_for_sound or not auto_power_enabled)


def toggle_power():
    global auto_power_enabled, is_waiting_for_sound

    if not auto_power_enabled or is_waiting_for_sound:
        # Rearm the automatism, relay ON (trigger active, amps follow sound)
        auto_power_enabled = True
        lgpio.gpio_write(h, POWER_GPIO, lgpio.HIGH)
        log.info("[POWER] Auto mode re-armed, relay ON")
        scroll_message("POWER AUTO")
        is_waiting_for_sound = False
    else:
        # Disengage the automatism, force the relay OFF (amps off)
        auto_power_enabled = False
        lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
        log.info("[POWER] Relay forced OFF (auto mode disengaged)")
        scroll_message("POWER OFF")


def shutdown_system():
    """Cut the power relay (if managed) and halt the Raspberry Pi."""
    if ENABLE_AUTO_POWEROFF:
        lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
    cancel_scroll()                 # nothing may overwrite the HALT message
    show(" HALT ")
    os.system("sudo poweroff")


async def auto_poweroff():
    """Switch the power relay off after POWER_OFF_DELAY minutes without sound and
    without key press, back on when sound returns, and halt the Raspberry Pi
    after HALT_DELAY hours of (audio) silence."""
    global is_waiting_for_sound
    power_off_done = False          # the relay cut has already been handled for this quiet period
    seen_activity = last_activity   # to notice a key press and re-arm the cut

    while True:
        await asyncio.sleep(1)

        # A key press restarts the countdown, so the cut can happen again
        if last_activity != seen_activity:
            seen_activity = last_activity
            power_off_done = False

        if silent_now:
            if (quiet_age() >= POWER_OFF_DELAY * 60
                    and not power_off_done and not is_waiting_for_sound):
                power_off_done = True
                if auto_power_enabled:
                    lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
                    scroll_message("POWER SAVE")
                    is_waiting_for_sound = True  # Wait for sound or POWER key to switch back on
                    # Let the message finish (no fixed delay to keep in sync with it).
                    # If a key cancelled it, the user is there: do not blank.
                    finished = await wait_scroll()
                    if finished and silent_now and is_waiting_for_sound:
                        blank_display()
                # else: relay already forced off, nothing to cut. The display is
                # blanked by display_manager_loop (normal delay, then WAKE_DISPLAY_DELAY after a key press)

            # The Pi halt only depends on audio silence, not on key presses
            if silence_age() >= HALT_DELAY * 3600:
                shutdown_system()
                break

        else:
            power_off_done = False
            if (auto_power_enabled and is_waiting_for_sound
                    and lgpio.gpio_read(h, POWER_GPIO) == 0):
                lgpio.gpio_write(h, POWER_GPIO, lgpio.HIGH)
                if not scroll_active():          # a running message redraws the volume itself
                    display_volume_info()
                is_waiting_for_sound = False
            elif not auto_power_enabled and lgpio.gpio_read(h, POWER_GPIO) == 1:
                lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)

# =====================================================================
#  DISPLAY - TM1637 and ambient light sensor
#  Needs ENABLE_DISPLAY.
# =====================================================================


# ---------- Display driver ----------
last_displayed = None           # "volume", "tone", "tilt", "loudness", or "message" (a scrolling message is
                                # running or has just ended: the volume screen is redrawn right after it)
blank_volume_when_mute = False
display_is_off = False          # True when the display has been blanked


class NullDisplay:
    """Stand-in for the TM1637 when ENABLE_DISPLAY is False: accepts every call
    and shows nothing, so the rest of the code does not have to care."""
    def encode_string(self, text):
        return bytearray(len(text))

    def encode_char(self, char):
        return 0

    def write(self, segs):
        pass

    def brightness(self, level):
        pass


def swap(segs):
    """Reorder TM1637 segments to match the physical wiring of the display.
    Always called with 6 segments: every text shown is 6 characters long
    (show(), clear_display(), display_volume_info())."""
    segs.extend(bytearray(max(0, 6 - len(segs))))   # no-op when already 6
    segs[0], segs[2] = segs[2], segs[0]
    segs[3], segs[5] = segs[5], segs[3]
    return segs


# Extra segments for characters encode_char rejects but we want to display:
# _ (underscore) = segment D only, | (bar) = segments B + C.
_EXTRA_SEGMENTS = {
    '_': 0x08,
    '|': 0x06,
}


def encode_string_extended(tm_obj, s):
    """Like tm.encode_string, but maps _ and | to their segments."""
    segs = bytearray(len(s))
    for i, c in enumerate(s):
        if c in _EXTRA_SEGMENTS:
            segs[i] = _EXTRA_SEGMENTS[c]
        else:
            segs[i] = tm_obj.encode_char(c)
    return segs


def show(text):
    """Write a 6-character text on the display."""
    tm.write(swap(encode_string_extended(tm, text)))


def clear_display():
    tm.write(tm.encode_string(" " * 6))


def blank_display():
    """Blank the display and remember it, so that a key or sound can wake it."""
    global display_is_off
    if not ENABLE_DISPLAY:
        return
    clear_display()
    display_is_off = True


# ---------- Scrolling messages ----------
_scroll_task = None             # Current scrolling task (a new one cancels the previous)


def scroll_active():
    """True while a scrolling message is running. Every automatic redraw of the
    volume screen must check this, otherwise it would overwrite the message."""
    return _scroll_task is not None and not _scroll_task.done()


def cancel_scroll():
    """Stop the running scrolling message, if any. Returns True if one was running.
    The end-of-scroll callback does nothing for a task that is no longer the
    current one, so a cancel never triggers a redraw by itself."""
    global _scroll_task
    task, _scroll_task = _scroll_task, None
    if task is not None and not task.done():
        task.cancel()
        return True
    return False


async def wait_scroll():
    """Wait until the running scrolling message is over.
    Returns True if it ran to the end, False if there was none or if it was
    cancelled (a key was pressed)."""
    task = _scroll_task
    if task is None:
        return False
    if not task.done():
        await asyncio.wait({task})      # does not raise, and does not cancel the task if we are cancelled
    return not task.cancelled()


def _start_scroll(coro):
    """Run a display coroutine as THE scrolling task (cancels the previous one).
    When it ends normally, the volume screen comes back, unless another scroll
    has taken over or the task was cancelled."""
    global _scroll_task, last_displayed
    cancel_scroll()
    last_displayed = "message"
    task = asyncio.create_task(coro)
    _scroll_task = task

    def _on_done(t):
        if t.cancelled() or t is not _scroll_task:
            return
        asyncio.create_task(return_to_volume_later(0))

    task.add_done_callback(_on_done)


async def _scroll_coro(text, frame_delay, end_pause):
    """Scroll `text` through the 6-char display, then leave the last frame
    visible for `end_pause` seconds. Cancellable."""
    text = " " + text + " " * 6   # leading/trailing blanks for a clean entry/exit
    for i in range(len(text) - 5):
        show(text[i:i + 6])
        await asyncio.sleep(frame_delay)
    await asyncio.sleep(end_pause)


def scroll_message(text, frame_delay=None, end_pause=None):
    """Start scrolling `text` on the display (cancels any previous scroll).
    After the scroll, falls back to the volume screen."""
    if not ENABLE_DISPLAY:
        return
    _start_scroll(_scroll_coro(
        text,
        SCROLL_FRAME_DELAY if frame_delay is None else frame_delay,
        SCROLL_END_PAUSE if end_pause is None else end_pause))


async def _scroll_frames_coro(frames, frame_delay, end_pause):
    """Show each 6-char frame in turn, then leave the last one visible for
    `end_pause` seconds. Cancellable."""
    for f in frames:
        show(f)
        await asyncio.sleep(frame_delay)
    await asyncio.sleep(end_pause)


def scroll_frames(frames, frame_delay=1.2, end_pause=0.6):
    """Start showing a sequence of 6-char frames (cancels any previous scroll).
    After the sequence, falls back to the volume screen."""
    if not ENABLE_DISPLAY:
        return
    _start_scroll(_scroll_frames_coro(frames, frame_delay, end_pause))


async def return_to_volume_later(delay=0):
    """Go back to the volume screen after a scrolling message, unless another
    screen has been shown in the meantime (a key was pressed), another message
    has started, or the display has been blanked."""
    await asyncio.sleep(delay)
    if last_displayed == "message" and not display_is_off and not scroll_active():
        display_volume_info()


# ---------- Screens ----------
def blink_on():
    """1 Hz blink phase shared by all blinking dots. Derived from the clock at
    draw time: no state to keep in sync, and every dot blinks in phase."""
    return int(time.monotonic()) % 2 == 0


def fmt_gain(value):
    """2-character value for the display; '--' if the filter is absent."""
    return "--" if value is None else f"{round(value):2}"


def fmt_loudness(value):
    """Format the loudness reference level for the display:
    '---' if the filter is absent, ' No' if loudness is off (-100), otherwise
    the rounded value right-aligned on 3 characters."""
    if value is None:
        return "---"
    if round(value) == -100:
        return " No"
    return f"{round(value):3}"


def tilt_step(value):
    """Convert the tilt gain (dB) to its displayed step (TILT_DB_STEP per step),
    or None if the filter is absent. Propagating None (instead of returning 0)
    is what lets fmt_gain render '--' for an absent tilt."""
    return None if value is None else round(value / TILT_DB_STEP)


def _strip_accents(s):
    """Convert accented characters to their base letters (é -> e, à -> a, ...)."""
    return "".join(
        c for c in unicodedata.normalize('NFD', s)
        if unicodedata.category(c) != 'Mn'
    )


def _tm1637_char_ok(c):
    """True if the character can be displayed: 0-9, A-Z, a-z, space, dash,
    star, and _ and | (mapped to their own segments in encode_string_extended).
    Everything else is filtered out before encoding, to avoid a ValueError."""
    o = ord(c)
    return (o == 32 or o == 42 or o == 45
            or 48 <= o <= 57 or 65 <= o <= 90 or 97 <= o <= 122
            or c == '_' or c == '|')


def _filter_display_label(s):
    """Remove every character that cannot be displayed (_ and | are kept)."""
    return "".join(c for c in s if _tm1637_char_ok(c))


def get_config_display_label():
    """Return the 3-character label shown on the display for the active config.
    - If the active config has a title: use it as-is (accents converted,
      unprintable characters filtered, _ and | kept and mapped to segments).
    - Otherwise: use the file name (extension removed, _ and | kept, then
      the same conversions and filtering)."""
    title = cdsp.config.title()
    if title:
        label = title.strip()
    else:
        path = cdsp.config.file_path()
        label = os.path.splitext(os.path.basename(path))[0] if path else "---"
    label = _strip_accents(label)
    label = _filter_display_label(label)
    if not label:
        label = "---"
    return label[:3].ljust(3)


def display_volume_info(current_volume=None, is_muted=None):
    """Show the config label and the volume on the 0..100 scale.
    `current_volume` is the CamillaDSP main volume in dB."""
    global blank_volume_when_mute, last_displayed, display_is_off

    if not ENABLE_DISPLAY:
        return                  # no display: do not query CamillaDSP for nothing

    if current_volume is None:
        current_volume = cdsp.volume.main_volume()
    if is_muted is None:
        is_muted = cdsp.volume.main_mute()

    config_path = get_config_display_label()

    pct = db_to_pct(current_volume)                 # 0..100 (LMS / Squeezelite curve)

    if is_muted and not is_volume_key_held:
        blank_volume_when_mute, volume_str = True, f"{config_path} --"
    else:
        shown = "HI" if pct >= 100 else f"{pct:02}"
        blank_volume_when_mute, volume_str = False, f"{config_path} {shown}"

    segs = encode_string_extended(tm, volume_str)

    # Read all five parameters in a single pipeline traversal (was 5 separate
    # calls to get_filter_param, each of which re-scanned the pipeline).
    p = get_all_tone_params(config_active)
    if any(p[k] for k in ("bass", "treble", "tilt", "presence")):
        segs[3] |= 0x01    # top horizontal segment (A) on the 4th character: tone settings active

    if p["loudness"] is not None and round(p["loudness"]) != -100:
        segs[3] |= 0x08    # bottom horizontal segment (D) on the 4th character: loudness active

    # 4th character middle horizontal segment (G, 0x40), blinking: the active
    # config was chosen OUTSIDE this script (CamillaDSP GUI). It plays its file
    # values, and what is set on it with the remote is temporary: it is not saved.
    if current_config_external and blink_on():
        segs[3] |= 0x40

    # 3rd character dot: CONF MANUAL blink (only when auto OFF)
    if ENABLE_TOSLINK_SWITCH and not toslink_auto_enabled and blink_on():
        segs[2] |= 0x80

    # 4th character dot: POWER state
    #   auto OFF (relay forced OFF) -> blinking (1 s / 1 s)
    #   otherwise                   -> off (never steady on)
    if ENABLE_AUTO_POWEROFF:
        if not auto_power_enabled and lgpio.gpio_read(h, POWER_GPIO) == 0:
            if blink_on():
                segs[3] |= 0x80

    tm.write(swap(segs))
    last_displayed = "volume"
    display_is_off = False


def display_tone_info():
    global last_displayed
    p = get_all_tone_params(config_active)
    show(f"{fmt_gain(p['bass'])}B{fmt_gain(p['treble'])}T")
    last_displayed = "tone"


def display_loudness_info():
    global last_displayed
    p = get_all_tone_params(config_active)
    show(f"{fmt_loudness(p['loudness'])} Ld")
    last_displayed = "loudness"


def display_tilt_info():
    global last_displayed
    p = get_all_tone_params(config_active)
    show(f"{fmt_gain(tilt_step(p['tilt']))}T{fmt_gain(p['presence'])}P")
    last_displayed = "tilt"


def tone_summary_frames():
    """Return the 6-char frames for the tone, tilt and loudness screens,
    built exactly like display_tone_info(), display_tilt_info() and
    display_loudness_info() do (same helpers, same rendering)."""
    p = get_all_tone_params(config_active)
    return [
        f"{fmt_gain(p['bass'])}B{fmt_gain(p['treble'])}T",
        f"{fmt_gain(tilt_step(p['tilt']))}T{fmt_gain(p['presence'])}P",
        f"{fmt_loudness(p['loudness'])} Ld",
    ]


def show_tone_summary():
    """Scroll through the tone, tilt and loudness screens (same duration for
    each), then return to the volume screen immediately."""
    scroll_frames(tone_summary_frames(), frame_delay=1.2, end_pause=0)


# ---------- Brightness (ambient light sensor) ----------
adc_buffer = b""                # Incomplete serial data waiting for its newline


def adc_to_brightness(adc_value):
    """Convert an ADC reading to a display brightness, with hysteresis."""
    global last_level

    for i, threshold in enumerate(ADC_LEVELS):
        if adc_value < threshold:
            target_level = i
            break
    else:
        target_level = len(BRIGHTNESS_LEVELS) - 1

    if target_level > last_level and adc_value > ADC_LEVELS[last_level] * (1 + HYSTERESIS_RATIO):
        last_level = target_level
    elif target_level < last_level and adc_value < ADC_LEVELS[target_level] * (1 - HYSTERESIS_RATIO):
        last_level = target_level

    return BRIGHTNESS_LEVELS[last_level]


async def adc_reader_loop():
    """Read the ambient light sensor without ever blocking the event loop.
    Only the most recent complete line received since the last pass is used."""
    global adc_buffer

    if ser is None:
        log.info("Ambient light sensor unavailable; using fallback brightness")
        return

    last_brightness = -1

    while True:
        if ser.in_waiting:
            adc_buffer += ser.read(ser.in_waiting)
            # Everything before the last \n is complete; the rest waits for more data
            *lines, adc_buffer = adc_buffer.split(b"\n")
            line = next(
                (l.decode('utf-8', errors='ignore').strip()
                 for l in reversed(lines) if l.strip()),
                None,
            )
            if line:
                try:
                    adc_value = int(line)
                    brightness = adc_to_brightness(adc_value)
                    if brightness != last_brightness:
                        tm.brightness(brightness)
                        last_brightness = brightness
                        log.info("ADC = %s -> Brightness = %s", adc_value, brightness)
                except ValueError:
                    log.warning("Non-numeric input received: %s", line)

        await asyncio.sleep(0.1)


# ---------- Display housekeeping loops ----------
async def display_manager_loop():
    """Housekeeping of the display, once per second: return to the volume screen
    after SCREEN_RETURN_DELAY s without key press, blanking after a long quiet
    period, wake-up when sound returns.
    Every delay is a timestamp comparison (idle_age / silence_age), so none of them
    depends on how often this loop runs."""
    while True:
        # Return to the volume screen after SCREEN_RETURN_DELAY seconds without
        # key event. Never during a scroll, and never on a blanked display
        # (display_volume_info() would wake it up).
        if (last_displayed != "volume"
                and not scroll_active()
                and not display_is_off
                and idle_age() >= SCREEN_RETURN_DELAY):
            display_volume_info()

        # Blank the display after a long silence, wake it when sound returns
        if silent_now:
            # Relay still on (managed, not yet cut): nothing to do here, auto_poweroff
            # cuts the relay, shows POWER SAVE, then blanks the display itself.
            # Relay off (or not managed): the silence must have lasted POWER_OFF_DELAY,
            # then the display goes off after POWER_OFF_DELAY without key press, or
            # after only WAKE_DISPLAY_DELAY when the relay is already off (key wake-up).
            normal_delay = POWER_OFF_DELAY * 60
            relay_managed_on = ENABLE_AUTO_POWEROFF and not relay_is_off()
            key_delay = WAKE_DISPLAY_DELAY if relay_is_off() else normal_delay
            if (not relay_managed_on and not display_is_off and not scroll_active()
                    and silence_age() >= normal_delay and idle_age() >= key_delay):
                blank_display()
        elif display_is_off:
            display_volume_info()

        await asyncio.sleep(1.0)


async def blink_loop():
    """1 s tick, aligned on the clock, that redraws the volume screen while a
    dot must blink (external config middle bar on the 4th character, CONF MANUAL
    dot on the 3rd character, POWER OFF dot on the 4th character). The phase
    itself comes from blink_on() at draw time."""
    while True:
        await asyncio.sleep(1.0 - time.monotonic() % 1.0)

        blinking = (current_config_external
                    or (ENABLE_TOSLINK_SWITCH and not toslink_auto_enabled)
                    or (ENABLE_AUTO_POWEROFF and not auto_power_enabled))

        # Only when the volume screen is at rest: never wake a blanked display,
        # never overwrite a message or a held-volume-key display
        if (blinking
                and last_displayed == "volume"
                and not display_is_off
                and not scroll_active()
                and not is_volume_key_held):
            display_volume_info()


# =====================================================================
#  TOSLINK - automatic _ / | config switching
#  Needs ENABLE_TOSLINK_SWITCH.
# =====================================================================

toslink_auto_enabled = True     # TOSLINK automatism active (toggled by a long HOMEPAGE press)
toslink_ready = False           # True once monitor_tv_toslink has found its "_*" and "|*" config files


def toslink_active():
    """True when the TOSLINK automatism can run: the feature is enabled AND
    monitor_tv_toslink found its config files. Otherwise BACK / HOMEPAGE behave
    as with ENABLE_TOSLINK_SWITCH = False."""
    return ENABLE_TOSLINK_SWITCH and toslink_ready


def toggle_toslink_auto():
    """Enable / disable the TOSLINK automatism (long press on HOMEPAGE)."""
    global toslink_auto_enabled
    toslink_auto_enabled = not toslink_auto_enabled
    log.info("[TOSLINK] Automatism %s", "ON" if toslink_auto_enabled else "OFF")
    scroll_message("CONF AUTO" if toslink_auto_enabled else "CONF MANUAL")


# --------------------- Config switching: TOSLINK mode ---------------------
# The prefix of the active config (_ or |) decides what is monitored. It is
# derived at each cycle from the config path published in vol_state, so manual
# BACK / HOMEPAGE presses (or any external change) are honoured automatically.
#
#   _XX : CamillaDSP captures the loopback -> TOSLINK is free.
#         The TOSLINK is probed directly through ALSA (0.5 s). A reading counts
#         when rms >= TOSLINK_ON_DB AND crest (peak - rms) >= CREST_MIN_DB
#         (a steady noise floor has peak ~= rms and is ignored).
#         2 consecutive readings -> switch to |XX
#
#   |XX : CamillaDSP captures TOSLINK -> `silent_now` is the TV level.
#         Every TV_LOOP_S, LMS is asked whether it is playing:
#           LMS playing + TOSLINK silent for SILENT_TICKS cycles
#             -> switch back to _XX
#           LMS playing while the TV plays -> LMS is paused
#
#   A failed switch is retried with an exponential delay
#   (RETRY_BASE_S, doubled each time, up to RETRY_MAX_S).
#
#   Automatism disabled (long HOMEPAGE press, `toslink_auto_enabled` False):
#   no probing, no automatic switch, no LMS pause. BACK keeps working (it then
#   forces the "_" group).

# Capture formats the probe can measure: CamillaDSP name -> (bytes per sample, bits)
TOSLINK_FORMATS = {
    "S16_LE":  (2, 16),
    "S24_3LE": (3, 24),
    "S24_LE":  (4, 24),     # 24 bits in a 32-bit word
    "S32_LE":  (4, 32),
}


def read_toslink_capture_params():
    """Read device / rate / channels / format from the first '|*' YAML config.
    All '|*' configs share the same capture input, so the first one is enough.
    Returns a dict {'device', 'rate', 'channels', 'format'} or None on failure."""
    files = sorted(glob.glob(CONFIG_DIR + "|*"))
    if not files:
        log.error("[TOSLINK] No '|*' config file found in %s", CONFIG_DIR)
        return None
    try:
        data = cdsp.config.read_and_parse_file(files[0])
        if not isinstance(data, dict):
            return None
        dev = data.get("devices") or {}
        cap = dev.get("capture") or {}
        device = cap.get("device")
        if not isinstance(device, str) or not device:
            log.error("[TOSLINK] No capture device in %s", files[0])
            return None
        return {
            "device":   device,
            "rate":     int(dev.get("samplerate") or 48000),
            "channels": int(cap.get("channels") or 2),
            "format":   cap["format"],
        }
    except Exception as e:
        log.error("[TOSLINK] Cannot read %s: %s", files[0], e)
        return None


def _levels(data, fmt, channels=2):
    """Peak and RMS level, in dBFS, of a buffer of samples in the CamillaDSP
    format `fmt`. Returns (peak_db, rms_db), or (None, None) if it is empty.
    Like the levels CamillaDSP reports (and is_silent() uses), the RMS is the one
    of the LOUDEST channel, not of the channels mixed together: otherwise a steady
    signal with different levels on the two channels (a DC offset, for instance)
    would get a peak - rms gap and pass the CREST_MIN_DB test as if it were audio.
    Stereo only; with another channel count the RMS is the mixed one.
    audioop does the work in C: far cheaper than a Python loop over samples."""
    if not data:
        return None, None
    width, bits = TOSLINK_FORMATS[fmt]
    full_scale = 2 ** (bits - 1)
    peak = audioop.max(data, width)
    if channels == 2:
        rms = max(audioop.rms(audioop.tomono(data, width, 1, 0), width),    # left
                  audioop.rms(audioop.tomono(data, width, 0, 1), width))    # right
    else:
        rms = audioop.rms(data, width)
    peak_db = 20.0 * math.log10(peak / full_scale) if peak > 0 else -1000.0
    rms_db = 20.0 * math.log10(rms / full_scale) if rms > 0 else -1000.0
    return peak_db, rms_db


def _toslink_probe(params):
    """Runs in a thread (blocks ~0.5 s).
    `params` is the dict returned by read_toslink_capture_params().
    Returns (peak_db, rms_db, None) if the TOSLINK is free,
    (None, None, err) if it cannot be opened, (None, None, None) if no samples."""
    try:
        pcm = alsaaudio.PCM(
            type=alsaaudio.PCM_CAPTURE, mode=alsaaudio.PCM_NONBLOCK,
            device=params["device"], channels=params["channels"],
            rate=params["rate"], format=getattr(alsaaudio, "PCM_FORMAT_" + params["format"]),
            periodsize=4096,
        )
    except Exception as e:
        return None, None, e

    chunks = []
    try:
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            length, data = pcm.read()
            if length > 0:
                chunks.append(data)
            else:
                time.sleep(0.01)
    finally:
        pcm.close()

    peak_db, rms_db = _levels(b"".join(chunks), params["format"], params["channels"])     # computed once, at the end
    return peak_db, rms_db, None


async def monitor_tv_toslink():
    """TOSLINK <-> SQUEEZELITE auto switching (see the block comment above)."""
    global toslink_ready
    loop = asyncio.get_running_loop()

    # Internal timings (not user-facing: they are calibrated on the detection
    # mechanism itself, not on the user's environment).
    LOOP_S       = 0.5      # base cycle period; "_" mode also spends ~0.5 s probing
    TV_LOOP_S    = 1.0      # "|" mode: exactly one LMS request per cycle
    SILENT_TICKS = 2        # consecutive silent cycles (LMS playing) before going back to "_"
                            # NB: depends on TV_LOOP_S: with 1.0 s -> ~2 s total
    RETRY_BASE_S = 5.0      # first delay after a failed switch; doubled at each failure
    RETRY_MAX_S  = 60.0     # backoff ceiling

    toslink_params = read_toslink_capture_params()
    if toslink_params is None:
        log.error("[TOSLINK] Cannot determine capture params, detection disabled")
        return
    log.info("[TOSLINK] Capture: %s @ %d Hz, %d ch, %s",
             toslink_params["device"], toslink_params["rate"],
             toslink_params["channels"], toslink_params["format"])
    if TOSLINK_ON_DB <= SILENCE_RMS_DB:
        log.warning("[TOSLINK] TOSLINK_ON_DB (%g) is not above SILENCE_RMS_DB (%g): "
                    "the TV may be detected, then counted as silence", TOSLINK_ON_DB, SILENCE_RMS_DB)

    if not glob.glob(CONFIG_DIR + "_*"):
        log.error("[LOOPBACK] No '_*' config file found in %s", CONFIG_DIR)
        return
    log.info("[LOOPBACK] Configs '_*' found in %s", CONFIG_DIR)
    toslink_ready = True        # both groups exist: BACK / HOMEPAGE may rely on the automatism

    last_config_path = cdsp.config.file_path() or ""
    toslink_hits = 0            # consecutive TOSLINK readings above the threshold ("_" mode)
    silent_ticks = 0            # consecutive cycles LMS playing + TOSLINK silent ("|" mode)
    switch_failures = 0         # consecutive failed automatic switches (backoff)
    last_err = None             # last probe error logged (each error is logged once)

    async def try_switch(pattern, target_prefix):
        """Switch to the requested group and check that it really happened.
        On failure: exponential backoff instead of retrying at every cycle."""
        nonlocal last_config_path, switch_failures
        await change_config(cdsp, pattern, cycle=False)
        # change_config publishes the new path into vol_state before returning;
        # if it failed, vol_state still holds the old path
        last_config_path = vol_state.config_path or ""
        if _prefix_of(last_config_path) == target_prefix:
            switch_failures = 0
            await asyncio.sleep(2.0)            # let the levels settle
            return
        switch_failures += 1
        delay = min(RETRY_MAX_S, RETRY_BASE_S * 2 ** (switch_failures - 1))
        log.warning("[TOSLINK] Switch to '%s' failed (#%d), retry in %g s",
                    target_prefix, switch_failures, delay)
        await asyncio.sleep(delay)

    while True:
        await asyncio.sleep(LOOP_S)

        # Config changed from outside (remote BACK / HOMEPAGE, GUI...):
        # restart the detection from scratch
        cp = vol_state.config_path
        if cp and cp != last_config_path:
            old_prefix, new_prefix = _prefix_of(last_config_path), _prefix_of(cp)
            last_config_path = cp
            if new_prefix != old_prefix:
                log.info("[TOSLINK] Config change: %s -> %s",
                         old_prefix, new_prefix)
            toslink_hits = silent_ticks = 0
            await asyncio.sleep(1.0)
            continue

        # Automatism disabled (long HOMEPAGE press): no probing, no switching,
        # no LMS pause. The change detection above still runs.
        if not toslink_auto_enabled:
            toslink_hits = silent_ticks = 0
            continue

        try:
            if _prefix_of(vol_state.config_path) == '_':
                # _XX: TOSLINK is free -> probe it
                silent_ticks = 0
                peak, rms, err = await loop.run_in_executor(
                    None, _toslink_probe, toslink_params)
                if err is not None:
                    if repr(err) != last_err:      # one log line per distinct error
                        log.warning("[TOSLINK] Probe failed: %r", err)
                        last_err = repr(err)
                    toslink_hits = 0
                    await asyncio.sleep(2.0)
                    continue
                last_err = None

                if TOSLINK_DEBUG and peak is not None:
                    log.info("[TOSLINK] probe: peak=%.1f rms=%.1f crest=%.1f",
                             peak, rms, peak - rms)

                # Protection 1: peak ~= rms -> steady noise floor, ignore it
                # Protection 2: RMS below the level threshold -> no audio
                if (peak is None or (peak - rms) < CREST_MIN_DB
                        or rms < TOSLINK_ON_DB):
                    toslink_hits = 0
                    continue

                # Protection 3: 2 consecutive readings above threshold
                toslink_hits += 1
                if toslink_hits >= 2:
                    log.info("[TOSLINK] TV confirmed (peak=%.1f rms=%.1f) "
                             "-> switch to |XX", peak, rms)
                    toslink_hits = 0
                    await try_switch(CONFIG_DIR + '|*', '|')

            else:
                # |XX: TOSLINK captured by CamillaDSP, silent_now is the TV level
                toslink_hits = 0
                lms_playing = await asyncio.to_thread(lms_is_playing)
                if lms_playing and silent_now:
                    # Debounce: a single silent cycle (dialogue pause, stale
                    # reading after a switch) must not send us back to _XX
                    silent_ticks += 1
                    if silent_ticks >= SILENT_TICKS:
                        log.info("[LOOPBACK] LMS playing + TOSLINK silent "
                                 "-> switch to _XX")
                        silent_ticks = 0
                        await try_switch(CONFIG_DIR + '_*', '_')
                        continue
                elif lms_playing:
                    # LMS started while the TV is playing: the TV has priority
                    silent_ticks = 0
                    log.info("[TV] LMS started while the TV plays -> pause")
                    # "pause 1" forces the pause (a bare "pause" toggles, and would
                    # restart the playback if LMS was paused in the meantime)
                    await asyncio.to_thread(send_lms_command, "pause 1")
                    await asyncio.sleep(2.0)
                else:
                    silent_ticks = 0
                await asyncio.sleep(TV_LOOP_S - LOOP_S)

        except Exception as e:
            toslink_hits = silent_ticks = 0
            log.warning("[TOSLINK] iteration failed: %s", e)
            await asyncio.sleep(2.0)


# =====================================================================
#  REMOTE - keys
#  Needs ENABLE_REMOTE.
# =====================================================================


# ---------- State of the keys ----------
key_hold_counter = 0            # Key-repeat events of the key being held (remote_events only).
                                # A repeat counter on purpose (HOLD_* thresholds are in events), not a clock.
is_volume_key_held = False      # True from a volume key press until VOLUME_PEEK_S after its release
enter_display_at_press = None   # Screen shown when ENTER was pressed (to tell short/long press apart)


# ---------- Key actions ----------
def change_volume_from_key(key, current_volume, volume_step=1):
    """Move one step on the 0..100 scale (LMS / Squeezelite curve).
    `current_volume` is the CamillaDSP main volume in dB."""
    pct = db_to_pct(current_volume)
    delta = -volume_step if key == KEY_BINDINGS['VOLUMEDOWN'] else volume_step
    new_pct = clamp(pct + delta, 0, 100)

    if new_pct != pct:
        new_volume = pct_to_db(new_pct)
        cdsp.volume.set_main_volume(new_volume)
        display_volume_info(new_volume)


_volume_release_task = None     # Pending "end of the volume key" task (see schedule_volume_release)


def cancel_volume_release():
    """Cancel the pending end-of-volume-key task, if any (a volume key was pressed again)."""
    global _volume_release_task
    task, _volume_release_task = _volume_release_task, None
    if task is not None and not task.done():
        task.cancel()


async def _volume_release_coro():
    """VOLUME_PEEK_S after the release: the volume key is no longer "held", the
    volume screen is redrawn (a muted state shows "--" again)."""
    global is_volume_key_held
    await asyncio.sleep(VOLUME_PEEK_S)
    is_volume_key_held = False
    if last_displayed == "volume" and not display_is_off and not scroll_active():
        display_volume_info()


def schedule_volume_release():
    """Called when a volume key is released."""
    global _volume_release_task
    cancel_volume_release()
    _volume_release_task = asyncio.create_task(_volume_release_coro())


def handle_arrow_keys(key):
    """Adjust the parameters of the screen currently displayed."""
    up, down = KEY_BINDINGS['UP'], KEY_BINDINGS['DOWN']

    if last_displayed == "loudness":
        loudness_params = get_filter_params(config_active, "Loudness")
        if loudness_params is not None and key in (up, down):   # LEFT / RIGHT do nothing here
            ref_level = loudness_params.get('reference_level', 0) + (LOUDNESS_STEP if key == up else -LOUDNESS_STEP)
            loudness_params['reference_level'] = clamp(ref_level, LOUDNESS_MIN, LOUDNESS_MAX)
            cdsp.config.set_active(config_active)
            display_loudness_info()

    elif last_displayed in ARROW_ADJUST:
        filt, step, limit = ARROW_ADJUST[last_displayed][key]
        params = get_filter_params(config_active, filt)
        if params is not None:      # filter absent from this config: the key does nothing
            params['gain'] = clamp(params.get('gain', 0) + step, -limit, limit)
            cdsp.config.set_active(config_active)
            (display_tilt_info if last_displayed == "tilt" else display_tone_info)()

    else:
        if last_tone_tilt == "tilt":
            display_tilt_info()
        else:
            display_tone_info()


def handle_enter_press(screen):
    """Short ENTER press.
    - tone / tilt screen: bass, treble, tilt and presence are ALL set to 0,
      or ALL restored to their previous values if they are already all at 0.
    - loudness screen: loudness is switched off (-100) or restored."""
    if screen in ("tone", "tilt"):
        params = {key: get_filter_params(config_active, filt) for key, filt in GAIN_KEYS.items()}
        existing = {key: p for key, p in params.items() if p is not None}
        if not existing:
            return

        if all(p.get('gain', 0) == 0 for p in existing.values()):
            # Everything is at 0: restore the previous values
            for key, p in existing.items():
                p['gain'] = prev_values[key]
            prev_values.update(dict.fromkeys(GAIN_KEYS, 0))
        else:
            # Remember the current values, then set everything to 0
            for key, p in params.items():
                prev_values[key] = p.get('gain', 0) if p is not None else 0
            for p in existing.values():
                p['gain'] = 0

        cdsp.config.set_active(config_active)
        if screen == "tone":
            display_tone_info()
        else:
            display_tilt_info()

    elif screen == "loudness":
        loudness_params = get_filter_params(config_active, "Loudness")
        if loudness_params is not None:
            if loudness_params.get('reference_level') == -100:      # -100 = loudness off
                loudness_params['reference_level'] = prev_values["loudness_ref"]
                prev_values["loudness_ref"] = -100
            else:
                prev_values["loudness_ref"] = loudness_params.get('reference_level', DEFAULT_LOUDNESS_REF)
                loudness_params['reference_level'] = -100
            cdsp.config.set_active(config_active)
            display_loudness_info()


# ---------- Remote device and key events ----------
def find_remote():
    """Return the remote input device (grabbed), or None if it is not available."""
    for path in evdev.list_devices():
        try:
            device = evdev.InputDevice(path)
        except OSError:                 # device vanished between list and open
            continue
        if device.name != REMOTE_NAME:
            device.close()
            continue
        try:
            # Exclusive access: the kernel stops delivering this device's events
            # to anyone else (logind no longer sees the remote's KEY_POWER)
            device.grab()
        except OSError as e:            # EBUSY: another process holds it / ENODEV: gone
            log.warning("Cannot grab '%s' (%s), will retry", REMOTE_NAME, e)
            device.close()
            continue
        log.info("'%s' found at %s.", REMOTE_NAME, path)
        return device
    return None


async def remote_watcher():
    """Wait for the remote, serve it until it disconnects, then start over."""
    while True:
        device = find_remote()
        if device is None:
            await asyncio.sleep(1)
            continue
        try:
            await remote_events(device)     # returns when the remote disconnects
        finally:
            device.close()                  # also releases the grab
        log.info("Remote disconnected, waiting for it to come back...")


async def remote_events(device):
    global last_displayed, key_hold_counter, is_volume_key_held
    global last_tone_tilt, enter_display_at_press

    last_seek_time = 0
    home_press_time = 0.0           # moment HOMEPAGE was pressed (long-press detection, TOSLINK switch ON)
    home_toggled = False            # the HOMEPAGE long press has already been handled
    volume_repeat_count = 0         # repeats since the last applied volume step (VOLUME_SLOWDOWN)
    swallowed_key = None            # key that only woke the display: ignored until released
    while True:
        try:
            async for event in device.async_read_loop():
                if event.type != evdev.ecodes.EV_KEY:
                    continue

                # Any key event restarts every inactivity delay: return to the volume
                # screen, relay cut, display blanking. Done BEFORE the display-off test,
                # so the key press that wakes the display counts too.
                touch_activity()

                attrib = evdev.categorize(event)
                key = attrib.keycode
                volume_keys = (KEY_BINDINGS['VOLUMEDOWN'], KEY_BINDINGS['VOLUMEUP'])

                # Display off: the first key only wakes it up, and its repeat / release are ignored
                if attrib.keystate == 1 and display_is_off:
                    display_volume_info()
                    swallowed_key = key
                    continue
                if swallowed_key is not None and key == swallowed_key:
                    if attrib.keystate == 0:
                        swallowed_key = None
                    continue

                if attrib.keystate == 1:  # Key pressed
                    key_hold_counter = 0

                    # Stop any running scroll so the key's action is visible immediately.
                    # A volume key is left alone here: its handler below shows the volume
                    # screen first when last_displayed is not "volume". For any other key
                    # the volume screen is restored right away, so a cancelled message never
                    # leaves a half-scrolled frame behind.
                    if cancel_scroll() and key not in volume_keys:
                        display_volume_info()

                    if key in volume_keys:
                        cancel_volume_release()
                        is_volume_key_held = True
                        volume_repeat_count = 0
                        current_volume = cdsp.volume.main_volume()
                        is_muted = cdsp.volume.main_mute()

                        if ENABLE_DISPLAY and (last_displayed != "volume" or blank_volume_when_mute):
                            display_volume_info(current_volume, is_muted)
                        else:
                            change_volume_from_key(key, current_volume)

                    # `in`, not `==`: evdev returns a list when a code has several names
                    elif KEY_BINDINGS['MUTE'] in key:
                        # End the volume "peek" now: the new mute state must show at once
                        cancel_volume_release()
                        is_volume_key_held = False
                        is_muted = not cdsp.volume.main_mute()
                        cdsp.volume.set_main_mute(is_muted)
                        display_volume_info(cdsp.volume.main_volume(), is_muted)

                    elif ENABLE_DISPLAY and key in (KEY_BINDINGS['UP'], KEY_BINDINGS['DOWN'],
                                                    KEY_BINDINGS['RIGHT'], KEY_BINDINGS['LEFT']):
                        handle_arrow_keys(key)

                    elif key == KEY_BINDINGS['BACK']:
                        if toslink_active() and toslink_auto_enabled:
                            # Auto ON: cycle INSIDE the active group, no group change.
                            # Fresh read on purpose (not vol_state): change_config re-reads
                            # file_path() itself, both must agree within one operation.
                            prefix = _prefix_of(cdsp.config.file_path())
                            await change_config(cdsp, CONFIG_DIR + prefix + '*')
                        else:
                            # Auto OFF (or feature disabled): force the "_" group
                            await change_config(cdsp, CONFIG_DIR + '_*')

                    elif key == KEY_BINDINGS['HOMEPAGE']:
                        if toslink_active():
                            # Arm the long-press detection; the short-press action
                            # (auto ON: tone summary, auto OFF: force "|" group) is
                            # decided on release, so a long press does not trigger it.
                            home_press_time = time.monotonic()
                            home_toggled = False
                        else:
                            await change_config(cdsp, CONFIG_DIR + '|*')

                    elif ENABLE_DISPLAY and key == KEY_BINDINGS['ENTER']:
                        # Remember the screen shown BEFORE any change, for the action on release
                        enter_display_at_press = last_displayed
                        if last_displayed == "volume":
                            display_loudness_info()

                elif attrib.keystate == 2:  # Key held down

                    if key in volume_keys:
                        # One step every VOLUME_SLOWDOWN repeats of the remote
                        volume_repeat_count += 1
                        if volume_repeat_count >= VOLUME_SLOWDOWN[key]:
                            volume_repeat_count = 0
                            change_volume_from_key(key, cdsp.volume.main_volume())

                    elif key in (KEY_BINDINGS['PREVIOUSSONG'], KEY_BINDINGS['NEXTSONG']):
                        key_hold_counter += 1
                        current_time = time.time()
                        if key_hold_counter >= HOLD_SEEK and current_time - last_seek_time >= SEEK_INTERVAL_S:
                            if key == KEY_BINDINGS['NEXTSONG']:
                                cmd = f"time +{SEEK_STEP_FORWARD_S}"
                            else:
                                cmd = f"time -{SEEK_STEP_BACKWARD_S}"
                            await asyncio.to_thread(send_lms_command, cmd)
                            last_seek_time = current_time

                    elif key == KEY_BINDINGS['PLAYPAUSE']:
                        key_hold_counter += 1
                        if key_hold_counter == HOLD_STOP:
                            await asyncio.to_thread(send_lms_command, "stop")

                    elif key == KEY_BINDINGS['POWER']:
                        key_hold_counter += 1
                        if ENABLE_AUTO_POWEROFF and key_hold_counter == HOLD_TOGGLE_POWER:
                            toggle_power()      # scroll_message handles the return to volume
                        elif key_hold_counter == HOLD_SHUTDOWN:
                            save_audio_settings(config_active)
                            shutdown_system()

                    elif key == KEY_BINDINGS['HOMEPAGE'] and toslink_active():
                        # Real-time measurement: independent of the remote's repeat rate
                        if (not home_toggled
                                and time.monotonic() - home_press_time >= HOLD_TOGGLE_AUTO_S):
                            home_toggled = True
                            toggle_toslink_auto()   # scroll_message handles the return to volume

                    elif key == KEY_BINDINGS['ENTER']:
                        key_hold_counter += 1
                        if key_hold_counter == HOLD_SWITCH_TONE_TILT and last_displayed in ("tone", "tilt"):
                            last_tone_tilt = "tilt" if last_displayed == "tone" else "tone"
                            (display_tilt_info if last_tone_tilt == "tilt" else display_tone_info)()

                elif attrib.keystate == 0:  # Key released

                    # HOMEPAGE short press (long press not triggered):
                    #   auto ON  -> scroll through the tone, tilt and loudness screens
                    #   auto OFF -> force the "|" group (active then cycle)
                    if toslink_active() and key == KEY_BINDINGS['HOMEPAGE'] and not home_toggled:
                        if toslink_auto_enabled:
                            show_tone_summary()
                        else:
                            await change_config(cdsp, CONFIG_DIR + '|*')

                    if ENABLE_DISPLAY and key == KEY_BINDINGS['ENTER'] and key_hold_counter < HOLD_SWITCH_TONE_TILT:
                        handle_enter_press(enter_display_at_press)

                    elif key == KEY_BINDINGS['PLAYPAUSE'] and key_hold_counter < HOLD_STOP:
                        await asyncio.to_thread(send_lms_command, "pause")

                    elif key in (KEY_BINDINGS['PREVIOUSSONG'], KEY_BINDINGS['NEXTSONG']) \
                            and key_hold_counter < HOLD_SEEK:
                        await asyncio.to_thread(
                            send_lms_command,
                            "playlist index -1" if key == KEY_BINDINGS['PREVIOUSSONG']
                            else "playlist index +1")

                    elif key in volume_keys:
                        volume_repeat_count = 0
                        schedule_volume_release()   # flag cleared + redraw after VOLUME_PEEK_S

                    key_hold_counter = 0
                    last_seek_time = 0

        except OSError as e:
            # Remote disconnected: return, remote_watcher will look for it again
            log.warning("Remote disconnected: %s", e)
            cancel_volume_release()
            is_volume_key_held = False      # no release event will ever come
            return

        except Exception:
            # An error while handling a key must not stop the remote (or the script)
            log.exception("Remote handler error, continuing")
            await asyncio.sleep(0.2)


# =====================================================================
#  STARTUP - module-level initialisation
#  Runs once, at import, in this order: serial, CamillaDSP (waits until it answers),
#  GPIO, display, settings. Do not reorder.
# =====================================================================

# Ambient light sensor: initial brightness level
ser = None
adc_value_init = 1500   # fallback if the sensor is absent (or the display is disabled)

if ENABLE_DISPLAY:
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        try:
            adc_value_init = int(ser.readline().decode().strip())
        except ValueError:
            pass
        ser.timeout = 0
    except serial.SerialException as e:
        log.warning("Ambient light sensor unavailable: %s", e)

last_level = next(
    (i for i, t in enumerate(ADC_LEVELS) if adc_value_init < t),
    len(BRIGHTNESS_LEVELS) - 1,
)

# CamillaDSP — always needed: the "at least one feature" check in the
# FEATURES block guarantees that at least one option requires it.
cdsp = CamillaClient(*CAMILLA_ADDR)
config_active = {}

# Wait for CamillaDSP instead of crashing if it is not up yet.
# bootlocal.sh launches us in the background, so this loop never
# blocks the boot. It exits as soon as CamillaDSP answers.
while True:
    try:
        cdsp.connect()
        cdsp.general.reload()
        break
    except Exception as e:
        log.warning("CamillaDSP not reachable (%s), waiting %g s",
                    e, CDSP_RECONNECT_DELAY_S)
        time.sleep(CDSP_RECONNECT_DELAY_S)

# Protection 1: never let config_active be None at startup
config_active = cdsp.config.active() or {}

# GPIO
h = None
if USE_GPIO:
    h = lgpio.gpiochip_open(0)
    lgpio.gpio_claim_output(h, POWER_GPIO, 0)   # Initial level LOW: no LOW -> HIGH glitch on the relay
    time.sleep(1)                   # Keep the relay off so the A70 can re-arm
    lgpio.gpio_write(h, POWER_GPIO, 1)

# TM1637 display
tm = TM1637(clk=CLK, dio=DIO) if ENABLE_DISPLAY else NullDisplay()
clear_display()
tm.brightness(BRIGHTNESS_LEVELS[last_level])
settings = load_settings()
last_tone_tilt = settings["last_tone_tilt"]
load_prev_values()
if TONE_ACTIVE:
    init_tone()


# =====================================================================
#  SHUTDOWN / MAIN
#  Signal handlers, then main(): the tasks are started according to the FEATURES switches.
# =====================================================================

def exit_gracefully(signum, frame):
    """Save the settings, cut the relay and release the GPIO on SIGINT/SIGTERM."""
    try:
        save_audio_settings(config_active)
        if ENABLE_AUTO_POWEROFF:
            lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
            time.sleep(0.5)
        if h is not None:
            lgpio.gpiochip_close(h)
        log.info("Gracefully shutting down the service...")
        sys.exit(0)
    except Exception as e:
        log.error("Error during cleanup: %s", e)
        sys.exit(1)


signal.signal(signal.SIGINT, exit_gracefully)
signal.signal(signal.SIGTERM, exit_gracefully)


async def main():
    # TOSLINK mode: nothing to do at startup, the config already in place is
    # kept (its prefix _ or | drives the very first iteration of
    # monitor_tv_toslink).
    if ENABLE_DISPLAY:
        display_volume_info()

    tasks = []

    def spawn(name, factory):
        tasks.append(asyncio.create_task(supervised(name, factory)))

    # Always: the periodic reader of volume / mute / config path / active config
    # (shared through vol_state) and the only place that reconnects to
    # CamillaDSP, which every feature needs
    spawn("cdsp-poller", cdsp_poller)
    if ENABLE_AUTO_POWEROFF or ENABLE_DISPLAY or ENABLE_TOSLINK_SWITCH:
        # Single reader of the CamillaDSP levels, shared through `silent_now` / `silent_since`
        spawn("silence", silence_monitor)
    if ENABLE_AUTO_POWEROFF:
        spawn("auto-poweroff", auto_poweroff)
    if ENABLE_DISPLAY:
        spawn("display", display_manager_loop)
        spawn("adc", adc_reader_loop)
    if ENABLE_DISPLAY:
        # 1 s tick that redraws the volume screen while a dot must blink
        spawn("blink", blink_loop)
    if ENABLE_DISPLAY or ENABLE_REMOTE or ENABLE_TOSLINK_SWITCH:
        # Redraws the display and resyncs config_active after an external change
        spawn("volume-watcher", volume_watcher_loop)
    if ENABLE_LMS_SYNC:
        spawn("lms-bridge", lms_bridge_task)
    if ENABLE_TOSLINK_SWITCH:
        spawn("tv-toslink", monitor_tv_toslink)

    if ENABLE_REMOTE:
        spawn("remote", remote_watcher)

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
