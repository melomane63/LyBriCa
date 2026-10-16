# LyBriCa

**Ly**rion + **Bri**dge + **Ca**millaDSP

A Raspberry Pi audio-control project connecting
**Lyrion Music Server** and **CamillaDSP**.

LyBriCa is built around **five independent features**, all **enabled by
default**. Each one can be turned off on its own by editing the corresponding
switch at the top of `lybrica.py`:

| # | Feature | Switch |
|---|---|---|
| 1 | Bidirectional volume / mute synchronisation | `ENABLE_LMS_SYNC` |
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
20. [Known limitations](#known-limitations)
21. [License](#license)

---

## What it does

LyBriCa keeps **Lyrion Music Server** (ex-LMS) and **CamillaDSP** in sync:

- **Bidirectional volume / mute sync** between Lyrion (on Material Skin) and CamillaDSP
- **Automatic DSP config switching** (TV / Squeezelite) via GPIO **or** TOSLINK
  detection (one method at a time)
- **BLE HID remote control** (volume, mute, config, tone, tilt, loudness,
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
                        | Sensors  |
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
| Raspberry Pi (**64-bit capable**, see note) | Runs LyBriCa | - |
| ESP32-C3 | Raspberry Pi power relay, BLE HID remote, light sensor | UART TX to `/dev/serial0` |
| HID remote | User input | USB / Bluetooth (evdev) |
| TM1637 display | 6-digit display | GPIO 23 (CLK), GPIO 24 (DIO) |
| Power relay | Cuts power to the amplifier | GPIO 4 |
| TV state input | Detects TV on/off | GPIO 12 (optional) |
| Ambient light sensor | Auto brightness | Via ESP32 (serial) |
| TOSLINK input | Source detection | ALSA (optional) |

> **64-bit only.** The installer downloads `aarch64` binaries (CamillaDSP,
> CamillaGUI), an `arm64` build of `bluetuith`, and sets `arm_64bit=1` in
> `config.txt`. A 32-bit-only Raspberry Pi (Pi 1, Zero, original Pi 2) is not
> supported by the installer, even though the GPIO features alone would run.

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

> **Running option 7 on its own?** Run **option 2 (Upgrade System)** first.
> The system packages needed to build `pyalsaaudio` (`libasound2-dev`, `git`,
> `python3-pip`...) are installed by option 2, not by option 7. Without them,
> the `pip install pyalsaaudio` step fails and the installer stops (`set -e`).

### Installer defaults

The installer uses the following fixed values:

| Setting | Value |
|---|---|
| LyBriCa directory | `$HOME/scripts` |
| LyBriCa repository | `https://raw.githubusercontent.com/melomane63/LyBriCa/main` |
| CamillaDSP version | `v4.1.3` |
| CamillaGUI version | `v4.1.0` |
| pycamilladsp version | `v4.0.0` |
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
| **1. System** | `apt update`, `apt upgrade`, installs `git`, `wget`, `curl`, `python3-pip`, `systemd`, `libasound2-dev` |
| **2. CamillaDSP** | Downloads and installs CamillaDSP v4.1.3 to `/usr/local/bin/`, CamillaGUI v4.1.0 to `/opt/camillagui_backend/`, creates the systemd services `camilladsp.service` and `camillagui.service`, writes a default `~/camilladsp/statefile.yml` and `~/camilladsp/configs/_Setup.yml`, sets up the ALSA loopback (`snd-aloop` loaded at boot) |
| **3. Lyrion** | Downloads and installs Lyrion Music Server 9.1.1, installs Squeezelite, configures the output device to `hw:Loopback,0` with options `-W -C 5 -r 48000-48000 -R hLE` |
| **4. USB drive** | Looks for a UUID on `/dev/sda1` only (via `blkid`); if none is found, asks for the UUID. Adds an entry to `/etc/fstab` (`auto defaults,nofail,x-systemd.device-timeout=1,noatime`) |
| **5. Sound card** | Launches `alsamixer` for manual level adjustment, then saves with `alsactl store` |
| **6. LyBriCa** | Rewrites `/boot/firmware/config.txt` and cleans `cmdline.txt`, adds the user to the required groups, creates the passwordless `/sbin/shutdown` sudoers rule, creates `/opt/venv`, installs Python dependencies (pycamilladsp pinned to v4.0.0), downloads `lybrica.py` and `tm1637_lgpio.py`, creates and starts the systemd service |
| **7. Bluetooth remote** | Installs `bluetuith` if needed, launches it for pairing, detects the remote with `evdev`, updates `REMOTE_NAME` in `lybrica.py`, restarts the service |
| **8. Shutdown** | Prompts for an optional system shutdown at the end |

### Files and directories touched by `install_lybrica()`

| Path | Change | Backup |
|---|---|---|
| `/boot/firmware/config.txt` | **Rewritten entirely** (see [Raspberry Pi boot configuration](#raspberry-pi-boot-configuration)) | `config.txt.bak` |
| `/boot/firmware/cmdline.txt` | `console=serial0,NNNN` removed | `cmdline.txt.bak` |
| `/etc/sudoers.d/lybrica` | Created (passwordless `/sbin/shutdown`), mode `440` | - |
| `/opt/venv` | Created (`python3 -m venv --system-site-packages`) | - |
| `/opt/venv/lib/pythonX.Y/site-packages/tm1637_lgpio.py` | Copied | - |
| `/etc/systemd/system/lybrica.service` | Created and enabled | - |
| `$HOME/scripts/lybrica.py` | Downloaded from GitHub | - |
| User groups | `dialout,gpio,input,spi,i2c,audio` added | - |

### User groups added by the installer

```bash
sudo usermod -aG dialout,gpio,input,spi,i2c,audio "$USER"
```

Log out and back in (or reboot) for the new groups to apply to your own shell.

### Sudoers rule added by the installer

```bash
echo "$USER ALL=(ALL) NOPASSWD: /sbin/shutdown" | sudo tee /etc/sudoers.d/lybrica > /dev/null
sudo chmod 440 /etc/sudoers.d/lybrica
```

This rule is **required** for `lybrica.py` to halt the Pi without a password
(see `shutdown_system()`). Without it, the **POWER very-long-press → shutdown**
feature fails silently: `os.system("sudo shutdown -h now")` returns an error
code that LyBriCa does not check.

The installer then checks the syntax of the file with `visudo -c`:

```bash
sudo visudo -c -f /etc/sudoers.d/lybrica > /dev/null
```

`visudo -c` does not modify the file; it only returns a non-zero exit code on
error, which stops the installer (`set -e`). Note that the check runs **after**
the file has been written: if the syntax were ever invalid, the installer would
stop but the faulty file would remain in `/etc/sudoers.d/` and could break
`sudo`. In that case, remove it from a root shell (`su -` or `pkexec`):

```bash
rm /etc/sudoers.d/lybrica
```

With the fixed content above, this does not happen in practice.

---

## Raspberry Pi boot configuration

The installer **rewrites** `/boot/firmware/config.txt` (a backup is saved as
`config.txt.bak` before the first install; a marker `#installed-by-lybrica` is
added at the end so the file is not rewritten on later runs).

> ⚠️ The file is **replaced**, not edited. Any personal line you had
> (`dtparam=i2c_arm=on`, `dtparam=spi=on`, extra overlays, display settings...)
> is lost and must be restored by hand from `config.txt.bak` if you need it.
> Notably, the new file does **not** enable I2C or SPI.

Key lines added:

| Line | Purpose |
|---|---|
| `dtparam=audio=off` | Disable the built-in audio, so the ALSA loopback / USB DAC can take over |
| `dtoverlay=gpio-poweroff,gpiopin=21,active_low=1` | Signal "Pi is really off" to the ESP32 on **GPIO21**. The ESP32 reads it on its own **GPIO6** (power-good). |
| `dtoverlay=gpio-shutdown,gpio_pin=20,active_low=0,gpio_pull=down` | Allow the ESP32 (**GPIO10**) to request a clean shutdown of the Pi on **GPIO20**. |
| `enable_uart=1` | Enable `/dev/serial0`, used to receive the ambient-light ADC value from the ESP32 |
| `arm_64bit=1` | Run in 64-bit mode |
| `arm_boost=1` | Run as fast as the firmware / board allows |

The installer also cleans `/boot/firmware/cmdline.txt` by removing
`console=serial0,NNNN` (backup as `cmdline.txt.bak`), so that `/dev/serial0`
is free for the ESP32 UART link.

### GPIO correspondence ESP32 ↔ Raspberry Pi

The installer's boot overlays expect the ESP32 to be wired as follows:

| ESP32-C3 pin | Direction | Function | Raspberry Pi side |
|---|---|---|---|
| GPIO0 (ADC1 channel 0) | Input | Ambient-light sensor (LDR, voltage divider) | - |
| GPIO5 | Output | Raspberry Pi power-relay control (HIGH = relay on) | - |
| GPIO6 | Input | Raspberry Pi power-good signal (internal pull-down) | **GPIO21** via `gpio-poweroff` |
| GPIO7 | Input | Physical button (also BLE-scan trigger at boot) | - |
| GPIO8 | Output | Bluetooth status LED, on-board (inverted: LOW = on) | - |
| GPIO10 | Output | Raspberry Pi shutdown request | **GPIO20** via `gpio-shutdown` |
| GPIO21 (UART1 TX) | Output | Filtered ambient-light ADC value (`<integer>\n`) | **GPIO15** (RXD, `/dev/serial0`) |

> ⚠️ **Wiring check**: the `gpio-shutdown` overlay expects the signal on
> Raspberry Pi **GPIO20**, while the ESP32 firmware drives its own **GPIO10**.
> Make sure the wire goes from ESP32 GPIO10 to Raspberry Pi GPIO20. If your
> wiring differs, adjust either `dtoverlay=gpio-shutdown,gpio_pin=NN` or the
> `#define GPIO_SHUTDOWN` in the firmware.

---

## Manual installation

If you prefer to install step by step:

### Prerequisites

- Raspberry Pi with GPIO, **64-bit capable** (if you use the installer binaries)
- Raspberry Pi OS (Bookworm or later recommended), 64-bit
- Python 3.9+
- CamillaDSP installed and running
- Lyrion Music Server installed and running
- ESP32 flashed with the companion firmware (see below)

### System dependencies

The installer (option 2) installs `git wget curl python3-pip systemd libasound2-dev`.
For a manual installation, install at least:

```bash
sudo apt update
sudo apt install -y git wget curl python3-pip python3-dev python3-venv \
                    liblgpio-dev libasound2-dev
```

(`python3-dev`, `python3-venv` and `liblgpio-dev` are not installed by the
installer; on Raspberry Pi OS they are normally already present or pulled in
by the pip builds.)

### Python dependencies

Install into the `/opt/venv` virtual environment (the same one used by the
installer and the systemd service):

```bash
sudo mkdir -p /opt/venv
sudo python3 -m venv --system-site-packages /opt/venv
sudo /opt/venv/bin/pip install --upgrade pip
sudo /opt/venv/bin/pip install evdev==1.6.1 lgpio==0.2.2.0 pyserial==3.5 pyalsaaudio
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

And for the shutdown command (required by `shutdown_system()` in `lybrica.py`):

```bash
echo "$USER ALL=(ALL) NOPASSWD: /sbin/shutdown" | sudo tee /etc/sudoers.d/lybrica > /dev/null
sudo chmod 440 /etc/sudoers.d/lybrica
sudo visudo -c -f /etc/sudoers.d/lybrica
```

Verify the rule is active. **The `sudo -k` step clears the credential cache,
otherwise the test is meaningless** (a previous `sudo` call in the same
session would make the test pass for the wrong reason):

```bash
sudo -k
sudo -n shutdown -h now   # should NOT ask for a password, and shuts the Pi down
```

If `sudo -n` prints `sudo: a password is required`, the rule is missing or
incorrect and the **POWER very-long-press → shutdown** feature will not work.

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

### Remote

```python
REMOTE_NAME = "HID Remote01 Keyboard"   # found with: python3 -m evdev.evtest
```

The installer (option 8) sets this line for you after pairing.

If `ENABLE_REMOTE = True` and the remote is not found at startup, LyBriCa
**waits and retries every second for up to `HALT_DELAY` hours (48 h by
default)**, then shuts the Pi down. While it waits, nothing else is started,
including the volume sync. If you want to run without a remote, set
`ENABLE_REMOTE = False`.

### Network addresses

```python
CAMILLA_ADDR = ("127.0.0.1", 1234)   # CamillaDSP WebSocket
LMS_ADDR     = ("127.0.0.1", 9090)   # Lyrion CLI
```

### Paths

```python
CONFIG_DIR = "~/camilladsp/configs/"   # DSP configs (expanded to the user's home)
# settings.json is stored next to lybrica.py
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

### Other settings (advanced)

| Constant | Default | Meaning |
|---|---|---|
| `TOSLINK_TV_ON_DB` | `-30.0` | TOSLINK level (dB) at or above which the source is considered "TV" |
| `VOLUME_SLOWDOWN` | `{UP: 2, DOWN: 1}` | Volume step applied once every N key repeats (the remote repeats every ~40 ms) |
| `HOLD_TOGGLE_POWER` | `2` | POWER held: repeats before the relay toggles |
| `HOLD_SHUTDOWN` | `300` | POWER held: repeats before the Pi shuts down (about 12 s at 40 ms per repeat) |
| `HOLD_STOP` / `HOLD_SEEK` | `10` | PLAY/PAUSE: repeats before *stop*; NEXT/PREVIOUS: repeats before seeking starts |
| `HOLD_SWITCH_TONE_TILT` | `15` | ENTER held: repeats before switching tone/tilt screen |
| `SEEK_STEP_S`, `SEEK_INTERVAL_S` | `10`, `0.5` | Seek jump (seconds) and delay between jumps while the key is held |
| `TONE_MAX_DB`, `PRESENCE_MAX_DB`, `TILT_MAX_STEPS`, `TILT_DB_STEP` | `9`, `3`, `3`, `2` | Tone / presence / tilt ranges |
| `LOUDNESS_MIN`, `LOUDNESS_MAX`, `LOUDNESS_STEP` | `-50`, `-10`, `5` | Loudness reference range and step (dB) |
| `LMS_PLAYER_MANUAL` | `""` | Player MAC address; empty = MAC of the default network interface |
| `LMS_SYNC_DEBUG` | `False` | Log every volume/mute change and raw LMS notification |
| `LMS_COALESCE_S`, `LMS_RESTORE_WINDOW_S` | `0.05`, `0.5` | Burst merging and post-unmute correction window |
| `LMS_CONNECT_TIMEOUT_S`, `LMS_RETRY_S` | `5`, `5` | LMS CLI connection timeout and restart delay |
| `POLL_INTERVAL_S`, `CDSP_RECONNECT_DELAY_S` | `0.25`, `2.0` | CamillaDSP polling period and reconnection delay |
| `ADC_LEVELS`, `BRIGHTNESS_LEVELS`, `HYSTERESIS_RATIO` | see [Display](#display) | Ambient light → brightness mapping |

---

## Feature switches

Each feature can be disabled independently. When disabled, the corresponding
hardware library is **not imported** and **not required**.

| Switch | Default | Values | Effect |
|---|---|---|---|
| `ENABLE_LMS_SYNC` | `True` | `True` / `False` | Lyrion <-> CamillaDSP volume/mute sync |
| `ENABLE_AUTO_POWEROFF` | `True` | `True` / `False` | Power relay: off after silence, on with sound |
| `ENABLE_REMOTE` | `True` | `True` / `False` | HID remote control (all keys; if `False`, no key works) |
| `ENABLE_DISPLAY` | `True` | `True` / `False` | TM1637 display + ambient light sensor (the tone / tilt / loudness keys need it) |
| `ENABLE_CONFIG_SWITCH` | `"gpio"` | `False` / `"gpio"` / `"toslink"` | Automatic config switching — **only one method at a time** |

### `ENABLE_CONFIG_SWITCH` details

The switch is a **single choice**. Set it to exactly one of these three
values:

| Value | Meaning |
|---|---|
| `False` | Automatic config switching is off. Only the manual keys `BACK` and `HOMEPAGE` can change the config. |
| `"gpio"` | The TV state is read on `TV_GPIO` (0 = TV on). |
| `"toslink"` | The TOSLINK input is monitored through ALSA. `TOSLINK_SILENCE_DELAY_S` seconds of silence switches back to Squeezelite. |

`"gpio"` and `"toslink"` are **mutually exclusive** — you cannot run both
detectors at the same time. An invalid value raises `ValueError` at startup
(checked in the `FEATURES` block of `lybrica.py`).

#### TOSLINK mode dependencies

The `"toslink"` mode imports the `alsaaudio` Python module
(`pyalsaaudio`). The installer installs it, together with its system
dependency `libasound2-dev` (installed by **option 2 / Upgrade System**; see the
note in [Quick install](#quick-install)):

```bash
# System library (ALSA headers, needed to BUILD pyalsaaudio)
sudo apt install -y libasound2-dev

# Python binding, installed inside the venv used by the service
sudo /opt/venv/bin/pip install pyalsaaudio
```

If the module is missing (for example after a manual install that skipped
the step), `lybrica.py` raises `ModuleNotFoundError: No module named
'alsaaudio'` at startup and the service **crashes in a restart loop**
(`Restart=on-failure`, `RestartSec=5`). In that case, run the two commands
above and then:

```bash
sudo systemctl restart lybrica
```

> ⚠️ `libasound2-dev` is needed to **compile** `pyalsaaudio` (it provides the
> ALSA headers). Once built, only the runtime library `libasound2` is needed,
> and it is present on any Raspberry Pi OS image. On most images
> `python3-alsaaudio` is also available via `apt`, which avoids compilation
> altogether:
>
> ```bash
> sudo apt install -y python3-alsaaudio
> ```
>
> Because the service uses a venv created with `--system-site-packages`, the
> apt package is visible from `/opt/venv`.

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
| `UP` / `DOWN` | Bass +/- on the tone screen; tilt +/- on the tilt screen; loudness +/- on the loudness screen | - |
| `LEFT` / `RIGHT` | Treble -/+ on the tone screen; presence -/+ on the tilt screen; nothing on the loudness screen | - |
| `ENTER` | Reset / restore tone values (tone and tilt screens); toggle loudness on/off (loudness screen) | Switch between tone and tilt screens |
| `BACK` | Next config whose filename starts with `_` (stereo group) | - |
| `HOMEPAGE` | Next config whose filename starts with `\|` (TV group) | - |
| `POWER` | - | Held ~2 repeats: toggle amplifier relay. Held ~300 repeats (about 12 s): shut down the Pi |

Notes:

- The UP / DOWN / LEFT / RIGHT / ENTER behaviours need `ENABLE_DISPLAY = True`.
- Toggling the amplifier relay with POWER needs `ENABLE_AUTO_POWEROFF = True`.
  The shutdown works regardless.
- If the display is blanked (after a long silence), the **first** key press only
  wakes it up and is otherwise ignored.

### Screen navigation

- From the **volume screen**, an arrow key opens the last used **tone** or
  **tilt** screen; `ENTER` opens the **loudness** screen.
- Between tone and tilt: long-press `ENTER`.
- The loudness screen is reachable **only from the volume screen**. To go from
  loudness to tone/tilt, wait for the automatic return to the volume screen
  (`SCREEN_RETURN_DELAY`, 30 s by default) and press an arrow key.
- Any other screen returns to the volume screen after `SCREEN_RETURN_DELAY`
  seconds without a key press.

---

## Display

The TM1637 display shows:

- **Volume screen**: config label (3 chars) + volume (0-100). At 100 the volume
  is shown as `HI`; when muted it is shown as `--`.
- **Tone screen**: bass / treble (` 3B 2T`)
- **Tilt screen**: tilt / presence (` 2T 1P`), tilt in steps of `TILT_DB_STEP` dB
- **Loudness screen**: reference level (`-30 Ld`); ` No Ld` when loudness is off
- **Power screen**: `PW ON` / `PW OFF` / ` HALT `

A value shown as `--` means the corresponding filter does not exist (or is not
used by the pipeline) in the active config.

Special indicators:

- Decimal point on the 3rd character: auto power-off active
- Decimal point on the 4th character: power relay ON
- Top segment on the 4th character: tone settings active

### Automatic brightness

The ambient-light value received from the ESP32 is compared with four
thresholds (`ADC_LEVELS = [80, 1000, 2500, 3500]`), which gives **five
brightness levels** (`BRIGHTNESS_LEVELS = [0, 1, 2, 3, 7]`), with a 3 %
hysteresis (`HYSTERESIS_RATIO`) between two levels to avoid flickering.

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

Automatic configuration switching uses filename prefixes in `CONFIG_DIR`
(`~/camilladsp/configs/`):

- `_` identifies the stereo/Squeezelite group, selected with `BACK`.
- `|` identifies the TV group, selected with `HOMEPAGE`.
- `ENABLE_CONFIG_SWITCH = False` disables automatic switching (manual keys
  still work).
- `ENABLE_CONFIG_SWITCH = "gpio"` uses the TV-state input (`TV_GPIO`).
- `ENABLE_CONFIG_SWITCH = "toslink"` detects audio on the TOSLINK input and
  uses `TOSLINK_SILENCE_DELAY_S` before returning to Squeezelite. The capture
  device, rate and channels are read at startup from the **first `|*` config
  file**, so all `|*` configs must share the same capture input.

Make sure the configuration files in `CONFIG_DIR` use the prefixes expected
by your setup. The installer creates a default `_Setup.yml` there.

Tone filters are looked up **by name** in each config: `Bass`, `Treble`,
`Tilt`, `Presence` (parameter `gain`) and `Loudness` (parameter
`reference_level`). A filter must also be listed in an enabled `Filter` step of
the pipeline to be taken into account; otherwise it is shown as `--` and its
keys do nothing.

---

## Settings file

`settings.json` (stored next to `lybrica.py`) contains:

- the saved tone-related values for each configuration (`configs`), including
  the previous values used by the ENTER restore function;
- the last config used in each group (`last_config`, for `_` and `|`), so that
  a TV on/off cycle or a restart returns to the last used config of the group;
- the last tone/tilt screen selection (`last_tone_tilt`).

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
| GPIO0 (ADC1 channel 0) | Input | Ambient-light sensor (LDR) |
| GPIO5 | Output | Raspberry Pi power-relay control: HIGH = relay on, LOW = relay off |
| GPIO6 | Input | Raspberry Pi power-good signal; has an internal pull-down |
| GPIO7 | Input | Physical button; also used to request BLE scanning during startup |
| GPIO8 | Output | Bluetooth status LED (on-board LED), inverted logic: LOW = on, HIGH = off |
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

> **Console output.** The firmware sends the light readings on **UART1 TX
> (GPIO21)**. By default, ESP-IDF on the ESP32-C3 also sends its console logs
> to UART0, whose TX pin is also GPIO21. To keep the serial link to the Pi
> clean, set the console to the USB port: `idf.py menuconfig` → *Component
> config → ESP System Settings → Channel for console output* →
> **USB Serial/JTAG**. This is also what makes `monitor` work over USB.

### Ambient-light sensor wiring

The firmware reads the LDR on **GPIO0 (ADC1 channel 0)** through a simple
voltage divider:

```
3V3 ──── LDR ────┬──── GPIO0 (ADC)
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

The ESP32 firmware acts as a **BLE HID host**. It stores the **address (MAC)**
of the remote in its NVS and connects to it by address. This pairing is
independent from the Raspberry Pi's own Bluetooth pairing (see
[Using one remote with both the ESP32 and the Pi](#using-one-remote-with-both-the-esp32-and-the-pi)).

**One-time pairing procedure:**

1. Power off the ESP32-C3 (or hold its reset).
2. Put the BLE remote in **pairing mode** (usually hold its pairing button
   until the LED blinks fast — see your remote's manual).
3. **Hold GPIO7 LOW** (press the ESP32 button) while the ESP32-C3 boots, and
   keep it held until the scan starts. Concretely: press the button, release
   the ESP32 reset, and release the button once the scan has begun (watch the
   `Starting BLE scan` line in the console; the status LED does **not**
   indicate the scan).
4. The firmware scans BLE for **5 seconds** and prints every advertisement
   with RSSI ≥ −80 dBm to its console. It then keeps the **strongest** device
   and saves its MAC in NVS under `target_mac`. The strongest device is
   simply the strongest BLE advertiser nearby, **not necessarily a remote**.
5. The ESP32 automatically **restarts**. On the next boot, it loads the saved
   MAC and opens the BLE HID connection to the remote.

**Re-pairing to another remote:**

- Repeat the same procedure: hold GPIO7 LOW at boot. The previous MAC in NVS
  is **overwritten** by the new strongest device found during the scan. If no
  device qualifies, the previous MAC is kept.

**If the scan picks the wrong device:**

- Only devices advertising with **RSSI greater than −80 dBm** are eligible.
  Move the remote closer to the ESP32 (a few centimetres) during the 5-second
  scan window, and move other BLE devices (phones, watches, speakers) away.

**Checking the status (status LED on GPIO8, on-board):**

- **Solid on**: a BLE HID connection is established and the remote is ready.
- **Slow blink** (on ~300 ms / off ~1.7 s): **no MAC is saved** in NVS. Re-do
  the pairing procedure.
- **Off**: a MAC is saved but the connection is not (yet) established. The
  firmware retries every 5 seconds; check that the remote is on, in range, and
  not connected to another host.
- The ESP32 logs (USB console) show:
  ```
  BLE_SCAN: ADV aa:bb:cc:dd:ee:ff   RSSI=-52
  BLE_SCAN: Best BLE device: aa:bb:cc:dd:ee:ff   RSSI=-52
  BLE_SCAN: MAC saved to NVS.
  BLE_SCAN: Restarting ESP32-C3...
  ```

#### Using one remote with both the ESP32 and the Pi

Two different things use a remote:

- **The ESP32** uses it to **wake the Raspberry Pi** (any HID input while the Pi
  is off triggers the relay sequence).
- **The Pi** (`lybrica.py`, via `evdev`) uses it for all the **key actions**
  (volume, tone, player, configs...). `ENABLE_REMOTE` and `REMOTE_NAME` refer to
  the remote as seen by the Pi, paired with the installer's option 8 (`bluetuith`).

As soon as the relay activates, the ESP32 calls `deinit_bluetooth()` and drops
its BLE connection, which frees the remote so that the Pi can connect to it once
booted. After the Pi shuts down, the ESP32 restarts and reconnects to the saved
MAC.

This hand-over only works if **the remote accepts being connected to two hosts
one after the other**. Many BLE remotes do, but some keep a single bond and
will refuse the second host. If yours does not, pair it with the Pi only and
use the ESP32's physical button (GPIO7) to power on. Use the remote
only on one side at a time; do not expect both to be connected simultaneously.

**Note on BLE shutdown:**

- While the relay is active (Pi powered on), the BLE remote **cannot** send
  HID events to the ESP32. Only the physical button (GPIO7) can request the
  Raspberry Pi shutdown during that window.
- This is intentional: it avoids a BLE event immediately re-triggering the
  relay while the Pi is booting, frees the remote for the Pi, and frees the BLE
  stack while the ESP32 is only polling GPIO6 (see
  [Bluetooth shutdown when the relay activates](#bluetooth-shutdown-when-the-relay-activates)).

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
that output is not explicitly returned to LOW afterward (it is reset by the
ESP32 restart that follows the shutdown); do not assume it is a short pulse.

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

Check that the remote name matches `REMOTE_NAME` in `lybrica.py`. If the remote
is missing at startup, LyBriCa keeps retrying (for up to `HALT_DELAY` hours)
before shutting the Pi down, and starts nothing else in the meantime.

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

### Shutdown from the remote (POWER very-long-press) does nothing

`lybrica.py` halts the Pi with `os.system("sudo shutdown -h now")`. If the
user running the service is not allowed to run `shutdown` as root **without a
password**, the command fails silently (its exit code is not checked).

Check the rule:

```bash
cat /etc/sudoers.d/lybrica
# expected:
# <your-user> ALL=(ALL) NOPASSWD: /sbin/shutdown

ls -l /etc/sudoers.d/lybrica
# expected mode: -r--r----- (440)
```

Test it correctly — **the credential cache must be cleared first**, otherwise
a previous `sudo` call may make the test pass for the wrong reason:

```bash
sudo -k
sudo -n shutdown -h now
```

- If the Pi shuts down: the rule is correct.
- If you see `sudo: a password is required`: add the rule (see
  [Manual installation → Permissions](#permissions)).

> ⚠️ A `sudo -n ...` test run **right after** another `sudo` command is
> unreliable, because `sudo` caches the credential for ~15 minutes by default.
> Always run `sudo -k` before testing.

### Display stays blank

- Check the TM1637 wiring (CLK, DIO)
- Check the ambient light sensor value in the logs
- Check that `ENABLE_DISPLAY = True`
- After a long silence the display is blanked on purpose: press a key or play
  sound to wake it

### Display brightness does not follow the light

- Check that the ESP32 sends values: `ADC = ... -> Brightness = ...` lines
  appear in the journal when the level changes.
- If the values never change when you cover the LDR, check that the LDR is on
  **GPIO0** of the ESP32-C3 (ADC1 channel 0), and adjust `ADC_LEVELS`.

### ESP32 not responding

- Check the serial port: `ls -l /dev/serial0`
- Check the baud rate: `115200`
- Check the ESP32 logs (if available)
- Check that `enable_uart=1` is present in `/boot/firmware/config.txt` and that
  `console=serial0,...` has been removed from `/boot/firmware/cmdline.txt`.
- Check that the ESP32 console is set to USB Serial/JTAG (see
  [Flashing the firmware](#flashing-the-firmware)), so that no log text is mixed
  with the light readings on GPIO21.

### BLE remote does not connect to the ESP32

- Verify that the remote was in **pairing mode** during the 5-second scan.
- Verify that GPIO7 was held LOW at ESP32 boot.
- Move the remote closer than usual (RSSI must be **> −80 dBm**).
- Check the ESP32 console logs for `ADV` and `MAC saved to NVS`.
- If the LED on GPIO8 blinks slowly, no MAC was saved: re-do the pairing.
- If the LED is off, a MAC is saved but the connection fails: check that the
  remote is on, in range, and not connected to another host (for example the Pi).

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

## Known limitations

- **ESP32 firmware, ADC API**: it uses the legacy `driver/adc.h` API, which is
  deprecated in ESP-IDF 5.x and removed in later major versions. A move to
  `esp_adc/adc_oneshot.h` will be needed for newer ESP-IDF releases.
- **ESP32 firmware, no saved MAC**: the firmware still logs `Loaded MAC` and
  tries to open the HID connection with an all-zero address, retrying every
  5 seconds. Harmless, but the log line is misleading; rely on the slow LED
  blink to detect this state.
- **ESP32 firmware, button while the relay is active**: once the relay
  sequence is running, the button is handled inside that sequence (second press
  asserts GPIO10); the "relay already active" branch of the main loop is not
  reached.
- **Config switching**: `ENABLE_CONFIG_SWITCH = 0` is accepted by the startup
  check (since `0 == False`) and behaves like `False`.
- **Installer, `/etc/sudoers.d/lybrica`**: syntax is checked after the file is
  written (see [Sudoers rule](#sudoers-rule-added-by-the-installer)).
- **Installer, USB drive**: only `/dev/sda1` is auto-detected; otherwise the
  UUID must be typed in.

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
