# Lybrica V2.0
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
                               # (the tone / loudness keys need it)
ENABLE_TOSLINK_SWITCH = True   # Auto config switching on the TOSLINK input.
                               # True : automatism ON  -> BACK cycles within the active
                               #                          group (any "_X-");
                               #                          HOMEPAGE short press = nothing.
                               #        automatism OFF -> BACK cycles within the active group;
                               #                          HOMEPAGE short press = cycle groups.
                               #        HOMEPAGE long press (1 s) enables / disables
                               #        the automatism (any state).
                               # False: BACK cycles within the active group,
                               #        HOMEPAGE cycles groups.
                               # The automatism itself only knows groups A and B.
                               # If either has no config file, the automatism cannot run:
                               # the keys then behave as with False.
ENABLE_TV_RELAY       = True   # Relay on TV_RELAY_GPIO: active (input pulled to ground) as long
                               # as a "_B-*" config is active, inactive (high impedance)
                               # otherwise. Independent of the POWER relay.


# At least one feature must be enabled, otherwise the script has nothing to do
if not (ENABLE_DISPLAY or ENABLE_REMOTE or ENABLE_AUTO_POWEROFF
        or ENABLE_TOSLINK_SWITCH or ENABLE_LMS_SYNC or ENABLE_TV_RELAY):
    raise ValueError("No feature enabled: turn on at least one option in the FEATURES block")

TONE_ACTIVE = ENABLE_REMOTE or ENABLE_TOSLINK_SWITCH   # tone is saved / restored per config
USE_GPIO    = ENABLE_AUTO_POWEROFF or ENABLE_TV_RELAY  # TOSLINK detection needs no GPIO


# ---------------------------------------------------------------------
#  CONDITIONAL IMPORTS - DO NOT EDIT
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


# ---------------------------------------------------------------------
#  LOGGING & ENVIRONMENT
# ---------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("remote")

# PiCorePlayer persistent partition: also tells us whether we run on pCP
PCP_DIR = "/mnt/mmcblk0p2/tce"


# =====================================================================
#  SETTINGS - EDITABLE
# =====================================================================

# ---------------------- CamillaDSP ----------------------
CAMILLA_ADDR = ("127.0.0.1", 1234)  # CamillaDSP on this Pi (must stay local: loopback audio)

# One shared reader (cdsp_poller) publishes into `vol_state`. The volume watcher
# (display / resync) and the LMS bridge subscribe to it; the TOSLINK monitor
# reads vol_state.config_path by polling.
POLL_INTERVAL_S = 0.25      # One read cycle of CamillaDSP (volume + mute)
PATH_EVERY = 2              # The config file path is read every N cycles (2 -> every 0.5 s)
CDSP_RECONNECT_DELAY_S = 2.0    # Seconds to wait before trying to reconnect to CamillaDSP after an error


# ---------------------- Logitech Media Server ----------------------
# LMS CLI, same Pi, port 9090 (not the web UI 9000, not SLIM 3483).
# Used by the volume bridge and by send_lms_command().
LMS_ADDR = ("127.0.0.1", 9090)

# LMS player used by the volume bridge AND by every LMS command. "" = auto
# (MAC of the Pi's default network interface, as Squeezelite announces it).
# Set manually if auto-detect picks the wrong interface or LMS uses a
# different MAC. Format: raw lowercase MAC.
LMS_PLAYER_MANUAL = ""

LMS_SYNC_DEBUG = False           # Log every volume/mute change and the raw LMS notifications


# ---------------------- LMS volume-sync bridge ----------------------
LMS_RESTORE_WINDOW_S = 0.5   # after an unmute, LMS may briefly announce a wrong volume
                             # the bridge corrects it during this window
LMS_CONNECT_TIMEOUT_S = 5    # TCP connect timeout to the LMS CLI
LMS_RETRY_S = 5              # delay before the bridge restarts after an error


# ---------------------- Power ----------------------

POWER_GPIO = 4              # GPIO controlling the power relay
POWER_OFF_DELAY = 2         # Minutes without sound AND without key press before the power relay is switched off
                            # (needs ENABLE_AUTO_POWEROFF); also the delay before the display is blanked
                            # (needs ENABLE_DISPLAY). A key press restarts the countdown.
HALT_DELAY = 48             # Hours of continuous silence before the Raspberry Pi shuts down

TV_RELAY_GPIO = 12          # Optocoupler board input of the "_B-" group relay
                            # LOW (ground) = relay active, high impedance = inactive.
                            # To avoid a pulse while the Pi boots, add "gpio=<TV_RELAY_GPIO>=ip,np"
                            # to config.txt.


# ---------------------- Audio analysis ----------------------
# is_silent() feeds `silent_now`, used by the power relay (auto power-off and
# HALT_DELAY), by the display blanking and by the TOSLINK -> loopback return.
# It declares silence when the RMS level is below SILENCE_RMS_DB, or when the
# signal is a steady noise floor (crest below CREST_MIN_DB).
# CREST_MIN_DB is also used by the TOSLINK probe, which has its own level
# threshold (TOSLINK_ON_DB).

SILENCE_RMS_DB = -80.0   # RMS below this -> silence
CREST_MIN_DB   = 2.0     # peak - rms below this -> steady noise, not audio


# ---------------------- TOSLINK auto switching ----------------------
# Automatic switch between group A (streamer, files matching "_A-*") and
# group B (TV, files matching "_B-*"), based on what is actually playing.
# While the automatism is ON, only these two groups can be active: a manual
# group ("_C-*", "_D-*", ...) is immediately replaced by A or B (see the
# block comment above TOSLINK_FORMATS).
#
#   group A active:
#       TOSLINK audio detected -> switch to B
#   group B active:
#       LMS playing + TOSLINK silent -> switch back to A
#       LMS playing while the TV plays   -> LMS is paused
#
# The automatism can be disabled / enabled at run time with a long press
# on HOMEPAGE. Enabled at every start.
#
# Capture params come from the first "_B-*" file: all files in that group must
# share the same capture input.

TOSLINK_ON_DB = -60.0   # above which audio is considered present (dBFS RMS).
                        # Keep it ABOVE SILENCE_RMS_DB, otherwise a signal could be detected
                        # as TV and then count as silence.
TOSLINK_DEBUG = False   # Log every TOSLINK probe measurement, to calibrate TOSLINK_ON_DB


# ---------------------- Display ----------------------

CLK, DIO = 23, 24           # TM1637 display pins
SCREEN_RETURN_DELAY = 30    # Seconds without key press before returning to the volume screen
WAKE_DISPLAY_DELAY = 30     # Seconds the display stays on after a key press that woke it while the relay is
                            # already off. Independent of POWER_OFF_DELAY.
SCROLL_FRAME_DELAY = 0.35   # Seconds per step of a scrolling message
SCROLL_END_PAUSE = 0.6      # Seconds the last frame of a scrolling message stays visible
VOLUME_PEEK_S = 0.5         # Seconds the volume level stays shown after a volume key is released while muted

# Ambient light sensor (ADC) -> display brightness
SERIAL_PORT = "/dev/serial0"
BAUD_RATE = 115200
ADC_LEVELS = [80, 1000, 2500, 3500]
BRIGHTNESS_LEVELS = [0, 1, 2, 3, 7]
HYSTERESIS_RATIO = 0.03     # 3 % hysteresis between two brightness levels


# ---------------------- Remote control ----------------------

# Name of the remote on Debian. On PiCorePlayer the name is fixed
# and the Debian value is ignored.
REMOTE_NAME = "HID Remote01 Keyboard"
REMOTE_NAME_PCP = "pCP BLE HID Keyboard"
if os.path.isdir(PCP_DIR):
    REMOTE_NAME = REMOTE_NAME_PCP

KEY_BINDINGS = {
    'VOLUMEDOWN':    'KEY_VOLUMEDOWN',
    'VOLUMEUP':      'KEY_VOLUMEUP',
    'MUTE':          'KEY_MUTE',
    'PLAYPAUSE':     'KEY_PLAYPAUSE',    # Pause (short) / stop (long) - LMS
    'PREVIOUSSONG':  'KEY_PREVIOUSSONG', # Previous (short) / rewind (long) - LMS
    'NEXTSONG':      'KEY_NEXTSONG',     # Next (short) / fast-forward (long) - LMS
    'UP':            'KEY_UP',           # Tone: BT Bass +1 / TL B- T+ (step depends on gains)
                                         # tone_select: toggle BT / TL. loudness: +
    'DOWN':          'KEY_DOWN',         # Tone: BT Bass -1 / TL B+ T- (step depends on gains)
                                         # tone_select: toggle def / alt. loudness: -
    'LEFT':          'KEY_LEFT',         # Tone: BT Treble -1 / TL B- T- (step depends on gains)
                                         # tone_select: exit
    'RIGHT':         'KEY_RIGHT',        # Tone: BT Treble +1 / TL B+ T+ (step depends on gains)
                                         # tone_select: exit
    'POWER':         'KEY_POWER',        # Long: toggle relay (needs ENABLE_AUTO_POWEROFF)
                                         # Very long: shutdown (always)
    'ENTER':         'KEY_ENTER',        # Short: tone reset/restore. Long: volume->Loudness,
                                         # tone->tone_select, loudness->volume,
                                         # tone_select->volume.
    'BACK':          'KEY_BACK',         # Cycle within the active group, whatever the TOSLINK mode
    'HOMEPAGE':      'KEY_HOMEPAGE',     # TOSLINK ON : long = automatism on/off
                                         #              short, auto ON : nothing
                                         #              short, auto OFF: cycle groups
                                         # TOSLINK OFF: cycle groups
}

# Hold durations, in key-repeat events. The remote repeats every 40 ms, but
# the first repeat only comes after the remote's own initial delay, so the
# times in the comments below are approximate (repeat events x 40 ms).
HOLD_TOGGLE_POWER = 10      # POWER: toggle the relay ~ 0.4 s of repeats
HOLD_SHUTDOWN = 200         # POWER: shut the Raspberry Pi down ~ 8 s
HOLD_STOP = 10              # PLAY/PAUSE: stop
HOLD_SEEK = 10              # NEXT/PREVIOUS: seeking starts
HOLD_SHOW_LOUDNESS = 15     # ENTER long threshold
SEEK_STEP_FORWARD_S = 30    # Seconds jumped at each seek (NEXTSONG)
SEEK_STEP_BACKWARD_S = 10   # Seconds jumped at each seek (PREVIOUSSONG)
SEEK_INTERVAL_S = 0.8       # Seconds between two seeks while the key is held
ARROW_REPEAT_EVERY = 5      # Arrow keys: one action every: value * 40 ms

HOLD_TOGGLE_AUTO_S = 1.0    # HOMEPAGE long press to enable/disable the TOSLINK automatism

# VOLUME+ / VOLUME- held: apply a volume step only on every Nth repeat.
# 1 = every repeat (fastest), 2 = every other repeat, etc. The remote
# repeats every 40 ms, so the real time to go from 0 to 100 is roughly
# N * 4 seconds.
VOLUME_SLOWDOWN = {
    KEY_BINDINGS['VOLUMEUP']:   2,
    KEY_BINDINGS['VOLUMEDOWN']: 1,
}
VOLUME_KEYS = (KEY_BINDINGS['VOLUMEDOWN'], KEY_BINDINGS['VOLUMEUP'])
ARROW_KEYS = (KEY_BINDINGS['UP'], KEY_BINDINGS['DOWN'],
              KEY_BINDINGS['RIGHT'], KEY_BINDINGS['LEFT'])


# ---------------------- Tone and loudness ----------------------
# SAVE_MODE decides how the tone (bass, treble) and the loudness
# are shared between configs when you switch config:
#
#   all_config       : each config keeps its own tone + loudness.
#   all_group        : all configs of the same group (any "_X-") share the same
#                      tone + loudness. Changing config within the same group copies
#                      the current values to the new config.
#   half             : tone per config, loudness per group. Changing config within
#                      the same group keeps the tone of the new config but copies the
#                      current loudness to the new config.
#   same_everywhere  : all configs share the same tone + loudness, regardless of the
#                      group. Changing config copies the current values to the new config.
#
# The copy is written into settings["configs"][new_config_key] before the config is
# applied, so apply_saved_values uses the copied values (if CamillaDSP rejects the
# new config, the previous entry is restored). The values survive restarts:
# they are saved per config in settings.json at every config switch and at script exit.
SAVE_MODE = "half"

if SAVE_MODE not in ("all_config", "all_group", "half", "same_everywhere"):
    raise ValueError(f"Unknown SAVE_MODE {SAVE_MODE!r}")

LOUDNESS_MIN, LOUDNESS_MAX = -50, -10   # dB
LOUDNESS_STEP = 5           # dB per key press


# ---------------------- Tone DSP profile ----------------------
# The user enters Bass and Treble directly with the arrow keys. The DSP
# parameters (frequencies, Q per filter, compensation) are derived from
# those two gains by blending between two anchors:
#
#   Tilt anchor (t=0): on the N/S axis (bass = -treble), the two filters
#     form a perfect see-saw with equal Q=0.35 and a geometric pivot at
#     620 Hz. Used when the two gains are opposite.
#
#   Tone anchor (t=1): on the diagonals and beyond (one gain dominates
#     the other), the filters become classical Baxandall shelves with
#     fixed Q. The exact frequencies and Q come from the active preset
#     in TONE_PRESETS below.
#
# The blending factor t is the angular distance to N/S, normalised:
#   t = 0 on N/S (bass = -treble)
#   t = 1 from 45° away from N/S (diagonals, E/W)
# Frequencies and Q are interpolated logarithmically between anchors.

TILT_BASS_FREQ   = 110.0
TILT_TREBLE_FREQ = 3500.0
TILT_NS_Q        = 0.35     # Q on both filters at t=0

# Tone anchor presets, selectable at runtime from the tone preset select
# screen (long ENTER press from the tone screen, then DOWN to toggle def / alt;
# UP toggles the BT / TL keys mode on the same screen).
#
#   Preset 0 ("def"): Baxandall-like shelves (85 Hz / 6500 Hz, Q 0.9 / 0.7).
#                     Frequencies and Q both morph along the path.
#   Preset 1 ("alt"): frequencies equal to the tilt anchor (110 Hz / 3500 Hz).
#                     Only Q morphs along the path (0.35 -> 0.7 on both filters).
TONE_PRESETS = [
    {   # preset 0
        "bass_freq":   85.0,
        "treble_freq": 6500.0,
        "bass_q":      0.9,
        "treble_q":    0.7,
    },
    {   # preset 1
        "bass_freq":   110.0,
        "treble_freq": 3500.0,
        "bass_q":      0.7,
        "treble_q":    0.7,
    },
]

# Runtime: which preset is active. Changed from the tone preset select
# screen (DOWN key), persisted in settings.json so that a restart resumes on
# the last preset used. Default 0 at first launch.
tone_preset_idx = 0

# How the arrow keys act on the tone screen, selectable at runtime from the
# same screen (UP key), persisted in settings.json. Default 0 at first launch.
#
#   0 "BT" (Baxandall): UP / DOWN = Bass +1 / -1, RIGHT / LEFT = Treble +1 / -1.
#   1 "TL" (Tilt)     : fixed direction per key, amplitude depending on the
#                       current gains (see TL_STEPS):
#                       UP = B- T+, DOWN = B+ T-, RIGHT = B+ T+, LEFT = B- T-.
#                       The gain already "past the target" of the key moves by
#                       0.5 or 0 instead of 1, so the gains are multiples of 0.5.
# In both modes the stored values are the two gains: only the keys differ.
TONE_KEY_MODES = ("BT", "TL")
tone_mode_idx = 0


# ---------------------- Tone gain compensation ----------------------
# The compensation gain keeps the perceived loudness roughly constant when
# the user boosts or cuts both filters. The model is symmetric in the two
# gains:
#   - At N/S (bass = -treble): the two filters form a tilt. The energy is
#     redistributed, not added or removed; the perceived level is unchanged;
#     no compensation is needed (comp = 0).
#   - At E/W (bass = treble): the two filters add up to a global boost or
#     cut. The perceived level changes by the same amount; full compensation
#     is applied (comp = -0.25 * 2 * ±6 = ∓3).
#   - In between: linear interpolation, proportional to (bass + treble).
#
# Names of the filters used by the tone / loudness features.
# They must match the filter names in the YAML pipeline.

BASS_FILTER = "L_Bass"
TREBLE_FILTER = "L_Treble"
COMP_FILTER = "L_Compensation"
LOUDNESS_FILTER = "L_Loudness"

# ---------------------------------------------------------------------
#  INTERNAL - DO NOT EDIT
# ---------------------------------------------------------------------

# Bass / Treble range, common to both filters.
# Keep it ≤ 9.0 to preserve the 6-char display layout
# (a negative gain below -9 would need 3 characters).
TONE_MAX_DB = 6.0

TASK_RESTART_DELAY_S = 2.0  # Seconds before a background task that crashed is restarted
TONE_PUBLISH_MAX_POLLS = 4  # A tone / loudness publish is dropped if the config read from
                            # CamillaDSP did not change within this many reads (about 2 s)

# CamillaDSP configs: PiCorePlayer's persistent partition (PCP_DIR), or ~ elsewhere.
BASE_DIR   = PCP_DIR if os.path.isdir(PCP_DIR) else os.path.expanduser("~")
CONFIG_DIR = BASE_DIR + "/camilladsp/configs/"

# settings.json must survive a reboot. On piCorePlayer the script runs from
# /usr/local/bin (RAM, rebuilt at every boot), so it goes next to the CamillaDSP
# configs, on the persistent partition. Elsewhere it stays next to the script.
if os.path.isdir(PCP_DIR):
    SETTINGS_PATH = BASE_DIR + "/camilladsp/settings.json"
else:
    SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

# Config group prefix: "_X-", where X is a single uppercase letter.
# "_A-" and "_B-" are used by the TOSLINK automatism; a user can define
# "_C-", "_D-", etc. and reach them in manual mode. A file whose name does
# not match falls back to group A.
_GROUP_RE = re.compile(r"^_([A-Z])-")

# Saved PER CONFIG, and driven by the tone screen and the ENTER key.
# settings.json key -> (CamillaDSP filter, parameter)
TONE_PARAMS = {
    "bass_gain":     (BASS_FILTER, "gain"),
    "treble_gain":   (TREBLE_FILTER, "gain"),
}

# The loudness filter (handled separately from the tone gains)
LOUDNESS_FILTER = (LOUDNESS_FILTER, "reference_level")

# Arrow keys on the tone screen, BT mode only (see TONE_KEY_MODES):
# key -> (bass step, treble step). The TL mode is computed by tl_steps()
# below, because its steps depend on the current gains.
TONE_ARROWS = {
    "BT": {
        KEY_BINDINGS['UP']:    ( 1,  0),
        KEY_BINDINGS['DOWN']:  (-1,  0),
        KEY_BINDINGS['RIGHT']: ( 0,  1),
        KEY_BINDINGS['LEFT']:  ( 0, -1),
    },
}

# TL mode: fixed direction per key. The gain whose step would move X away
# from 0 is slowed down according to |X|:
#   X == 0        : full steps on both gains
#   0 < |X| <= 1  : that gain moves by 0.5
#   |X| > 1       : that gain does not move
# Cells: (bass step, treble step).
#   UP    (B- T+) uses X = S = B + T
#   DOWN  (B+ T-) uses X = S = B + T
#   RIGHT (B+ T+) uses X = D = B - T
#   LEFT  (B- T-) uses X = D = B - T
TL_STEPS = {
    KEY_BINDINGS['UP']: {
        "zero": (-1.0,  1.0),
        "pos_low": (-1.0,  0.5), "pos_high": (-1.0,  0.0),
        "neg_low": (-0.5,  1.0), "neg_high": ( 0.0,  1.0)},
    KEY_BINDINGS['DOWN']: {
        "zero": ( 1.0, -1.0),
        "pos_low": ( 0.5, -1.0), "pos_high": ( 0.0, -1.0),
        "neg_low": ( 1.0, -0.5), "neg_high": ( 1.0,  0.0)},
    KEY_BINDINGS['RIGHT']: {
        "zero": ( 1.0,  1.0),
        "pos_low": ( 0.5,  1.0), "pos_high": ( 0.0,  1.0),
        "neg_low": ( 1.0,  0.5), "neg_high": ( 1.0,  0.0)},
    KEY_BINDINGS['LEFT']: {
        "zero": (-1.0, -1.0),
        "pos_low": (-1.0, -0.5), "pos_high": (-1.0,  0.0),
        "neg_low": (-0.5, -1.0), "neg_high": ( 0.0, -1.0)},
}


def tl_steps(key, bass, treble):
    """(bass step, treble step) of an arrow key in TL mode, from the current gains."""
    if key in (KEY_BINDINGS['UP'], KEY_BINDINGS['DOWN']):
        x = bass + treble          # S
    else:
        x = bass - treble          # D
    if abs(x) < 1e-9:
        case = "zero"
    else:
        case = ("pos" if x > 0 else "neg") + ("_low" if abs(x) <= 1.0 + 1e-9 else "_high")
    return TL_STEPS[key][case]


# =====================================================================
#  CORE - shared by every feature
# =====================================================================


def clamp(value, low, high):
    return max(low, min(high, value))


# ---------- Volume curve (level 0..100 <-> dB) ----------
# Based on the gain sent by LMS to Squeezelite (dB = 20 * log10(gain / 65536)).
# 0.5 dB per level from level 25 up (a 0.5 dB grid), 1.5 dB per level below.
# Strictly increasing, so the inverse is exact.
def pct_to_db(p):
    p = clamp(p, 0, 100)
    if p >= 25:
        return (p - 100) * 0.5          # 0.5 dB / level, up to 0 dB
    return -74.0 + p * 1.5              # 1.5 dB / level, -74 .. -38 dB

def db_to_pct(d):
    if d >= -37.5:
        return clamp(round(100 + d * 2), 25, 100)
    if d >= -74.0:
        return clamp(round((d + 74.0) / 1.5), 0, 24)
    return 0


# ---------- Shared CamillaDSP state ----------
class VolumeState:
    """Last CamillaDSP reading, published by a single reader (cdsp_poller).

    `stamp` is the time at which the reading STARTED, used to discard stale
    readings. `config_active` is the full content of cdsp.config.active():
    it is the change-detection source (INTERNAL or EXTERNAL), because it
    reflects what CamillaDSP actually plays, not what file_path() stores.
    `config_data` is published by change_config (config switch) and by
    _push_filters (tone / loudness change) for an INTERNAL change, so the
    watcher does not have to re-read active() while CamillaDSP is still
    applying it. A config switch publish never expires (a switch always ends
    up changing the config, however long CamillaDSP takes to load it). A
    tone / loudness publish expires after `max_polls` reads of active(): one
    that does not end up changing the config (arrow at its limit...) must not
    hide a later EXTERNAL change. Reads are counted, not timed, so holding a
    volume key (which pauses the reads) or a slow reload cannot expire it."""

    def __init__(self):
        self.db = None
        self.muted = None
        self.config_path = None
        self.config_data = None
        self.config_data_polls = 0         # reads of active() since config_data was published
        self.config_data_max_polls = None  # None = never expires
        self.config_active = None
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

    def config_data_expired(self):
        return (self.config_data_max_polls is not None
                and self.config_data_polls > self.config_data_max_polls)

    def take_config_data(self):
        """Return the published config_data if it has not expired, and clear it."""
        expired = self.config_data_expired()
        data, self.config_data = self.config_data, None
        return None if expired else data

    def publish(self, db, muted, config_path=None, config_data=None,
                config_active=None, stamp=None, config_data_max_polls=None):
        self.db = db
        self.muted = bool(muted)
        if config_path:                # empty path (during a reload) is ignored
            self.config_path = config_path
        if config_active is not None:  # overwritten every poll
            self.config_active = config_active
            self.config_data_polls += 1
        if config_data is not None:    # consumed by the watcher
            self.config_data = config_data
            self.config_data_polls = 0
            self.config_data_max_polls = config_data_max_polls
        self.stamp = time.monotonic() if stamp is None else stamp
        for ev in self._events:
            ev.set()


vol_state = VolumeState()


# ---------- Activity and silence clocks ----------
# All inactivity delays are computed from timestamps, never from counters
# incremented by a loop tick: the loops do not run at an exact rate, and a
# counter reset from another task is easy to forget.

silent_now = False
silent_since = None
last_activity = time.monotonic()


def touch_activity():
    global last_activity
    last_activity = time.monotonic()


def idle_age():
    return time.monotonic() - last_activity


def silence_age():
    if not silent_now or silent_since is None:
        return 0.0
    return time.monotonic() - silent_since


def quiet_age():
    if not silent_now:
        return 0.0
    return min(silence_age(), idle_age())


def is_silent():
    """RMS threshold, then crest factor (peak vs RMS). A steady noise floor
    has peak ≈ RMS; real audio does not."""
    rms = cdsp.levels.capture_rms()
    peak = cdsp.levels.capture_peak()
    rms_vals = list(rms.values()) if isinstance(rms, dict) else list(rms)
    peak_vals = list(peak.values()) if isinstance(peak, dict) else list(peak)
    if not rms_vals or not peak_vals:
        return True

    rms_db = max(float(v) for v in rms_vals)
    if rms_db <= SILENCE_RMS_DB:
        return True

    peak_db = max(float(v) for v in peak_vals)
    return (peak_db - rms_db) < CREST_MIN_DB


async def silence_monitor():
    """Single reader of the CamillaDSP levels. On a read error the previous
    value is kept (no power cut because of a glitch)."""
    global silent_now, silent_since
    while True:
        try:
            now_silent = is_silent()
            if now_silent and not silent_now:
                silent_since = time.monotonic()
            elif not now_silent:
                silent_since = None
            silent_now = now_silent
        except Exception as e:
            log.warning("Silence monitor: cannot read levels (%s)", e)
        await asyncio.sleep(1)


# ---------- Filters and settings.json ----------
prev_values = {}                # ENTER restore values (the two tone gains)
current_config_key = None
current_config_external = False # True when the config was chosen OUTSIDE this script
                                # (GUI, another client): its tone/loudness are neither
                                # saved nor copied to the next config


# ---------- Filter helpers ----------
def active_filter_names(config):
    """Names of the filters really used by the pipeline. A filter defined in
    `filters:` but not listed in an (enabled) Filter step has no effect."""
    names = set()
    for step in config.get("pipeline") or []:
        if isinstance(step, dict) and step.get("type") == "Filter" and not step.get("bypassed"):
            step_names = step.get("names")
            if isinstance(step_names, list):
                names.update(n for n in step_names if isinstance(n, str))
    return names


def tone_filters_ready(config, active=None):
    """True if Bass, Treble AND Compensation are all defined AND used by the
    pipeline. Any missing or bypassed filter returns False: the tone screen
    then shows 'B----T' and no tone operation is performed. This single
    check is the only guard needed: everything downstream can then access
    the filters directly. `active` (active_filter_names(config)) can be
    passed to avoid a second pipeline traversal."""
    if active is None:
        active = active_filter_names(config)
    filters = config.get("filters") or {}
    return all(
        filt in active and isinstance((filters.get(filt) or {}).get("parameters"), dict)
        for filt in (BASS_FILTER, TREBLE_FILTER, COMP_FILTER)
    )


def tone_param(config, filt, name, active=None):
    """Read one parameter of a filter, or None (absent or not used by the
    pipeline). Used together with tone_filters_ready() for Bass/Treble;
    standalone for Loudness."""
    if active is None:
        active = active_filter_names(config)
    if filt not in active:
        return None
    params = (config.get("filters") or {}).get(filt, {}).get("parameters")
    return params.get(name) if isinstance(params, dict) else None


def get_all_tone_params(config):
    """Return the two tone gains and the loudness reference level in one call.
    bass and treble are both None unless ALL THREE tone filters are present
    AND active. Single pipeline traversal (one active_filter_names call)."""
    active = active_filter_names(config)
    ready = tone_filters_ready(config, active)
    return {
        "bass":     tone_param(config, BASS_FILTER,   "gain", active) if ready else None,
        "treble":   tone_param(config, TREBLE_FILTER, "gain", active) if ready else None,
        "loudness": tone_param(config, *LOUDNESS_FILTER, active),
    }


def config_key(path):
    return os.path.splitext(os.path.basename(path))[0]


def get_tone(config):
    tone = {}
    for key, (filt, param) in TONE_PARAMS.items():
        value = tone_param(config, filt, param)
        if value is not None:
            tone[key] = value
    return tone


def apply_tone(filters, tone):
    """Apply a saved tone dict. Filters missing from the config are ignored."""
    for key, (filt, param) in TONE_PARAMS.items():
        if key in tone:
            f = filters.get(filt)
            if isinstance(f, dict) and isinstance(f.get("parameters"), dict):
                f["parameters"][param] = tone[key]


# ---------- Tone DSP profile ----------
def interp_log(v0, v1, t):
    """Geometric interpolation: the ear perceives ratios, not differences."""
    return v0 * (v1 / v0) ** t


def compute_dsp_params(bass, treble):
    """Blend between the tilt and tone anchors based on the direction of
    (bass, treble) relative to the N/S axis.
      t = 0 : on N/S (bass = -treble) -> tilt anchor
      t = 1 : 45° or more from N/S     -> tone anchor
    Logarithmic interpolation for both frequencies and Q.
    With bass == treble == 0, atan2(0, 0) gives t = 1 (inaudible anyway)."""
    angle = math.degrees(math.atan2(treble, bass)) % 360
    d = abs((angle - 135.0) % 180.0)
    if d > 90.0:
        d = 180.0 - d
    t = min(1.0, d / 45.0)

    preset = TONE_PRESETS[tone_preset_idx % len(TONE_PRESETS)]

    bass_freq   = interp_log(TILT_BASS_FREQ,   preset["bass_freq"],   t)
    treble_freq = interp_log(TILT_TREBLE_FREQ, preset["treble_freq"], t)
    bass_q      = interp_log(TILT_NS_Q,        preset["bass_q"],      t)
    treble_q    = interp_log(TILT_NS_Q,        preset["treble_q"],    t)

    return (round(bass_freq, 1), round(bass_q, 2),
            round(treble_freq, 1), round(treble_q, 2))


def compute_comp(bass, treble):
    """comp = -0.25*(bass + treble). Zero on N/S (tilt preserves loudness);
    full at E/W (filters add up). The clamp (= TONE_MAX_DB / 2, i.e. ±3 dB)
    is only a safety net for gains outside the ±TONE_MAX_DB range, for
    instance coming from a config file."""
    limit = TONE_MAX_DB / 2
    return round(clamp(-0.25 * (bass + treble), -limit, limit), 1) + 0.0   # + 0.0: no "-0.0"


def apply_tone_to_config(config):
    """Apply the tone profile in place. No-op unless Bass, Treble AND
    Compensation are all present AND active (see tone_filters_ready)."""
    if not tone_filters_ready(config):
        return

    filters = config["filters"]
    bass   = filters[BASS_FILTER]["parameters"].get("gain", 0.0) or 0.0
    treble = filters[TREBLE_FILTER]["parameters"].get("gain", 0.0) or 0.0

    bass_freq, bass_q, treble_freq, treble_q = compute_dsp_params(bass, treble)
    comp = compute_comp(bass, treble)

    filters[BASS_FILTER]["parameters"]["freq"]      = bass_freq
    filters[BASS_FILTER]["parameters"]["q"]         = bass_q
    filters[TREBLE_FILTER]["parameters"]["freq"]    = treble_freq
    filters[TREBLE_FILTER]["parameters"]["q"]       = treble_q
    filters[COMP_FILTER]["parameters"]["gain"] = comp


TONE_FILTERS = (BASS_FILTER, TREBLE_FILTER, COMP_FILTER)


def _push_filters(filter_names):
    """Send only the requested filters via PatchConfig (each filter is sent
    WHOLE: PatchConfig replaces the leaf object)."""
    filters = config_active.get("filters") or {}
    patch_filters = {name: filters[name] for name in filter_names if name in filters}
    if not patch_filters:
        return
    cdsp.config.patch({"filters": patch_filters})
    vol_state.publish(vol_state.db, vol_state.muted,
                      config_data=config_active,
                      config_data_max_polls=TONE_PUBLISH_MAX_POLLS)


def apply_tone_processing():
    """Push ONLY the affected filters to CamillaDSP (no full reload).
    No-op unless Bass, Treble AND Compensation are all present AND active."""
    if not tone_filters_ready(config_active):
        return
    apply_tone_to_config(config_active)
    _push_filters(TONE_FILTERS)


def toggle_tone_preset():
    """Toggle the tone preset (def <-> alt) and re-display the select screen.
    The DSP is applied only if the three tone filters are present AND active."""
    global tone_preset_idx
    tone_preset_idx = (tone_preset_idx + 1) % len(TONE_PRESETS)
    preset = TONE_PRESETS[tone_preset_idx]
    log.info("[TONE] Preset %s: bass %g Hz Q %g, treble %g Hz Q %g",
             "DEF" if tone_preset_idx == 0 else "ALT",
             preset["bass_freq"], preset["bass_q"],
             preset["treble_freq"], preset["treble_q"])
    apply_tone_processing()
    settings["tone_preset_idx"] = tone_preset_idx
    write_settings()
    display_tone_preset_select()


def toggle_tone_mode():
    """Toggle the arrow keys mode of the tone screen (BT <-> TL) and
    re-display the select screen. Only the keys change, not the DSP."""
    global tone_mode_idx
    tone_mode_idx = (tone_mode_idx + 1) % len(TONE_KEY_MODES)
    log.info("[TONE] Keys mode: %s", TONE_KEY_MODES[tone_mode_idx])
    settings["tone_mode_idx"] = tone_mode_idx
    write_settings()
    display_tone_preset_select()


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
    preset_idx = data.get("tone_preset_idx", 0)
    if not isinstance(preset_idx, int) or not (0 <= preset_idx < len(TONE_PRESETS)):
        preset_idx = 0
    mode_idx = data.get("tone_mode_idx", 0)
    if not isinstance(mode_idx, int) or not (0 <= mode_idx < len(TONE_KEY_MODES)):
        mode_idx = 0
    return {
        "configs": configs if isinstance(configs, dict) else {},
        "last_config": last_config if isinstance(last_config, dict) else {},
        "tone_preset_idx": preset_idx,
        "tone_mode_idx": mode_idx,
    }


def write_settings():
    """Atomic write (no half-written file on power loss)."""
    tmp_path = SETTINGS_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(settings, f, indent=4, ensure_ascii=False)
    os.replace(tmp_path, SETTINGS_PATH)


def save_audio_settings(config, write=True):
    """Store tone + loudness per config, then write settings.json (unless
    write=False: the caller writes it itself right after). How the values are
    shared between configs is decided by SAVE_MODE in change_config.
    A config chosen OUTSIDE this script is not stored."""
    if not TONE_ACTIVE:
        return
    if (current_config_key and not current_config_external
            and is_complete_config(config)):
        entry = get_tone(config)
        entry["prev"] = {key: prev_values[key] for key in TONE_PARAMS}
        ref = tone_param(config, *LOUDNESS_FILTER)
        if ref is not None:
            entry["loudness_ref"] = ref
        settings["configs"][current_config_key] = entry
    if write:
        write_settings()


def load_prev_values(saved=None):
    """Load the ENTER restore values (the two tone gains) from the saved
    entry of a config (defaults if none)."""
    saved_prev = saved.get("prev") if saved else None
    if not isinstance(saved_prev, dict):
        saved_prev = {}
    for key in TONE_PARAMS:
        prev_values[key] = saved_prev.get(key, 0)


def apply_saved_values(filters, saved):
    """Apply the saved tone + loudness of a config to a `filters` dict.
    Filters missing from the config are ignored (Loudness is optional)."""
    if saved:
        apply_tone(filters, saved)
        if "loudness_ref" in saved:
            f = filters.get(LOUDNESS_FILTER[0])
            if isinstance(f, dict) and isinstance(f.get("parameters"), dict):
                f["parameters"][LOUDNESS_FILTER[1]] = saved["loudness_ref"]


def is_complete_config(cfg):
    """A config without a non-empty 'devices' section is rejected by CamillaDSP."""
    return (isinstance(cfg, dict)
            and isinstance(cfg.get("devices"), dict)
            and bool(cfg["devices"]))


def init_tone():
    """Apply the saved tone/loudness of the config CamillaDSP started with,
    then push the tone filters (apply_tone_processing, which is a no-op
    unless Bass, Treble AND Compensation are all present and active). Also
    applies the DSP profile so the filter params match the code."""
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
        apply_tone_processing()


# ---------- CamillaDSP connection, polling and task supervision ----------
def reconnect_cdsp():
    """Reopen the SAME `cdsp` object, so every existing call keeps working."""
    try:
        cdsp.disconnect()
    except Exception:
        pass
    cdsp.connect()


def resync_after_reconnect():
    """After a CamillaDSP restart: re-read the active config, re-apply the
    saved tone/loudness, and push the config back. If CamillaDSP comes back
    with ANOTHER config (GUI restarting CamillaDSP, etc.), it is an external
    change: the config we leave is saved, the new one keeps its file values."""
    global config_active, current_config_key, current_config_external
    new_active = cdsp.config.active() or {}
    path = cdsp.config.file_path()
    new_key = config_key(path) if path else None
    if new_key and current_config_key and new_key != current_config_key:
        try:
            save_audio_settings(config_active)
        except Exception as e:
            log.warning("Resync: could not save the settings (%s)", e)
        log.info("[CONFIG] External change after a CamillaDSP restart: %s -> %s (%s)",
                 current_config_key, new_key,
                 "previous config not saved: it was already external"
                 if current_config_external else "previous config saved")
        current_config_external = True
    elif new_key == current_config_key and not current_config_external:
        # Same config: CamillaDSP came back with the file values. Save the
        # in-memory tone / loudness first, so that they are re-applied below
        # instead of the older values of settings.json.
        try:
            save_audio_settings(config_active)
        except Exception as e:
            log.warning("Resync: could not save the settings (%s)", e)
    if isinstance(new_active, dict) and new_active:
        config_active = new_active
    current_config_key = new_key
    update_tv_relay()
    saved = settings["configs"].get(current_config_key) if current_config_key else None
    load_prev_values(saved)
    if TONE_ACTIVE and is_complete_config(config_active):
        filters = config_active.get("filters")
        if not current_config_external:
            apply_saved_values(filters if isinstance(filters, dict) else {}, saved)
            apply_tone_to_config(config_active)
        cdsp.config.set_active(config_active)


async def supervised(name, factory):
    """Restart a task that crashes, instead of bringing the whole script down."""
    while True:
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Task '%s' crashed, restarting in %g s", name, TASK_RESTART_DELAY_S)
            await asyncio.sleep(TASK_RESTART_DELAY_S)


async def cdsp_poller():
    """Periodic reader of volume / mute / config path. Also publishes the full
    content of cdsp.config.active(): it is the change-detection source that
    works even when the GUI changed the config without updating file_path()
    (CamillaDSP stores the path but does not touch it on SetConfig: only
    change_config calls set_file_path). On error: reconnection, keep running."""
    n = 0
    failures = 0
    while True:
        try:
            t0 = time.monotonic()
            db = cdsp.volume.main_volume()
            muted = bool(cdsp.volume.main_mute())

            path = active = None
            if n % PATH_EVERY == 0:
                path = cdsp.config.file_path()
                # active() serializes the whole config: skip it while a volume
                # key is held, otherwise the volume display stutters on every
                # repeat (the remote handler already refreshes it directly).
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


# ---------- Config switching ----------
def _prefix_of(path):
    """Group letter of a config file ('A', 'B', ...). Files that do not match
    the '_X-' pattern fall back to 'A'."""
    m = _GROUP_RE.match(os.path.basename(path or ""))
    return m.group(1) if m else "A"


def config_files_matching(pattern):
    """Sorted config files matching a glob pattern. Only .yml / .yaml files
    count: a backup (.bak, .orig) or an editor file left in the folder is
    never a config."""
    return sorted(p for p in glob.glob(pattern)
                  if p.lower().endswith((".yml", ".yaml")) and os.path.isfile(p))


def _list_groups():
    """Sorted list of groups that currently have at least one config file."""
    groups = set()
    for path in config_files_matching(CONFIG_DIR + "_*-*"):
        m = _GROUP_RE.match(os.path.basename(path))
        if m:
            groups.add(m.group(1))
    return sorted(groups)


async def cycle_group():
    """Switch to the next group (sorted order, wrapping around). Lands on the
    last used config of the target group. Does nothing if there is only one."""
    groups = _list_groups()
    if not groups:
        return
    current = _prefix_of(cdsp.config.file_path())
    target = groups[(groups.index(current) + 1) % len(groups)] if current in groups else groups[0]
    await change_config(CONFIG_DIR + "_" + target + "-*", cycle=False)


def find_config_file(active_config):
    """Return the YAML file matching active_config, or None. Used ONLY when
    the GUI changed the config without updating file_path() (CamillaDSP does
    not touch file_path() on SetConfig: only change_config calls set_file_path)."""
    if not isinstance(active_config, dict) or not active_config:
        return None
    for path in config_files_matching(CONFIG_DIR + "*"):
        try:
            data = cdsp.config.read_and_parse_file(path)
        except Exception:
            continue
        if isinstance(data, dict) and data == active_config:
            return path
    return None


_MISSING = object()


async def change_config(config_pattern, cycle=True):
    """Switch config within the group of files matching the pattern.
    cycle=True : next file of the group if the active config belongs to it.
    Otherwise : last config used in this group.
    A file that cannot be read, or that is not a complete config, is skipped:
    the next file of the group is tried instead.

    SAVE_MODE controls what is copied from the config we leave into the new
    one before it is applied (see the SETTINGS block). The values are always
    saved per config; SAVE_MODE only decides what gets copied. If CamillaDSP
    rejects the new config, the previous settings entry of the target is put
    back.

    The new config is published into vol_state so the other tasks notice it.
    The caller checks the result through vol_state.config_path."""
    global config_active, current_config_key, current_config_external

    group = _prefix_of(config_pattern)
    config_files = config_files_matching(config_pattern)
    if not config_files:
        return
    current_config = cdsp.config.file_path()

    # BACK on a single-config setup re-adopts the file as internal
    if (current_config_external and current_config in config_files
            and cycle and len(config_files) == 1):
        current_config_external = False
        settings["last_config"][group] = config_key(current_config)
        write_settings()
        if not scroll_active():
            display_volume_info()
        return

    if cycle and current_config in config_files:
        index = config_files.index(current_config)
        start = (index + 1) % len(config_files)
    else:
        last_key = settings["last_config"].get(group)
        first = next(
            (p for p in config_files if config_key(p) == last_key),
            config_files[0],
        )
        if first == current_config:
            return
        start = config_files.index(first)
    # Files to try, in order (the active one is never a candidate)
    candidates = [p for p in config_files[start:] + config_files[:start]
                  if p != current_config]
    if not candidates:
        return

    if current_config_external:
        log.info("[CONFIG] Leaving the external config %s: its tone / loudness are not saved",
                 current_config_key)
    save_audio_settings(config_active, write=False)   # written once, at the end of the switch

    # The config we leave is the last one used in its group: remember it now,
    # so coming back to this group lands on it with its latest values.
    if current_config:
        settings["last_config"][_prefix_of(current_config)] = config_key(current_config)

    new_config_path = new_config_data = None
    for path in candidates:
        try:
            data = cdsp.config.read_and_parse_file(path)
        except Exception as e:
            log.error("Cannot read %s: %s (skipped)", path, e)
            continue
        if not is_complete_config(data):
            log.error("Config %s is invalid or incomplete (skipped)", path)
            continue
        new_config_path, new_config_data = path, data
        break
    if new_config_path is None:
        log.error("No usable config in this group, switch aborted")
        return

    filters = new_config_data.get('filters')
    if not isinstance(filters, dict):
        filters = {}
        new_config_data['filters'] = filters

    new_key = config_key(new_config_path)
    new_group = _prefix_of(new_config_path)

    # SAVE_MODE copy. Written into settings["configs"][new_key] BEFORE applying,
    # so apply_saved_values below picks the copy up. No write_settings() here:
    # the one at the end of a successful switch saves it.
    # Nothing is copied from a config chosen outside this script.
    previous_entry = settings["configs"].get(new_key, _MISSING)
    if SAVE_MODE != "all_config" and not current_config_external:
        same_group = (new_group == _prefix_of(current_config))
        old_entry = settings["configs"].get(current_config_key)
        if isinstance(old_entry, dict):
            if SAVE_MODE == "all_group" and same_group:
                settings["configs"][new_key] = dict(old_entry)
            elif SAVE_MODE == "half" and same_group:
                # Copy: the existing entry must stay untouched for the rollback
                entry_b = settings["configs"].get(new_key)
                entry_b = dict(entry_b) if isinstance(entry_b, dict) else {}
                if "loudness_ref" in old_entry:
                    entry_b["loudness_ref"] = old_entry["loudness_ref"]
                settings["configs"][new_key] = entry_b
            elif SAVE_MODE == "same_everywhere":
                settings["configs"][new_key] = dict(old_entry)

    saved = settings["configs"].get(new_key)
    apply_saved_values(filters, saved)

    # Re-derive freq/q/comp AFTER apply_saved_values (which may have changed
    # the gains) and BEFORE set_active (so CamillaDSP receives final values).
    apply_tone_to_config(new_config_data)

    # Make relative filter file names absolute
    config_dir = os.path.dirname(os.path.abspath(new_config_path))
    for f in filters.values():
        params = f.get("parameters") if isinstance(f, dict) else None
        filename = params.get("filename") if isinstance(params, dict) else None
        if isinstance(filename, str) and filename and not os.path.isabs(filename):
            params["filename"] = os.path.normpath(os.path.join(config_dir, filename))

    # Only update the local state if CamillaDSP accepted the config
    try:
        cdsp.config.set_active(new_config_data)
        cdsp.config.set_file_path(new_config_path)
    except Exception as e:
        log.error("Failed to apply config %s: %s", new_config_path, e)
        if previous_entry is _MISSING:
            settings["configs"].pop(new_key, None)
        else:
            settings["configs"][new_key] = previous_entry
        return

    config_active = new_config_data
    current_config_key = new_key
    current_config_external = False
    update_tv_relay()

    load_prev_values(saved)

    settings["last_config"][group] = new_key
    write_settings()

    vol_state.publish(
        vol_state.db, vol_state.muted,
        config_path=new_config_path,
        config_data=new_config_data,
    )

    # Let CamillaDSP finish loading before the display redraws.
    await asyncio.sleep(0.1)

    if not scroll_active():
        display_volume_info()


async def volume_watcher_loop():
    """Detect config changes by comparing the full active config content to
    the last known one. That content is the ONLY detection source: it changes
    on any config change, INTERNAL (change_config, tone / loudness tweak by the
    remote) or EXTERNAL (GUI).

    INTERNAL change: change_config / _push_filters published config_data
      (fresh, see VolumeState) -> skip.
    Already known content (current_active == config_active) -> nothing.
    EXTERNAL change (GUI): identify the file, save the config we leave, update
    state, reset prev_values, redraw. Do NOT restore the saved values of the
    external config, do NOT push it back."""
    global config_active, current_config_key, current_config_external

    ev = vol_state.subscribe()
    while vol_state.db is None or vol_state.config_active is None:
        await ev.wait()
        ev.clear()
    last_volume, last_mute = vol_state.db, vol_state.muted
    last_active = vol_state.config_active
    resync_retries = 0
    RESYNC_RETRIES = 5

    while True:
        await ev.wait()
        ev.clear()
        current_volume, current_mute = vol_state.db, vol_state.muted
        current_path = vol_state.config_path
        current_active = vol_state.config_active

        config_changed = (current_active is not None
                          and current_active != last_active)

        if config_changed:
            published = vol_state.take_config_data()

            if published is not None:
                last_active = current_active
                resync_retries = 0
                continue

            if current_active == config_active:
                last_active = current_active
                resync_retries = 0
                continue

            # EXTERNAL change (GUI). Give CamillaDSP time to finish loading.
            await asyncio.sleep(0.3)
            try:
                new_active = cdsp.config.active() or {}
            except Exception as e:
                log.warning("Volume watcher: could not fetch active config (%s)", e)
                new_active = None

            if is_complete_config(new_active):
                # Identify the file. file_path() may still point to the PREVIOUS
                # config (the GUI does not call set_file_path). Try the current
                # file first, then scan the YAML files, then keep the current path.
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
                elif new_path != current_path:
                    # The GUI does not update file_path(): do it, so that BACK,
                    # the groups and last_config start from the config really
                    # playing. Only the stored path changes, not the config.
                    try:
                        cdsp.config.set_file_path(new_path)
                        vol_state.publish(vol_state.db, vol_state.muted,
                                          config_path=new_path)
                    except Exception as e:
                        log.warning("Volume watcher: could not update file_path (%s)", e)

                new_key = config_key(new_path) if new_path else None
                key_changed = new_key != current_config_key
                old_key, was_external = current_config_key, current_config_external

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

                config_active = new_active
                current_config_key = new_key
                current_config_external = True
                update_tv_relay()

                load_prev_values(None)

                resync_retries = 0

                redraw = {"tone": display_tone_info,
                          "tone_select": display_tone_preset_select,
                          "loudness": display_loudness_info}.get(last_displayed)
                if redraw and not scroll_active() and not display_is_off:
                    redraw()

                last_active = current_active

            else:
                # Incomplete config: probably still loading, wait for the next
                # readings. After RESYNC_RETRIES attempts, give up on this one.
                resync_retries += 1
                if resync_retries > RESYNC_RETRIES:
                    resync_retries = 0
                    log.warning("Config changed but active config is incomplete; keeping previous")
                    last_active = current_active

        elif (vol_state.config_data is not None
              and vol_state.config_data_expired()):
            # A publish that changed nothing (arrow at its limit...): drop it,
            # so it cannot hide a later EXTERNAL change.
            vol_state.config_data = None

        if current_volume != last_volume or current_mute != last_mute or config_changed:
            last_volume = current_volume
            last_mute = current_mute
            if (last_displayed == "volume" and not is_volume_key_held
                    and not scroll_active() and not display_is_off):
                display_volume_info(current_volume, current_mute)

# =====================================================================
#  LMS - Logitech Media Server
# =====================================================================


@functools.cache   # read once; a failure (exception) is not cached
def detect_player_mac():
    """MAC of the default network interface, raw lowercase (dc:a6:32:3c:1c:21)."""
    with open("/proc/net/route") as f:
        next(f)
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
    return LMS_PLAYER_MANUAL or detect_player_mac()


def send_lms_command(command, quiet=False):
    """Own socket (no shared state): safe to run in a thread. The reply is
    read until LMS closes the connection (after our "exit")."""
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


_lms_unreachable = False


def lms_is_playing() -> bool:
    """True if LMS is in 'play' mode. Called in a loop by monitor_tv_toslink:
    only logs state changes (unreachable / reachable), never every request."""
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
# Reads the values published by cdsp_poller (no direct queries), uses asyncio
# streams for the LMS connection, and ignores CamillaDSP readings that started
# before our last write (otherwise a stale reading could undo a change just
# received from LMS). Inspired by mbrennwa/camilla_LMS_volume_sync.

bridge_log = logging.getLogger("lms_bridge")


class LmsBridge:
    def __init__(self, player, state, set_volume_db, set_mute, lms_addr, *,
                 restore_window_s, connect_timeout_s, debug=False):
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

    def _reset(self):
        self.pct = 0
        self.muted = False
        self.restore_until = 0.0   # post-unmute window deadline
        self.prev = None           # previous CamillaDSP reading (change seen twice)
        self._written_at = 0.0     # time of our last write to CamillaDSP

    def _say(self, message):
        bridge_log.info("[LMS sync] %s", message)

    def _send(self, cmd):
        w = self._writer
        if w is None or w.is_closing():
            raise ConnectionError("LMS connection closed")
        w.write((cmd + "\n").encode())

    def _write_volume(self, p):
        self.prev = None
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

    def _set_mute(self, new, origin, apply):
        # After an unmute, LMS may announce a wrong volume (the +/- keys restart
        # from 0/5 %), or nothing at all (click on the mute icon). The restore
        # window starts here: during it, wrong volumes announced by LMS are
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
        if p != self.pct:   # echo of our own push has p == pct
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
        """One bridge session. Ends only by raising an exception or being cancelled."""
        self._reset()
        ev = self.state.subscribe()
        try:
            while self.state.db is None:
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
                    t.result()
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
        while True:
            try:
                await self.run_session()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if repr(e) != self._last_err:
                    self._say(f"Bridge error: {e!r}, retrying every {retry_s:g}s")
                    self._last_err = repr(e)
            await asyncio.sleep(retry_s)


async def lms_bridge_task():
    """Only the player detection is retried here (no network interface yet at
    boot). The bridge restarts itself after any error, and `supervised`
    restarts this task if it ever crashes."""
    player = None
    warned = False
    while player is None:
        try:
            player = player_id()
        except Exception as e:
            if not warned:
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
# =====================================================================

auto_power_enabled = True
is_waiting_for_sound = False


def relay_is_off():
    """True when the relay is already off: POWER SAVE done (waiting for
    sound) or relay forced OFF by the POWER key."""
    return ENABLE_AUTO_POWEROFF and (is_waiting_for_sound or not auto_power_enabled)


def toggle_power():
    global auto_power_enabled, is_waiting_for_sound

    if not auto_power_enabled or is_waiting_for_sound:
        auto_power_enabled = True
        lgpio.gpio_write(h, POWER_GPIO, lgpio.HIGH)
        log.info("[POWER] Auto mode re-armed, relay ON")
        scroll_message("POWER AUTO")
        is_waiting_for_sound = False
    else:
        auto_power_enabled = False
        lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
        log.info("[POWER] Relay forced OFF (auto mode disengaged)")
        scroll_message("POWER OFF")


def shutdown_system():
    try:
        save_audio_settings(config_active)
    except Exception as e:
        log.warning("Shutdown: could not save the settings (%s)", e)
    if ENABLE_AUTO_POWEROFF:
        lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
    release_tv_relay()
    cancel_scroll()                 # nothing may overwrite the HALT message
    show(" HALT ")
    os.system("sudo poweroff")


async def auto_poweroff():
    """Cut the relay after POWER_OFF_DELAY min of silence AND no key press,
    back on when sound returns, and halt after HALT_DELAY hours of (audio)
    silence. The halt only depends on silence, not on key presses.
    The GPIO state is tracked by auto_power_enabled / is_waiting_for_sound
    (the relay is LOW whenever one of them says it is off), so it is never
    read back."""
    global is_waiting_for_sound
    power_off_done = False
    seen_activity = last_activity

    while True:
        await asyncio.sleep(1)

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
                    is_waiting_for_sound = True
                    # If the scroll was cancelled (key pressed), the user is
                    # there: do not blank.
                    finished = await wait_scroll()
                    if finished and silent_now and is_waiting_for_sound:
                        blank_display()
                # else: relay already forced off, nothing to cut. The display
                # is blanked by display_manager_loop.

            if silence_age() >= HALT_DELAY * 3600:
                shutdown_system()
                break

        else:
            power_off_done = False
            if auto_power_enabled and is_waiting_for_sound:
                lgpio.gpio_write(h, POWER_GPIO, lgpio.HIGH)
                if not scroll_active():
                    display_volume_info()
                is_waiting_for_sound = False


# =====================================================================
#  TV RELAY - shared helpers
# =====================================================================

tv_relay_on = False             # Known state: the GPIO is only written on a change.
                                # The input is claimed (inactive) at startup.


def release_tv_relay():
    if not ENABLE_TV_RELAY:
        return
    try:
        lgpio.gpio_free(h, TV_RELAY_GPIO)
    except Exception:
        pass
    lgpio.gpio_claim_input(h, TV_RELAY_GPIO)


def update_tv_relay():
    """Active (GPIO to ground) if the active config belongs to group B,
    inactive (high impedance) otherwise. Called after every change of
    current_config_key."""
    global tv_relay_on
    if not ENABLE_TV_RELAY or not current_config_key:
        return
    want = _prefix_of(current_config_key) == 'B'
    if want == tv_relay_on:
        return
    try:
        lgpio.gpio_free(h, TV_RELAY_GPIO)
    except Exception:
        pass
    if want:
        lgpio.gpio_claim_output(h, TV_RELAY_GPIO, 0)
    else:
        lgpio.gpio_claim_input(h, TV_RELAY_GPIO)
    tv_relay_on = want
    log.info("[TV RELAY] %s (config %s)", "ON" if want else "OFF", current_config_key)


# =====================================================================
#  DISPLAY - TM1637 and ambient light sensor
# =====================================================================


last_displayed = None           # "volume", "tone", "tone_select", "loudness", "message"
blank_volume_when_mute = False
display_is_off = False


class NullDisplay:
    """Stand-in for the TM1637 when ENABLE_DISPLAY is False: accepts every
    call and shows nothing, so the rest of the code does not have to care."""
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
    Always called with 6 segments."""
    segs.extend(bytearray(max(0, 6 - len(segs))))
    segs[0], segs[2] = segs[2], segs[0]
    segs[3], segs[5] = segs[5], segs[3]
    return segs


# Extra segments for characters encode_char rejects:
#   _ : segment D only. Marks a label from a FILE NAME (not a title).
#   | : segments B+C. Safety net for a non-renamed "|tv.yml" file.
_EXTRA_SEGMENTS = {
    '_': 0x08,
    '|': 0x06,
}


def encode_string_extended(tm_obj, s):
    segs = bytearray(len(s))
    for i, c in enumerate(s):
        if c in _EXTRA_SEGMENTS:
            segs[i] = _EXTRA_SEGMENTS[c]
        else:
            segs[i] = tm_obj.encode_char(c)
    return segs


def show(text):
    tm.write(swap(encode_string_extended(tm, text)))


def clear_display():
    tm.write(tm.encode_string(" " * 6))


def blank_display():
    global display_is_off
    if not ENABLE_DISPLAY:
        return
    clear_display()
    display_is_off = True


# ---------- Scrolling messages ----------
_scroll_task = None


def scroll_active():
    """True while a scrolling message runs. Every automatic redraw of the
    volume screen must check this, otherwise it would overwrite the message."""
    return _scroll_task is not None and not _scroll_task.done()


def cancel_scroll():
    """Stop the running scroll. The end-of-scroll callback does nothing for a
    task that is no longer current, so a cancel never triggers a redraw itself."""
    global _scroll_task
    task, _scroll_task = _scroll_task, None
    if task is not None and not task.done():
        task.cancel()
        return True
    return False


async def wait_scroll():
    """True if the scroll ran to the end, False if there was none or if it
    was cancelled (a key was pressed)."""
    task = _scroll_task
    if task is None:
        return False
    if not task.done():
        await asyncio.wait({task})
    return not task.cancelled()


def return_to_screen():
    """Redraw the volume screen after a scroll. Skipped if another screen has
    been shown or the display is blanked."""
    if last_displayed == "message" and not display_is_off and not scroll_active():
        display_volume_info()


def _start_scroll(coro):
    global _scroll_task, last_displayed
    cancel_scroll()
    last_displayed = "message"
    task = _scroll_task = asyncio.create_task(coro)

    def _on_done(t):
        if t.cancelled() or t is not _scroll_task:
            return
        asyncio.get_running_loop().call_soon(return_to_screen)

    task.add_done_callback(_on_done)


async def _scroll_coro(text):
    text = " " + text + " " * 6
    for i in range(len(text) - 5):
        show(text[i:i + 6])
        await asyncio.sleep(SCROLL_FRAME_DELAY)
    await asyncio.sleep(SCROLL_END_PAUSE)


def scroll_message(text):
    if not ENABLE_DISPLAY:
        return
    _start_scroll(_scroll_coro(text))


# ---------- Screens ----------
def blink_on():
    """1 Hz blink phase, derived from the clock at draw time: no state to keep
    in sync, and every dot blinks in phase."""
    return int(time.monotonic()) % 2 == 0


def fmt_loudness(value):
    """3 chars right-aligned: ' --' if the Loudness filter is absent from the
    pipeline, otherwise the reference level."""
    if value is None:
        return " --"
    return f"{round(value):3}"


def _strip_accents(s):
    return "".join(
        c for c in unicodedata.normalize('NFD', s)
        if unicodedata.category(c) != 'Mn'
    )


def _tm1637_char_ok(c):
    """True if the character can be displayed. Everything else is filtered
    out before encoding, to avoid a ValueError in encode_char()."""
    o = ord(c)
    return (o == 32 or o == 42 or o == 45
            or 48 <= o <= 57 or 65 <= o <= 90 or 97 <= o <= 122
            or c == '_' or c == '|')


def _filter_display_label(s):
    return "".join(c for c in s if _tm1637_char_ok(c))


def get_config_display_label():
    """3-char label: title if present, else the file name. The group prefix
    "_X-" is reduced to a single "_" ("_B-television" -> "_te"). The '|'
    character is a safety net for a non-renamed file: it shows up instead of
    silently displaying a shifted name."""
    title = config_active.get("title") if isinstance(config_active, dict) else None
    if title:
        label = title.strip()
    else:
        path = vol_state.config_path or cdsp.config.file_path()
        label = os.path.splitext(os.path.basename(path))[0] if path else "---"
        m = _GROUP_RE.match(label)
        if m:
            label = "_" + label[m.end():]
    label = _strip_accents(label)
    label = _filter_display_label(label)
    if not label:
        label = "---"
    return label[:3].ljust(3)


def display_volume_info(current_volume=None, is_muted=None):
    """Show the config label and the volume.

    Indicators (blink phase shared by all):
      segs[0..2] DP : CONF MANUAL (TOSLINK auto OFF)
      segs[3] DP    : TONE active (bass or treble non-zero), fixed
      segs[3] Seg G : separator; blinks when POWER auto is OFF
      segs[5] DP    : half-dB indicator
      label (3 chars): blinks when the config was chosen OUTSIDE this script
    """
    global blank_volume_when_mute, last_displayed, display_is_off

    if not ENABLE_DISPLAY:
        return

    if current_volume is None:
        current_volume = vol_state.db
        if current_volume is None:
            current_volume = cdsp.volume.main_volume()

    if is_muted is None:
        is_muted = vol_state.muted
        if is_muted is None:
            is_muted = cdsp.volume.main_mute()

    config_label = get_config_display_label()

    if current_config_external and not blink_on():
        config_label_display = "   "
    else:
        config_label_display = config_label

    if is_muted and not is_volume_key_held:
        blank_volume_when_mute, volume_str = True, f"{config_label_display} --"
        has_half = False
    else:
        abs_db = abs(current_volume)
        whole = int(abs_db)
        has_half = (abs_db - whole) >= 0.25
        blank_volume_when_mute, volume_str = False, f"{config_label_display} {whole:02d}"

    segs = encode_string_extended(tm, volume_str)

    if has_half:
        segs[5] |= 0x80

    p = get_all_tone_params(config_active)

    # DP on segs[3]: TONE active, fixed.
    if any(p[k] for k in ("bass", "treble")):
        segs[3] |= 0x80

    # Seg G on segs[3]: permanent separator, blinks when POWER auto is OFF
    # (relay forced off by the POWER key).
    if ENABLE_AUTO_POWEROFF and not auto_power_enabled:
        if blink_on():
            segs[3] |= 0x40
    else:
        segs[3] |= 0x40

    # DP on the three label characters: CONF MANUAL, blinking.
    if ENABLE_TOSLINK_SWITCH and not toslink_auto_enabled and blink_on():
        segs[0] |= 0x80
        segs[1] |= 0x80
        segs[2] |= 0x80

    tm.write(swap(segs))
    last_displayed = "volume"
    display_is_off = False


def _tone_gain_text(value):
    """(text, half) of a gain: integer part of the absolute value with its sign,
    half=True if the absolute value has an extra half unit (the decimal point
    is then lit on the digit). Gains are multiples of 0.5."""
    mag = abs(value)
    whole = int(mag)
    half = (mag - whole) >= 0.25
    neg = value < 0 and (whole > 0 or half)
    return ("-" if neg else "") + str(whole), half


def display_tone_info():
    """'B{xx}T{yy}'. 'B----T' if the tone filters are not ready.
    A decimal point on the bass or treble digit marks an extra half unit
    (like the half-dB indicator of the volume): -1.5 shows '-1.', 1.5 shows '1.'."""
    global last_displayed
    p = get_all_tone_params(config_active)

    if p['bass'] is None or p['treble'] is None:
        show("B----T")
    else:
        b_txt, b_half = _tone_gain_text(p['bass'])
        t_txt, t_half = _tone_gain_text(p['treble'])
        segs = encode_string_extended(tm, f"B{b_txt:<2}{t_txt:>2}T")
        if b_half:
            segs[len(b_txt)] |= 0x80   # bass digit (left-aligned, positions 1-2)
        if t_half:
            segs[4] |= 0x80            # treble digit (right-aligned, positions 3-4)
        tm.write(swap(segs))
    last_displayed = "tone"


def display_tone_preset_select():
    """'BT def', 'BT alt', 'TL def' or 'TL alt': keys mode, then DSP preset.
    DOWN toggles def / alt, UP toggles BT / TL, LEFT and RIGHT exit."""
    global last_displayed
    show(TONE_KEY_MODES[tone_mode_idx] + " " + ("def" if tone_preset_idx == 0 else "alt"))
    last_displayed = "tone_select"


def display_loudness_info():
    global last_displayed
    p = get_all_tone_params(config_active)
    show(f"LDN{fmt_loudness(p['loudness'])}")
    last_displayed = "loudness"


# ---------- Brightness (ambient light sensor) ----------
adc_buffer = b""


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
    """Only the most recent complete line received since the last pass is used."""
    global adc_buffer

    if ser is None:
        log.info("Ambient light sensor unavailable; using fallback brightness")
        return

    last_brightness = -1

    while True:
        if ser.in_waiting:
            adc_buffer += ser.read(ser.in_waiting)
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


# ---------- Display housekeeping ----------
async def display_manager_loop():
    """Return to volume after SCREEN_RETURN_DELAY s, blank after a long quiet
    period, wake when sound returns. Every delay is a timestamp comparison
    (idle_age / silence_age): none of them depends on the loop rate."""
    while True:
        if (last_displayed != "volume"
                and not scroll_active()
                and not display_is_off
                and idle_age() >= SCREEN_RETURN_DELAY):
            display_volume_info()

        if silent_now:
            # Relay still on (managed): auto_poweroff cuts it, shows POWER SAVE,
            # then blanks the display itself.
            # Relay off (or not managed): silence must have lasted
            # POWER_OFF_DELAY, then the display goes off after POWER_OFF_DELAY
            # without key press, or after WAKE_DISPLAY_DELAY when the relay is
            # already off (key wake-up).
            if not ENABLE_AUTO_POWEROFF or relay_is_off():
                is_off = relay_is_off()
                normal_delay = POWER_OFF_DELAY * 60
                key_delay = WAKE_DISPLAY_DELAY if is_off else normal_delay
                if (not display_is_off and not scroll_active()
                        and silence_age() >= normal_delay and idle_age() >= key_delay):
                    blank_display()
        elif display_is_off:
            display_volume_info()

        await asyncio.sleep(1.0)


async def blink_loop():
    """1 s tick, aligned on the clock, that redraws the volume screen while an
    indicator must blink. The phase itself comes from blink_on() at draw time."""
    while True:
        await asyncio.sleep(1.0 - time.monotonic() % 1.0)

        blinking = (current_config_external
                    or (ENABLE_TOSLINK_SWITCH and not toslink_auto_enabled)
                    or (ENABLE_AUTO_POWEROFF and not auto_power_enabled))

        if (blinking
                and last_displayed == "volume"
                and not display_is_off
                and not scroll_active()
                and not is_volume_key_held):
            display_volume_info()


# =====================================================================
#  TOSLINK - automatic _A- / _B- config switching
# =====================================================================

toslink_auto_enabled = True
toslink_ready = False


def toslink_active():
    """True when the automatism can run: feature enabled AND monitor_tv_toslink
    found its config files. Otherwise BACK/HOMEPAGE behave as if the feature
    were off."""
    return ENABLE_TOSLINK_SWITCH and toslink_ready


def toggle_toslink_auto():
    global toslink_auto_enabled
    toslink_auto_enabled = not toslink_auto_enabled
    log.info("[TOSLINK] Automatism %s", "ON" if toslink_auto_enabled else "OFF")
    scroll_message("CONF AUTO" if toslink_auto_enabled else "CONF MANUAL")


# Only two groups are recognised by the automatism:
#
#   _A-XX : CamillaDSP captures the loopback -> TOSLINK is free.
#           Probed directly through ALSA (0.5 s). A reading counts when
#           rms >= TOSLINK_ON_DB AND crest (peak - rms) >= CREST_MIN_DB
#           (a steady noise floor has peak ~= rms and is ignored).
#           2 consecutive readings -> switch to _B-XX
#
#   _B-XX : CamillaDSP captures TOSLINK -> `silent_now` is the TV level.
#           Every TV_LOOP_S, LMS is asked whether it is playing:
#             LMS playing + TOSLINK silent for SILENT_TICKS cycles -> back to _A-XX
#             LMS playing while the TV plays -> LMS is paused
#
# A manual group ("_C-", "_D-", ...) is only meaningful in manual mode. While
# the automatism is ON and a manual group is active, the loop probes TOSLINK
# once and immediately switches to the automatic group matching the state.
# After a TV -> streamer return the automatism lands on A, not on the manual
# group.
#
# Automatism OFF: no probing, no switching, no LMS pause. BACK cycles within
# the active group, HOMEPAGE cycles groups.

TOSLINK_FORMATS = {
    "S16_LE":  (2, 16),
    "S24_3LE": (3, 24),
    "S24_LE":  (4, 24),     # 24 bits in a 32-bit word
    "S32_LE":  (4, 32),
}


def read_toslink_capture_params():
    """Read device / rate / channels / format from the first '_B-*' YAML config.
    All '_B-*' configs share the same capture input, so the first is enough."""
    files = config_files_matching(CONFIG_DIR + "_B-*")
    if not files:
        log.error("[TOSLINK] No '_B-*' config file found in %s", CONFIG_DIR)
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


def _levels(data, fmt, channels):
    """Peak and RMS in dBFS. The RMS is the one of the LOUDEST channel, not of
    the channels mixed together (a steady signal with different levels on the
    two channels would otherwise pass the CREST_MIN_DB test as if it were
    audio). Stereo only; other channel counts use the mixed RMS."""
    if not data:
        return None, None
    width, bits = TOSLINK_FORMATS[fmt]
    full_scale = 2 ** (bits - 1)
    peak = audioop.max(data, width)
    if channels == 2:
        rms = max(audioop.rms(audioop.tomono(data, width, 1, 0), width),
                  audioop.rms(audioop.tomono(data, width, 0, 1), width))
    else:
        rms = audioop.rms(data, width)
    peak_db = 20.0 * math.log10(peak / full_scale) if peak > 0 else -1000.0
    rms_db = 20.0 * math.log10(rms / full_scale) if rms > 0 else -1000.0
    return peak_db, rms_db


def _toslink_probe(params):
    """Runs in a thread (blocks ~0.5 s).
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

    peak_db, rms_db = _levels(b"".join(chunks), params["format"], params["channels"])
    return peak_db, rms_db, None


def tv_detected(peak, rms):
    """A probe reading counts as TV audio: samples present, not a steady noise
    floor (crest), and level above TOSLINK_ON_DB."""
    return (peak is not None and (peak - rms) >= CREST_MIN_DB
            and rms >= TOSLINK_ON_DB)


async def monitor_tv_toslink():
    """See the block comment 'Only two groups are recognised...' above
    TOSLINK_FORMATS."""
    global toslink_ready
    loop = asyncio.get_running_loop()

    LOOP_S       = 0.5      # base cycle period; group A mode also spends ~0.5 s probing
    TV_LOOP_S    = 1.0      # "_B-" mode: exactly one LMS request per cycle
    SILENT_TICKS = 2        # consecutive silent cycles (LMS playing) -> back to _A-
    RETRY_BASE_S = 5.0      # first delay after a failed switch; doubled each failure
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

    if not config_files_matching(CONFIG_DIR + "_A-*"):
        log.error("[LOOPBACK] No '_A-*' config file found in %s", CONFIG_DIR)
        return
    log.info("[LOOPBACK] Configs '_A-*' found in %s", CONFIG_DIR)
    toslink_ready = True

    last_config_path = cdsp.config.file_path() or ""
    toslink_hits = 0
    silent_ticks = 0
    switch_failures = 0
    last_err = None

    async def try_switch(pattern, target_prefix):
        """Switch to the requested group and check that it really happened.
        On failure: exponential backoff instead of retrying every cycle."""
        nonlocal last_config_path, switch_failures
        await change_config(pattern, cycle=False)
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

        if not toslink_auto_enabled:
            toslink_hits = silent_ticks = 0
            continue

        try:
            current_group = _prefix_of(vol_state.config_path)

            # Manual group + automatism ON: pick the right automatic group
            # right away, based on a single TOSLINK probe.
            if current_group not in ('A', 'B'):
                peak, rms, err = await loop.run_in_executor(
                    None, _toslink_probe, toslink_params)

                if err is None and tv_detected(peak, rms):
                    log.info("[TOSLINK] Automatism ON, manual group %s + TV "
                             "playing -> _B-", current_group)
                    await try_switch(CONFIG_DIR + '_B-*', 'B')
                else:
                    log.info("[TOSLINK] Automatism ON, leaving manual group "
                             "%s -> _A-", current_group)
                    await try_switch(CONFIG_DIR + '_A-*', 'A')
                continue

            if current_group != 'B':
                # Group A: TOSLINK is free -> probe it. LMS is not touched:
                # the streamer group stays active until the TV really plays.
                silent_ticks = 0
                peak, rms, err = await loop.run_in_executor(
                    None, _toslink_probe, toslink_params)

                if err is not None:
                    if repr(err) != last_err:
                        log.warning("[TOSLINK] Probe failed: %r", err)
                        last_err = repr(err)
                    toslink_hits = 0
                    await asyncio.sleep(2.0)
                    continue
                last_err = None

                if TOSLINK_DEBUG and peak is not None:
                    log.info("[TOSLINK] probe: peak=%.1f rms=%.1f crest=%.1f",
                             peak, rms, peak - rms)

                # Protections 1 and 2: no samples, steady noise floor (peak ~= rms)
                # or RMS below the level threshold -> no audio
                if not tv_detected(peak, rms):
                    toslink_hits = 0
                    continue

                # Protection 3: 2 consecutive readings above threshold
                toslink_hits += 1
                if toslink_hits >= 2:
                    log.info("[TOSLINK] TV confirmed (peak=%.1f rms=%.1f) "
                             "-> switch to _B-XX", peak, rms)
                    toslink_hits = 0
                    await try_switch(CONFIG_DIR + '_B-*', 'B')

            else:
                # _B-XX: TOSLINK captured by CamillaDSP, silent_now is the TV level
                toslink_hits = 0
                lms_playing = await asyncio.to_thread(lms_is_playing)
                if lms_playing and silent_now:
                    # Debounce: a single silent cycle (dialogue pause, stale
                    # reading after a switch) must not send us back to _A-XX
                    silent_ticks += 1
                    if silent_ticks >= SILENT_TICKS:
                        log.info("[LOOPBACK] LMS playing + TOSLINK silent "
                                 "-> switch to _A-XX")
                        silent_ticks = 0
                        await try_switch(CONFIG_DIR + '_A-*', 'A')
                        continue
                elif lms_playing:
                    silent_ticks = 0
                    log.info("[TV] LMS started while the TV plays -> pause")
                    # "pause 1" forces the pause (a bare "pause" toggles, and
                    # would restart playback if LMS was already paused)
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
# =====================================================================


is_volume_key_held = False      # True from a volume key press until VOLUME_PEEK_S after release


def change_volume_from_key(key, current_volume):
    pct = db_to_pct(current_volume)
    delta = -1 if key == KEY_BINDINGS['VOLUMEDOWN'] else 1
    new_pct = clamp(pct + delta, 0, 100)

    if new_pct != pct:
        new_volume = pct_to_db(new_pct)
        cdsp.volume.set_main_volume(new_volume)
        display_volume_info(new_volume)
        return new_volume

    return current_volume


_volume_release_task = None


def cancel_volume_release():
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
    global _volume_release_task
    cancel_volume_release()
    _volume_release_task = asyncio.create_task(_volume_release_coro())


def handle_arrow_keys(key):
    """Adjust the parameters of the screen currently displayed.
      tone         : keys depend on the mode (TONE_KEY_MODES):
                       BT: UP/DOWN = Bass ±1, RIGHT/LEFT = Treble ±1
                       TL: fixed direction per key (UP = B- T+, DOWN = B+ T-,
                           RIGHT = B+ T+, LEFT = B- T-); the amplitude of each
                           gain (1, 0.5 or 0) comes from tl_steps() and depends on
                           the current gains.
                     Each gain stops at ±TONE_MAX_DB on its own, the other one
                     keeps moving until it reaches its limit too.
                     (no-op if the three tone filters are not ready)
      tone_select  : DOWN toggles the DSP preset (def / alt), UP toggles the
                     keys mode (BT / TL); LEFT / RIGHT exit to tone
      loudness     : UP/DOWN = loudness ±step (Loudness is optional: no
                     helper needed, direct access protected by an activity check)
      any other    : opens the tone screen
    """
    up, down = KEY_BINDINGS['UP'], KEY_BINDINGS['DOWN']

    if last_displayed == "tone_select":
        if key == down:
            toggle_tone_preset()
        elif key == up:
            toggle_tone_mode()
        else:
            display_tone_info()

    elif last_displayed == "loudness":
        loud_filter, loud_param = LOUDNESS_FILTER
        if loud_filter in active_filter_names(config_active) and key in (up, down):
            # LEFT / RIGHT do nothing here
            params = config_active["filters"][loud_filter]["parameters"]
            ref_level = params.get(loud_param, 0) + (
                LOUDNESS_STEP if key == up else -LOUDNESS_STEP)
            params[loud_param] = clamp(ref_level, LOUDNESS_MIN, LOUDNESS_MAX)
            _push_filters((loud_filter,))
            display_loudness_info()

    elif last_displayed == "tone":
        if not tone_filters_ready(config_active):
            return
        bass_p   = config_active["filters"][BASS_FILTER]["parameters"]
        treble_p = config_active["filters"][TREBLE_FILTER]["parameters"]
        mode = TONE_KEY_MODES[tone_mode_idx]
        if mode == "TL":
            bass_step, treble_step = tl_steps(
                key,
                bass_p.get('gain', 0) or 0,
                treble_p.get('gain', 0) or 0)
        else:
            bass_step, treble_step = TONE_ARROWS[mode][key]
        # Each gain is limited on its own: when one reaches its limit the other
        # keeps moving until it reaches its own.
        changed = False
        for params, step in ((bass_p, bass_step), (treble_p, treble_step)):
            if step == 0:
                continue
            old = params.get('gain', 0) or 0
            new = clamp(old + step, -TONE_MAX_DB, TONE_MAX_DB)
            if new != old:
                params['gain'] = new
                changed = True
        if not changed:
            return          # both gains already at their limit: nothing to send
        apply_tone_processing()
        display_tone_info()

    else:
        display_tone_info()


def handle_enter_press(screen):
    """Short ENTER press.
    - volume screen: nothing.
    - tone screen: bass and treble are ALL set to 0, or ALL restored to their
      previous values if they are already all at 0.
      No-op if the three tone filters are not ready.
    - tone_select screen: nothing (only UP and DOWN act).
    - loudness screen: nothing."""
    if screen != "tone" or not tone_filters_ready(config_active):
        return

    bass   = config_active["filters"][BASS_FILTER]["parameters"]
    treble = config_active["filters"][TREBLE_FILTER]["parameters"]
    params = {"bass_gain": bass, "treble_gain": treble}

    if bass.get('gain', 0) == 0 and treble.get('gain', 0) == 0:
        for key, p in params.items():
            p['gain'] = prev_values[key]
        prev_values.update(dict.fromkeys(TONE_PARAMS, 0))
    else:
        for key, p in params.items():
            prev_values[key] = p.get('gain', 0)
        for p in params.values():
            p['gain'] = 0

    apply_tone_processing()
    display_tone_info()


# ---------- Remote device and key events ----------
def find_remote():
    """Return the remote input device (grabbed), or None if it is not available.
    The grab makes this script the only receiver of the remote's events
    (logind no longer sees KEY_POWER)."""
    for path in evdev.list_devices():
        try:
            device = evdev.InputDevice(path)
        except OSError:                 # device vanished between list and open
            continue
        if device.name != REMOTE_NAME:
            device.close()
            continue
        try:
            device.grab()
        except OSError as e:            # EBUSY: another process holds it / ENODEV: gone
            log.warning("Cannot grab '%s' (%s), will retry", REMOTE_NAME, e)
            device.close()
            continue
        log.info("'%s' found at %s.", REMOTE_NAME, path)
        return device
    return None


async def remote_watcher():
    while True:
        device = find_remote()
        if device is None:
            await asyncio.sleep(1)
            continue
        try:
            await remote_events(device)
        finally:
            device.close()
        log.info("Remote disconnected, waiting for it to come back...")


async def remote_events(device):
    global is_volume_key_held

    key_hold_counter = 0            # Key-repeat events of the key being held
    enter_display_at_press = None   # Screen shown when ENTER was pressed (to tell short/long apart)
    last_seek_time = 0
    home_press_time = 0.0
    home_toggled = False
    volume_repeat_count = 0
    arrow_repeat_count = 0
    swallowed_key = None            # key that only woke the display: ignored until released
    while True:
        try:
            async for event in device.async_read_loop():
                if event.type != evdev.ecodes.EV_KEY:
                    continue

                # Any key event restarts every inactivity delay. Done BEFORE
                # the display-off test, so the key that wakes the display
                # counts too.
                touch_activity()

                attrib = evdev.categorize(event)
                key = attrib.keycode

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

                    # Stop any scroll so the key's action is visible immediately.
                    # A volume key is left alone: its handler shows the volume
                    # screen first when last_displayed is not "volume".
                    if cancel_scroll() and key not in VOLUME_KEYS:
                        display_volume_info()

                    if key in VOLUME_KEYS:
                        cancel_volume_release()
                        is_volume_key_held = True
                        volume_repeat_count = 0
                        current_volume = cdsp.volume.main_volume()
                        is_muted = cdsp.volume.main_mute()

                        if ENABLE_DISPLAY and (last_displayed != "volume" or blank_volume_when_mute):
                            display_volume_info(current_volume, is_muted)
                        else:
                            current_volume = change_volume_from_key(key, current_volume)

                    # `in`, not `==`: evdev returns a list when a code has several names
                    elif KEY_BINDINGS['MUTE'] in key:
                        cancel_volume_release()
                        is_volume_key_held = False
                        is_muted = not cdsp.volume.main_mute()
                        cdsp.volume.set_main_mute(is_muted)
                        display_volume_info(vol_state.db, is_muted)

                    elif ENABLE_DISPLAY and key in ARROW_KEYS:
                        arrow_repeat_count = 0
                        handle_arrow_keys(key)

                    elif key == KEY_BINDINGS['BACK']:
                        # Always: cycle INSIDE the active group, whatever the
                        # TOSLINK mode. Fresh read on purpose: change_config
                        # re-reads file_path() itself, both must agree.
                        prefix = _prefix_of(cdsp.config.file_path())
                        await change_config(CONFIG_DIR + '_' + prefix + '-*')

                    elif key == KEY_BINDINGS['HOMEPAGE']:
                        home_toggled = False
                        if toslink_active():
                            home_press_time = time.monotonic()

                    elif ENABLE_DISPLAY and key == KEY_BINDINGS['ENTER']:
                        # Remember the screen shown BEFORE any change, for the
                        # action on release and on hold.
                        enter_display_at_press = last_displayed

                elif attrib.keystate == 2:  # Key held down

                    if key in VOLUME_KEYS:
                        volume_repeat_count += 1
                        if volume_repeat_count >= VOLUME_SLOWDOWN[key]:
                            volume_repeat_count = 0
                            current_volume = change_volume_from_key(key, current_volume)

                    elif ENABLE_DISPLAY and key in ARROW_KEYS:
                        arrow_repeat_count += 1
                        if arrow_repeat_count >= ARROW_REPEAT_EVERY:
                            arrow_repeat_count = 0
                            handle_arrow_keys(key)

                    elif key in (KEY_BINDINGS['PREVIOUSSONG'], KEY_BINDINGS['NEXTSONG']):
                        key_hold_counter += 1
                        current_time = time.monotonic()
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
                            shutdown_system()       # saves the settings itself

                    elif key == KEY_BINDINGS['HOMEPAGE'] and toslink_active():
                        # Real-time measurement: independent of the repeat rate
                        if (not home_toggled
                                and time.monotonic() - home_press_time >= HOLD_TOGGLE_AUTO_S):
                            home_toggled = True
                            toggle_toslink_auto()

                    elif key == KEY_BINDINGS['ENTER']:
                        key_hold_counter += 1
                        if key_hold_counter == HOLD_SHOW_LOUDNESS:
                            # ENTER long:
                            #   volume      -> Loudness
                            #   tone        -> Tone preset select screen
                            #   loudness    -> Volume
                            #   tone_select -> Volume
                            if enter_display_at_press == "volume":
                                display_loudness_info()
                            elif enter_display_at_press == "tone":
                                display_tone_preset_select()
                            elif enter_display_at_press in ("loudness", "tone_select"):
                                display_volume_info()

                elif attrib.keystate == 0:  # Key released

                    # HOMEPAGE short press: nothing when the automatism is ON
                    # (the automatism is active, no manual action), otherwise
                    # cycle groups.
                    if key == KEY_BINDINGS['HOMEPAGE'] and not home_toggled:
                        if not (toslink_active() and toslink_auto_enabled):
                            await cycle_group()

                    if ENABLE_DISPLAY and key == KEY_BINDINGS['ENTER'] and key_hold_counter < HOLD_SHOW_LOUDNESS:
                        handle_enter_press(enter_display_at_press)

                    elif key == KEY_BINDINGS['PLAYPAUSE'] and key_hold_counter < HOLD_STOP:
                        await asyncio.to_thread(send_lms_command, "pause")

                    elif key in (KEY_BINDINGS['PREVIOUSSONG'], KEY_BINDINGS['NEXTSONG']) \
                            and key_hold_counter < HOLD_SEEK:
                        await asyncio.to_thread(
                            send_lms_command,
                            "playlist index -1" if key == KEY_BINDINGS['PREVIOUSSONG']
                            else "playlist index +1")

                    elif key in VOLUME_KEYS:
                        schedule_volume_release()   # flag cleared + redraw after VOLUME_PEEK_S

                    key_hold_counter = 0
                    last_seek_time = 0

        except OSError as e:
            log.warning("Remote disconnected: %s", e)
            cancel_volume_release()
            is_volume_key_held = False      # no release event will ever come
            return

        except Exception:
            # An error while handling a key must not stop the remote (or the script)
            log.exception("Remote handler error, continuing")
            await asyncio.sleep(0.2)


# =====================================================================
#  STARTUP - serial, CamillaDSP, GPIO, display, settings (do not reorder)
# =====================================================================

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

cdsp = CamillaClient(*CAMILLA_ADDR)

# Wait for CamillaDSP instead of crashing if it is not up yet. bootlocal.sh
# launches us in the background, so this loop never blocks the boot.
while True:
    try:
        cdsp.connect()
        cdsp.general.reload()
        break
    except Exception as e:
        log.warning("CamillaDSP not reachable (%s), waiting %g s",
                    e, CDSP_RECONNECT_DELAY_S)
        time.sleep(CDSP_RECONNECT_DELAY_S)

config_active = cdsp.config.active() or {}

h = None
if USE_GPIO:
    h = lgpio.gpiochip_open(0)
    if ENABLE_TV_RELAY:
        lgpio.gpio_claim_input(h, TV_RELAY_GPIO)    # inactive from the start (tv_relay_on = False)
    if ENABLE_AUTO_POWEROFF:
        lgpio.gpio_claim_output(h, POWER_GPIO, 0)   # LOW first: no LOW -> HIGH glitch
        time.sleep(1)                   # Keep the relay off so the A70 can re-arm
        lgpio.gpio_write(h, POWER_GPIO, 1)

tm = TM1637(clk=CLK, dio=DIO) if ENABLE_DISPLAY else NullDisplay()
clear_display()
tm.brightness(BRIGHTNESS_LEVELS[last_level])
settings = load_settings()
load_prev_values()
tone_preset_idx = settings["tone_preset_idx"]   # resume on the last preset used
tone_mode_idx = settings["tone_mode_idx"]       # and on the last keys mode used
if TONE_ACTIVE:
    init_tone()

if ENABLE_TV_RELAY:
    if current_config_key is None:          # init_tone did not run (TONE_ACTIVE False)
        _p = cdsp.config.file_path()
        if _p:
            current_config_key = config_key(_p)
    update_tv_relay()


# =====================================================================
#  SHUTDOWN / MAIN
# =====================================================================

def exit_gracefully(signum, frame):
    try:
        save_audio_settings(config_active)
        if ENABLE_AUTO_POWEROFF:
            lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
            time.sleep(0.5)
        release_tv_relay()
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
    if ENABLE_DISPLAY:
        display_volume_info()

    tasks = []

    def spawn(name, factory):
        tasks.append(asyncio.create_task(supervised(name, factory)))

    spawn("cdsp-poller", cdsp_poller)
    if ENABLE_AUTO_POWEROFF or ENABLE_DISPLAY or ENABLE_TOSLINK_SWITCH:
        spawn("silence", silence_monitor)
    if ENABLE_AUTO_POWEROFF:
        spawn("auto-poweroff", auto_poweroff)
    if ENABLE_DISPLAY:
        spawn("display", display_manager_loop)
        spawn("adc", adc_reader_loop)
        spawn("blink", blink_loop)
    if ENABLE_DISPLAY or ENABLE_REMOTE or ENABLE_TOSLINK_SWITCH or ENABLE_TV_RELAY:
        # Redraws and resyncs config_active after an external change (also
        # keeps the TV relay in step with an external config change)
        spawn("volume-watcher", volume_watcher_loop)
    if ENABLE_LMS_SYNC:
        spawn("lms-bridge", lms_bridge_task)
    if ENABLE_TOSLINK_SWITCH:
        spawn("tv-toslink", monitor_tv_toslink)
    if ENABLE_REMOTE:
        spawn("remote", remote_watcher)

    await asyncio.gather(*tasks)


# Everything above (serial, CamillaDSP, GPIO) already ran at import time:
# this script is meant to be run directly, not imported.
asyncio.run(main())
