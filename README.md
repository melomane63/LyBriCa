# LyBriCa

**Ly**rion + **Bri**dge + **Ca**millaDSP

A Raspberry Pi audio-control project connecting
**Lyrion Music Server** and **CamillaDSP**.

LyBriCa is built around **five independent features**, all **enabled by
default**. Each one can be turned off on its own by editing the corresponding
switch at the top of `lybrica.py`:

| # | Feature | Switch |
|---|---|---|
| 1 | Bidirectional volume / mute synchronisation on Material skin | `ENABLE_LMS_SYNC` |
| 2 | BLE HID remote (volume, tone, player control, configs) | `ENABLE_REMOTE` |
| 3 | TM1637 display with automatic brightness | `ENABLE_DISPLAY` |
| 4 | Trigger control for amplifier power | `ENABLE_AUTO_POWEROFF` |
| 5 | Automatic DSP configuration switching (GPIO or TOSLINK) | `ENABLE_CONFIG_SWITCH` |

An optional **ESP32-C3 companion** manages the Raspberry Pi power, reads
a physical button and the BLE HID remote, and sends ambient-light readings to
the Pi over UART serial.

When a feature is disabled, the hardware library it needs is **not imported
and not required**, so LyBriCa can run on a minimal setup (for example
volume-sync only, with no display and no remote). See
[Feature switches](#feature-switches) for details.

---

## Table of contents

1. [What it does](#what-it-does)
2. [Architecture](#architecture)
3. [Hardware](#hardware)
4. [Software components](#software-components)
5. [Quick install](#quick-install)
6. [What the installer does](#what-the-installer-does)
7. [Raspberry Pi boot configuration](#raspberry-pi-boot-configuration)
8. [Manual installation](#manual-installation)
9. [Configuration](#configuration)
10. [Feature switches](#feature-switches)
11. [Lyrion Music Server setup](#lyrion-music-server-setup)
12. [Remote control keys](#remote-control-keys)
13. [Display](#display)
14. [Volume curve](#volume-curve)
15. [DSP configuration files](#dsp-configuration-files)
16. [Settings file](#settings-file)
17. [ESP32-C3 companion](#esp32-c3-companion)
18. [Systemd service](#systemd-service)
19. [Troubleshooting](#troubleshooting)
20. [License](#license)

---

## What it does

LyBriCa keeps **Lyrion Music Server** (ex-LMS) and **CamillaDSP** in sync:

- **Bidirectional volume / mute sync** between Lyrion and CamillaDSP
- **Automatic DSP config switching** (TV / Squeezelite) via GPIO **or** TOSLINK
  detection (one method at a time)
- **HID remote control** (volume, mute, config, tone, tilt, loudness,
  power)
- **TM1637 display** with ambient light sensor for automatic brightness
- **Power relay control**: switch off after silence, back on with sound,
  halt the Pi after a long silence
- **Physical power on/off** handled by an ESP32 companion
- **Per-config tone settings** saved in `settings.json`

---

## Architecture

```
                        +----------+
                        | Capteurs |
                        | Light    |
                        | Button   |
                        +----+-----+
                             |
                             | ADC / GPIO
                             v
+----------------+     +---------+     +-------------+
|  Power supply  |---->|  ESP32  |---->| Power relay |
+----------------+     +----+----+     +------+------+
                           |                 |
                           | GPIO            | 5V
                           | Serial          |
                           v                 v
                      +----+-----------------+----+
                      |      Raspberry Pi        |
                      |  (LyBriCa + CamillaDSP   |
                      |       + Lyrion)          |
                      +---+--------+-------+-----+
                          |        |       |
                          v        v       v
              +-----------+--+ +---+------+ +------+-------+
              |  HID remote  | | TM1637   | |  Relay       |
              |              | | display  | |  Power Amp   |
              +--------------+ +----------+ +--------------+
```

The **ESP32** manages the Raspberry Pi's physical power supply and can request
a wake-up from a button or remote-control event. It also reads the ambient-light
sensor and sends readings to the Pi over serial.

This is separate from the **GPIO 4 relay controlled by LyBriCa on the Pi**:
that relay switches the amplifier, not the Raspberry Pi's own power supply.

---

## Hardware

| Component | Role | Connection |
|---|---|---|
| Raspberry Pi | Runs LyBriCa | - |
| ESP32-C3 | Raspberry Pi power relay, BLE HID remote, light sensor | UART TX to `/dev/serial0` |
| HID remote | User input | USB / Bluetooth (evdev) |
| TM1637 display | 6-digit display | GPIO 23 (CLK), GPIO 24 (DIO) |
| Power relay | Cuts power to the amplifier | GPIO 4 |
| TV state input | Detects TV on/off | GPIO 12 (optional) |
| Ambient light sensor | Auto brightness | Via ESP32 (serial) |
| TOSLINK input | Source detection | ALSA (optional) |

---

## Software components

| Component | Language | Role |
|---|---|---|
| **LyBriCa** | Python 3 | Main bridge, asyncio event loop |
| **ESP32-C3 companion** | C / ESP-IDF | Power management, BLE HID host, light sensor, UART output |
| **CamillaDSP** | Rust | Audio DSP engine |
| **Lyrion Music Server** | Perl | Music server |

---

## Quick install

A single script installs everything: CamillaDSP, Lyrion Music Server,
Squeezelite, LyBriCa, the ALSA loopback, the USB drive mount, and the
Bluetooth remote pairing.

```bash
wget https://raw.githubusercontent.com/melomane63/LyBriCa/main/install-lybrica.sh -O /tmp/install-lybrica.sh
chmod +x /tmp/install-lybrica.sh
bash /tmp/install-lybrica.sh
```

The script opens an interactive menu:

```
==================================
  Raspberry Pi Audio Setup Menu
==================================
1) Install Everything
2) Upgrade System
3) Install CamillaDSP & GUI
4) Install Lyrion Media Server & Squeezelite
5) Mount USB Drive
6) Configure Sound Levels (alsamixer)
7) Install Lybrica (Remote, Display, Trigger, Autoconfig...)
8) Pair Bluetooth Remote
9) Shutdown System
10) Exit
==================================
```

Choose **1) Install Everything** for a full installation, or pick individual
steps.

### Installer defaults

The installer uses the following fixed values:

| Setting | Value |
|---|---|
| LyBriCa directory | `$HOME/scripts` |
| LyBriCa repository | `https://raw.githubusercontent.com/melomane63/LyBriCa/main` |
| CamillaDSP version | `v4.1.3` |
| CamillaGUI version | `v4.1.0` |
| pycamilladsp version | `v4.0.0` (pinned) |
| Lyrion version | `9.1.1` |
| Python venv | `/opt/venv` (created with `--system-site-packages`) |
| Service Python | `/opt/venv/bin/python3` |
| Service user | `$USER` (the user running the installer) |

To change any of these, edit `install-lybrica.sh` directly.

---

## What the installer does

### Step-by-step actions

| Step | Action |
|---|---|
| **1. System** | `apt update`, `apt upgrade`, installs `git`, `wget`, `curl`, `python3-pip`, `python3-venv`, `systemd` |
| **2. CamillaDSP** | Downloads and installs CamillaDSP v4.1.3 to `/usr/local/bin/`, CamillaGUI v4.1.0 to `/opt/camillagui_backend/`, creates the systemd services `camilladsp.service` and `camillagui.service`, writes a default `statefile.yml` and `_Setup.yml` config, sets up the ALSA loopback (`snd-aloop` loaded at boot) |
| **3. Lyrion** | Downloads and installs Lyrion Music Server 9.1.1, installs Squeezelite, configures the output device to `hw:Loopback,0` with options `-W -C 5 -r 48000-48000 -R hLE` |
| **4. USB drive** | Detects the USB drive UUID with `blkid` and adds an entry to `/etc/fstab` (`auto defaults,nofail,x-systemd.device-timeout=1,noatime`) |
| **5. Sound card** | Launches `alsamixer` for manual level adjustment, then saves with `alsactl store` |
| **6. LyBriCa** | Rewrites `/boot/firmware/config.txt` and cleans `cmdline.txt`, adds the user to the required groups, creates `/opt/venv`, installs Python dependencies (pycamilladsp pinned to v4.0.0), downloads `lybrica.py` and `tm1637_lgpio.py`, creates and starts the systemd service |
| **7. Bluetooth remote** | Installs `bluetuith` if needed, launches it for pairing, detects the remote with `evdev`, updates `REMOTE_NAME` in `lybrica.py`, restarts the service |
| **8. Shutdown** | Prompts for an optional system shutdown at the end |

### Files and directories touched by `install_lybrica()`

| Path | Change | Backup |
|---|---|---|
| `/boot/firmware/config.txt` | Rewritten (see [Raspberry Pi boot configuration](#raspberry-pi-boot-configuration)) | `config.txt.bak` |
| `/boot/firmware/cmdline.txt` | `console=serial0,NNNN` removed | `cmdline.txt.bak` |
| `/opt/venv` | Created (`python3 -m venv --system-site-packages`) | - |
| `/opt/venv/lib/pythonX.Y/site-packages/tm1637_lgpio.py` | Copied | - |
| `/etc/systemd/system/lybrica.service` | Created and enabled | - |
| `$HOME/scripts/lybrica.py` | Downloaded from GitHub | - |
| User groups | `dialout,gpio,input,spi,i2c,audio` added | - |

### User groups added by the installer

```bash
sudo usermod -aG dialout,gpio,input,spi,i2c,audio "$USER"
```

---

## Raspberry Pi boot configuration

The installer **rewrites** `/boot/firmware/config.txt` (a backup is saved as
`config.txt.bak` before the first install; a marker `#installed-by-lybrica` is
added at the end so the file is not rewritten on later runs).

Key lines added:

| Line | Purpose |
|---|---|
| `dtparam=audio=off` | Disable the built-in audio, so the ALSA loopback / USB DAC can take over |
| `dtoverlay=gpio-poweroff,gpiopin=21,active_low=1` | Signal "Pi is really off" to the ESP32 on **GPIO21**. The ESP32 reads it on its own **GPIO6** (power-good). |
| `dtoverlay=gpio-shutdown,gpio_pin=20,active_low=0,gpio_pull=down` | Allow the ESP32 (**GPIO10**) to request a clean shutdown of the Pi on **GPIO20**. |
| `enable_uart=1` | Enable `/dev/serial0`, used to receive the ambient-light ADC value from the ESP32 |
| `dtoverlay=disable-wifi` | Disable the built-in Wi-Fi (see the warning below) |
| `arm_64bit=1` | Run in 64-bit mode |
| `arm_boost=1` | Run as fast as the firmware / board allows |

The installer also cleans `/boot/firmware/cmdline.txt` by removing
`console=serial0,NNNN` (backup as `cmdline.txt.bak`), so that `/dev/serial0`
is free for the ESP32 UART link.

### GPIO correspondence ESP32 ↔ Raspberry Pi

The installer's boot overlays expect the ESP32 to be wired as follows:

| ESP32-C3 pin | Direction | Function | Raspberry Pi side |
|---|---|---|---|
| GPIO5 | Output | Raspberry Pi power-relay control (HIGH = relay on) | - |
| GPIO6 | Input | Raspberry Pi power-good signal (internal pull-down) | **GPIO21** via `gpio-poweroff` |
| GPIO7 | Input | Physical button (also BLE-scan trigger at boot) | - |
| GPIO8 | Output | Bluetooth status LED (inverted: LOW = on) | - |
| GPIO10 | Output | Raspberry Pi shutdown request | **GPIO20** via `gpio-shutdown` |
| GPIO21 (UART1 TX) | Output | Filtered ambient-light ADC value (`<integer>\n`) | **GPIO15** (RXD, `/dev/serial0`) |

> ⚠️ **Wiring check**: the `gpio-shutdown` overlay expects the signal on
> Raspberry Pi **GPIO20**, while the ESP32 firmware drives its own **GPIO10**.
> Make sure the wire goes from ESP32 GPIO10 to Raspberry Pi GPIO20. If your
> wiring differs, adjust either `dtoverlay=gpio-shutdown,gpio_pin=NN` or the
> `#define GPIO_SHUTDOWN` in the firmware.

> ⚠️ **`dtoverlay=disable-wifi`**: this disables the built-in Wi-Fi. On some
> Raspberry Pi models this can also disable the built-in Bluetooth. If you use
> the Bluetooth remote pairing step (`pair_bluetooth_remote`), verify that the
> Bluetooth adapter is still present (`hciconfig -a`, `bluetoothctl list`). If
> not, remove `dtoverlay=disable-wifi` from `/boot/firmware/config.txt` and
> reboot.

---

## Manual installation

If you prefer to install step by step:

### Prerequisites

- Raspberry Pi (any model with GPIO)
- Raspberry Pi OS (Bookworm or later recommended)
- Python 3.9+
- CamillaDSP installed and running
- Lyrion Music Server installed and running
- ESP32 flashed with the companion firmware (see below)

### System dependencies

```bash
sudo apt update
sudo apt install -y python3-pip python3-dev python3-venv \
                    liblgpio-dev libasound2-dev
```

### Python dependencies

Install into the `/opt/venv` virtual environment (the same one used by the
installer and the systemd service):

```bash
sudo mkdir -p /opt/venv
sudo python3 -m venv --system-site-packages /opt/venv
sudo /opt/venv/bin/pip install --upgrade pip
sudo /opt/venv/bin/pip install evdev==1.6.1 lgpio==0.2.2.0 pyserial==3.5
sudo /opt/venv/bin/pip install "git+https://github.com/HEnquist/pycamilladsp.git@v4.0.0"
```

### Install LyBriCa

```bash
mkdir -p "$HOME/scripts"
wget https://raw.githubusercontent.com/melomane63/LyBriCa/main/lybrica.py -O "$HOME/scripts/lybrica.py"

PYVER=$(/opt/venv/bin/python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
wget https://raw.githubusercontent.com/melomane63/tm1637_lgpio/main/tm1637_lgpio.py -O /tmp/tm1637_lgpio.py
sudo cp /tmp/tm1637_lgpio.py "/opt/venv/lib/python${PYVER}/site-packages/tm1637_lgpio.py"
rm -f /tmp/tm1637_lgpio.py
```

### Permissions

The user running LyBriCa needs access to:

- GPIO: add to the `gpio` group
- Serial: add to the `dialout` group
- Input devices: add to the `input` group
- Audio: add to the `audio` group
- SPI: add to the `spi` group
- I2C: add to the `i2c` group

```bash
sudo usermod -aG dialout,gpio,input,spi,i2c,audio $USER
```

---

## Configuration

All configuration is at the top of `lybrica.py`:

```python
# Feature switches
ENABLE_LMS_SYNC      = True
ENABLE_AUTO_POWEROFF = True
ENABLE_REMOTE        = True
ENABLE_DISPLAY       = True
ENABLE_CONFIG_SWITCH = "gpio"   # False | "gpio" | "toslink"
```

### Network addresses

```python
CAMILLA_ADDR = ("127.0.0.1", 1234)   # CamillaDSP WebSocket
LMS_ADDR     = ("127.0.0.1", 9090)   # Lyrion CLI
```

### GPIO pins

```python
POWER_GPIO = 4     # Power relay
TV_GPIO    = 12    # TV state input
CLK, DIO   = 23, 24 # TM1637 display
```

### Timing

```python
POWER_OFF_DELAY         = 2     # Minutes of silence before the amplifier relay switches off
HALT_DELAY              = 48    # Hours before the Pi shuts down after prolonged inactivity
SCREEN_RETURN_DELAY     = 30    # Seconds before returning to the volume screen
TOSLINK_SILENCE_DELAY_S = 120   # Seconds of TOSLINK silence before switching back to Squeezelite
```

`ENABLE_AUTO_POWEROFF` controls the Pi-side amplifier relay and the associated
silence/shutdown behaviour. It is not the same as the ESP32's physical power
management for the Raspberry Pi.

---

## Feature switches

Each feature can be disabled independently. When disabled, the corresponding
hardware library is **not imported** and **not required**.

| Switch | Default | Values | Effect |
|---|---|---|---|
| `ENABLE_LMS_SYNC` | `True` | `True` / `False` | Lyrion <-> CamillaDSP volume/mute sync |
| `ENABLE_AUTO_POWEROFF` | `True` | `True` / `False` | Power relay: off after silence, on with sound |
| `ENABLE_REMOTE` | `True` | `True` / `False` | HID remote control |
| `ENABLE_DISPLAY` | `True` | `True` / `False` | TM1637 display + ambient light sensor |
| `ENABLE_CONFIG_SWITCH` | `"gpio"` | `False` / `"gpio"` / `"toslink"` | Automatic config switching — **only one method at a time** |

### `ENABLE_CONFIG_SWITCH` details

The switch accepts **exactly one** of these three values:

| Value | Meaning |
|---|---|
| `False` | Automatic config switching is disabled. Only the manual keys `BACK` and `HOMEPAGE` can change the config. |
| `"gpio"` | The TV state is read on `TV_GPIO` (0 = TV on). Matches the original hardware setup. |
| `"toslink"` | The TOSLINK input is probed with ALSA. `TOSLINK_SILENCE_DELAY_S` seconds of silence switches back to Squeezelite. |

An invalid value raises `ValueError` at startup (checked in the `FEATURES`
block of `lybrica.py`).

---

## Lyrion Music Server setup

The bidirectional bridge between Lyrion and CamillaDSP is driven by Lyrion's
own CLI (`LMS_ADDR`, port 9090), but **Lyrion itself must be configured** for
the bridge to work correctly.

### Required Lyrion player settings

LyBriCa expects the Squeezelite player to expose a **fixed 100 % volume** to
Lyrion, and to keep its volume slider always visible. Otherwise, Lyrion's
internal volume curve fights with the CamillaDSP curve and the display shows
inconsistent values.

**Material Skin is required** to reach these options easily from the web UI.

1. Install the **Material Skin** plugin in Lyrion:
   - *Settings → Plugins → Material Skin* → install, then restart Lyrion.
   - Reload the web UI: it switches to the Material layout.
2. Open *Settings → Player → [your Squeezelite player]*.
3. Under **Audio**, set:
   - **Volume control**: `Output level is fixed at 100%`
   - This forces Lyrion to always send gain `65536` to Squeezelite, so the
     volume you see and change is the CamillaDSP one.
4. Under **Interface**, enable:
   - **Always show volume slider** (Material Skin option)
   - This keeps the volume slider visible in the Material UI so you can
     drive CamillaDSP through LyBriCa without switching screens.
5. Save and, if needed, restart the player.

> Without a **fixed 100 % output** and **Material Skin**, the volume reported
> by Lyrion and the volume reported by CamillaDSP can diverge, and LyBriCa will
> fight to resynchronise them (see `LMS_RESTORE_WINDOW_S` and the
> `[LMS sync]` debug messages when `LMS_SYNC_DEBUG = True`).

### Debugging the bridge

Enable `LMS_SYNC_DEBUG = True` in `lybrica.py` to log every volume/mute change
and every raw LMS notification:

```
[LMS sync] << dc:a6:32:3c:1c:21 prefset server volume 42
[LMS sync] LMS 42% -> Camilla -29.0 dB
[LMS sync] Camilla 43% -> LMS
```

If the bridge keeps correcting the volume in a loop, the player is not set to
**fixed 100 % output**.

---

## Remote control keys

| Key | Short press | Long press |
|---|---|---|
| `VOLUMEUP` / `VOLUMEDOWN` | Volume +/-1 step | Continuous volume |
| `MUTE` | Mute / unmute | - |
| `PLAYPAUSE` | Pause | Stop (LMS) |
| `PREVIOUSSONG` | Previous track | Rewind (10 s steps) |
| `NEXTSONG` | Next track | Fast-forward (10 s steps) |
| `UP` / `DOWN` | Increase / decrease bass on the tone screen; tilt on the tilt screen | - |
| `LEFT` / `RIGHT` | Decrease / increase treble on the tone screen; presence on the tilt screen | - |
| `ENTER` | Reset / restore tone values; toggle loudness on its screen | Switch between tone and tilt screens |
| `BACK` | Next config whose filename starts with `_` (stereo group) | - |
| `HOMEPAGE` | Next config whose filename starts with `|` (TV group) | - |
| `POWER` | - | Toggle amplifier relay; a very long press shuts down the Pi |

---

## Display

The TM1637 display shows:

- **Volume screen**: config label (3 chars) + volume (0-100)
- **Tone screen**: bass / treble (` 3B 2T`)
- **Tilt screen**: tilt / presence (` 2T 1P`)
- **Loudness screen**: reference level (`-30 Ld`)
- **Power screen**: `PW ON` / `PW OFF` / ` HALT `

Special indicators:

- Decimal point on the 3rd character: auto power-off active
- Decimal point on the 4th character: power relay ON
- Top segment on the 4th character: tone settings active

Brightness is adjusted automatically from the ambient light sensor
(4 levels with hysteresis).

---

## Volume curve

LyBriCa converts Lyrion's integer volume level (0–100) to the corresponding
CamillaDSP main-volume level in dB, and converts changes in the other direction.

| Lyrion level | CamillaDSP level | Mapping |
|---|---:|---|
| 0 | -100 dB | Special low-level value; mute is synchronised separately |
| 1 | -75 dB | Special case |
| 2–24 | -71 to -38 dB | 1.5 dB per level |
| 25–100 | -37.5 to 0 dB | 0.5 dB per level |

The inverse conversion rounds to the nearest integer Lyrion level. Mute is
synchronised separately from volume, so level `0` should not be treated as
the only mute mechanism.

---

## DSP configuration files

Automatic configuration switching uses filename prefixes:

- `_` identifies the stereo/Squeezelite group, selected with `BACK`.
- `|` identifies the TV group, selected with `HOMEPAGE`.
- `ENABLE_CONFIG_SWITCH = False` disables automatic switching (manual keys
  still work).
- `ENABLE_CONFIG_SWITCH = "gpio"` uses the TV-state input (`TV_GPIO`).
- `ENABLE_CONFIG_SWITCH = "toslink"` detects audio on the TOSLINK input and
  uses `TOSLINK_SILENCE_DELAY_S` before returning to Squeezelite.

Make sure the configuration files in `CONFIG_DIR` use the prefixes expected
by your setup.

---

## Settings file

`settings.json` stores the saved tone-related values for each configuration,
the previous values used by the ENTER restore function, and the last tone/tilt
screen selection.

If the file is missing, `load_settings()` falls back to safe defaults (empty
dictionaries, `last_tone_tilt = "tone"`), so LyBriCa still starts and creates
it on the first write.

The file is written **atomically** using a temporary file followed by
`os.replace()`, reducing the risk of leaving a partially written settings file
if the process is interrupted.

---

## ESP32-C3 companion

The ESP32-C3 firmware handles the Raspberry Pi's **physical power relay**. It
also hosts a BLE HID remote and reads the ambient-light sensor. The GPIO numbers
in this section refer to the **ESP32-C3**, not to Raspberry Pi GPIO numbers.

### ESP32-C3 pin assignments

| ESP32-C3 pin | Direction | Function |
|---|---|---|
| GPIO2 (ADC1 channel 0) | Input | Ambient-light sensor (LDR) |
| GPIO5 | Output | Raspberry Pi power-relay control: HIGH = relay on, LOW = relay off |
| GPIO6 | Input | Raspberry Pi power-good signal; has an internal pull-down |
| GPIO7 | Input | Physical button; also used to request BLE scanning during startup |
| GPIO8 | Output | Bluetooth status LED, inverted logic: LOW = on, HIGH = off |
| GPIO10 | Output | Raspberry Pi shutdown request |
| GPIO21 (UART1 TX) | Output | Sends filtered ADC readings to the Raspberry Pi |

### Flashing the firmware

The firmware is built with **ESP-IDF** (v5.x). Install ESP-IDF first, then:

```bash
source ~/esp-idf/export.sh

idf.py fullclean
idf.py set-target esp32c3
idf.py build
idf.py -p /dev/ttyACM0 flash
idf.py -p /dev/ttyACM0 monitor
```

- `set-target esp32c3` is required the first time (or after `fullclean`) to
  configure the build for the ESP32-C3.
- `/dev/ttyACM0` is the USB CDC serial port exposed by the ESP32-C3. On some
  systems it may be `/dev/ttyUSB0` — check with `ls /dev/tty*` before flashing.
- `monitor` prints the BLE scan logs (`BLE_SCAN: ADV ...`) and the relay
  messages. Press `Ctrl+]` to exit.

### Ambient-light sensor wiring

The firmware reads the LDR on **GPIO2 (ADC1 channel 0)** through a simple
voltage divider:

```
3V3 ──── LDR ────┬──── GPIO2 (ADC)
                 │
                R
                 │
                GND
```

| Component | Value | Notes |
|---|---|---|
| LDR | **GL5516** | Common photoresistor; ~5–10 kΩ in bright light, ~1 MΩ in the dark |
| Resistor `R` | **15 kΩ** | Default value used in the reference build |
| Resistor `R` (alt.) | **16.2 kΩ** | Alternative value if 15 kΩ is not available; only shifts the ADC curve slightly |

Either resistor value works: the ADC levels in `lybrica.py`
(`ADC_LEVELS = [80, 1000, 2500, 3500]`) are thresholds, not absolute
calibrations. If the display brightness does not react as expected, log the
raw ADC value (see the ESP32 `monitor` output) and adjust `ADC_LEVELS` in
`lybrica.py` to match your environment.

### Pairing the BLE remote with the ESP32

The ESP32 firmware acts as a **BLE HID host**: it does not pair from the
Raspberry Pi side, and the remote must **not** be paired with the Pi's own
Bluetooth stack. Pairing is done entirely on the ESP32, using its physical
button (GPIO7) and the NVS storage.

**One-time pairing procedure:**

1. Power off the ESP32-C3 (or hold its reset).
2. Put the BLE remote in **pairing mode** (usually hold its pairing button
   until the LED blinks fast — see your remote's manual).
3. **Hold GPIO7 LOW** (press the ESP32 button) while the ESP32-C3 boots, and
   keep it held until the scan starts. Concretely: press the button, release
   the ESP32 reset, then release the button once the scan has begun (the LED
   on GPIO8 will blink slowly during the scan).
4. The firmware scans BLE for **5 seconds** and prints every advertisement
   with RSSI ≥ −80 dBm to its console. It then keeps the **strongest** device
   and saves its MAC in NVS under `target_mac`.
5. The ESP32 automatically **restarts**. On the next boot, it loads the saved
   MAC and opens the BLE HID connection to the remote.

**Re-pairing to another remote:**

- Repeat the same procedure: hold GPIO7 LOW at boot. The previous MAC in NVS
  is simply **overwritten** by the new strongest device found during the scan.

**If the scan picks the wrong device:**

- Only devices advertising with **RSSI greater than −80 dBm** are eligible.
  Move the remote closer to the ESP32 (a few centimetres) during the 5-second
  scan window, and move other BLE devices (phones, watches, speakers) away.

**Checking the status:**

- GPIO8 LED behaviour on the ESP32:
  - **Solid** (LOW): a BLE HID connection is established and the remote is
    ready.
  - **Slow blink** (LOW/HIGH ~300 ms / 1.7 s): no MAC is saved in NVS yet, or
    the connection to the saved MAC failed. Re-do the pairing procedure.
- The ESP32 logs (UART0, typically the USB console) show:
  ```
  BLE_SCAN: ADV aa:bb:cc:dd:ee:ff   RSSI=-52
  BLE_SCAN: Best BLE device: aa:bb:cc:dd:ee:ff   RSSI=-52
  BLE_SCAN: MAC saved to NVS.
  BLE_SCAN: Restarting ESP32-C3...
  ```

**After pairing:**

- The remote is used **only through the ESP32**, not through the Pi's
  `bluetuith` / `evdev` stack. On the Pi side, `ENABLE_REMOTE` and
  `REMOTE_NAME` refer to a **separate** HID remote that is paired directly
  with the Pi via USB or Bluetooth.
- To use a single remote, either pair it with the ESP32 (for wake-up) **or**
  with the Pi (for key events), but pairing with both at the same time usually
  does not work: a BLE HID device can typically only be connected to one host.

**Note on BLE shutdown:**

- As soon as the relay activates (RPi powered on), the firmware calls
  `deinit_bluetooth()` and drops the BLE connection. During that window the
  remote is **not** connected to the ESP32 (see
  [Bluetooth shutdown when the relay activates](#bluetooth-shutdown-when-the-relay-activates)).
  The ESP32 reconnects on the next restart.

### Button and BLE pairing

- If GPIO7 is held LOW when the ESP32-C3 starts, the firmware scans BLE for
  five seconds and selects the device with the strongest qualifying signal.
- The selected device address is saved in NVS under `target_mac`, then the
  ESP32-C3 restarts. The code accepts RSSI readings of at least −80 dBm for
  logging, but only saves a device when its RSSI is **greater than −80 dBm**.
- On a normal startup, the firmware loads the saved address and tries to open
  that BLE HID device. It retries failed connections every five seconds.
- During normal operation, pressing GPIO7 starts the relay sequence. A further
  button press while the relay sequence is active asserts GPIO10 to request
  Raspberry Pi shutdown.
- If no saved MAC address is available, the firmware logs a warning and the
  status LED blinks slowly. The physical button can still start the relay
  sequence.

### Raspberry Pi power monitoring

After activating GPIO5, the firmware ignores an initial LOW on GPIO6. It arms
shutdown detection only after reading GPIO6 HIGH on two successive checks. Once
armed, two LOW readings trigger relay shutdown and an ESP32-C3 restart. Checks
are separated by approximately 500 ms, so the actual detection time depends on
when the signal changes relative to the polling loop.

GPIO10 is set HIGH when a shutdown request is made. In the supplied firmware,
that output is not explicitly returned to LOW afterward; do not assume it is a
short pulse.

### Bluetooth shutdown when the relay activates

When `activate_relay_sequence()` runs, the firmware calls `deinit_bluetooth()`
**before** entering the relay-watch loop. This means:

- The BLE HID connection to the remote is **torn down** as soon as the relay
  is switched on (i.e. when the Raspberry Pi is powered up).
- After the relay sequence ends (Pi powered off), the ESP32 **restarts**, which
  re-initialises BLE and reconnects to the saved remote MAC.
- Consequence: while the relay is active, the BLE remote **cannot** send new
  HID events to the ESP32. Only the physical button (GPIO7) can request the
  Raspberry Pi shutdown during that window.
- This is intentional: it avoids a BLE event immediately re-triggering the
  relay while the Pi is booting, and frees the BLE stack while the ESP32 is
  only polling GPIO6.

### Ambient-light serial data

UART1 uses **115200 baud, 8 data bits, no parity, and one stop bit**. Every
approximately 200 ms, the firmware takes 20 ADC samples, averages them, applies
an exponential filter (alpha 0.2), and sends the resulting integer followed by
newline (`<integer>\n`) on ESP32-C3 GPIO21 TX. LyBriCa reads these newline-terminated
integer values from `/dev/serial0` to adjust the TM1637 display brightness.

This firmware's serial output is for the **ambient-light ADC value only**. The
provided code does not send `POWER` messages or receive `ON` / `OFF` commands
through UART; power control is performed through the ESP32-C3 GPIOs.

> The firmware source, wiring diagram and flashing instructions are in the
> [`firmware/`](firmware/) directory of this repository, if included in your
> checkout.

---

## Systemd service

The installer creates `/etc/systemd/system/lybrica.service`:

```ini
[Unit]
Description=Lybrica (Remote, Display, Trigger, Autoconfig for CamillaDSP <-> LMS volume sync)
After=default.target

[Service]
User=<user>
Type=simple
WorkingDirectory=/home/<user>/scripts
ExecStart=/opt/venv/bin/python3 /home/<user>/scripts/lybrica.py
Restart=on-failure
RestartSec=5
KillMode=control-group
KillSignal=SIGTERM
TimeoutStopSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=lybrica

[Install]
WantedBy=default.target
```

Manage the service:

```bash
sudo systemctl status lybrica
sudo systemctl restart lybrica
sudo systemctl stop lybrica
journalctl -u lybrica -f
```

---

## Troubleshooting

### Remote not detected

```bash
/opt/venv/bin/python3 -m evdev.evtest
```

Check that the remote name matches `REMOTE_NAME` in `lybrica.py`.

### CamillaDSP unreachable

```bash
camilladsp --check
```

Check `CAMILLA_ADDR` and that CamillaDSP is running.

### Lyrion CLI unreachable

```bash
telnet 127.0.0.1 9090
```

Check `LMS_ADDR` and that Lyrion is running.

### Volume fights between Lyrion and CamillaDSP

If the volume keeps jumping or correcting itself:

- Check that the Squeezelite player is set to **fixed 100 % output** in
  *Lyrion → Settings → Player → Audio → Volume control*.
- Check that **Material Skin** is installed and active.
- Enable `LMS_SYNC_DEBUG = True` in `lybrica.py` and look at the `[LMS sync]`
  lines in the journal.

### Display stays blank

- Check the TM1637 wiring (CLK, DIO)
- Check the ambient light sensor value in the logs
- Check that `ENABLE_DISPLAY = True`

### ESP32 not responding

- Check the serial port: `ls -l /dev/serial0`
- Check the baud rate: `115200`
- Check the ESP32 logs (if available)
- Check that `enable_uart=1` is present in `/boot/firmware/config.txt` and that
  `console=serial0,...` has been removed from `/boot/firmware/cmdline.txt`.

### BLE remote does not connect to the ESP32

- Verify that the remote was in **pairing mode** during the 5-second scan.
- Verify that GPIO7 was held LOW at ESP32 boot.
- Move the remote closer than usual (RSSI must be **> −80 dBm**).
- Check the ESP32 console logs for `ADV` and `MAC saved to NVS`.
- If the LED on GPIO8 blinks slowly, no MAC was saved: re-do the pairing.

### Bluetooth remote pairing fails on the Pi

- Check that the Bluetooth adapter is still present:
  `hciconfig -a` or `bluetoothctl list`.
- If `dtoverlay=disable-wifi` disabled the Bluetooth adapter, remove that line
  from `/boot/firmware/config.txt` and reboot.

### Automatic configuration switching does not work

- For `"gpio"` mode, check `TV_GPIO`, the input wiring and the configured
  pull-up.
- For `"toslink"` mode, check the ALSA capture device and the TOSLINK signal
  threshold (`TOSLINK_TV_ON_DB`).
- Check that the config filenames use the expected `_` and `|` prefixes.

### ALSA loopback not loaded

```bash
sudo modprobe snd-aloop
lsmod | grep snd_aloop
```

To load it automatically at boot:

```bash
echo "snd-aloop" | sudo tee -a /etc/modules-load.d/snd-aloop.conf
```

### Logs

```bash
journalctl -u lybrica -f
journalctl -u camilladsp -f
journalctl -u camillagui -f
```

---

## License

MIT License - see [LICENSE](LICENSE) for details.

---

## Credits

- [CamillaDSP](https://github.com/HEnquist/camilladsp) by Henrik Enquist
- [Lyrion Music Server](https://lyrion.org/) (ex-Logitech Media Server)
- [Material Skin](https://github.com/CDrummond/lms-material) by Craig Drummond
- [python-evdev](https://github.com/gvalkov/python-evdev)
- [lgpio](https://github.com/joan2937/lg)
- [tm1637_lgpio](https://github.com/melomane63/tm1637_lgpio)
- [bluetuith](https://github.com/bluetuith-org/bluetuith)
- [RPi-CamillaDSP](https://github.com/mdsimon2/RPi-CamillaDSP) by mdsimon2 (CamillaDSP setup guidance on Raspberry Pi)
- Original LMS volume sync idea by [mbrennwa](https://github.com/mbrennwa/camilla_LMS_volume_sync)

---

## Contributing

Pull requests are welcome. For major changes, please open an issue first
to discuss what you would like to change.

---

## Disclaimer

This project is not affiliated with CamillaDSP, Lyrion Music Server,
Logitech or any of their respective owners.
