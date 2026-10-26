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
from urllib.parse import quote, unquote
from camilladsp import CamillaClient


# ========================= FEATURES =========================

ENABLE_LMS_SYNC       = True
ENABLE_AUTO_POWEROFF  = True
ENABLE_REMOTE         = True
ENABLE_DISPLAY        = True
ENABLE_TOSLINK_SWITCH = True

if not (ENABLE_DISPLAY or ENABLE_REMOTE or ENABLE_AUTO_POWEROFF
        or ENABLE_TOSLINK_SWITCH or ENABLE_LMS_SYNC):
    raise ValueError("No feature enabled: turn on at least one option in the FEATURES block")

TONE_ACTIVE = ENABLE_REMOTE or ENABLE_TOSLINK_SWITCH
USE_GPIO    = ENABLE_AUTO_POWEROFF


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
        warnings.simplefilter("ignore")   # DeprecationWarning en Python 3.11/3.12
        import audioop                    # Python >= 3.13: pip/apt install audioop-lts


logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("remote")

PCP_DIR = "/mnt/mmcblk0p2/tce"


# ========================= SETTINGS =========================

CAMILLA_ADDR = ("127.0.0.1", 1234)

POLL_INTERVAL_S = 0.25
TITLE_EVERY = 2
CDSP_RECONNECT_DELAY_S = 2.0


LMS_ADDR = ("127.0.0.1", 9090)
LMS_PLAYER_MANUAL = ""
LMS_SYNC_DEBUG = False

LMS_COALESCE_S = 0.05
LMS_RESTORE_WINDOW_S = 0.5
LMS_CONNECT_TIMEOUT_S = 5
LMS_RETRY_S = 5


POWER_GPIO = 4
POWER_OFF_DELAY = 2
HALT_DELAY = 48


TOSLINK_ON_DB  = -40.0
TOSLINK_LOOP_S = 0.5            # cadence de la boucle de détection (mode _)
TOSLINK_TV_LOOP_S = 1.0         # cadence en mode | (une requête LMS par cycle)
SILENT_TICKS_TO_LOOPBACK = 2    # debounce du retour |XX -> _XX (~2 s)
SWITCH_RETRY_BASE_S = 5.0       # délai après un échec de bascule, doublé à chaque échec
SWITCH_RETRY_MAX_S = 60.0


CLK, DIO = 23, 24
SCREEN_RETURN_DELAY = 30
POWER_MSG_DURATION = 1.5

SERIAL_PORT = "/dev/serial0"
BAUD_RATE = 115200
ADC_LEVELS = [80, 1000, 2500, 3500]
BRIGHTNESS_LEVELS = [0, 1, 2, 3, 7]
HYSTERESIS_RATIO = 0.03


REMOTE_NAME = "HID Remote01 Keyboard"
REMOTE_NAME_PCP = "pCP BLE HID Keyboard"
if os.path.isdir(PCP_DIR):
    REMOTE_NAME = REMOTE_NAME_PCP

KEY_BINDINGS = {
    'VOLUMEDOWN':    'KEY_VOLUMEDOWN',
    'VOLUMEUP':      'KEY_VOLUMEUP',
    'MUTE':          'KEY_MUTE',
    'PLAYPAUSE':     'KEY_PLAYPAUSE',
    'PREVIOUSSONG':  'KEY_PREVIOUSSONG',
    'NEXTSONG':      'KEY_NEXTSONG',
    'UP':            'KEY_UP',
    'DOWN':          'KEY_DOWN',
    'LEFT':          'KEY_LEFT',
    'RIGHT':         'KEY_RIGHT',
    'POWER':         'KEY_POWER',
    'ENTER':         'KEY_ENTER',
    'BACK':          'KEY_BACK',
    'HOMEPAGE':      'KEY_HOMEPAGE',
}

HOLD_TOGGLE_POWER = 2
HOLD_SHUTDOWN = 300
HOLD_STOP = 10
HOLD_SEEK = 10
HOLD_SWITCH_TONE_TILT = 15
SEEK_STEP_S = 10
SEEK_INTERVAL_S = 0.5

VOLUME_SLOWDOWN = {
    KEY_BINDINGS['VOLUMEUP']:   2,
    KEY_BINDINGS['VOLUMEDOWN']: 1,
}


TONE_MAX_DB = 9
PRESENCE_MAX_DB = 3
TILT_MAX_STEPS = 3
TILT_DB_STEP = 2
LOUDNESS_MIN, LOUDNESS_MAX = -50, -10
LOUDNESS_STEP = 5
DEFAULT_LOUDNESS_REF = -20


# ===================== INTERNAL =====================

TASK_RESTART_DELAY_S = 2.0

BASE_DIR   = PCP_DIR if os.path.isdir(PCP_DIR) else os.path.expanduser("~")
CONFIG_DIR = BASE_DIR + "/camilladsp/configs/"

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

CONFIG_GROUPS = ("_", "|")

TONE_PARAMS = {
    "bass_gain":     ("Bass", "gain"),
    "treble_gain":   ("Treble", "gain"),
    "tilt_gain":     ("Tilt", "gain"),
    "loudness_ref":  ("Loudness", "reference_level"),
    "presence_gain": ("Presence", "gain"),
}

GAIN_KEYS = {
    "bass_gain":     "Bass",
    "treble_gain":   "Treble",
    "tilt_gain":     "Tilt",
    "presence_gain": "Presence",
}

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

bridge_log = logging.getLogger("lms_bridge")


# ====================== VOLUME CURVE ======================

def clamp(value, low, high):
    return max(low, min(high, value))


def pct_to_db(p):
    p = clamp(p, 0, 100)
    if p >= 25:
        return (p - 100) * 0.5
    if p >= 2:
        return -71.0 + (p - 2) * 1.5
    return -75.0 if p == 1 else -100.0


def db_to_pct(d):
    if d >= -37.5:
        return clamp(round(100 + d * 2), 25, 100)
    if d >= -71.0:
        return clamp(round((d + 71.0) / 1.5) + 2, 2, 24)
    return min((0, 1, 2), key=lambda i: abs(pct_to_db(i) - d))


# ====================== SHARED STATE ======================

class VolumeState:
    def __init__(self):
        self.db = None
        self.muted = None
        self.title = None
        self.config_path = None
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

    def publish(self, db, muted, title=None, config_path=None, stamp=None):
        self.db = db
        self.muted = bool(muted)
        if title:
            self.title = title
        if config_path:
            self.config_path = config_path
        self.stamp = time.monotonic() if stamp is None else stamp
        for ev in self._events:
            ev.set()


# ====================== THE BRIDGE ======================

class LmsBridge:
    def __init__(self, player, state, set_volume_db, set_mute, lms_addr, *,
                 coalesce_s, restore_window_s, connect_timeout_s,
                 debug=False, clock=time.monotonic):
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

    def _reset(self):
        self.pct = 0
        self.muted = False
        self.restore_until = 0.0
        self.prev = None
        self._written_at = 0.0
        self._pending_pct = None
        self._flush_handle = None

    def _say(self, message):
        bridge_log.info("[LMS sync] %s", message)

    def _send(self, cmd):
        w = self._writer
        if w is None or w.is_closing():
            raise ConnectionError("LMS connection closed")
        w.write((cmd + "\n").encode())

    def _cancel_flush(self):
        if self._flush_handle is not None:
            self._flush_handle.cancel()
        self._flush_handle = None
        self._pending_pct = None

    def _schedule_cdsp_volume(self, p):
        self._pending_pct = p
        self.prev = None
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

    def _set_mute(self, new, origin, apply):
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
        if p != self.pct:
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
        if p != self.pct and p == self.prev:
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
                continue
            if self._flush_handle is not None:
                continue
            self._on_camilla_reading(st.db, bool(st.muted))

    async def run_session(self, connect=None):
        if connect is None:
            async def connect(host, port):
                return await asyncio.open_connection(host, port, limit=1 << 20)
        self._reset()
        ev = self.state.subscribe()
        try:
            while self.state.db is None:
                await ev.wait()
                ev.clear()
            reader, writer = await asyncio.wait_for(
                connect(*self.lms_addr), timeout=self.connect_timeout_s)
            self._writer = writer
            self._say("Connected to LMS CLI")

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
            self._cancel_flush()
            w, self._writer = self._writer, None
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass

    async def run_forever(self, *, retry_s, connect=None):
        while True:
            try:
                await self.run_session(connect)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._say(f"Bridge error: {e!r}, restart in {retry_s:g}s")
            await asyncio.sleep(retry_s)


# ====================== GLOBAL VARIABLES ======================

key_hold_counter = 0
screen_idle_ticks = 0
auto_power_enabled = True
is_waiting_for_sound = False
is_key_held = False
is_volume_key_held = False
last_displayed = None
blank_volume_when_mute = False
enter_display_at_press = None

prev_values = {}
current_config_key = None

adc_buffer = b""

silent_now = False

display_is_off = False

display_refresh_event = asyncio.Event()

vol_state = VolumeState()

current_prefix = '_'


# ====================== HELPERS ======================

class NullDisplay:
    def encode_string(self, text):
        return bytearray(len(text))

    def write(self, segs):
        pass

    def brightness(self, level):
        pass


def swap(segs):
    length = len(segs)
    if length == 4 or length == 5:
        segs.extend(bytearray([0] * (6 - length)))
    segs[0], segs[2] = segs[2], segs[0]
    if length >= 4:
        segs[3], segs[5] = segs[5], segs[3]
    return segs


def show(text):
    tm.write(swap(tm.encode_string(text)))


def clear_display():
    tm.write(tm.encode_string(" " * 6))


def blank_display():
    global display_is_off
    if not ENABLE_DISPLAY:
        return
    clear_display()
    display_is_off = True


def is_silent():
    RMS_DB = -80.0
    CREST_MIN_DB = 2.0

    rms = cdsp.levels.capture_rms()
    peak = cdsp.levels.capture_peak()
    rms_vals = list(rms.values()) if isinstance(rms, dict) else list(rms)
    peak_vals = list(peak.values()) if isinstance(peak, dict) else list(peak)
    if not rms_vals or not peak_vals:
        return True

    rms_db = max(float(v) for v in rms_vals)
    if rms_db <= RMS_DB:
        return True

    peak_db = max(float(v) for v in peak_vals)
    return (peak_db - rms_db) < CREST_MIN_DB


def shutdown_system():
    if ENABLE_AUTO_POWEROFF:
        lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
    show(" HALT ")
    os.system("sudo poweroff")


def adc_to_brightness(adc_value):
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


@functools.lru_cache(maxsize=None)
def detect_player_mac():
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


def get_config_display_label():
    title = cdsp.config.title()
    label = title.strip().replace("_", "").replace("|", "") if title else "---"
    return label[:3].ljust(3)


# ====================== INITIALISATION ======================

ser = None
adc_value_init = 1500
if ENABLE_DISPLAY:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    try:
        adc_value_init = int(ser.readline().decode().strip())
    except ValueError:
        pass
    ser.timeout = 0

last_level = next(
    (i for i, t in enumerate(ADC_LEVELS) if adc_value_init < t),
    len(BRIGHTNESS_LEVELS) - 1,
)


def find_remote():
    for path in evdev.list_devices():
        try:
            device = evdev.InputDevice(path)
        except OSError:
            continue
        if device.name != REMOTE_NAME:
            device.close()
            continue
        try:
            device.grab()
        except OSError as e:
            log.warning("Cannot grab '%s' (%s), will retry", REMOTE_NAME, e)
            device.close()
            continue
        log.info("'%s' found at %s.", REMOTE_NAME, path)
        return device
    return None


cdsp = CamillaClient(*CAMILLA_ADDR)
config_active = {}
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
    lgpio.gpio_claim_output(h, POWER_GPIO, 0)
    time.sleep(1)
    lgpio.gpio_write(h, POWER_GPIO, 1)

tm = TM1637(clk=CLK, dio=DIO) if ENABLE_DISPLAY else NullDisplay()
clear_display()
tm.brightness(BRIGHTNESS_LEVELS[last_level])


# ====================== FILTER / SETTINGS ACCESS ======================

def apply_filter_param(filters, filt, param, value):
    f = filters.get(filt)
    if not isinstance(f, dict):
        return
    params = f.get("parameters")
    if not isinstance(params, dict):
        return
    params[param] = value


def active_filter_names(config):
    names = set()
    for step in config.get("pipeline") or []:
        if isinstance(step, dict) and step.get("type") == "Filter" and not step.get("bypassed"):
            step_names = step.get("names")
            if isinstance(step_names, list):
                names.update(n for n in step_names if isinstance(n, str))
    return names


def get_filter_params(config, filt):
    if filt not in active_filter_names(config):
        return None
    f = (config.get("filters") or {}).get(filt)
    params = f.get("parameters") if isinstance(f, dict) else None
    return params if isinstance(params, dict) else None


def get_filter_param(config, filt, param):
    params = get_filter_params(config, filt)
    return params.get(param) if params is not None else None


def config_key(path):
    return os.path.splitext(os.path.basename(path))[0]


def get_tone(config):
    tone = {}
    for key, (filt, param) in TONE_PARAMS.items():
        value = get_filter_param(config, filt, param)
        if value is not None:
            tone[key] = value
    return tone


def apply_tone(filters, tone):
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
    tmp_path = SETTINGS_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(settings, f, indent=4, ensure_ascii=False)
    os.replace(tmp_path, SETTINGS_PATH)


def save_audio_settings(config):
    if not TONE_ACTIVE:
        return
    if current_config_key:
        entry = get_tone(config)
        entry["prev"] = dict(prev_values)
        settings["configs"][current_config_key] = entry
    settings["last_tone_tilt"] = last_tone_tilt
    write_settings()


def load_prev_values(saved=None):
    saved_prev = saved.get("prev") if saved else None
    if not isinstance(saved_prev, dict):
        saved_prev = {}
    for key in TONE_PARAMS:
        default = DEFAULT_LOUDNESS_REF if key == "loudness_ref" else 0
        prev_values[key] = saved_prev.get(key, default)


def is_complete_config(cfg):
    return (isinstance(cfg, dict)
            and isinstance(cfg.get("devices"), dict)
            and bool(cfg["devices"]))


def init_tone():
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
    return "--" if value is None else f"{round(value):2}"


def display_volume_info(current_volume=None, is_muted=None):
    global blank_volume_when_mute, last_displayed, display_is_off

    if current_volume is None:
        current_volume = cdsp.volume.main_volume()
    if is_muted is None:
        is_muted = cdsp.volume.main_mute()

    config_path = get_config_display_label()
    pct = db_to_pct(current_volume)

    if is_muted and not is_volume_key_held:
        blank_volume_when_mute, volume_str = True, f"{config_path} --"
    else:
        shown = "HI" if pct >= 100 else f"{pct:02}"
        blank_volume_when_mute, volume_str = False, f"{config_path} {shown}"

    segs = tm.encode_string(volume_str)

    if any(get_filter_param(config_active, f, "gain") for f in GAIN_KEYS.values()):
        segs[3] |= 0x01

    loudness_ref = get_filter_param(config_active, "Loudness", "reference_level")
    if loudness_ref is not None and round(loudness_ref) != -100:
        segs[3] |= 0x08

    if ENABLE_AUTO_POWEROFF and auto_power_enabled:
        segs[2] |= 0x80

    if ENABLE_AUTO_POWEROFF and lgpio.gpio_read(h, POWER_GPIO) == 1:
        segs[3] |= 0x80

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
        value_str = "---"
    elif round(loudness_ref) == -100:
        value_str = " No"
    else:
        value_str = f"{round(loudness_ref)}"
    show(f"{value_str} Ld")
    last_displayed = "loudness"


def display_tilt_info():
    global last_displayed
    tilt_gain = get_filter_param(config_active, "Tilt", "gain")
    presence_gain = get_filter_param(config_active, "Presence", "gain")
    tilt_step = None if tilt_gain is None else round(tilt_gain / TILT_DB_STEP)
    show(f"{fmt_gain(tilt_step)}T{fmt_gain(presence_gain)}P")
    last_displayed = "tilt"


# ====================== REMOTE KEY ACTIONS ======================

def change_volume_from_key(key, current_volume, volume_step=1):
    pct = db_to_pct(current_volume)
    delta = -volume_step if key == KEY_BINDINGS['VOLUMEDOWN'] else volume_step
    new_pct = clamp(pct + delta, 0, 100)

    if new_pct != pct:
        new_volume = pct_to_db(new_pct)
        cdsp.volume.set_main_volume(new_volume)
        display_volume_info(new_volume)


def handle_arrow_keys(key):
    up, down = KEY_BINDINGS['UP'], KEY_BINDINGS['DOWN']

    if last_displayed == "loudness":
        loudness_params = get_filter_params(config_active, "Loudness")
        if loudness_params is not None and key in (up, down):
            ref_level = loudness_params.get('reference_level', 0) + (LOUDNESS_STEP if key == up else -LOUDNESS_STEP)
            loudness_params['reference_level'] = clamp(ref_level, LOUDNESS_MIN, LOUDNESS_MAX)
            cdsp.config.set_active(config_active)
            display_loudness_info()

    elif last_displayed in ARROW_ADJUST:
        filt, step, limit = ARROW_ADJUST[last_displayed][key]
        params = get_filter_params(config_active, filt)
        if params is not None:
            params['gain'] = clamp(params.get('gain', 0) + step, -limit, limit)
            cdsp.config.set_active(config_active)
            (display_tilt_info if last_displayed == "tilt" else display_tone_info)()

    else:
        if last_tone_tilt == "tilt":
            display_tilt_info()
        else:
            display_tone_info()


def handle_enter_press(screen):
    if screen in ("tone", "tilt"):
        params = {key: get_filter_params(config_active, filt) for key, filt in GAIN_KEYS.items()}
        existing = {key: p for key, p in params.items() if p is not None}
        if not existing:
            return

        if all(p.get('gain', 0) == 0 for p in existing.values()):
            for key, p in existing.items():
                p['gain'] = prev_values[key]
            prev_values.update(dict.fromkeys(GAIN_KEYS, 0))
        else:
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
            if loudness_params.get('reference_level') == -100:
                loudness_params['reference_level'] = prev_values["loudness_ref"]
                prev_values["loudness_ref"] = -100
            else:
                prev_values["loudness_ref"] = loudness_params.get('reference_level', DEFAULT_LOUDNESS_REF)
                loudness_params['reference_level'] = -100
            cdsp.config.set_active(config_active)
            display_loudness_info()


def send_lms_command(command, quiet=False):
    try:
        mac = quote(detect_player_mac(), safe="")
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
    """True si LMS est en mode 'play' pour ce player.
    Appelée en boucle: ne logue que les changements d'état (injoignable / revenu)."""
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


# ========================== REMOTE =============================

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


# ====================== DSP CONFIGURATION ======================

async def change_config(cdsp, config_pattern, cycle=True):
    global config_active, current_config_key, current_prefix

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

    save_audio_settings(config_active)

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
    saved = settings["configs"].get(new_key)
    if saved:
        apply_tone(filters, saved)

    config_dir = os.path.dirname(os.path.abspath(new_config_path))
    for f in filters.values():
        params = f.get("parameters") if isinstance(f, dict) else None
        filename = params.get("filename") if isinstance(params, dict) else None
        if isinstance(filename, str) and filename and not os.path.isabs(filename):
            params["filename"] = os.path.normpath(os.path.join(config_dir, filename))

    try:
        cdsp.config.set_active(new_config_data)
        cdsp.config.set_file_path(new_config_path)
    except Exception as e:
        log.error("Failed to apply config %s: %s", new_config_path, e)
        return None

    config_active = new_config_data
    current_config_key = new_key

    if config_pattern.startswith(CONFIG_DIR) and len(config_pattern) > len(CONFIG_DIR):
        current_prefix = config_pattern[len(CONFIG_DIR)]

    load_prev_values(saved)

    if group in CONFIG_GROUPS:
        settings["last_config"][group] = new_key
        write_settings()

    vol_state.publish(
        vol_state.db, vol_state.muted,
        title=new_config_data.get("title"),
        config_path=new_config_path,
    )

    await asyncio.sleep(0.1)

    display_volume_info()
    return new_config_data.get("title")


# ====================== POWER ======================

def toggle_power():
    global auto_power_enabled, is_waiting_for_sound, last_displayed

    if not auto_power_enabled or is_waiting_for_sound:
        auto_power_enabled = True
        lgpio.gpio_write(h, POWER_GPIO, lgpio.HIGH)
        show("PW ON")
        is_waiting_for_sound = False
    else:
        auto_power_enabled = False
        lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
        show("PW OFF")

    last_displayed = "power"


async def return_to_volume_later(delay=POWER_MSG_DURATION):
    await asyncio.sleep(delay)
    if last_displayed == "power":
        display_volume_info()


# ====================== CAMILLADSP CONNECTION ======================

def reconnect_cdsp():
    try:
        cdsp.disconnect()
    except Exception:
        pass
    cdsp.connect()


def resync_after_reconnect():
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
    n = 0
    failures = 0
    while True:
        try:
            t0 = time.monotonic()
            db = cdsp.volume.main_volume()
            muted = bool(cdsp.volume.main_mute())
            title = path = None
            if n % TITLE_EVERY == 0:
                title = cdsp.config.title()
                path = cdsp.config.file_path()
            n += 1
            vol_state.publish(db, muted, title, config_path=path, stamp=t0)
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
            await bridge.run_forever(retry_s=LMS_RETRY_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("[LMS sync] cannot start (%s), retry in %s s", e, LMS_RETRY_S)
            await asyncio.sleep(LMS_RETRY_S)


async def silence_monitor():
    global silent_now
    while True:
        try:
            silent_now = is_silent()
        except Exception as e:
            log.warning("Silence monitor: cannot read levels (%s)", e)
        await asyncio.sleep(1)


async def adc_reader_loop():
    global adc_buffer
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


async def volume_watcher_loop():
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

        title_changed = bool(current_title) and current_title != last_title
        if current_title:
            last_title = current_title

        if title_changed:
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

        if silent_now:
            if not display_is_off:
                idle_display_counter += 1
                if idle_display_counter >= POWER_OFF_DELAY * 60 + 5:
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
                    is_waiting_for_sound = True
                    await asyncio.sleep(1.5)
                    if silent_now and is_waiting_for_sound:
                        blank_display()
                else:
                    blank_display()

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


# --------------------- Config switching: TOSLINK mode ---------------------

def read_toslink_capture_params():
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


def _levels(data):
    """Retourne (peak_db, rms_db) pour des échantillons S16_LE, ou (None, None)."""
    data = data[: len(data) // 2 * 2]
    if not data:
        return None, None
    peak = audioop.max(data, 2)
    rms = audioop.rms(data, 2)
    peak_db = 20.0 * math.log10(peak / 32768.0) if peak > 0 else -1000.0
    rms_db = 20.0 * math.log10(rms / 32768.0) if rms > 0 else -1000.0
    return peak_db, rms_db


def _toslink_probe(params):
    try:
        pcm = alsaaudio.PCM(
            type=alsaaudio.PCM_CAPTURE, mode=alsaaudio.PCM_NONBLOCK,
            device=params["device"], channels=params["channels"],
            rate=params["rate"], format=alsaaudio.PCM_FORMAT_S16_LE,
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

    peak_db, rms_db = _levels(b"".join(chunks))
    return peak_db, rms_db, None


def _prefix_of(path):
    return '|' if os.path.basename(path or "").startswith('|') else '_'


async def monitor_tv_toslink():
    global current_prefix
    loop = asyncio.get_running_loop()

    toslink_params = read_toslink_capture_params()
    if toslink_params is None:
        log.error("[TOSLINK] Cannot determine capture params, detection disabled")
        return
    log.info("[TOSLINK] Capture: %s @ %d Hz, %d ch",
             toslink_params["device"], toslink_params["rate"],
             toslink_params["channels"])

    if not glob.glob(CONFIG_DIR + "_*"):
        log.error("[LOOPBACK] No '_*' config file found in %s", CONFIG_DIR)
        return
    log.info("[LOOPBACK] Configs '_*' found in %s", CONFIG_DIR)

    path = cdsp.config.file_path() or ""
    current_prefix = _prefix_of(path)
    last_config_path = path
    toslink_hits = 0
    silent_ticks = 0
    switch_failures = 0
    last_err = None

    async def try_switch(pattern, target_prefix):
        """Bascule vers le groupe demandé; backoff exponentiel en cas d'échec."""
        nonlocal last_config_path, switch_failures
        global current_prefix
        await change_config(cdsp, pattern, cycle=False)
        last_config_path = cdsp.config.file_path() or ""
        current_prefix = _prefix_of(last_config_path)
        if current_prefix == target_prefix:
            switch_failures = 0
            await asyncio.sleep(2.0)
            return
        switch_failures += 1
        delay = min(SWITCH_RETRY_MAX_S,
                    SWITCH_RETRY_BASE_S * 2 ** (switch_failures - 1))
        log.warning("[TOSLINK] Switch to '%s' failed (#%d), retry in %g s",
                    target_prefix, switch_failures, delay)
        await asyncio.sleep(delay)

    while True:
        await asyncio.sleep(TOSLINK_LOOP_S)

        # Changement de config externe (télécommande, GUI...)
        cp = vol_state.config_path
        if cp and cp != last_config_path:
            last_config_path = cp
            new_prefix = _prefix_of(cp)
            if new_prefix != current_prefix:
                log.info("[TOSLINK] Config change: %s -> %s",
                         current_prefix, new_prefix)
                current_prefix = new_prefix
            toslink_hits = silent_ticks = 0
            await asyncio.sleep(1.0)
            continue

        try:
            if current_prefix == '_':
                silent_ticks = 0
                peak, rms, err = await loop.run_in_executor(
                    None, _toslink_probe, toslink_params)
                if err is not None:
                    if repr(err) != last_err:      # un seul log par erreur
                        log.warning("[TOSLINK] Probe failed: %r", err)
                        last_err = repr(err)
                    toslink_hits = 0
                    await asyncio.sleep(2.0)
                    continue
                last_err = None
                if (peak is None or (peak - rms) < 2.0
                        or peak < TOSLINK_ON_DB):
                    toslink_hits = 0
                    continue
                toslink_hits += 1
                if toslink_hits >= 2:
                    log.info("[TOSLINK] TV confirmed (peak=%.1f rms=%.1f) "
                             "-> switch to |XX", peak, rms)
                    toslink_hits = 0
                    await try_switch(CONFIG_DIR + '|*', '|')

            else:
                toslink_hits = 0
                lms_playing = await asyncio.to_thread(lms_is_playing)
                if lms_playing and silent_now:
                    silent_ticks += 1
                    if silent_ticks >= SILENT_TICKS_TO_LOOPBACK:
                        log.info("[LOOPBACK] LMS playing + TOSLINK silent "
                                 "-> switch to _XX")
                        silent_ticks = 0
                        await try_switch(CONFIG_DIR + '_*', '_')
                        continue
                elif lms_playing:
                    silent_ticks = 0
                    log.info("[TV] LMS started while the TV plays -> pause")
                    await asyncio.to_thread(send_lms_command, "pause")
                    await asyncio.sleep(2.0)
                else:
                    silent_ticks = 0
                await asyncio.sleep(TOSLINK_TV_LOOP_S - TOSLINK_LOOP_S)

        except Exception as e:
            toslink_hits = silent_ticks = 0
            log.warning("[TOSLINK] iteration failed: %s", e)
            await asyncio.sleep(2.0)


async def remote_events(device):
    global last_displayed, key_hold_counter, screen_idle_ticks, is_key_held, is_volume_key_held
    global last_tone_tilt, enter_display_at_press

    last_seek_time = 0
    volume_repeat_count = 0
    swallowed_key = None
    while True:
        try:
            async for event in device.async_read_loop():
                if event.type != evdev.ecodes.EV_KEY:
                    continue

                screen_idle_ticks = 0

                attrib = evdev.categorize(event)
                key = attrib.keycode
                volume_keys = (KEY_BINDINGS['VOLUMEDOWN'], KEY_BINDINGS['VOLUMEUP'])

                if attrib.keystate == 1 and display_is_off:
                    is_key_held = True
                    display_volume_info()
                    swallowed_key = key
                    continue
                if swallowed_key is not None and key == swallowed_key:
                    if attrib.keystate == 0:
                        swallowed_key = None
                    continue

                if attrib.keystate == 1:
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
                        enter_display_at_press = last_displayed
                        if last_displayed == "volume":
                            display_loudness_info()

                elif attrib.keystate == 2:

                    if key in volume_keys:
                        is_volume_key_held = True
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

                elif attrib.keystate == 0:

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
            log.warning("Remote disconnected: %s", e)
            return

        except Exception:
            log.exception("Remote handler error, continuing")
            await asyncio.sleep(0.2)


# ====================== SHUTDOWN / MAIN ======================

def exit_gracefully(signum, frame):
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
    if ENABLE_DISPLAY:
        display_volume_info()

    tasks = []

    def spawn(name, factory):
        tasks.append(asyncio.create_task(supervised(name, factory)))

    if ENABLE_DISPLAY or ENABLE_REMOTE or ENABLE_TOSLINK_SWITCH or ENABLE_LMS_SYNC:
        spawn("cdsp-poller", cdsp_poller)
    if ENABLE_AUTO_POWEROFF or ENABLE_DISPLAY or ENABLE_TOSLINK_SWITCH:
        spawn("silence", silence_monitor)
    if ENABLE_AUTO_POWEROFF:
        spawn("auto-poweroff", auto_poweroff)
    if ENABLE_DISPLAY:
        spawn("display", display_manager_loop)
        spawn("adc", adc_reader_loop)
    if ENABLE_DISPLAY or ENABLE_REMOTE or ENABLE_TOSLINK_SWITCH:
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
