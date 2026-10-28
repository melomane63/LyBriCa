# LyBriCa

**Ly**rion + **Bri**dge + **Ca**millaDSP

A Raspberry Pi audio-control project connecting
**Lyrion Music Server** and **CamillaDSP**. It runs on **Raspberry Pi OS
(Debian)** and on **piCorePlayer**.

LyBriCa is built around **five independent features**, all **enabled by
default**. Each one can be turned off on its own by editing the corresponding
switch at the top of `lybrica.py`:

| # | Feature | Switch |
|---|---|---|
| 1 | Bidirectional volume / mute synchronisation | `ENABLE_LMS_SYNC` |
| 2 | BLE HID remote (volume, tone, player control, configs) | `ENABLE_REMOTE` |
| 3 | TM1637 display with automatic brightness | `ENABLE_DISPLAY` |
| 4 | Trigger control for amplifier power | `ENABLE_AUTO_POWEROFF` |
| 5 | Automatic DSP configuration switching (TOSLINK detection) | `ENABLE_TOSLINK_SWITCH` |

An optional **ESP32-C3 companion** manages the Raspberry Pi power, reads
a physical button and the BLE HID remote, and sends ambient-light readings to
the Pi over UART serial.

When a feature is disabled, the hardware library it needs is **not imported
and not required**, so LyBriCa can run on a minimal setup (for example
volume-sync only, with no display and no remote). At least one feature must be
enabled, otherwise `lybrica.py` stops at startup with a `ValueError`. See
[Feature switches](#feature-switches) for details.

---

## Table of contents

1. [What it does](#what-it-does)
2. [Architecture](#architecture)
3. [Hardware](#hardware)
4. [Software components](#software-components)
5. [Quick install (Raspberry Pi OS)](#quick-install-raspberry-pi-os)
6. [What the installer does](#what-the-installer-does)
7. [Raspberry Pi boot configuration](#raspberry-pi-boot-configuration)
8. [Manual installation](#manual-installation)
9. [piCorePlayer](#picoreplayer)
10. [Configuration](#configuration)
11. [Feature switches](#feature-switches)
12. [Lyrion Music Server setup](#lyrion-music-server-setup)
13. [Remote control keys](#remote-control-keys)
14. [Display](#display)
15. [Volume curve](#volume-curve)
16. [DSP configuration files and automatic switching](#dsp-configuration-files-and-automatic-switching)
17. [Tone and loudness sharing (`SAVE_MODE`)](#tone-and-loudness-sharing-save_mode)
18. [Settings file](#settings-file)
19. [ESP32-C3 companion](#esp32-c3-companion)
20. [Systemd service](#systemd-service)
21. [Troubleshooting](#troubleshooting)
22. [Known limitations](#known-limitations)
23. [License](#license)

---

## What it does

LyBriCa keeps **Lyrion Music Server** (ex-LMS) and **CamillaDSP** in sync:

- **Bidirectional volume / mute sync** between Lyrion (on Material Skin) and CamillaDSP
- **Automatic DSP config switching** (Squeezelite `_*` configs / TV `|*` configs)
  by listening to the **TOSLINK** input through ALSA
- **BLE HID remote control** (volume, mute, player control, config, tone, tilt,
  loudness, power)
- **TM1637 display** with ambient light sensor for automatic brightness
- **Power relay control**: switch off after silence, back on with sound,
  halt the Pi after a very long silence
- **Physical power on/off** handled by an ESP32 companion
- **Per-config tone and loudness settings** saved in `settings.json`, shared
  between configs according to `SAVE_MODE`

---

## Architecture

```
                               +-----------+
                               |  Sensors  |
                               |   Light   |
                               |   Button  |
                               +-----+-----+
                                     |
                                     | ADC / GPIO
                                     v
+--------------+           +---------+---------+           +----------------+
| Power supply |---------->|       ESP32       |---------->| Pi power relay |
+--------------+           +---------+---------+           +-------+--------+
                                     |                             |
                         GPIO / UART |                             | 5V
                                     v                             v
+----------------------------------------------------------------------------+
|                                Raspberry Pi                                |
|                 volume / mute              volume / mute                   |
|     +----------+             +------------+             +------------+     |
|     |  Lyrion  |<----------->|  LyBriCa   |<----------->| CamillaDSP |     |
|     |  (LMS)   |  CLI :9090  |  (bridge)  |   WS :1234  |   engine   |     |
|     +----------+             +------------+             +------------+     |
|                                                                            |
|  Audio path: Lyrion -> Squeezelite -> ALSA loopback -> CamillaDSP -> DAC   |
|                                                                            |
+----------------------------------------------------------------------------+
            | evdev                  |                       |
            v                        v                       v
    +--------------+        +---------------+        +--------------+
    |  HID remote  |        | TM1637 display|        |  Trigger Amp |
    +--------------+        | CLK 23 DIO 24 |        |   (GPIO 4)   |
                            +---------------+        +--------------+

            +-------------------------------------------+
            | TOSLINK in (ALSA capture device)          |
            | probed by LyBriCa while the "_*" config   |
            | is active, captured by CamillaDSP while   |
            | the "|*" config is active                 |
            +-------------------------------------------+
```

How the pieces talk to each other:

- **Volume bridge**: LyBriCa listens to Lyrion through its CLI
  (port 9090) and reads/writes the CamillaDSP main volume and mute through its
  WebSocket (port 1234), in both directions. CamillaDSP is read by a **single
  poller** (`cdsp_poller`) that publishes into a shared state; the display, the
  TOSLINK monitor and the bridge all subscribe to it. A CamillaDSP change is
  forwarded to Lyrion only after it has been seen on two consecutive readings,
  and readings that started before LyBriCa's own last write are discarded (see
  [Lyrion Music Server setup](#lyrion-music-server-setup)).
- **Audio path** (it does not go through LyBriCa): Lyrion streams to
  Squeezelite, which plays into the ALSA loopback (`hw:Loopback,0`);
  CamillaDSP captures it (`hw:Loopback,1`), applies the filters and
  plays to the DAC.
- **ESP32 <-> Pi signals** (Raspberry Pi OS; see [piCorePlayer](#picoreplayer) for
  the pCP pins):
  - ESP32 GPIO5 -> Pi power relay (HIGH = on)
  - ESP32 GPIO10 -> Pi GPIO20: shutdown request (`gpio-shutdown` overlay)
  - Pi GPIO21 -> ESP32 GPIO6: power-good (`gpio-poweroff` overlay)
  - ESP32 GPIO21 -> Pi GPIO15: ambient light over UART (115200 8N1)
- **HID remote**: BLE to the ESP32 while the Pi is off (wake-up), then to the Pi
  (evdev) once it has booted. The same remote is used on both sides, one at a
  time (see [Using one remote with both the ESP32 and the Pi](#using-one-remote-with-both-the-esp32-and-the-pi)).
- **Config switching**: LyBriCa tells the two groups of configs apart by the
  filename prefix (`_` = streamer, `|` = TV). While a `_*` config is active,
  CamillaDSP does not use the TOSLINK input, so LyBriCa probes it through ALSA;
  when the TV plays, it loads the last used `|*` config. While a `|*` config is
  active, CamillaDSP captures the TOSLINK input, and LyBriCa uses CamillaDSP's
  own level meter to know when the TV has gone quiet while Lyrion is playing
  (see [DSP configuration files and automatic switching](#dsp-configuration-files-and-automatic-switching)).

The **ESP32** manages the Raspberry Pi's physical power supply and can request
a wake-up from a button or remote-control event. It also reads the ambient-light
sensor and sends readings to the Pi over serial.

This is separate from the **GPIO 4 relay controlled by LyBriCa on the Pi**:
that relay switches the amplifier, not the Raspberry Pi's own power supply.

---

## Hardware

| Component | Role | Connection |
|---|---|---|
| Raspberry Pi (**64-bit capable** on Raspberry Pi OS, see note) | Runs LyBriCa | - |
| ESP32-C3 | Raspberry Pi power relay, BLE HID remote, light sensor | UART TX to `/dev/serial0` |
| HID remote | User input | USB / Bluetooth (evdev) |
| TM1637 display | 6-digit display | GPIO 23 (CLK), GPIO 24 (DIO) |
| Power relay | Cuts power to the amplifier | GPIO 4 |
| TOSLINK input | Source detection (automatic config switching) | ALSA capture device (optional) |
| Ambient light sensor | Auto brightness | Via ESP32 (serial) |

> **64-bit only on Raspberry Pi OS.** The Debian installer downloads `aarch64`
> binaries (CamillaDSP, CamillaGUI), an `arm64` build of `bluetuith`, and sets
> `arm_64bit=1` in `config.txt`. A 32-bit-only Raspberry Pi (Pi 1, Zero,
> original Pi 2) is not supported by that installer. The piCorePlayer installer
> also supports 32-bit (`armv7`) builds.

---

## Software components

| Component | Language | Role |
|---|---|---|
| **LyBriCa** | Python 3 | Main bridge, asyncio event loop |
| **ESP32-C3 companion** | C / ESP-IDF | Power management, BLE HID host, light sensor, UART output |
| **CamillaDSP** | Rust | Audio DSP engine |
| **Lyrion Music Server** | Perl | Music server |

---

## Quick install (Raspberry Pi OS)

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

> **Running on piCorePlayer?** Use `install_pcp.sh` instead, see
> [piCorePlayer](#picoreplayer).

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
| **6. LyBriCa** | Rewrites `/boot/firmware/config.txt` and cleans `cmdline.txt`, adds the user to the required groups, creates the passwordless shutdown sudoers rule, creates `/opt/venv`, installs Python dependencies (pycamilladsp pinned to v4.0.0), downloads `lybrica.py` and `tm1637_lgpio.py`, creates and starts the systemd service |
| **7. Bluetooth remote** | Installs `bluetuith` if needed, launches it for pairing, detects the remote with `evdev`, updates `REMOTE_NAME` in `lybrica.py`, restarts the service |
| **8. Shutdown** | Prompts for an optional system shutdown at the end |

### Files and directories touched by `install_lybrica()`

| Path | Change | Backup |
|---|---|---|
| `/boot/firmware/config.txt` | **Rewritten entirely** (see [Raspberry Pi boot configuration](#raspberry-pi-boot-configuration)) | `config.txt.bak` |
| `/boot/firmware/cmdline.txt` | `console=serial0,NNNN` removed | `cmdline.txt.bak` |
| `/etc/sudoers.d/lybrica` | Created (passwordless shutdown), mode `440` | - |
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
SUDOERS_TMP=$(mktemp)
echo "$USER ALL=(ALL) NOPASSWD: /sbin/shutdown" > "$SUDOERS_TMP"
if ! sudo visudo -c -f "$SUDOERS_TMP" > /dev/null; then
    rm -f "$SUDOERS_TMP"
    echo "Invalid sudoers rule, nothing installed."
    exit 1
fi
sudo install -m 440 -o root -g root "$SUDOERS_TMP" /etc/sudoers.d/lybrica
rm -f "$SUDOERS_TMP"
```

This rule is **required** for `lybrica.py` to halt the Pi without a password
(see `shutdown_system()`, which runs `sudo poweroff`). Without it, the
**POWER very-long-press → shutdown** feature, and the automatic halt after
`HALT_DELAY` hours of silence, fail silently: the command's exit code is not
checked.

> ⚠️ `lybrica.py` calls **`sudo poweroff`**. Make sure the rule covers that
> command on your system (`command -v poweroff` gives its path; add it to the
> rule if it is not already authorised) and test it as described in
> [Manual installation → Permissions](#permissions).

The rule is first written to a temporary file and checked with `visudo -c`
(which does not modify anything and only returns a non-zero exit code on
error). It is copied to `/etc/sudoers.d/lybrica` (mode `440`, owner `root`)
only if the syntax is valid, so an invalid rule can never break `sudo`
system-wide. If the check fails, the installer stops and nothing is installed.

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

The Raspberry Pi OS installer's boot overlays expect the ESP32 to be wired as
follows (the piCorePlayer installer uses other Pi pins for the two overlay
signals, see [piCorePlayer](#picoreplayer)):

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
- Python 3.9+ (see the note on `audioop` below for Python 3.13+)
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

Only the libraries of the features you enable are required (see
[Feature switches](#feature-switches)).

> **`audioop` and Python 3.13+.** The TOSLINK probe uses the standard-library
> `audioop` module, which was **removed in Python 3.13**. On Python 3.13 or
> later, install the replacement package: `pip install audioop-lts`
> (Debian: `apt install python3-audioop-lts`). On Python 3.11 / 3.12 it is part
> of the standard library (a deprecation warning is silenced).

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

And for the shutdown command (required by `shutdown_system()` in `lybrica.py`,
which runs `sudo poweroff`):

```bash
SUDOERS_TMP=$(mktemp)
echo "$USER ALL=(ALL) NOPASSWD: /sbin/shutdown, /sbin/poweroff" > "$SUDOERS_TMP"
if ! sudo visudo -c -f "$SUDOERS_TMP" > /dev/null; then
    rm -f "$SUDOERS_TMP"
    echo "Invalid sudoers rule, nothing installed."
    exit 1
fi
sudo install -m 440 -o root -g root "$SUDOERS_TMP" /etc/sudoers.d/lybrica
rm -f "$SUDOERS_TMP"
```

Verify the rule is active. **The `sudo -k` step clears the credential cache,
otherwise the test is meaningless** (a previous `sudo` call in the same
session would make the test pass for the wrong reason):

```bash
sudo -k
sudo -n poweroff   # should NOT ask for a password, and shuts the Pi down
```

If `sudo -n` prints `sudo: a password is required`, the rule is missing or
incorrect and the **POWER very-long-press → shutdown** feature will not work.

---

## piCorePlayer

`install_pcp.sh` is an all-in-one installer for **piCorePlayer** (run it over
SSH as user `tc`, not as root). Everything is installed as piCore extensions
(`.tcz`) so it survives reboots.

```sh
sh install_pcp.sh
```

```
==============================================
  CamillaDSP installer for piCorePlayer
==============================================
  1) CamillaDSP + CamillaGUI
  2) LyBriCa + BLE HID bridge (patches pcp-bt.tcz)
  3) ESP32C3 (GPIO overlays, shutdown monitor)
  R) Reboot now
  Q) Quit
```

Several steps can be typed at once (`1 2 3`). **Recommended order: 1, 2, 3,
then a single reboot.** Each step runs in its own process: an error in one step
does not stop the menu, and nothing is saved (`pcp bu`) if that step fails.
Versions and URLs are all grouped at the top of the script.

| Step | What it does |
|---|---|
| **1. CamillaDSP + CamillaGUI** | Builds `CDSPpiCore.tcz` containing CamillaDSP (`5.0.0-beta.2`, `aarch64` or `armv7`) and CamillaGUI, with a start script run at boot. Persistent data (configs, coefficients, statefile, GUI config) lives in `/mnt/mmcblk0p2/tce/camilladsp`, outside the extension. Writes a minimal `loopback.yml` (Loopback,1 → gain → DAC; the DAC is taken from `OUTPUT` in `pcp.cfg` when it is a `hw:` device), loads `snd-aloop` at boot, and points Squeezelite to `hw:Loopback,0` in `pcp.cfg` (`-r 48000-48000`, `-R hLE`). Needs ~150 MB free on the partition. GUI: `http://pcp.local:5000/gui/index.html` |
| **2. LyBriCa + BLE HID bridge** | Builds `lybrica.tcz`: Python modules (pycamilladsp from the `next5` branch, `websocket_client`, `pyserial`, `lgpio`, `pyalsaaudio`, `tm1637_lgpio`), `lybrica.py`, and the `start_lybrica.sh` launcher (`lybrica` symlink). Python, evdev and bleak are permanent extensions; build tools are temporary. **Patches `pcp-bt.tcz`** with the `hid_ble_bridge.py` and `hidbridge` files from [pcp-ble-gatt](https://github.com/melomane63/pcp-ble-gatt) (the original md5 is kept as `.orig`; only warns on failure). Registers the launcher as `USER_COMMAND_1` in `pcp.cfg`. Bluetooth must be installed from the pCP web interface first. |
| **3. ESP32C3** | After confirmation, **adds** (nothing is removed) to `config.txt` under `[all]`: `dtoverlay=gpio-poweroff,gpiopin=26,active_low=1`, `dtoverlay=gpio-shutdown,gpio_pin=16,active_low=0,gpio_pull=down` and `enable_uart=1`. Sets the matching `GPIOPOWEROFF*` / `GPIOSHUTDOWN*` options in `pcp.cfg` and installs `shutdown-monitor.tcz` (required for the shutdown request to work). A backup `config.txt.bak` is created once. |

### ESP32 ↔ Raspberry Pi wiring on piCorePlayer

The overlays added by the pCP installer use **different Raspberry Pi pins** from
the Raspberry Pi OS installer. The ESP32 side is unchanged.

| ESP32-C3 pin | Function | Pi pin, Raspberry Pi OS | Pi pin, piCorePlayer |
|---|---|---|---|
| GPIO6 | Power-good (Pi → ESP32) | GPIO21 | **GPIO26** |
| GPIO10 | Shutdown request (ESP32 → Pi) | GPIO20 | **GPIO16** |
| GPIO21 (UART1 TX) | Ambient light | GPIO15 (RXD) | GPIO15 (RXD) |

On a bare Raspberry Pi these pins are free (GPIO 14/15 = UART). If you use a
HAT or another add-on board, check its pinout first.

### Differences when running on piCorePlayer

`lybrica.py` detects piCorePlayer by the presence of `/mnt/mmcblk0p2/tce` and
adapts automatically:

- the remote name is forced to **`pCP BLE HID Keyboard`** (the device created by
  the patched BLE HID bridge); `REMOTE_NAME` is ignored;
- the CamillaDSP configs are read from `/mnt/mmcblk0p2/tce/camilladsp/configs/`;
- LyBriCa is **not** a systemd service: it is started by the launcher below, and
  at boot it waits for CamillaDSP instead of crashing if it is not up yet.

### The `lybrica` command

```
lybrica start | restart | stop    (root required: use sudo)
lybrica status | log [N] | reset | help
```

The factory `lybrica.py` lives inside `lybrica.tcz` and is read-only. To change
a setting:

1. `lybrica reset` — create (or restore) the editable copy `/home/tc/lybrica-mod.py`
2. `vi /home/tc/lybrica-mod.py` — edit it
3. `sudo lybrica restart` — apply it (the copy overrides the factory file at each start)
4. `pcp bu` — persist across reboots

Deleting `lybrica-mod.py` falls back to the factory version at the next start.
Logs: `/home/tc/lybrica.log` (Python output), `/home/tc/pcp_startup.log`
(launcher trace), PID in `/home/tc/lybrica.pid`.

---

## Configuration

All configuration is at the top of `lybrica.py` (on piCorePlayer: in
`/home/tc/lybrica-mod.py`, see above):

```python
# Feature switches
ENABLE_LMS_SYNC       = True
ENABLE_AUTO_POWEROFF  = True
ENABLE_REMOTE         = True
ENABLE_DISPLAY        = True
ENABLE_TOSLINK_SWITCH = True
```

### Remote

```python
REMOTE_NAME = "HID Remote01 Keyboard"   # found with: python3 -m evdev.evtest
```

The Raspberry Pi OS installer (option 8) sets this line for you after pairing.
On piCorePlayer the name is fixed (`pCP BLE HID Keyboard`).

If `ENABLE_REMOTE = True` and the remote is not found, LyBriCa **keeps looking
for it every second, in the background**. Nothing else is blocked: the volume
sync, the display, the power relay and the TOSLINK switching keep running, and
the remote is picked up as soon as it appears (and again after each
disconnection).

### Network addresses

```python
CAMILLA_ADDR = ("127.0.0.1", 1234)   # CamillaDSP WebSocket (must stay local)
LMS_ADDR     = ("127.0.0.1", 9090)   # Lyrion CLI
```

### Paths

```python
CONFIG_DIR = "~/camilladsp/configs/"   # DSP configs (expanded to the user's home;
                                       # /mnt/mmcblk0p2/tce/camilladsp/configs/ on pCP)
# settings.json is stored next to lybrica.py
```

### GPIO pins

```python
POWER_GPIO = 4      # Power relay
CLK, DIO   = 23, 24 # TM1637 display
```

### Timing

```python
POWER_OFF_DELAY     = 2     # Minutes without sound AND without key press before the
                            # amplifier relay switches off (also the delay before the display is blanked)
HALT_DELAY          = 48    # Hours of continuous audio silence before the Pi shuts down
SCREEN_RETURN_DELAY = 30    # Seconds before returning to the volume screen
WAKE_DISPLAY_DELAY  = 30    # Seconds the display stays on after a key press woke it while the relay is already off
```

`ENABLE_AUTO_POWEROFF` controls the Pi-side amplifier relay and the associated
silence/shutdown behaviour. It is not the same as the ESP32's physical power
management for the Raspberry Pi. The halt after `HALT_DELAY` only depends on
audio silence (key presses do not postpone it).

### Silence and TOSLINK detection

```python
SILENCE_RMS_DB  = -80.0   # RMS below this -> silence
CREST_MIN_DB    = 2.0     # peak - rms below this -> steady noise floor, not audio
TOSLINK_ON_DB   = -60.0   # RMS above which TOSLINK audio is considered present
TOSLINK_DEBUG   = False   # log every probe (peak / rms / crest) to calibrate TOSLINK_ON_DB
```

Keep `TOSLINK_ON_DB` **above** `SILENCE_RMS_DB`; otherwise a signal could be
detected as TV and then immediately count as silence (a warning is logged at
startup).

### Other settings (advanced)

| Constant | Default | Meaning |
|---|---|---|
| `SAVE_MODE` | `"half"` | How tone / loudness are shared between configs, see [Tone and loudness sharing](#tone-and-loudness-sharing-save_mode) |
| `VOLUME_SLOWDOWN` | `{UP: 2, DOWN: 1}` | Volume step applied once every N key repeats (the remote repeats every ~40 ms) |
| `VOLUME_PEEK_S` | `0.5` | Seconds the volume stays shown after a volume key is released while muted |
| `HOLD_TOGGLE_POWER` | `2` | POWER held: repeats before the relay toggles (~1 s) |
| `HOLD_SHUTDOWN` | `200` | POWER held: repeats before the Pi shuts down (about 8 s) |
| `HOLD_STOP` / `HOLD_SEEK` | `10` | PLAY/PAUSE: repeats before *stop*; NEXT/PREVIOUS: repeats before seeking starts |
| `HOLD_SWITCH_TONE_TILT` | `15` | ENTER held: repeats before switching tone/tilt screen |
| `HOLD_TOGGLE_AUTO_S` | `2.0` | HOMEPAGE held: **seconds** (real time) before the TOSLINK automatism is enabled / disabled |
| `SEEK_STEP_FORWARD_S`, `SEEK_STEP_BACKWARD_S`, `SEEK_INTERVAL_S` | `30`, `10`, `0.8` | Seek jumps (seconds) and delay between jumps while the key is held |
| `TONE_MAX_DB`, `PRESENCE_MAX_DB`, `TILT_MAX_STEPS`, `TILT_DB_STEP` | `9`, `3`, `3`, `2` | Tone / presence / tilt ranges (the display has 2 digits: keep them ≤ 9) |
| `LOUDNESS_MIN`, `LOUDNESS_MAX`, `LOUDNESS_STEP` | `-50`, `-10`, `5` | Loudness reference range and step (dB) |
| `DEFAULT_LOUDNESS_REF` | `-20` | Loudness level restored by ENTER when no previous value is saved |
| `SCROLL_FRAME_DELAY`, `SCROLL_END_PAUSE` | `0.35`, `0.6` | Speed of scrolling messages (`POWER OFF`, `CONF AUTO`...) |
| `LMS_PLAYER_MANUAL` | `""` | Player MAC address; empty = MAC of the default network interface |
| `LMS_SYNC_DEBUG` | `False` | Log every volume/mute change and raw LMS notification |
| `LMS_RESTORE_WINDOW_S` | `0.5` | Post-unmute correction window |
| `LMS_CONNECT_TIMEOUT_S`, `LMS_RETRY_S` | `5`, `5` | LMS CLI connection timeout and restart delay |
| `POLL_INTERVAL_S`, `TITLE_EVERY`, `CDSP_RECONNECT_DELAY_S` | `0.25`, `2`, `2.0` | CamillaDSP polling period, config title/path read every N cycles, reconnection delay |
| `ADC_LEVELS`, `BRIGHTNESS_LEVELS`, `HYSTERESIS_RATIO` | see [Display](#display) | Ambient light → brightness mapping |

A background task that crashes is restarted automatically after 2 s, so a single
error does not bring the whole script down.

---

## Feature switches

Each feature can be disabled independently. When disabled, the corresponding
hardware library is **not imported** and **not required**.

| Switch | Default | Values | Effect | Needs |
|---|---|---|---|---|
| `ENABLE_LMS_SYNC` | `True` | `True` / `False` | Lyrion <-> CamillaDSP volume/mute sync | - |
| `ENABLE_AUTO_POWEROFF` | `True` | `True` / `False` | Power relay: off after silence, on with sound, halt after `HALT_DELAY` | `lgpio` |
| `ENABLE_REMOTE` | `True` | `True` / `False` | HID remote control (all keys; if `False`, no key works) | `evdev` |
| `ENABLE_DISPLAY` | `True` | `True` / `False` | TM1637 display + ambient light sensor (the tone / tilt / loudness keys need it) | `pyserial`, `tm1637_lgpio` |
| `ENABLE_TOSLINK_SWITCH` | `True` | `True` / `False` | Automatic config switching on the TOSLINK input | `pyalsaaudio`, `audioop` |

`pycamilladsp` is always required. The LMS CLI is used anyway by the player
keys (play / pause / seek) and by the TOSLINK monitor, even with
`ENABLE_LMS_SYNC = False` (that switch only controls the volume bridge).

### `ENABLE_TOSLINK_SWITCH` details

This switch replaces the former `ENABLE_CONFIG_SWITCH` (`"gpio"` / `"toslink"`):
**the GPIO TV-state input (`TV_GPIO`) has been removed**, TOSLINK detection is
now the only automatic method. The manual keys work whatever its value (see
[Remote control keys](#remote-control-keys)).

With the switch on, the automatism can additionally be **enabled / disabled at
run time** with a long press (2 s) on `HOMEPAGE`. It is enabled at every start.

If no `_*` or no `|*` config file exists, the automatism cannot run: the keys
then behave exactly as with the switch off.

#### TOSLINK mode dependencies

The TOSLINK mode imports `alsaaudio` (`pyalsaaudio`) and `audioop`. The
installer installs `pyalsaaudio`, together with its system dependency
`libasound2-dev` (installed by **option 2 / Upgrade System**; see the note in
[Quick install](#quick-install-raspberry-pi-os)):

```bash
# System library (ALSA headers, needed to BUILD pyalsaaudio)
sudo apt install -y libasound2-dev

# Python binding, installed inside the venv used by the service
sudo /opt/venv/bin/pip install pyalsaaudio
```

If a module is missing (for example after a manual install that skipped the
step), `lybrica.py` raises `ModuleNotFoundError` at startup and the service
**crashes in a restart loop** (`Restart=on-failure`, `RestartSec=5`). In that
case, install it (and `audioop-lts` on Python ≥ 3.13) and then:

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

### How the bridge behaves

- **At startup, CamillaDSP wins**: its volume and mute are pushed to Lyrion.
- A volume change coming from Lyrion is written to CamillaDSP immediately.
- A volume change coming from CamillaDSP (remote keys, GUI...) is pushed to
  Lyrion only once it has been read twice in a row.
- After an **unmute**, Lyrion may briefly announce a wrong volume (the `+/-`
  keys restart from 0 / 5 %). During `LMS_RESTORE_WINDOW_S` the bridge corrects
  Lyrion back to the right level and pushes nothing from CamillaDSP.
- If the connection to Lyrion is lost, the bridge restarts itself every
  `LMS_RETRY_S` seconds (one log line per distinct error).

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

All keys require `ENABLE_REMOTE = True`.

| Key | Short press | Long press |
|---|---|---|
| `VOLUMEUP` / `VOLUMEDOWN` | Volume +/-1 step | Continuous volume (up: every 2nd repeat, down: every repeat, see `VOLUME_SLOWDOWN`) |
| `MUTE` | Mute / unmute | - |
| `PLAYPAUSE` | Pause / resume (Lyrion) | Stop (Lyrion) |
| `PREVIOUSSONG` | Previous track | Rewind (10 s steps) |
| `NEXTSONG` | Next track | Fast-forward (30 s steps) |
| `UP` / `DOWN` | Bass +/- on the tone screen; tilt +/- on the tilt screen; loudness +/- on the loudness screen | - |
| `LEFT` / `RIGHT` | Treble -/+ on the tone screen; presence -/+ on the tilt screen; nothing on the loudness screen | - |
| `ENTER` | Tone / tilt screens: set bass, treble, tilt and presence **all** to 0, or restore their previous values if they are already all at 0. Loudness screen: toggle loudness on/off. Volume screen: open the loudness screen | Switch between tone and tilt screens |
| `BACK` | See [below](#back-and-homepage) | - |
| `HOMEPAGE` | See [below](#back-and-homepage) | See [below](#back-and-homepage) |
| `POWER` | - | Held ~1 s: toggle the amplifier relay (`POWER AUTO` / `POWER OFF`). Held ~8 s: shut down the Pi |

### BACK and HOMEPAGE

Config files are split in two groups by prefix: `_` (streamer) and `|` (TV).

| Situation | `BACK` | `HOMEPAGE` short press | `HOMEPAGE` long press (2 s) |
|---|---|---|---|
| `ENABLE_TOSLINK_SWITCH` on, automatism **ON** | Next config **inside the active group** (`_` or `\|`) | Scroll the tone, tilt and loudness values | Disable the automatism (`CONF MANUAL`) |
| `ENABLE_TOSLINK_SWITCH` on, automatism **OFF** | Next `_*` config | Next `\|*` config | Enable the automatism (`CONF AUTO`) |
| `ENABLE_TOSLINK_SWITCH` off (or no `_*` / `\|*` files) | Next `_*` config | Next `\|*` config | - |

When switching *to* a group from another one, LyBriCa lands on the **last config
used in that group** (kept in `settings.json`); inside a group, each press goes
to the next file in alphabetical order.

### Notes

- The UP / DOWN / LEFT / RIGHT / ENTER behaviours need `ENABLE_DISPLAY = True`.
- Toggling the amplifier relay with POWER needs `ENABLE_AUTO_POWEROFF = True`.
  The shutdown works regardless (it also saves the tone settings first).
- If the display is blanked (after a long silence), the **first** key press only
  wakes it up and is otherwise ignored (including its repeats and release).
- If the volume screen is not shown (or the volume is displayed as `--` while
  muted), the **first** volume key press only brings the volume screen back,
  without changing the volume.
- Any key press cancels a running scrolling message.

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

- **Volume screen**: config label (3 chars, taken from the config `title`, or
  `---` if it has none; the `_` and `|` prefixes are stripped) + volume (0-100).
  At 100 the volume is shown as `HI`; when muted it is shown as `--` (the real
  volume appears while a volume key is held, and for `VOLUME_PEEK_S` after).
- **Tone screen**: bass / treble (` 3B 2T`)
- **Tilt screen**: tilt / presence (` 2T 1P`), tilt in steps of `TILT_DB_STEP` dB
- **Loudness screen**: reference level (`-30 Ld`); ` No Ld` when loudness is off;
  `--- Ld` when the config has no Loudness filter
- **Scrolling messages**: `POWER AUTO`, `POWER OFF`, `POWER SAVE`, `CONF AUTO`,
  `CONF MANUAL`, then `HALT` before a shutdown. The volume screen comes back
  when the message ends.

A value shown as `--` means the corresponding filter does not exist (or is not
used by the pipeline) in the active config.

Special indicators on the volume screen:

- Blinking dot on the 3rd character: TOSLINK automatism **disabled** (`CONF MANUAL`)
- Blinking dot on the 4th character: power relay **forced OFF** with the POWER key
- Top segment on the 4th character: tone settings active (a non-zero bass,
  treble, tilt or presence gain)
- Bottom segment on the 4th character: loudness active

### Blanking and wake-up

After `POWER_OFF_DELAY` minutes of silence without key press, the relay is cut
(`POWER SAVE`) and the display is blanked. If the relay was already off, the
display goes off after `WAKE_DISPLAY_DELAY` seconds following a wake-up key
press. Sound (or a key) wakes it up again; with sound, the relay is switched
back on.

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

The curve is strictly increasing, and the inverse conversion rounds to the
nearest integer Lyrion level. Mute is synchronised separately from volume, so
level `0` should not be treated as the only mute mechanism.

---

## DSP configuration files and automatic switching

Configs live in `CONFIG_DIR` and are grouped by filename prefix:

- `_` identifies the stereo / Squeezelite group (captures the ALSA loopback).
- `|` identifies the TV group (captures the TOSLINK input).

Make sure the configuration files in `CONFIG_DIR` use the prefixes expected by
your setup. The installer creates a default `_Setup.yml` there (piCorePlayer:
`loopback.yml`, which you should rename / copy to a `_*` name).

### How the automatism works

It only runs when `ENABLE_TOSLINK_SWITCH = True` and the automatism is ON.

| Active config | What LyBriCa does | Action |
|---|---|---|
| `_*` (loopback) | CamillaDSP does not use the TOSLINK input, so LyBriCa **probes it through ALSA** (~0.5 s per cycle). A reading counts when its RMS is ≥ `TOSLINK_ON_DB` **and** its crest (peak − rms) is ≥ `CREST_MIN_DB` (a steady noise floor is ignored). | **2 consecutive readings** → switch to the last used `\|*` config |
| `\|*` (TOSLINK) | Once per second, asks Lyrion whether the player is playing, and reads the TV level from CamillaDSP. | Lyrion playing **and** TOSLINK silent for 2 cycles → switch back to the last used `_*` config. Lyrion starts playing **while the TV plays** → Lyrion is **paused** (the TV has priority). |

A failed switch is retried with an exponential delay (5 s, doubled each time,
up to 60 s). A config change made from outside (remote, CamillaDSP GUI) restarts
the detection from scratch. With the automatism disabled, there is no probing,
no automatic switch and no Lyrion pause.

### Capture parameters

The capture device, sample rate, channel count and sample format used for the
probe are read at startup from the **first `|*` config file** (alphabetical
order), so **all `|*` configs must share the same capture input**. Supported
capture formats: `S16_LE`, `S24_3LE`, `S24_LE`, `S32_LE`.

### Tone filters

Tone filters are looked up **by name** in each config: `Bass`, `Treble`,
`Tilt`, `Presence` (parameter `gain`) and `Loudness` (parameter
`reference_level`; `-100` means "off"). A filter must also be listed in an
enabled `Filter` step of the pipeline to be taken into account; otherwise it is
shown as `--` and its keys do nothing.

### Configs without a complete definition

A config that fails to parse, or has no (or an empty) `devices` section, is
rejected: LyBriCa keeps the previous config and logs a warning rather than
pushing an incomplete config to CamillaDSP.

---

## Tone and loudness sharing (`SAVE_MODE`)

Tone (bass, treble, tilt, presence) and loudness values are always **saved per
config** in `settings.json`. `SAVE_MODE` decides what is **copied into the new
config** when you switch, before it is applied:

| `SAVE_MODE` | Behaviour |
|---|---|
| `"all_config"` | Each config keeps its own tone **and** loudness. |
| `"all_group"` | All configs of the same group (`_` or `\|`) share the same tone and loudness: switching inside a group copies the current values to the new config. |
| `"half"` *(default)* | Tone per config, **loudness per group**: switching inside a group keeps the tone of the new config but copies the current loudness. |
| `"same_everywhere"` | All configs share the same tone and loudness, whatever the group. |

Coming back to a group needs no copy: you land on the last config used in that
group, and its values were saved when you left it. An unknown value raises
`ValueError` at startup.

---

## Settings file

`settings.json` (stored next to `lybrica.py`) contains:

- the saved tone-related values for each configuration (`configs`), including
  the previous values used by the ENTER restore function (`prev`) and the
  loudness reference level (`loudness_ref`);
- the last config used in each group (`last_config`, for `_` and `|`), so that
  a TV on/off cycle or a restart returns to the last used config of the group;
- the last tone/tilt screen selection (`last_tone_tilt`).

The values are saved at every config switch, at shutdown (POWER very-long-press)
and when LyBriCa receives `SIGTERM` / `SIGINT`. This needs `ENABLE_REMOTE` or
`ENABLE_TOSLINK_SWITCH` (tone is only managed when one of them is on).

At startup, the saved tone and loudness of the active config are re-applied, and
the config is pushed to CamillaDSP. After a CamillaDSP restart (detected by the
poller), LyBriCa reconnects, re-reads the active config and re-applies the saved
values.

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
  the remote as seen by the Pi, paired with the installer's option 8
  (`bluetuith`) on Raspberry Pi OS, or through the patched BLE HID bridge on
  piCorePlayer.

As soon as the relay activates, the ESP32 calls `deinit_bluetooth()` and drops
its BLE connection, which frees the remote so that the Pi can connect to it once
booted. After the Pi shuts down, the ESP32 restarts and reconnects to the saved
MAC.

This hand-over works with the reference remote of this project (the one
matching the default `REMOTE_NAME = "HID Remote01 Keyboard"`): it can be known
to the ESP32 (by its saved address) and paired with the Pi, and it reconnects to
whichever of the two is available. The remote is only ever connected to **one
host at a time**: the ESP32 while the Pi is off, the Pi once it has booted.

The hand-over depends on the remote itself, so it is **not guaranteed with
other models**. Some remotes keep a single bond or only advertise after a key
press, and would refuse the second host. If yours does, pair it with the Pi
only and use the ESP32's physical button (GPIO7) to power on.

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
integer values from `/dev/serial0` to adjust the TM1637 display brightness (only
the most recent complete line is used at each pass).

This firmware's serial output is for the **ambient-light ADC value only**. The
provided code does not send `POWER` messages or receive `ON` / `OFF` commands
through UART; power control is performed through the ESP32-C3 GPIOs.

> The firmware source, wiring diagram and flashing instructions are in the
> [`firmware/`](firmware/) directory of this repository, if included in your
> checkout.

---

## Systemd service

*(Raspberry Pi OS only; on piCorePlayer see [The `lybrica` command](#the-lybrica-command).)*

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

On `SIGTERM` / `SIGINT`, LyBriCa saves its settings, cuts the amplifier relay
and releases the GPIO before exiting.

---

## Troubleshooting

On piCorePlayer, replace `journalctl -u lybrica -f` with `lybrica log`.

### Remote not detected

```bash
/opt/venv/bin/python3 -m evdev.evtest
```

Check that the remote name matches `REMOTE_NAME` in `lybrica.py` (on pCP the name
must be `pCP BLE HID Keyboard`). If the remote is missing, LyBriCa keeps looking
for it every second **without blocking the other features**, and logs
`'<name>' found at ...` once it is grabbed. A message `Cannot grab ... will retry`
means another process holds the device.

### CamillaDSP unreachable

```bash
camilladsp --check
```

Check `CAMILLA_ADDR` and that CamillaDSP is running. At startup, LyBriCa waits
for CamillaDSP; once running, it reconnects by itself (`CamillaDSP unreachable
... reconnecting` then `CamillaDSP connection restored`).

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

`lybrica.py` halts the Pi with `sudo poweroff`. If the user running the service
is not allowed to run it as root **without a password**, the command fails
silently (its exit code is not checked). The display shows `HALT` even if the
command failed.

Check the rule:

```bash
cat /etc/sudoers.d/lybrica
# expected: <your-user> ALL=(ALL) NOPASSWD: ... (covering poweroff)

ls -l /etc/sudoers.d/lybrica
# expected mode: -r--r----- (440)
```

Test it correctly — **the credential cache must be cleared first**, otherwise
a previous `sudo` call may make the test pass for the wrong reason:

```bash
sudo -k
sudo -n poweroff
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
- Check that `enable_uart=1` is present in `config.txt` and (Raspberry Pi OS) that
  `console=serial0,...` has been removed from `/boot/firmware/cmdline.txt`.
- Check that the ESP32 console is set to USB Serial/JTAG (see
  [Flashing the firmware](#flashing-the-firmware)), so that no log text is mixed
  with the light readings on GPIO21.
- Note: with `ENABLE_DISPLAY = True`, LyBriCa opens `/dev/serial0` at startup;
  without the serial port, it fails to start.

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
- On piCorePlayer, make sure Bluetooth is installed from the pCP web interface
  and that step 2 of `install_pcp.sh` patched `pcp-bt.tcz` (check the
  `pcp-bt.tcz rebuilt` / `already contains these two files` message).

### Automatic configuration switching does not work

- Check that `ENABLE_TOSLINK_SWITCH = True` and that the automatism is **ON**
  (long-press `HOMEPAGE`: `CONF AUTO`; a blinking dot on the 3rd character means
  it is off).
- Check that at least one `_*` **and** one `|*` config exist in `CONFIG_DIR`:
  otherwise the logs show `[TOSLINK] No '|*' config file found` /
  `[LOOPBACK] No '_*' config file found`, detection is disabled and the keys
  behave as with the switch off.
- Check the capture parameters in the log (`[TOSLINK] Capture: device @ rate Hz,
  ch, format`) and that the format is one of `S16_LE`, `S24_3LE`, `S24_LE`,
  `S32_LE`.
- `[TOSLINK] Probe failed: ...` means the ALSA capture device cannot be opened
  (wrong device name, or it is already in use).
- Set `TOSLINK_DEBUG = True` to see the measured peak / rms / crest, and adjust
  `TOSLINK_ON_DB` (keep it above `SILENCE_RMS_DB`).
- If the TV is detected but LyBriCa goes straight back to `_*`, check that
  Lyrion is actually playing (`mode` is `play`) and that the TOSLINK level in
  the `|*` config is above the silence threshold.

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

- **TV detection by GPIO removed**: the former `ENABLE_CONFIG_SWITCH = "gpio"`
  mode and the `TV_GPIO` setting no longer exist. The only automatic method is
  TOSLINK detection (`ENABLE_TOSLINK_SWITCH`).
- **TOSLINK, shared capture input**: the probe parameters come from the first
  `|*` config file, so all `|*` configs must use the same capture input. The
  probe can only listen while the `_*` group is active (CamillaDSP holds the
  device in the `|*` group).
- **TOSLINK, Python 3.13+**: needs the `audioop-lts` package (see
  [Manual installation](#python-dependencies)).
- **Shutdown command**: `shutdown_system()` runs `sudo poweroff` without
  checking the result; the sudoers rule must allow it.
- **piCorePlayer, settings location**: `settings.json` is written next to
  `lybrica.py`. Check that your tone / loudness values survive a reboot on pCP
  (the script is launched from `/usr/local/bin`, which is not on the persistent
  partition) and run `pcp bu` if needed.
- **piCorePlayer, CamillaDSP version**: `install_pcp.sh` installs CamillaDSP /
  CamillaGUI `5.0.0-beta.2` with pycamilladsp from the `next5` branch, while the
  Raspberry Pi OS installer pins CamillaDSP `v4.1.3` and pycamilladsp `v4.0.0`.
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
- [pcp-ble-gatt](https://github.com/melomane63/pcp-ble-gatt) (BLE HID bridge for piCorePlayer)
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
