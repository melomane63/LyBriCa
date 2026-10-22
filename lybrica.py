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
import struct
import sys
import time
from urllib.parse import quote, unquote
from camilladsp import CamillaClient


# ========================= FEATURES =========================
# Each block can be switched off. A disabled block is never started, and the
# hardware libraries it needs are neither imported nor required.

ENABLE_LMS_SYNC      = True   # LMS <-> CamillaDSP volume bridge (asyncio task)
ENABLE_AUTO_POWEROFF = True   # Power relay: off after silence, on with sound,
                              # halt after HALT_DELAY, relay toggle on POWER long press
ENABLE_REMOTE        = True   # Remote control (all keys: volume, mute, LMS, BACK,
                              # HOMEPAGE, POWER). If False, no key works at all.
ENABLE_DISPLAY       = True   # TM1637 display + ambient light sensor
                              # (the tone / tilt / loudness keys need it)
ENABLE_CONFIG_SWITCH = "toslink" # Auto config switching: False | "gpio" | "toslink"
                              #   False     : disabled
                              #   "gpio"    : TV GPIO (original logic)
                              #   "toslink" : audio detection on the TOSLINK input
                              # (BACK / HOMEPAGE keys do NOT depend on this)

if ENABLE_CONFIG_SWITCH not in (False, "gpio", "toslink"):
    raise ValueError(f"Invalid ENABLE_CONFIG_SWITCH: {ENABLE_CONFIG_SWITCH!r}")

# Derived from the switches above (do not edit)
SWITCH_GPIO    = ENABLE_CONFIG_SWITCH == "gpio"
SWITCH_TOSLINK = ENABLE_CONFIG_SWITCH == "toslink"
NEED_CDSP    = (ENABLE_DISPLAY or ENABLE_REMOTE or ENABLE_AUTO_POWEROFF
                or bool(ENABLE_CONFIG_SWITCH) or ENABLE_LMS_SYNC)
TONE_ACTIVE  = ENABLE_REMOTE or bool(ENABLE_CONFIG_SWITCH)   # tone is saved / restored per config
USE_GPIO     = ENABLE_AUTO_POWEROFF or SWITCH_GPIO           # TOSLINK detection needs no GPIO


if ENABLE_REMOTE:
    import evdev
if ENABLE_DISPLAY:
    import serial
    from tm1637_lgpio import TM1637
if USE_GPIO:
    import lgpio
if SWITCH_TOSLINK:
    import alsaaudio


logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("remote")


# ========================= SETTINGS =========================
# Everything you may want to change is here, grouped by topic.
# (The on/off switches are in the FEATURES block above.)


# ---------------------- Remote control ----------------------

REMOTE_NAME = "pCP BLE HID Keyboard"

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
    'BACK':          'KEY_BACK',         # Next DSP config (files prefixed with "_")
    'HOMEPAGE':      'KEY_HOMEPAGE',     # Next DSP config (files prefixed with "|")
}

# Hold durations, in key-repeat events
HOLD_TOGGLE_POWER = 2       # POWER: toggle the relay
HOLD_SHUTDOWN = 300         # POWER: shut the Raspberry Pi down
HOLD_STOP = 10              # PLAY/PAUSE: stop
HOLD_SEEK = 10              # NEXT/PREVIOUS: seeking starts
HOLD_SWITCH_TONE_TILT = 15  # ENTER: switch between the tone and tilt screens
SEEK_STEP_S = 10            # Seconds jumped at each seek
SEEK_INTERVAL_S = 0.5       # Seconds between two seeks while the key is held (10 s / 0.5 s = 20x speed)

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


# ---------------------- Power ----------------------

POWER_GPIO = 4              # GPIO controlling the power relay
POWER_OFF_DELAY = 2         # Minutes of silence before the power relay is switched off
HALT_DELAY = 48             # Hours of silence (or without remote) before the Raspberry Pi shuts down


# ---------------------- Tone and loudness ----------------------

TILT_DB_STEP = 2            # Real dB applied per displayed tilt step
DEFAULT_LOUDNESS_REF = -20  # Loudness level restored by ENTER when no previous value is saved

# Adjustment ranges (TONE_MAX_DB, PRESENCE_MAX_DB and TILT_MAX_STEPS: 9 at most, the display has 2 digits)
TONE_MAX_DB = 9             # Bass / treble: -9 .. +9 dB
PRESENCE_MAX_DB = 3         # Presence: -3 .. +3 dB
TILT_MAX_STEPS = 3          # Tilt: -3 .. +3 displayed steps (TILT_DB_STEP dB each)
LOUDNESS_MIN, LOUDNESS_MAX = -50, -10   # Loudness reference level range in dB (stay above -100, which means "off")
LOUDNESS_STEP = 5           # Loudness: dB per key press


# ---------------------- Display ----------------------

CLK, DIO = 23, 24           # TM1637 display pins
SCREEN_RETURN_DELAY = 30    # Seconds without key press before returning to the volume screen
POWER_MSG_DURATION = 1.5    # Seconds PW ON / PW OFF stays on the display

# Ambient light sensor (ADC) -> display brightness
SERIAL_PORT = "/dev/serial0"
BAUD_RATE = 115200
ADC_LEVELS = [80, 1000, 2500, 3500]
BRIGHTNESS_LEVELS = [0, 1, 2, 3, 7]
HYSTERESIS_RATIO = 0.03     # 3 % hysteresis between two brightness levels


# ---------------------- CamillaDSP and config switching ----------------------

CAMILLA_ADDR = ("127.0.0.1", 1234)

PCP_DIR = "/mnt/mmcblk0p2/tce"   # exists only on PiCorePlayer
BASE_DIR = PCP_DIR if os.path.isdir(PCP_DIR) else os.path.expanduser("~")
CONFIG_DIR = BASE_DIR + "/camilladsp/configs/"

POLL_INTERVAL_S = 0.25      # One read cycle of CamillaDSP (volume + mute)
TITLE_EVERY = 2             # The config title is read every N cycles (2 -> every 0.5 s)
CDSP_RECONNECT_DELAY_S = 2.0    # Seconds to wait before trying to reconnect to CamillaDSP after an error

# ENABLE_CONFIG_SWITCH = "gpio"
TV_GPIO = 12                # GPIO reading the TV state (0 = TV on)

# ENABLE_CONFIG_SWITCH = "toslink"
# NOTE: device / rate / channels are read at startup from the first "|*" YAML.
TOSLINK_ON_DB           = -40.0   # TOSLINK free: peak level >= threshold -> TV
LOOPBACK_ON_DB          = -50.0   # Loopback free: peak level >= threshold -> SQUEEZELITE

# ---------------------- Robustness ----------------------

TASK_RESTART_DELAY_S = 2.0  # Seconds before a background task that crashed is restarted


# ---------------------- Logitech Media Server ----------------------

LMS_ADDR = ("127.0.0.1", 9090)   # LMS CLI (the server runs on this Pi)
LMS_PLAYER_MANUAL = ""           # "" = auto (MAC of the default interface); or e.g. "dc:a6:32:3c:1c:21" (raw, lowercase)
LMS_SYNC_DEBUG = False           # Log every volume/mute change and the raw LMS notifications
LMS_COALESCE_S = 0.05            # Bursts of volume changes coming from LMS are merged: one write to CamillaDSP per this delay (0 = write immediately)
LMS_RESTORE_WINDOW_S = 0.5       # After an unmute, LMS may announce a wrong volume during this delay (seconds): the bridge corrects it
LMS_CONNECT_TIMEOUT_S = 5        # Timeout when connecting to the LMS CLI (seconds)
LMS_RETRY_S = 5                  # Seconds before the bridge restarts after an error


# ===================== INTERNAL (do not edit) =====================

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

CONFIG_GROUPS = ("_", "|")  # Config file prefixes: "_" = stereo (BACK key), "|" = TV (HOMEPAGE key)

# settings.json key -> (CamillaDSP filter, parameter)
TONE_PARAMS = {
    "bass_gain":     ("Bass", "gain"),
    "treble_gain":   ("Treble", "gain"),
    "tilt_gain":     ("Tilt", "gain"),
    "loudness_ref":  ("Loudness", "reference_level"),
    "presence_gain": ("Presence", "gain"),
}

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

# ============================================================================
# LMS <-> CAMILLADSP VOLUME BRIDGE (asyncio, no thread)
# Inspired by the original work by mbrennwa, entirely reworked:
#     https://github.com/mbrennwa/camilla_LMS_volume_sync
#
#   - it reads the values published by ONE reader (cdsp_poller) in a VolumeState,
#     instead of querying CamillaDSP itself;
#   - the LMS connection uses asyncio streams;
#   - bursts of volume changes coming from LMS are coalesced (at most one write
#     to CamillaDSP every `coalesce_s` seconds);
#   - CamillaDSP readings that started before our last write, or that are taken
#     while a coalesced write is pending, are ignored (otherwise a stale reading
#     could undo a change that was just received from LMS).
# ============================================================================

bridge_log = logging.getLogger("lms_bridge")


# ====================== VOLUME CURVE FUNCTIONS ======================
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


# ====================== SHARED STATE ======================

class VolumeState:
    """Last CamillaDSP reading, published by a single reader (cdsp_poller).

    `stamp` is the time (time.monotonic) at which the reading STARTED; it is
    used to discard stale readings."""

    def __init__(self):
        self.db = None
        self.muted = None
        self.title = None
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

    def publish(self, db, muted, title=None, stamp=None):
        self.db = db
        self.muted = bool(muted)
        if title:                      # an empty title (e.g. during a config reload) is ignored: the previous one is kept
            self.title = title
        self.stamp = time.monotonic() if stamp is None else stamp
        for ev in self._events:
            ev.set()


# ====================== THE BRIDGE ======================

class LmsBridge:
    def __init__(self, player, state, set_volume_db, set_mute, lms_addr, *,
                 coalesce_s, restore_window_s, connect_timeout_s,
                 debug=False, clock=time.monotonic):
        """
        player            : MAC address of the LMS player (raw format, lowercase)
        state             : VolumeState fed by the single CamillaDSP reader
        set_volume_db     : function(db) that writes the volume to CamillaDSP
        set_mute          : function(bool) that writes the mute to CamillaDSP
        lms_addr          : (host, port) of the LMS CLI
        coalesce_s        : delay used to coalesce bursts of volume changes
                            coming from LMS (0 = write immediately)
        restore_window_s  : after an unmute, LMS may announce a wrong volume
                            during this delay
        connect_timeout_s : timeout when connecting to the LMS CLI
        (the timings come from the SETTINGS block, see LMS_* constants)
        """
        self.player = player
        self.pq = quote(player, safe="")
        self.state = state
        self.set_volume_db = set_volume_db
        self.set_mute = set_mute
        self.lms_addr = lms_addr
        self.debug = debug
        self.coalesce_s = coalesce_s
        self.restore_window_s = restore_window_s
        self.connect_timeout_s = connect_timeout_s
        self.clock = clock
        player_re = re.escape(player)
        self._re_vol = re.compile(
            rf"{player_re} prefset server volume (-?\d+(?:\.\d+)?)$", re.I)
        self._re_mute = re.compile(
            rf"{player_re} prefset server mute ([01])$", re.I)
        self._writer = None
        self._reset()

    # ---------- internal state ----------

    def _reset(self):
        self.pct = 0
        self.muted = False
        self.restore_until = 0.0   # deadline (clock) of the post-unmute window
        self.prev = None           # previous CamillaDSP reading (a change must be seen twice)
        self._written_at = 0.0     # time (clock) of our last write to CamillaDSP
        self._pending_pct = None
        self._flush_handle = None

    def _say(self, message):
        bridge_log.info("[LMS sync] %s", message)

    def _send(self, cmd):
        w = self._writer
        if w is None or w.is_closing():
            raise ConnectionError("LMS connection closed")
        w.write((cmd + "\n").encode())

    # ---------- writes to CamillaDSP ----------

    def _cancel_flush(self):
        if self._flush_handle is not None:
            self._flush_handle.cancel()
        self._flush_handle = None
        self._pending_pct = None

    def _schedule_cdsp_volume(self, p):
        """Coalesce bursts: at most one write every coalesce_s seconds."""
        self._pending_pct = p
        self.prev = None   # a change already seen once is obsolete: we are about to write
        if self._flush_handle is not None:
            return
        if self.coalesce_s <= 0:
            self._flush()
            return
        loop = asyncio.get_running_loop()
        self._flush_handle = loop.call_later(self.coalesce_s, self._flush)

    def _flush(self):
        self._flush_handle = None
        p, self._pending_pct = self._pending_pct, None
        if p is None:
            return
        try:
            self.set_volume_db(pct_to_db(p))
        except Exception as e:
            self._say(f"cannot write volume to CamillaDSP: {e!r}")
        self._written_at = self.clock()

    def _apply_mute_to_camilla(self, new):
        try:
            self.set_mute(new)
        finally:
            self._written_at = self.clock()

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
        self.restore_until = 0.0 if new else self.clock() + self.restore_window_s

    def _on_lms_volume(self, p):
        if self.clock() < self.restore_until:
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
            self._schedule_cdsp_volume(p)

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
        if self.muted or self.clock() < self.restore_until:
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
            if self._flush_handle is not None:
                continue   # a coalesced write is pending: CamillaDSP still shows
                           # the old value, so skip this reading (nothing is
                           # sent back to LMS)
            self._on_camilla_reading(st.db, bool(st.muted))

    async def run_session(self, connect=None):
        """One bridge session. It only ends by raising an exception (connection
        lost...) or when cancelled."""
        if connect is None:
            async def connect(host, port):
                return await asyncio.open_connection(host, port, limit=1 << 20)
        self._reset()
        ev = self.state.subscribe()
        try:
            while self.state.db is None:       # wait for the first CamillaDSP reading
                await ev.wait()
                ev.clear()
            reader, writer = await asyncio.wait_for(
                connect(*self.lms_addr), timeout=self.connect_timeout_s)
            self._writer = writer
            self._say("Connected to LMS CLI")

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
            self._cancel_flush()
            w, self._writer = self._writer, None
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass

    async def run_forever(self, *, retry_s, connect=None):
        """Restart the bridge `retry_s` seconds after any error."""
        while True:
            try:
                await self.run_session(connect)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._say(f"Bridge error: {e!r}, restart in {retry_s:g}s")
            await asyncio.sleep(retry_s)


# ====================== GLOBAL VARIABLES ======================

key_hold_counter = 0            # Key-repeat events of the key being held (remote_events only)
screen_idle_ticks = 0           # Seconds since the last key event (display_manager_loop)
auto_power_enabled = True
is_waiting_for_sound = False
is_key_held = False
is_volume_key_held = False
last_displayed = None
blank_volume_when_mute = False
enter_display_at_press = None   # Screen shown when ENTER was pressed (to tell short/long press apart)

prev_values = {}                # ENTER-key restore values, same keys as TONE_PARAMS (filled by load_prev_values)
current_config_key = None       # Settings key of the active config file

adc_buffer = b""                # Incomplete serial data waiting for its newline

silent_now = False              # True when CamillaDSP reports silence (updated once per second by silence_monitor)

display_is_off = False          # True when the display has been blanked

display_refresh_event = asyncio.Event()

vol_state = VolumeState()       # Last volume / mute / title read from CamillaDSP (published by cdsp_poller)


# ====================== HELPERS ======================

class NullDisplay:
    """Stand-in for the TM1637 when ENABLE_DISPLAY is False: accepts every call
    and shows nothing, so the rest of the code does not have to care."""
    def encode_string(self, text):
        return bytearray(len(text))

    def write(self, segs):
        pass

    def brightness(self, level):
        pass


def swap(segs):
    """Reorder TM1637 segments to match the physical wiring of the display."""
    length = len(segs)
    if length == 4 or length == 5:
        segs.extend(bytearray([0] * (6 - length)))
    segs[0], segs[2] = segs[2], segs[0]
    if length >= 4:
        segs[3], segs[5] = segs[5], segs[3]
    return segs


def show(text):
    """Write a 6-character text on the display."""
    tm.write(swap(tm.encode_string(text)))


def clear_display():
    tm.write(tm.encode_string(" " * 6))


def blank_display():
    """Blank the display and remember it, so that a key or sound can wake it."""
    global display_is_off
    if not ENABLE_DISPLAY:
        return
    clear_display()
    display_is_off = True


def is_silent():
    """Silence detection: RMS threshold, then crest factor (peak vs RMS).
    A steady noise floor has peak ≈ RMS; real audio does not."""
    RMS_DB = -80.0        # RMS below this -> silence
    CREST_MIN_DB = 2.0    # crest (peak - RMS) below this -> steady noise, not audio

    rms = cdsp.levels.capture_rms()
    peak = cdsp.levels.capture_peak()
    rms_vals = list(rms.values()) if isinstance(rms, dict) else list(rms)
    peak_vals = list(peak.values()) if isinstance(peak, dict) else list(peak)
    if not rms_vals or not peak_vals:
        return True

    rms_db = max(float(v) for v in rms_vals)
    if rms_db <= RMS_DB:                            # below threshold: silence
        return True

    peak_db = max(float(v) for v in peak_vals)
    return (peak_db - rms_db) < CREST_MIN_DB        # steady noise: silence


def shutdown_system():
    """Cut the power relay (if managed) and halt the Raspberry Pi."""
    if ENABLE_AUTO_POWEROFF:
        lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
    show(" HALT ")
    os.system("sudo poweroff")


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

def get_config_display_label():
    """Return the 3-character label shown on the display for the active config.
    Prefer the config `title` field; fall back to the config file name.
    Config file prefixes ("_" and "|") are stripped in both cases."""
    title = cdsp.config.title()
    label = title.strip().replace("_", "").replace("|", "") if title else "---"
    return label[:3].ljust(3)


# ====================== INITIALISATION ======================

# Ambient light sensor: initial brightness level
ser = None
adc_value_init = 1500   # fallback if the sensor is absent (or the display is disabled)
if ENABLE_DISPLAY:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    try:
        adc_value_init = int(ser.readline().decode().strip())
    except ValueError:
        pass
    ser.timeout = 0   # non-blocking reads from here on

last_level = next(
    (i for i, t in enumerate(ADC_LEVELS) if adc_value_init < t),
    len(BRIGHTNESS_LEVELS) - 1,
)


def find_remote():
    """Return the remote input device, or None if it is not present."""
    for path in evdev.list_devices():
        device = evdev.InputDevice(path)
        if device.name == REMOTE_NAME:
            log.info("'%s' found at %s.", REMOTE_NAME, path)
            #device.grab()
            return device
    return None


remote = None


# CamillaDSP
cdsp = None
config_active = {}
if NEED_CDSP:
    cdsp = CamillaClient(*CAMILLA_ADDR)
    cdsp.connect()
    cdsp.general.reload()
    # Protection 1: never let config_active be None at startup
    config_active = cdsp.config.active() or {}

# GPIO
h = None
if USE_GPIO:
    h = lgpio.gpiochip_open(0)
    if ENABLE_AUTO_POWEROFF:
        lgpio.gpio_claim_output(h, POWER_GPIO, 0)   # Initial level LOW: no LOW -> HIGH glitch on the relay
        time.sleep(1)                   # Keep the relay off so the A70 can re-arm
        lgpio.gpio_write(h, POWER_GPIO, 1)
    if SWITCH_GPIO:
        lgpio.gpio_claim_input(h, TV_GPIO, lgpio.SET_PULL_UP)

# TM1637 display
tm = TM1637(clk=CLK, dio=DIO) if ENABLE_DISPLAY else NullDisplay()
clear_display()
tm.brightness(BRIGHTNESS_LEVELS[last_level])


# ====================== FILTER / SETTINGS ACCESS ======================

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
    """Store the tone of the active config, then write settings.json."""
    if not TONE_ACTIVE:
        return
    if current_config_key:
        entry = get_tone(config)
        entry["prev"] = dict(prev_values)
        settings["configs"][current_config_key] = entry
    settings["last_tone_tilt"] = last_tone_tilt
    write_settings()


def load_prev_values(saved=None):
    """Load the ENTER-key restore values saved for a config (defaults if none)."""
    saved_prev = saved.get("prev") if saved else None
    if not isinstance(saved_prev, dict):
        saved_prev = {}
    for key in TONE_PARAMS:
        default = DEFAULT_LOUDNESS_REF if key == "loudness_ref" else 0
        prev_values[key] = saved_prev.get(key, default)


def is_complete_config(cfg):
    """True si cfg a la section obligatoire 'devices' non vide.
    CamillaDSP rejette toute config sans 'devices'."""
    return (isinstance(cfg, dict)
            and isinstance(cfg.get("devices"), dict)
            and bool(cfg["devices"]))

def init_tone():
    """Apply the saved tone of the config CamillaDSP started with."""
    global current_config_key
    path = cdsp.config.file_path()
    if not path:
        return
    current_config_key = config_key(path)
    saved = settings["configs"].get(current_config_key)
    load_prev_values(saved)
    if saved and is_complete_config(config_active):
        filters = config_active.get("filters")
        apply_tone(filters if isinstance(filters, dict) else {}, saved)
        cdsp.config.set_active(config_active)


settings = load_settings()
last_tone_tilt = settings["last_tone_tilt"]
load_prev_values()
if TONE_ACTIVE:
    init_tone()


# ====================== DISPLAY ======================

def fmt_gain(value):
    """2-character value for the display; '--' if the filter is absent."""
    return "--" if value is None else f"{round(value):2}"


def display_volume_info(current_volume=None, is_muted=None):
    """Show the config label and the volume on the 0..100 scale.
    `current_volume` is the CamillaDSP main volume in dB."""
    global blank_volume_when_mute, last_displayed, display_is_off

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

    segs = tm.encode_string(volume_str)

    if any(get_filter_param(config_active, f, "gain") for f in GAIN_KEYS.values()):
        segs[3] |= 0x01    # top horizontal segment (A) on the 4th character: tone settings active

    loudness_ref = get_filter_param(config_active, "Loudness", "reference_level")
    if loudness_ref is not None and round(loudness_ref) != -100:
        segs[3] |= 0x08    # bottom horizontal segment (D) on the 4th character: loudness active

    if ENABLE_AUTO_POWEROFF and auto_power_enabled:
        segs[2] |= 0x80    # decimal point on the 3rd character: auto power-off active

    if ENABLE_AUTO_POWEROFF and lgpio.gpio_read(h, POWER_GPIO) == 1:
        segs[3] |= 0x80    # decimal point on the 4th character: power relay really ON

    tm.write(swap(segs))
    last_displayed = "volume"
    display_is_off = False


def display_tone_info():
    global last_displayed
    bass_gain = get_filter_param(config_active, "Bass", "gain")
    treble_gain = get_filter_param(config_active, "Treble", "gain")
    show(f"{fmt_gain(bass_gain)}B{fmt_gain(treble_gain)}T")
    last_displayed = "tone"


def display_loudness_info():
    global last_displayed
    loudness_ref = get_filter_param(config_active, "Loudness", "reference_level")
    if loudness_ref is None:
        value_str = "---"        # no Loudness filter in this config
    elif round(loudness_ref) == -100:
        value_str = " No"        # loudness switched off (-100)
    else:
        value_str = f"{round(loudness_ref)}"
    show(f"{value_str} Ld")
    last_displayed = "loudness"


def display_tilt_info():
    global last_displayed
    tilt_gain = get_filter_param(config_active, "Tilt", "gain")
    presence_gain = get_filter_param(config_active, "Presence", "gain")
    # displayed in steps, actual gain = step * TILT_DB_STEP dB
    tilt_step = None if tilt_gain is None else round(tilt_gain / TILT_DB_STEP)
    show(f"{fmt_gain(tilt_step)}T{fmt_gain(presence_gain)}P")
    last_displayed = "tilt"


# ====================== REMOTE KEY ACTIONS ======================

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


def send_lms_command(command):
    """Send a command to Logitech Media Server, identifying the player by the
    MAC address of the default network interface.
    Uses its own socket (no shared state), so it is safe to run in a thread."""
    try:
        mac = quote(detect_player_mac(), safe="")

        with socket.create_connection(LMS_ADDR, timeout=5) as sock:
            sock.sendall(f"{mac} {command}\r\nexit\r\n".encode("utf-8"))
            return sock.recv(4096).decode("utf-8").strip()

    except Exception as e:
        log.error("LMS command failed: %s", e)
        return None

# ========================== REMOTE =============================


async def remote_watcher():
    """Background task: detect and reconnect the remote without blocking boot."""
    global remote
    while ENABLE_REMOTE:
        if remote is None:
            remote = find_remote()
            if remote:
                log.info("'%s' connected successfully!", REMOTE_NAME)
                asyncio.create_task(supervised("remote", lambda: remote_events(remote)))
        await asyncio.sleep(5)


# ====================== DSP CONFIGURATION ======================

async def change_config(cdsp, config_pattern, cycle=True):
    """Switch config within the group of files matching the pattern.
    cycle=True : go to the next file of the group if the active config belongs to it.
    Otherwise (or if cycle=False): go to the last config used in this group.
    Each config keeps its own tone settings (bass, treble, tilt, loudness, presence).
    Returns the title of the new config if a switch was performed, None otherwise."""
    global config_active, current_config_key

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

    # Remember the tone of the config we are leaving (and write it to disk)
    save_audio_settings(config_active)

    # Protection 3: abort if the new config does not parse to a dict
    new_config_data = cdsp.config.read_and_parse_file(new_config_path)
    if not isinstance(new_config_data, dict):
        log.error("Config %s is invalid, aborting switch", new_config_path)
        return None

    filters = new_config_data.get('filters')
    if not isinstance(filters, dict):
        filters = {}
        new_config_data['filters'] = filters

    # Saved tone for this config; if none, the values from the .yml are kept
    new_key = config_key(new_config_path)
    saved = settings["configs"].get(new_key)
    if saved:
        apply_tone(filters, saved)

    # Make relative filter file names absolute
    config_dir = os.path.dirname(os.path.abspath(new_config_path))
    for f in filters.values():
        filename = f.get("parameters", {}).get("filename")
        if isinstance(filename, str) and filename and not os.path.isabs(filename):
            f["parameters"]["filename"] = os.path.normpath(os.path.join(config_dir, filename))

    # Protection 4: only update the local state if CamillaDSP accepted the config
    try:
        cdsp.config.set_active(new_config_data)
        cdsp.config.set_file_path(new_config_path)
    except Exception as e:
        log.error("Failed to apply config %s: %s", new_config_path, e)
        return None

    config_active = new_config_data
    current_config_key = new_key
    load_prev_values(saved)

    # Remember this config as the last used of its group (survives TV on/off and restarts)
    if group in CONFIG_GROUPS:
        settings["last_config"][group] = new_key
        write_settings()

    # Wait until CamillaDSP has finished loading the new config, so that
    # cdsp.config.title() returns the NEW title (or "" if it has none),
    # not the previous one.
    await asyncio.sleep(0.1)

    display_volume_info()
    return new_config_data.get("title")


# ====================== POWER ======================

def toggle_power():
    global auto_power_enabled, is_waiting_for_sound, last_displayed

    if not auto_power_enabled or is_waiting_for_sound:
        # Relay forced off (re-enable auto power-off), or switched off by
        # auto power-off (back on): either way, switch it on
        auto_power_enabled = True
        lgpio.gpio_write(h, POWER_GPIO, lgpio.HIGH)
        show("PW ON")
        is_waiting_for_sound = False

    else:
        # Force the relay off and disable auto power
        auto_power_enabled = False
        lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
        show("PW OFF")

    last_displayed = "power"


async def return_to_volume_later(delay=POWER_MSG_DURATION):
    """Go back to the volume screen shortly after PW ON / PW OFF, unless another
    screen has been shown in the meantime (a key was pressed)."""
    await asyncio.sleep(delay)
    if last_displayed == "power":
        display_volume_info()


# ====================== CAMILLADSP CONNECTION ======================

def reconnect_cdsp():
    """Close and reopen the main connection. It is the SAME `cdsp` object, so
    every existing call keeps working."""
    try:
        cdsp.disconnect()
    except Exception:
        pass
    cdsp.connect()


def resync_after_reconnect():
    """After a CamillaDSP restart: re-read the active config and re-apply the
    saved tone."""
    global config_active, current_config_key
    new_active = cdsp.config.active() or {}
    if isinstance(new_active, dict) and new_active:
        config_active = new_active
    path = cdsp.config.file_path()
    current_config_key = config_key(path) if path else None
    saved = settings["configs"].get(current_config_key) if current_config_key else None
    load_prev_values(saved)
    if TONE_ACTIVE and saved and is_complete_config(config_active):
        filters = config_active.get("filters")
        apply_tone(filters if isinstance(filters, dict) else {}, saved)
        cdsp.config.set_active(config_active)


# ====================== BACKGROUND TASKS ======================

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
    """Periodic reader of the CamillaDSP volume / mute / title (the TOSLINK
    monitor still reads the title by itself).
    Publishes into vol_state; volume_watcher_loop and the LMS bridge consume it.
    On error: automatic reconnection, the script keeps running."""
    n = 0
    failures = 0
    while True:
        try:
            t0 = time.monotonic()           # start of the read (stale-reading guard)
            db = cdsp.volume.main_volume()
            muted = bool(cdsp.volume.main_mute())
            title = cdsp.config.title() if n % TITLE_EVERY == 0 else None
            n += 1
            vol_state.publish(db, muted, title, stamp=t0)
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


async def lms_bridge_task():
    """Run the LMS <-> CamillaDSP volume bridge (see LmsBridge above)."""
    while True:
        try:
            player = LMS_PLAYER_MANUAL or detect_player_mac()
            bridge = LmsBridge(
                player, vol_state,
                set_volume_db=cdsp.volume.set_main_volume,
                set_mute=cdsp.volume.set_main_mute,
                lms_addr=LMS_ADDR, debug=LMS_SYNC_DEBUG,
                coalesce_s=LMS_COALESCE_S,
                restore_window_s=LMS_RESTORE_WINDOW_S,
                connect_timeout_s=LMS_CONNECT_TIMEOUT_S)
            await bridge.run_forever(retry_s=LMS_RETRY_S)   # restarts itself after any error
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("[LMS sync] cannot start (%s), retry in %s s", e, LMS_RETRY_S)
            await asyncio.sleep(LMS_RETRY_S)


async def silence_monitor():
    """Single reader of the CamillaDSP levels: refreshes `silent_now` once per
    second. auto_poweroff, display_manager_loop and monitor_tv_toslink read the
    variable instead of each querying CamillaDSP themselves.
    On a read error the previous value is kept (no power cut because of a glitch)."""
    global silent_now
    while True:
        try:
            silent_now = is_silent()
        except Exception as e:
            log.warning("Silence monitor: cannot read levels (%s)", e)
        await asyncio.sleep(1)


async def adc_reader_loop():
    """Read the ambient light sensor without ever blocking the event loop.
    Only the most recent complete line received since the last pass is used."""
    global adc_buffer
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


async def volume_watcher_loop():
    """Consume vol_state (no direct CamillaDSP reads any more): redraw the
    display when volume / mute / title change, and resync the local state when
    the config changes from outside, so that the script does not later
    overwrite the external config."""
    global config_active, current_config_key

    ev = vol_state.subscribe()
    while vol_state.db is None:
        await ev.wait()
        ev.clear()
    last_volume, last_mute, last_title = vol_state.db, vol_state.muted, vol_state.title
    last_warned_title = None

    while True:
        await ev.wait()
        ev.clear()
        current_volume, current_mute = vol_state.db, vol_state.muted
        current_title = vol_state.title

        # Empty titles are ignored by vol_state (CamillaDSP can briefly report "" during a reload)
        title_changed = bool(current_title) and current_title != last_title
        if current_title:
            last_title = current_title

        if title_changed:
            # Protection 2: never let config_active become None
            try:
                new_active = cdsp.config.active() or {}
            except Exception as e:
                log.warning("Volume watcher: could not fetch active config (%s)", e)
                new_active = None

            if isinstance(new_active, dict) and new_active:
                config_active = new_active
                path = cdsp.config.file_path()
                current_config_key = config_key(path) if path else None
                saved = settings["configs"].get(current_config_key) if current_config_key else None
                load_prev_values(saved)
                last_warned_title = None
                log.debug("Config title changed to '%s'", current_title)

            else:
                if current_title != last_warned_title:
                    log.warning("Config changed but active config is unusable; keeping previous (title=%r)", current_title)
                    last_warned_title = current_title

        if current_volume != last_volume or current_mute != last_mute or title_changed:
            last_volume = current_volume
            last_mute = current_mute
            # Redraw only if the volume (or PW ON/OFF) screen is shown and no volume key is held
            if last_displayed in ("volume", "power") and not is_volume_key_held:
                display_volume_info(current_volume, current_mute)


async def display_manager_loop():
    global screen_idle_ticks, is_key_held, is_volume_key_held
    idle_display_counter = 0

    while True:
        if is_volume_key_held:
            is_volume_key_held = False
            await asyncio.sleep(0.5)
            if last_displayed == "volume":
                display_volume_info()

        # Return to the volume screen after SCREEN_RETURN_DELAY seconds without key event
        if last_displayed != "volume":
            screen_idle_ticks += 1
            if screen_idle_ticks >= SCREEN_RETURN_DELAY:
                screen_idle_ticks = 0
                display_volume_info()
        else:
            screen_idle_ticks = 0

        if is_key_held:
            idle_display_counter = 0
            is_key_held = False

        # Blank the display after a long silence, wake it when sound returns
        if silent_now:
            if not display_is_off:
                idle_display_counter += 1
                if idle_display_counter >= POWER_OFF_DELAY * 60 + 5:  # +5 s: margin after the relay cut, so the 1.5 s PW OFF message stays visible
                    idle_display_counter = 0
                    blank_display()
        else:
            idle_display_counter = 0
            if display_is_off:
                display_volume_info()

        try:
            await asyncio.wait_for(display_refresh_event.wait(), timeout=1.0)
            display_refresh_event.clear()
        except asyncio.TimeoutError:
            pass

async def auto_poweroff():
    """Switch the power relay off after POWER_OFF_DELAY minutes of silence, back on
    when sound returns, and halt the Raspberry Pi after HALT_DELAY hours of silence."""
    global is_waiting_for_sound
    silence_counter = 0

    while True:
        await asyncio.sleep(1)

        if silent_now:
            silence_counter += 1
            if silence_counter == POWER_OFF_DELAY * 60 and not is_waiting_for_sound:
                if auto_power_enabled:
                    lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
                    show("PW OFF")
                    is_waiting_for_sound = True  # Wait for sound or POWER key to switch back on
                    await asyncio.sleep(1.5)     # how long PW OFF stays visible
                    if silent_now and is_waiting_for_sound:
                        blank_display()
                else:
                    blank_display()              # relay already forced off: just blank the display

            if silence_counter >= HALT_DELAY * 3600:
                shutdown_system()
                break

        else:
            silence_counter = 0
            if (auto_power_enabled and is_waiting_for_sound
                    and lgpio.gpio_read(h, POWER_GPIO) == 0):
                lgpio.gpio_write(h, POWER_GPIO, lgpio.HIGH)
                display_volume_info()
                is_waiting_for_sound = False
            elif not auto_power_enabled and lgpio.gpio_read(h, POWER_GPIO) == 1:
                lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)


# ---------------------- Config switching: GPIO mode ----------------------

async def monitor_tv_gpio():
    """Watch the TV GPIO; a state change is validated once it stays stable for 1 second."""
    last_state = lgpio.gpio_read(h, TV_GPIO)

    while True:
        current_state = lgpio.gpio_read(h, TV_GPIO)

        if current_state != last_state:
            await asyncio.sleep(1.0)
            if lgpio.gpio_read(h, TV_GPIO) == current_state:
                pattern = '|*' if current_state == 0 else '_*'  # 0 = TV on
                await change_config(cdsp, CONFIG_DIR + pattern, cycle=False)
                last_state = current_state

        await asyncio.sleep(0.05)

# --------------------- Config switching: TOSLINK mode ---------------------
# The prefix of the currently active config (_ or |) is the single source of
# truth, re-read every second from cdsp.config.file_path(). No internal state
# is maintained: manual BACK / HOMEPAGE presses are honoured automatically.
#
#   _XX : CamillaDSP captures the loopback -> TOSLINK is free.
#         TOSLINK peak level >= TOSLINK_ON_DB AND crest >= 2.0
#         for 2 consecutive readings
#           -> switch to |XX + STOP LMS
#
#   |XX : CamillaDSP captures TOSLINK -> loopback is free.
#         TOSLINK silent (silent_now == True) AND
#         loopback level >= LOOPBACK_ON_DB
#           -> switch to _XX

def read_toslink_capture_params():
    """Read device / rate / channels from the first '|*' YAML config.
    All '|*' configs share the same capture input, so the first one is enough.
    Returns a dict {'device', 'rate', 'channels'} or None on failure."""
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
        }
    except Exception as e:
        log.error("[TOSLINK] Cannot read %s: %s", files[0], e)
        return None


_CAMILLA_FMT_TO_ALSA = {
    "S16_LE":  (alsaaudio.PCM_FORMAT_S16_LE,  2),
    "S24_3LE": (alsaaudio.PCM_FORMAT_S24_3LE, 3),
    "S32_LE":  (alsaaudio.PCM_FORMAT_S32_LE,  4),
}


def read_loopback_capture_params():
    """Read device / rate / channels / format from the first '_*' YAML config.
    All '_*' configs share the same capture input (Squeezelite loopback),
    so the first one is enough.
    Returns a dict {'device', 'rate', 'channels', 'format', 'sample_bytes'}
    or None on failure."""
    files = sorted(glob.glob(CONFIG_DIR + "_*"))
    if not files:
        log.error("[LOOPBACK] No '_*' config file found in %s", CONFIG_DIR)
        return None
    try:
        data = cdsp.config.read_and_parse_file(files[0])
        if not isinstance(data, dict):
            return None
        dev = data.get("devices") or {}
        cap = dev.get("capture") or {}
        device = cap.get("device")
        if not isinstance(device, str) or not device:
            log.error("[LOOPBACK] No capture device in %s", files[0])
            return None
        fmt_name = (cap.get("format") or "S32_LE").upper()
        fmt, sb = _CAMILLA_FMT_TO_ALSA.get(
            fmt_name, (alsaaudio.PCM_FORMAT_S32_LE, 4))
        return {
            "device":       device,
            "rate":         int(dev.get("samplerate") or 48000),
            "channels":     int(cap.get("channels") or 2),
            "format":       fmt,
            "sample_bytes": sb,
        }
    except Exception as e:
        log.error("[LOOPBACK] Cannot read %s: %s", files[0], e)
        return None


def _toslink_probe(params):
    """Runs in a thread (blocks ~0.5 s).
    `params` is the dict returned by read_toslink_capture_params().
    Returns (peak_db, rms_db, err) if the TOSLINK is free,
    (None, None, err) if it is busy, (None, None, None) if no samples."""
    try:
        pcm = alsaaudio.PCM(
            type=alsaaudio.PCM_CAPTURE, mode=alsaaudio.PCM_NONBLOCK,
            device=params["device"], channels=params["channels"],
            rate=params["rate"], format=alsaaudio.PCM_FORMAT_S16_LE,
            periodsize=4096,
        )
    except Exception as e:
        return None, None, e

    sum_sq = 0
    count = 0
    peak = 0
    try:
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            length, data = pcm.read()
            if length > 0:
                samples = struct.unpack(f"<{len(data) // 2}h", data)
                for s in samples:
                    a = abs(s)
                    if a > peak:
                        peak = a
                    sum_sq += s * s
                count += len(samples)
            else:
                time.sleep(0.01)
    finally:
        pcm.close()

    if count == 0:
        return None, None, None
    peak_db = 20.0 * math.log10(peak / 32768.0) if peak > 0 else -1000.0
    rms     = math.sqrt(sum_sq / count) / 32768.0
    rms_db  = 20.0 * math.log10(rms) if rms > 0 else -1000.0
    return peak_db, rms_db, None


def _loopback_probe(params):
    """Runs in a thread (blocks ~0.5 s).
    `params` is the dict returned by read_loopback_capture_params().
    Returns (db, None) if the loopback is free, (None, error) if it is busy."""
    try:
        pcm = alsaaudio.PCM(
            type=alsaaudio.PCM_CAPTURE, mode=alsaaudio.PCM_NONBLOCK,
            device=params["device"], channels=params["channels"],
            rate=params["rate"], format=params["format"], periodsize=4096,
        )
    except Exception as e:
        return None, e

    peak = 0.0
    sb = params["sample_bytes"]
    scale = 2147483648.0 if sb == 4 else 32768.0
    fmt_char = "i" if sb == 4 else "h"
    try:
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            length, data = pcm.read()
            if length > 0:
                samples = struct.unpack(f"<{len(data) // sb}{fmt_char}", data)
                peak = max(peak, max((abs(s) for s in samples), default=0) / scale)
            else:
                time.sleep(0.01)
    finally:
        pcm.close()
    return (20.0 * math.log10(peak) if peak > 0 else -1000.0), None


async def monitor_tv_toslink():
    """TOSLINK <-> SQUEEZELITE auto switching.

    The config prefix (_ or |) is the single source of truth, re-read every
    second from cdsp.config.file_path(). Manual BACK / HOMEPAGE presses are
    therefore always honoured without any extra state.
    """
    loop = asyncio.get_running_loop()

    toslink_params = read_toslink_capture_params()
    if toslink_params is None:
        log.error("[TOSLINK] Cannot determine capture params, detection disabled")
        return
    log.info("[TOSLINK] Capture: %s @ %d Hz, %d ch",
             toslink_params["device"], toslink_params["rate"],
             toslink_params["channels"])

    loopback_params = read_loopback_capture_params()
    if loopback_params is None:
        log.error("[LOOPBACK] Cannot determine capture params, detection disabled")
        return
    log.info("[LOOPBACK] Capture: %s @ %d Hz, %d ch (%dB samples)",
             loopback_params["device"], loopback_params["rate"],
             loopback_params["channels"], loopback_params["sample_bytes"])

    toslink_hits = 0

    while True:
        await asyncio.sleep(1.0)
        try:
            if cdsp.general.state().name == "STARTING":
                continue

            path = cdsp.config.file_path() or ""
            if path.startswith(CONFIG_DIR + '|'):
                prefix = '|'
            elif path.startswith(CONFIG_DIR + '_'):
                prefix = '_'
            else:
                continue

            if prefix == '_':
                # _XX: TOSLINK is free -> probe it
                peak_db, rms_db, err = await loop.run_in_executor(
                    None, _toslink_probe, toslink_params)

                if err is not None or peak_db is None:
                    toslink_hits = 0
                    continue

                crest = peak_db - rms_db

                # Protection 1: peak ~= rms -> steady noise floor, ignore it
                if crest < 2.0:
                    toslink_hits = 0
                    continue

                if peak_db < TOSLINK_ON_DB:
                    toslink_hits = 0
                    continue

                # Protection 2: N consecutive readings above threshold
                toslink_hits += 1
                if toslink_hits >= 2:
                    log.info("[TOSLINK] TV confirmed: peak=%.1f rms=%.1f "
                             "crest=%.1f (%d hits) -> switch to |XX + STOP LMS",
                             peak_db, rms_db, crest, toslink_hits)
                    await change_config(cdsp, CONFIG_DIR + '|*', cycle=False)
                    await asyncio.to_thread(send_lms_command, "pause")
                    toslink_hits = 0
                    await asyncio.sleep(2.0)

            else:
                # |XX: TOSLINK captured by CamillaDSP, loopback is free
                toslink_hits = 0

                if not silent_now:
                    continue

                db, err = await loop.run_in_executor(
                    None, _loopback_probe, loopback_params)
                if err is None and db is not None and db >= LOOPBACK_ON_DB:
                    log.info("[LOOPBACK] Squeezelite %.1f dB + TOSLINK silent "
                             "-> switch to _XX", db)
                    await change_config(cdsp, CONFIG_DIR + '_*', cycle=False)
                    await asyncio.sleep(2.0)

        except Exception as e:
            log.warning("[TOSLINK] iteration failed: %s", e)


async def remote_events(device):
    global last_displayed, key_hold_counter, screen_idle_ticks, is_key_held, is_volume_key_held
    global last_tone_tilt, enter_display_at_press

    last_seek_time = 0
    volume_repeat_count = 0         # repeats since the last applied volume step (VOLUME_SLOWDOWN)
    swallowed_key = None            # key that only woke the display: ignored until released
    while True:
        try:
            async for event in device.async_read_loop():
                if event.type != evdev.ecodes.EV_KEY:
                    continue

                screen_idle_ticks = 0  # Any key event restarts the return-to-volume timer

                attrib = evdev.categorize(event)
                key = attrib.keycode
                volume_keys = (KEY_BINDINGS['VOLUMEDOWN'], KEY_BINDINGS['VOLUMEUP'])

                # Display off: the first key only wakes it up, and its repeat / release are ignored
                if attrib.keystate == 1 and display_is_off:
                    is_key_held = True
                    display_volume_info()
                    swallowed_key = key
                    continue
                if swallowed_key is not None and key == swallowed_key:
                    if attrib.keystate == 0:
                        swallowed_key = None
                    continue

                if attrib.keystate == 1:  # Key pressed
                    is_key_held = True
                    key_hold_counter = 0

                    if key in volume_keys:
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
                        is_muted = not cdsp.volume.main_mute()
                        cdsp.volume.set_main_mute(is_muted)
                        display_volume_info(cdsp.volume.main_volume(), is_muted)

                    elif ENABLE_DISPLAY and key in (KEY_BINDINGS['UP'], KEY_BINDINGS['DOWN'],
                                                    KEY_BINDINGS['RIGHT'], KEY_BINDINGS['LEFT']):
                        handle_arrow_keys(key)

                    elif key == KEY_BINDINGS['BACK']:
                        await change_config(cdsp, CONFIG_DIR + '_*')

                    elif key == KEY_BINDINGS['HOMEPAGE']:
                        await change_config(cdsp, CONFIG_DIR + '|*')

                    elif ENABLE_DISPLAY and key == KEY_BINDINGS['ENTER']:
                        # Remember the screen shown BEFORE any change, for the action on release
                        enter_display_at_press = last_displayed
                        if last_displayed == "volume":
                            display_loudness_info()

                elif attrib.keystate == 2:  # Key held down

                    if key in volume_keys:
                        is_volume_key_held = True
                        # One step every VOLUME_SLOWDOWN repeats of the remote
                        volume_repeat_count += 1
                        if volume_repeat_count >= VOLUME_SLOWDOWN[key]:
                            volume_repeat_count = 0
                            change_volume_from_key(key, cdsp.volume.main_volume())

                    elif key in (KEY_BINDINGS['PREVIOUSSONG'], KEY_BINDINGS['NEXTSONG']):
                        key_hold_counter += 1
                        current_time = time.time()
                        if key_hold_counter >= HOLD_SEEK and current_time - last_seek_time >= SEEK_INTERVAL_S:
                            sign = "+" if key == KEY_BINDINGS['NEXTSONG'] else "-"
                            await asyncio.to_thread(send_lms_command, f"time {sign}{SEEK_STEP_S}")
                            last_seek_time = current_time

                    elif key == KEY_BINDINGS['PLAYPAUSE']:
                        key_hold_counter += 1
                        if key_hold_counter == HOLD_STOP:
                            await asyncio.to_thread(send_lms_command, "stop")

                    elif key == KEY_BINDINGS['POWER']:
                        key_hold_counter += 1
                        if ENABLE_AUTO_POWEROFF and key_hold_counter == HOLD_TOGGLE_POWER:
                            toggle_power()
                            asyncio.create_task(return_to_volume_later())
                        elif key_hold_counter == HOLD_SHUTDOWN:
                            save_audio_settings(config_active)
                            shutdown_system()

                    elif key == KEY_BINDINGS['ENTER']:
                        key_hold_counter += 1
                        if key_hold_counter == HOLD_SWITCH_TONE_TILT and last_displayed in ("tone", "tilt"):
                            last_tone_tilt = "tilt" if last_displayed == "tone" else "tone"
                            (display_tilt_info if last_tone_tilt == "tilt" else display_tone_info)()

                elif attrib.keystate == 0:  # Key released

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
                        display_refresh_event.set()
                        volume_repeat_count = 0

                    key_hold_counter = 0
                    last_seek_time = 0

        except OSError as e:
            log.warning("Remote disconnected: %s. Attempting to reconnect...", e)
            device = None
            while device is None:
                device = find_remote()
                if device is None:
                    await asyncio.sleep(1)
            log.info("Remote reconnected.")

        except Exception:
            # An error while handling a key must not stop the remote (or the script)
            log.exception("Remote handler error, continuing")
            await asyncio.sleep(0.2)


# ====================== SHUTDOWN / MAIN ======================

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
    if SWITCH_GPIO:
        # Load the config matching the TV state at startup
        if lgpio.gpio_read(h, TV_GPIO) == 0:
            log.info("TV detected ON at startup: loading ON config")
            await change_config(cdsp, CONFIG_DIR + '|*', cycle=False)
        else:
            await change_config(cdsp, CONFIG_DIR + '_*', cycle=False)
    # TOSLINK mode: nothing to do at startup, the config already in place is
    # kept (its prefix _ or | drives the very first iteration of
    # monitor_tv_toslink).
    if ENABLE_DISPLAY:
        display_volume_info()  # change_config returns early when the config is already active

    tasks = []

    def spawn(name, factory):
        tasks.append(asyncio.create_task(supervised(name, factory)))

    if ENABLE_DISPLAY or ENABLE_REMOTE or ENABLE_CONFIG_SWITCH or ENABLE_LMS_SYNC:
        # The periodic reader of volume / mute / title, shared through vol_state
        spawn("cdsp-poller", cdsp_poller)
    if ENABLE_AUTO_POWEROFF or ENABLE_DISPLAY or SWITCH_TOSLINK:
        # Single reader of the CamillaDSP levels, shared through `silent_now`
        spawn("silence", silence_monitor)
    if ENABLE_AUTO_POWEROFF:
        spawn("auto-poweroff", auto_poweroff)
    if ENABLE_DISPLAY:
        spawn("display", display_manager_loop)
        spawn("adc", adc_reader_loop)
    if ENABLE_DISPLAY or ENABLE_REMOTE or ENABLE_CONFIG_SWITCH:
        # Redraws the display and resyncs config_active after an external change
        spawn("volume-watcher", volume_watcher_loop)
    if ENABLE_LMS_SYNC:
        spawn("lms-bridge", lms_bridge_task)
    if SWITCH_GPIO:
        spawn("tv-gpio", monitor_tv_gpio)
    elif SWITCH_TOSLINK:
        spawn("tv-toslink", monitor_tv_toslink)

    if ENABLE_REMOTE:
        tasks.append(asyncio.create_task(remote_watcher()))

    if tasks:
        await asyncio.gather(*tasks)
    else:
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
