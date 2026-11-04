# LyBriCa

**Ly**rion + **Bri**dge + **Ca**millaDSP

A Raspberry Pi audio-control project connecting **Lyrion Music Server** and
**CamillaDSP**. It runs on **piCorePlayer** (recommended) and on **Raspberry Pi OS
(Debian)**.

LyBriCa is made of **six independent features**, all enabled by default. Each
one can be turned off with a switch at the top of `lybrica.py`:

| # | Feature | Switch |
|---|---|---|
| 1 | Volume / mute synchronisation Lyrion <-> CamillaDSP | `ENABLE_LMS_SYNC` |
| 2 | BLE HID remote (volume, tone, player, configs) | `ENABLE_REMOTE` |
| 3 | TM1637 display with automatic brightness | `ENABLE_DISPLAY` |
| 4 | Amplifier power relay (off after silence, on with sound, Pi halt) | `ENABLE_AUTO_POWEROFF` |
| 5 | Automatic DSP config switching (TOSLINK detection) | `ENABLE_TOSLINK_SWITCH` |
| 6 | TV relay, active while a "TV" config is loaded | `ENABLE_TV_RELAY` |

A disabled feature is never started, and the library it needs is neither
imported nor required, so LyBriCa can run on a minimal setup (for example
volume sync only).

An optional **ESP32-C3 companion** powers the Raspberry Pi, on /off with the physical
button and the BLE remote, and sends ambient-light readings to the Pi.

---

## Files

| File | Purpose |
|---|---|
| [`install_cdsp5_pcp.sh`](https://github.com/melomane63/LyBriCa/blob/main/install_cdsp5_pcp.sh) | piCorePlayer: installs CamillaDSP 5 + CamillaGUI |
| [`install_lybrica_pcp.sh`](https://github.com/melomane63/LyBriCa/blob/main/install_lybrica_pcp.sh) | piCorePlayer: installs LyBriCa, the BLE HID bridge and the ESP32 boot settings |
| [`rpiOs_install_cdsp5_lybrica.sh`](https://github.com/melomane63/LyBriCa/blob/main/rpiOs_install_cdsp5_lybrica.sh) | Raspberry Pi OS: menu installer for everything (CamillaDSP, Lyrion, Squeezelite, LyBriCa...) |
| [`lybrica.py`](https://github.com/melomane63/LyBriCa/blob/main/lybrica.py) | The LyBriCa script itself |

---

## Table of contents

1. [How it works](#how-it-works)
2. [Hardware](#hardware)
3. [Installation: piCorePlayer](#installation-picoreplayer)
4. [Installation: Raspberry Pi OS](#installation-raspberry-pi-os)
5. [Configuration](#configuration)
6. [Lyrion setup](#lyrion-setup)
7. [DSP configs and automatic switching](#dsp-configs-and-automatic-switching)
8. [Tone and loudness](#tone-and-loudness)
9. [Remote control](#remote-control)
10. [Display](#display)
11. [Volume curve](#volume-curve)
12. [Settings file](#settings-file)
13. [ESP32-C3 companion](#esp32-c3-companion)
14. [Troubleshooting](#troubleshooting)
15. [Known limitations](#known-limitations)
16. [License and credits](#license-and-credits)

---

## How it works

```
 Lyrion  <--- CLI :9090 --->  LyBriCa  <--- WebSocket :1234 --->  CamillaDSP
 (LMS)                       (bridge)                              (engine)
                                 |
        +------------+-----------+-----------+-------------+
        |            |           |           |             |
   HID remote  TM1637 display  Amp relay   TV relay    TOSLINK probe
    (evdev)    + light sensor  (GPIO 4)    (GPIO 12)      (ALSA)

 Audio path (does not go through LyBriCa):
 Lyrion -> Squeezelite -> ALSA loopback -> CamillaDSP -> DAC
```

- **Volume sync**: LyBriCa reads and writes the CamillaDSP main volume and mute,
  and mirrors them to Lyrion in both directions. At startup, CamillaDSP wins.
- **Single reader**: CamillaDSP is polled by one task that shares its state with
  the display, the TOSLINK monitor and the bridge.
- **Config switching**: DSP configs are organised in groups by file name
  (`_A-*` streamer, `_B-*` TV, ...). LyBriCa can switch between them
  automatically, based on what is actually playing.
- **External changes**: if the config is changed from outside (CamillaDSP GUI,
  another client), LyBriCa detects it, saves the config it was on, and shows the
  new one with a blinking label. The tone / loudness of an external config are
  neither saved nor copied.
- **Resilience**: a crashed task restarts after 2 s; a lost CamillaDSP or Lyrion
  connection is re-established automatically.

---

## Hardware

| Component | Role | Connection |
|---|---|---|
| Raspberry Pi | Runs LyBriCa | - |
| ESP32-C3 (optional) | Pi power, BLE remote, light sensor | UART to `/dev/serial0` |
| BLE HID remote | User input | Bluetooth (evdev) |
| TM1637 display (6 digits) | Status display | GPIO 23 (CLK), GPIO 24 (DIO) |
| Amplifier power relay | Cuts power to the amplifier | GPIO 4 |
| TV relay (optocoupler board) | Follows the "TV" group | GPIO 12 |
| TOSLINK input | TV audio detection | ALSA capture device |
| Ambient light sensor | Auto brightness | Via the ESP32 (serial) |

### Raspberry Pi GPIO summary

| Pi GPIO | Function | Set up by |
|---|---|---|
| GPIO 4 | Amplifier power relay (`POWER_GPIO`) | `lybrica.py` |
| **GPIO 12** | **TV relay (`TV_RELAY_GPIO`): to ground while a `_B-*` config is active, high-impedance otherwise** | `lybrica.py`; `gpio=12=ip,np` in `config.txt` (installers) |
| GPIO 14 / 15 | UART (`/dev/serial0`): GPIO 15 (RXD) receives the ambient light from the ESP32 | `enable_uart=1` (installers) |
| GPIO 16 | Shutdown request from the ESP32 (`gpio-shutdown`) | `config.txt` overlay (installers) |
| GPIO 23 / 24 | TM1637 display (CLK / DIO) | `lybrica.py` |
| GPIO 26 | "Pi is off" signal to the ESP32 (`gpio-poweroff`) | `config.txt` overlay (installers) |

### ESP32 <-> Pi wiring

Both installers use the same Pi pins:

| ESP32-C3 | Function | Raspberry Pi |
|---|---|---|
| GPIO6 | Power-good (Pi -> ESP32) | GPIO26 (`gpio-poweroff`) |
| GPIO10 | Shutdown request (ESP32 -> Pi) | GPIO16 (`gpio-shutdown`) |
| GPIO21 (UART TX) | Ambient light | GPIO15 (RXD, `/dev/serial0`) |

On a bare Raspberry Pi these pins are free (GPIO 14/15 = UART). If you use a HAT
or another add-on board, check its pinout first (the installers use GPIO
12 / 14 / 15 / 16 / 26).

**TV relay**: the input on GPIO 12 is pulled to ground when a `_B-*` config is
active, and left high-impedance otherwise. Both installers add `gpio=12=ip,np`
to `config.txt` so the relay board does not pulse while the Pi boots. The line
is harmless if no TV relay is wired.

---

## Installation: piCorePlayer

Everything is installed as piCore extensions (`.tcz`), so it survives reboots.
Run the scripts over SSH as user **`tc`** (not root).

### Before you start

1. Install and configure piCorePlayer, and **choose your DAC as the audio
   output** in the web interface. The CamillaDSP installer reads this setting
   before it switches the output to the loopback.
2. Install **Bluetooth** from the pCP web interface (needed for the BLE remote).
3. Free at least **150 MB** on the partition (*Main Page -> Additional functions
   -> Resize FS*).

### Step 1: CamillaDSP + CamillaGUI

```sh
wget -O install_cdsp5_pcp.sh https://github.com/melomane63/LyBriCa/raw/refs/heads/main/install_cdsp5_pcp.sh
sh install_cdsp5_pcp.sh      # choose 1
```

What it does:

- Builds the `CDSPpiCore.tcz` extension with CamillaDSP `5.0.0-beta.2` and
  CamillaGUI (`aarch64` or `armv7`, auto-detected). Configs, coefficients and
  the statefile stay on the persistent partition, in
  `/mnt/mmcblk0p2/tce/camilladsp`.
- Writes a minimal pass-through config **`_A-loopback.yml`**
  (Loopback,1 -> 0 dB gain -> your DAC, 48 kHz). If the DAC cannot be read from
  `pcp.cfg`, it falls back to a `null` output: edit the config in the GUI.
- Loads `snd-aloop` at boot and points Squeezelite to `hw:Loopback,0` in
  `pcp.cfg`.
- GUI: `http://pcp.local:5000/gui/index.html`

### Step 2: LyBriCa, BLE bridge and ESP32 settings

```sh
wget -O install_lybrica_pcp.sh https://github.com/melomane63/LyBriCa/raw/refs/heads/main/install_lybrica_pcp.sh
sh install_lybrica_pcp.sh    # choose 1
```

Install CamillaDSP **first**: `lybrica.tcz` only depends on `CDSPpiCore.tcz` if
it is already there. Choice 1 runs two sub-steps:

| Sub-step | What it does |
|---|---|
| **LyBriCa** | Builds `lybrica.tcz` (the script and its Python modules: pycamilladsp from the `next5` branch, `websocket_client`, `pyserial`, `lgpio`, `pyalsaaudio`, `tm1637_lgpio`). **Patches `pcp-bt.tcz`** with the BLE HID bridge from [pcp-ble-gatt](https://github.com/melomane63/pcp-ble-gatt) (a failure only warns). Registers the launcher as `USER_COMMAND_1` in `pcp.cfg`. |
| **ESP32-C3** (asks for confirmation) | Under `[all]` in `config.txt`, **adds** the missing lines `dtoverlay=gpio-poweroff,gpiopin=26,active_low=1`, `dtoverlay=gpio-shutdown,gpio_pin=16,active_low=0,gpio_pull=down`, `enable_uart=1` and `gpio=12=ip,np` (backup: `config.txt.bak`, created once). Sets the matching `GPIOPOWEROFF*` / `GPIOSHUTDOWN*` options in `pcp.cfg` and installs `shutdown-monitor.tcz` (required for the shutdown request to work). |

If you cancel the ESP32 sub-step, LyBriCa stays installed. The two sub-steps can
also be run alone: `sh install_lybrica_pcp.sh --run lybrica` or `--run esp32`.

**Then reboot** (`pcp reboot`). To re-run a script, reboot first (it refuses to
start while its temporary folder exists).

### What changes on piCorePlayer

`lybrica.py` detects piCorePlayer from the folder `/mnt/mmcblk0p2/tce` and
adapts automatically:

- the remote name is forced to **`pCP BLE HID Keyboard`** (the device created by
  the patched BLE HID bridge); `REMOTE_NAME` is ignored;
- DSP configs are read from `/mnt/mmcblk0p2/tce/camilladsp/configs/`;
- `settings.json` is stored on the persistent partition (see
  [Settings file](#settings-file));
- LyBriCa is **not** a systemd service: the launcher starts it at boot, and it
  waits for CamillaDSP instead of crashing if it is not up yet.

### The `lybrica` command

```
lybrica start | restart | stop    (use sudo)
lybrica status | log [N] | reset | help
```

The factory `lybrica.py` lives inside `lybrica.tcz` and is read-only. To change
a setting:

1. `lybrica reset` creates the editable copy `/home/tc/lybrica-mod.py`
2. edit it with `vi`
3. `sudo lybrica restart` (the copy overrides the factory file at each start)
4. `pcp bu` to persist it

Deleting `lybrica-mod.py` falls back to the factory version at the next start.
Logs: `/home/tc/lybrica.log` (script) and `/home/tc/pcp_startup.log` (launcher).

---

## Installation: Raspberry Pi OS

A single menu script installs everything. It needs **64-bit** Raspberry Pi OS
(it downloads `aarch64` binaries and checks the architecture).

```bash
wget https://raw.githubusercontent.com/melomane63/LyBriCa/main/rpiOs_install_cdsp5_lybrica.sh -O /tmp/rpiOs_install.sh
bash /tmp/rpiOs_install.sh
```

```
1) Upgrade System                          6) Mount USB Drive
2) Install CamillaDSP & GUI (v5.0.0-beta.2)  7) Install Lybrica
3) Install Lyrion Media Server (v9.1.1)      8) Pair Bluetooth Remote
4) Install Squeezelite                       S) Shutdown System
5) Configure Sound Levels (alsamixer)        Q) Quit
```

Actions can be chained: `1 2 3 4 7` runs them in ascending order. Versions are
set at the top of the script.

| Option | What it does |
|---|---|
| **1** | `apt update` / `upgrade`. |
| **2** | Installs CamillaDSP and CamillaGUI (`/opt/camillagui_backend`), their systemd services, a statefile, a default config `~/camilladsp/configs/_Setup.yml`, and loads `snd-aloop` at boot. |
| **3** | Installs Lyrion Music Server. |
| **4** | Installs Squeezelite on `hw:Loopback,0` with `-W -C 5 -r 48000-48000 -R hLE`. |
| **5** | Opens `alsamixer`, then saves the levels. |
| **6** | Mounts a USB drive in `/mnt/usb` (auto-detects `/dev/sda1`, otherwise asks for the UUID). |
| **7** | Installs LyBriCa (details below). |
| **8** | Pairs the remote with `bluetuith`, detects it and writes `REMOTE_NAME` in `lybrica.py`. |

Existing service files, the statefile and the default config are never
overwritten without asking (a timestamped backup is kept).

**Option 7** installs the system packages (`libasound2-dev`,
`python3-audioop-lts`...), then:

- **Rewrites `/boot/firmware/config.txt`** (backup: `config.txt.bak`; marker
  `#installed-by-lybrica` so it is done only once). The file is **replaced**:
  personal lines (I2C, SPI, extra overlays) must be restored by hand from the
  backup. It sets `dtparam=audio=off`, the `gpio-poweroff` (GPIO 26) and
  `gpio-shutdown` (GPIO 16) overlays, `enable_uart=1`, `gpio=12=ip,np`,
  `arm_64bit=1` and `arm_boost=1`.
- Removes `console=serial0,...` from `cmdline.txt` (the UART is for the ESP32).
- Adds your user to the `dialout, gpio, input, spi, i2c, audio` groups.
- Creates a sudoers rule for the shutdown command (see the warning below).
- Creates a Python venv in `/opt/venv` and installs the dependencies and
  `tm1637_lgpio`.
- Downloads `lybrica.py` to `~/scripts/` (the previous one is backed up, and
  your `REMOTE_NAME` is kept), and creates and starts the `lybrica` systemd
  service.

> Running option 7 alone? Run option 1 first, so the system is up to date.

> **Shutdown rule.** LyBriCa halts the Pi with `sudo poweroff`, but the
> installer's sudoers rule only allows `/sbin/shutdown`. Add `poweroff` to it,
> otherwise the POWER very-long-press and the automatic halt fail silently:
>
> ```bash
> echo "$USER ALL=(ALL) NOPASSWD: /sbin/shutdown, /sbin/poweroff" | sudo tee /etc/sudoers.d/lybrica
> sudo chmod 440 /etc/sudoers.d/lybrica
> sudo -k && sudo -n poweroff    # test (really shuts the Pi down)
> ```

### Service

```bash
sudo systemctl status lybrica
sudo systemctl restart lybrica
journalctl -u lybrica -f
```

On `SIGTERM` / `SIGINT`, LyBriCa saves its settings, cuts the amplifier relay,
releases the TV relay and the GPIO, then exits.

### Manual installation

```bash
sudo apt install -y git wget curl python3-pip python3-venv liblgpio-dev libasound2-dev
sudo python3 -m venv --system-site-packages /opt/venv
sudo /opt/venv/bin/pip install evdev lgpio pyserial pyalsaaudio
sudo /opt/venv/bin/pip install "git+https://github.com/HEnquist/pycamilladsp.git@next5"

mkdir -p ~/scripts
wget https://raw.githubusercontent.com/melomane63/LyBriCa/main/lybrica.py -O ~/scripts/lybrica.py
# also install tm1637_lgpio.py in the venv's site-packages
# (https://github.com/melomane63/tm1637_lgpio)
```

Only the libraries of the features you enable are needed; `pycamilladsp` is
always required. On **Python 3.13+**, the TOSLINK probe needs `audioop-lts`
(`audioop` was removed from the standard library).

---

## Configuration

All settings are at the top of `lybrica.py` (piCorePlayer: `lybrica-mod.py`).

| Setting | Default | Meaning |
|---|---|---|
| `REMOTE_NAME` | `"HID Remote01 Keyboard"` | Remote name as seen by the Pi (`python3 -m evdev.evtest`). Set by option 8 on Raspberry Pi OS. Fixed on pCP. |
| `CAMILLA_ADDR` | `("127.0.0.1", 1234)` | CamillaDSP WebSocket (must stay local) |
| `LMS_ADDR` | `("127.0.0.1", 9090)` | Lyrion CLI |
| `LMS_PLAYER_MANUAL` | `""` | Player MAC; empty = MAC of the default network interface |
| `POWER_GPIO` | `4` | Amplifier relay |
| `TV_RELAY_GPIO` | `12` | TV relay |
| `CLK`, `DIO` | `23`, `24` | TM1637 pins |
| `POWER_OFF_DELAY` | `2` min | Silence and no key press before the amplifier relay is cut (also before the display is blanked) |
| `HALT_DELAY` | `48` h | Continuous audio silence before the Pi shuts down |
| `SCREEN_RETURN_DELAY` | `30` s | Return to the volume screen after the last key press |
| `WAKE_DISPLAY_DELAY` | `30` s | Display time after a key press woke it while the relay is already off |
| `SILENCE_RMS_DB` | `-80` | RMS below this = silence |
| `CREST_MIN_DB` | `2.0` | Peak - RMS below this = steady noise, not audio |
| `TOSLINK_ON_DB` | `-60` | RMS above which TOSLINK audio counts as present |
| `SAVE_MODE` | `"half"` | How tone / loudness are shared between configs |
| `LMS_SYNC_DEBUG`, `TOSLINK_DEBUG` | `False` | Verbose logging for calibration |

Keep `TOSLINK_ON_DB` **above** `SILENCE_RMS_DB`, otherwise a TV signal could be
detected and then counted as silence (a warning is logged at startup).

The halt after `HALT_DELAY` depends only on audio silence: key presses do not
postpone it. Other timings (key repeat, seek steps, scroll speed, brightness
thresholds...) are commented in the script.

### Feature dependencies and behaviour

| Switch | Needs | Notes |
|---|---|---|
| `ENABLE_LMS_SYNC` | - | Only controls the volume bridge. The Lyrion CLI is still used by the player keys and the TOSLINK monitor. |
| `ENABLE_AUTO_POWEROFF` | `lgpio` | Amplifier relay, silence power-off, halt. |
| `ENABLE_REMOTE` | `evdev` | If `False`, no key works. If the remote is missing, LyBriCa keeps looking for it every second without blocking anything else. |
| `ENABLE_DISPLAY` | `pyserial`, `tm1637_lgpio` | The tone and loudness keys need it. Opens `/dev/serial0` at startup. |
| `ENABLE_TOSLINK_SWITCH` | `pyalsaaudio`, `audioop` | See [automatic switching](#dsp-configs-and-automatic-switching). |
| `ENABLE_TV_RELAY` | `lgpio` | Independent of the amplifier relay. |

Tone and loudness are saved per config only when `ENABLE_REMOTE` or
`ENABLE_TOSLINK_SWITCH` is on.

---

## Lyrion setup

The bridge works only if Lyrion exposes a **fixed 100 % volume** for the
Squeezelite player. Otherwise Lyrion's volume curve fights the CamillaDSP one.

1. Install the **Material Skin** plugin (*Settings -> Plugins*), restart Lyrion.
2. *Settings -> Player -> [your player] -> Audio*: set **Volume control** to
   `Output level is fixed at 100%`.
3. *Interface*: enable **Always show volume slider**.

How the bridge behaves:

- A volume change from Lyrion is written to CamillaDSP immediately.
- A change from CamillaDSP (remote, GUI) is forwarded to Lyrion once it has been
  read twice in a row. Readings older than LyBriCa's own last write are ignored.
- After an **unmute**, Lyrion may briefly announce a wrong volume. During a short
  window (`LMS_RESTORE_WINDOW_S`) the bridge corrects it.
- If the connection to Lyrion is lost, the bridge retries every 5 s.

If the volume keeps jumping, the player is not set to fixed 100 %. Enable
`LMS_SYNC_DEBUG = True` to log every change.

---

## DSP configs and automatic switching

### File naming

Configs live in the CamillaDSP `configs/` folder (`/mnt/mmcblk0p2/tce/camilladsp/configs/`
on pCP, `~/camilladsp/configs/` on Raspberry Pi OS). Only `.yml` / `.yaml` files
count. The group is given by a prefix **`_X-`**, where X is one uppercase letter:

| Group | Role | Example |
|---|---|---|
| `_A-` | Streamer: CamillaDSP captures the ALSA loopback | `_A-loopback.yml` |
| `_B-` | TV: CamillaDSP captures the TOSLINK input | `_B-TV.yml` |
| `_C-`, `_D-`... | Your own groups, reachable in manual mode | `_C-Headphones.yml` |

Name your files with the full `_A-` / `_B-` prefix. The pCP installer already
creates `_A-loopback.yml`. The Raspberry Pi OS installer creates `_Setup.yml`:
rename it (for example `_A-Setup.yml`) and update `config_path` in
`~/camilladsp/statefile.yml` accordingly.

A config that cannot be read, or has no `devices` section, is skipped. The
config `title` (if any) is what the display shows.

### Automatism (`ENABLE_TOSLINK_SWITCH`)

The automatism only knows groups **A** and **B**, and is ON at every start.
It can be switched on / off at run time with a long press on `HOMEPAGE`.

| Active group | What LyBriCa does |
|---|---|
| **A** (streamer) | Probes the TOSLINK input through ALSA. Audio detected on **2 consecutive probes** -> switch to the last used `_B-*` config. |
| **B** (TV) | Asks Lyrion once per second whether it is playing. Lyrion playing **and** TV silent for 2 cycles -> back to the last used `_A-*` config. Lyrion playing **while the TV plays** -> Lyrion is paused (the TV has priority). |
| **C, D...** (manual) | With the automatism ON, a single probe decides: TV playing -> B, otherwise -> A. |

A "playing" reading requires the level to be above `TOSLINK_ON_DB` **and** a
crest above `CREST_MIN_DB` (a steady noise floor is ignored). A failed switch is
retried with an increasing delay (5 s doubling, up to 60 s). With the automatism
OFF there is no probing, no switch and no Lyrion pause.

The probe parameters (device, rate, channels, format) are read from the first
`_B-*` file, so **all `_B-*` configs must share the same capture input**.
Supported formats: `S16_LE`, `S24_3LE`, `S24_LE`, `S32_LE`.

If there is no `_A-*` or no `_B-*` file, the automatism cannot run and the keys
behave as if the feature were off.

### TV relay

With `ENABLE_TV_RELAY`, the relay on `TV_RELAY_GPIO` is active whenever a `_B-*`
config is loaded (whatever the cause: automatism, keys or GUI), inactive
otherwise.

---

## Tone and loudness

### Tone

The tone is two gains, **Bass** and **Treble** (max +/- 6 dB each), entered
directly with the arrow keys. LyBriCa derives the filter frequencies, Q and the
compensation gain automatically, so the perceived level stays roughly constant.
Only the changed filters are pushed to CamillaDSP, with no full reload.

Your CamillaDSP configs must contain these filters, **and** list them in an
enabled `Filter` step of the pipeline (the installers' default configs do not:
add them to use the tone keys):

| Filter name | Required | Role |
|---|---|---|
| `L_Bass` | yes | Bass shelf (`gain`) |
| `L_Treble` | yes | Treble shelf (`gain`) |
| `L_Compensation` | yes | Level compensation (`gain`) |
| `L_Loudness` | optional | Loudness (`reference_level`) |

If one of the three tone filters is missing, the tone screen shows `B----T` and
the keys do nothing. If `L_Loudness` is missing, the loudness screen shows `--`.

Two options are chosen on the tone preset screen (long `ENTER` from the tone
screen) and remembered across restarts:

| Option | Keys | Effect |
|---|---|---|
| Key mode **BT** | `UP` / `DOWN` = bass +/-1, `RIGHT` / `LEFT` = treble +/-1 | Classic bass / treble |
| Key mode **TL** | `UP` = bass- treble+, `DOWN` = bass+ treble-, `RIGHT` = both +, `LEFT` = both - | Tilt-style: the gain already past its target moves by 0.5 or 0, so values are multiples of 0.5 |
| Preset **def** | - | Baxandall-like shelves (85 Hz / 6.5 kHz) |
| Preset **alt** | - | Shelves at 110 Hz / 3.5 kHz |

<img width="1900" height="650" alt="lybrica_response1" src="https://github.com/user-attachments/assets/e2ac4b11-0dfc-4787-adff-db7ff996417d" />

### Loudness

The loudness screen adjusts the `reference_level` of `L_Loudness` between
-50 and -10 dB, in 5 dB steps.

### Sharing between configs (`SAVE_MODE`)

Tone and loudness are always saved **per config**. `SAVE_MODE` decides what is
copied into the new config when you switch inside the same group:

| `SAVE_MODE` | Behaviour |
|---|---|
| `"all_config"` | Each config keeps its own tone and loudness |
| `"all_group"` | Configs of the same group share tone and loudness |
| `"half"` *(default)* | Tone per config, loudness per group |
| `"same_everywhere"` | All configs share tone and loudness, whatever the group |

When you come back to a group you land on its last used config, whose values
were saved when you left it.

---

## Remote control

All keys need `ENABLE_REMOTE = True`.

| Key | Short press | Long press |
|---|---|---|
| `VOLUMEUP` / `VOLUMEDOWN` | Volume +/- 1 step | Continuous change |
| `MUTE` | Mute / unmute | - |
| `PLAYPAUSE` | Pause / resume (Lyrion) | Stop |
| `PREVIOUSSONG` | Previous track | Rewind (10 s steps) |
| `NEXTSONG` | Next track | Fast-forward (30 s steps) |
| `UP` `DOWN` `LEFT` `RIGHT` | Tone / loudness adjustment (see below) | Repeat |
| `ENTER` | Tone screen: set bass and treble to 0, or restore the previous values if already 0 | Switch screens (see below) |
| `BACK` | Next config **inside the active group** | - |
| `HOMEPAGE` | Next group (see below) | Enable / disable the automatism (~1 s) |
| `POWER` | - | ~0.4 s: toggle the amplifier relay (`POWER AUTO` / `POWER OFF`). ~8 s: shut the Pi down |

### Screens

- From the **volume screen**, an arrow key opens the **tone** screen.
- Long `ENTER` from the volume screen opens the **loudness** screen.
- Long `ENTER` from the **tone** screen opens the **tone preset** screen.
- Long `ENTER` from the loudness or preset screen returns to the volume screen.
- On the loudness screen, `UP` / `DOWN` change the level (+/- 5 dB).
- On the preset screen, `UP` toggles BT / TL, `DOWN` toggles def / alt,
  `LEFT` / `RIGHT` go back to the tone screen.
- Any screen returns to the volume screen after `SCREEN_RETURN_DELAY`.

### BACK and HOMEPAGE

| Situation | `BACK` | `HOMEPAGE` short | `HOMEPAGE` long |
|---|---|---|---|
| Automatism **ON** | Next config in the active group | Nothing | Disable automatism (`CONF MANUAL`) |
| Automatism **OFF** | Next config in the active group | Next group | Enable automatism (`CONF AUTO`) |
| Feature off, or no `_A-` / `_B-` files | Next config in the active group | Next group | - |

Switching to another group lands on its **last used config**. Groups are cycled
in alphabetical order, and only groups with at least one file are visited.

### Good to know

- When the display is blank, the **first** key press only wakes it up (the key
  is then ignored until released).
- When the volume screen is not shown, or the volume reads `--` while muted,
  the **first** volume key press only brings the volume screen back.
- Any key press cancels a scrolling message.
- `POWER` relay toggling needs `ENABLE_AUTO_POWEROFF`. The shutdown works
  regardless and saves the settings first.

---

## Display

### Volume screen

`[label][space][volume]`, 6 characters:

- **Label** (3 chars): the config `title`, or the file name with its group prefix
  reduced to `_` (`_B-television` -> `_te`). It **blinks** when the config was
  chosen outside LyBriCa.
- **Volume**: attenuation in dB (`30` = -30 dB, `00` = 0 dB). A dot on the last
  digit means an extra half dB. When muted it shows `--` (the real value shows
  while a volume key is held, and briefly after).

Indicators:

| Indicator | Meaning |
|---|---|
| Blinking dots on the 3 label characters | Automatism disabled (`CONF MANUAL`) |
| Dot on the 4th character | Tone active (non-zero bass or treble) |
| Middle segment on the 4th character | Separator; **blinks** when the amplifier relay is forced OFF |
| Blinking label | Config chosen outside LyBriCa |

### Other screens

| Screen | Example | Meaning |
|---|---|---|
| Tone | `B 3 2T` | Bass +3, treble +2 (a dot = extra half unit); `B----T` if filters missing |
| Tone preset | `BT def` | Key mode, then preset |
| Loudness | `LDN-30` | Reference level; `LDN --` if no loudness filter |
| Messages | `POWER AUTO`, `POWER OFF`, `POWER SAVE`, `CONF AUTO`, `CONF MANUAL`, `HALT` | Scrolling, then back to volume |

### Blanking and wake-up

After `POWER_OFF_DELAY` minutes of silence without key press, the amplifier
relay is cut (`POWER SAVE`) and the display is blanked. Sound or a key wakes the
display; sound also switches the relay back on. If the relay was already off,
a wake-up key press keeps the display on for `WAKE_DISPLAY_DELAY` seconds.

### Brightness

The ambient-light value from the ESP32 is compared with four thresholds
(`ADC_LEVELS`), giving five brightness levels (`BRIGHTNESS_LEVELS`), with 3 %
hysteresis to avoid flickering. Without the sensor, a fallback brightness is used.

---

## Volume curve

Lyrion level (0-100) <-> CamillaDSP volume (dB). The curve is strictly
increasing, so the conversion is reversible.

| Lyrion level | CamillaDSP volume |
|---|---|
| 0-24 | -74 to -38 dB (1.5 dB per level) |
| 25-100 | -37.5 to 0 dB (0.5 dB per level) |

Mute is synchronised separately from the volume.

---

## Settings file

`settings.json` stores:

- tone and loudness per config (including the values used by the `ENTER`
  restore),
- the last config used in each group,
- the tone preset and key mode.

It is written at every config switch, on shutdown and on `SIGTERM` / `SIGINT`,
atomically (temporary file then rename). Location:

- piCorePlayer: `/mnt/mmcblk0p2/tce/camilladsp/settings.json` (persistent)
- Raspberry Pi OS: next to `lybrica.py`

At startup, and after a CamillaDSP restart, the saved values of the active
config are re-applied. A missing file is recreated with defaults.

---

## ESP32-C3 companion

The firmware (ESP-IDF 5.x) powers the Pi through a relay, hosts the BLE HID
remote, and sends ambient-light readings. GPIO numbers below are ESP32-C3 pins.

| Pin | Direction | Function |
|---|---|---|
| GPIO0 (ADC1 ch. 0) | Input | Ambient-light sensor (LDR) |
| GPIO5 | Output | Pi power relay (HIGH = on) |
| GPIO6 | Input | Pi power-good (internal pull-down) |
| GPIO7 | Input | Physical button; held LOW at boot = BLE pairing |
| GPIO8 | Output | Bluetooth status LED (inverted: LOW = on) |
| GPIO10 | Output | Pi shutdown request |
| GPIO21 (UART1 TX) | Output | Light readings to the Pi |

### Flashing

```bash
source ~/esp-idf/export.sh
idf.py set-target esp32c3
idf.py build
idf.py -p /dev/ttyACM0 flash monitor
```

Set the console to USB Serial/JTAG (`idf.py menuconfig` -> *Component config ->
ESP System Settings -> Channel for console output*), otherwise log text is mixed
with the light readings on GPIO21.

### Power sequence

1. The button (or any key of the BLE remote) switches the relay on and drops
   BLE, so the Pi can take the remote over once booted.
2. The ESP32 waits for the Pi's power-good signal (GPIO6 HIGH on two checks),
   which arms the shutdown detection.
3. Once armed, GPIO6 staying LOW for about **10 s** means the Pi is off: the
   relay is cut and the ESP32 restarts, which brings BLE back.
4. While the Pi runs, a button press asks for a clean shutdown (GPIO10 HIGH, left
   HIGH until the restart).

### Light sensor

LDR (GL5516) on GPIO0 with a 15 k ohm resistor to ground (16.2 k ohm also works).
Every ~200 ms, the firmware averages 20 samples, filters them and sends
`<integer>\n` at 115200 8N1. LyBriCa only uses the latest complete line. If the
brightness does not react as expected, adjust `ADC_LEVELS`.

### Pairing the BLE remote

1. Put the remote in pairing mode.
2. Hold the ESP32 button (GPIO7) while it boots, until the scan starts.
3. The ESP32 scans for 5 s, keeps the **strongest** BLE advertiser (RSSI above
   -80 dBm), saves its MAC in NVS and restarts.

The strongest device is not necessarily a remote: keep the remote close and
other BLE devices away. Repeating the procedure overwrites the saved MAC; if no
device qualifies, the previous one is kept.

Status LED: **solid** = connected; **slow blink** = no MAC saved (pair again;
the button still powers the Pi); **off** = MAC saved, not connected (the ESP32
retries every 5 s).

### One remote for the ESP32 and the Pi

The remote is connected to **one host at a time**: the ESP32 while the Pi is
off, the Pi (through evdev, used by LyBriCa) once it has booted. This works with
the project's reference remote. Other models may refuse a second host: in that
case pair the remote with the Pi only and use the ESP32 button to power on.

---

## Troubleshooting

On Raspberry Pi OS use `journalctl -u lybrica -f`; on piCorePlayer use
`lybrica log`.

**Remote not detected**: run `python3 -m evdev.evtest` and check `REMOTE_NAME`
(on pCP the name must be `pCP BLE HID Keyboard`). `Cannot grab ... will retry`
means another process holds the device. On the ESP32 side, see the LED states
above.

**CamillaDSP unreachable**: check that it is running and `CAMILLA_ADDR`.
LyBriCa waits for it at startup and reconnects by itself later. On pCP, check
`tce-status -i | grep CDSPpiCore` and `pidof camilladsp camillagui`.

**Lyrion CLI unreachable**: `telnet 127.0.0.1 9090`, check Lyrion is running.

**Volume fights between Lyrion and CamillaDSP**: see [Lyrion setup](#lyrion-setup).

**Shutdown from the remote does nothing**: LyBriCa runs `sudo poweroff` and does
not check the result (the display shows `HALT` anyway). On Raspberry Pi OS,
check that `/etc/sudoers.d/lybrica` covers `poweroff` (see
[the shutdown rule](#installation-raspberry-pi-os)).

**Display stays blank**: check CLK / DIO wiring and `ENABLE_DISPLAY`. After a
long silence the display is blanked on purpose: press a key or play sound.

**Brightness does not follow the light**: look for `ADC = ... -> Brightness = ...`
in the logs; check the LDR is on GPIO0 and adjust `ADC_LEVELS`.

**No light data / LyBriCa fails to start with the display**: check
`ls -l /dev/serial0`, 115200 baud, `enable_uart=1`, and that `console=serial0`
was removed from `cmdline.txt` (Raspberry Pi OS).

**Tone keys do nothing (`B----T`)**: the config lacks `L_Bass`, `L_Treble` or
`L_Compensation`, or they are not used in the pipeline.

**Automatic switching does not work**:
- the automatism must be ON (a blinking dot on the label means OFF);
- at least one `_A-*` **and** one `_B-*` file must exist in the configs folder;
- check the `[TOSLINK] Capture: ...` log line and the supported formats;
- `[TOSLINK] Probe failed` = the ALSA capture device cannot be opened (wrong
  name, or already in use);
- set `TOSLINK_DEBUG = True` to see peak / rms / crest and tune `TOSLINK_ON_DB`;
- if the TV is detected but LyBriCa returns to `_A-` at once, check that Lyrion
  is really playing and that the TV level is above the silence threshold.

**Service crashes in a loop at startup** (Raspberry Pi OS): a library of an
enabled feature is missing (`ModuleNotFoundError`). Install it, then restart the
service.

**ALSA loopback not loaded**: `sudo modprobe snd-aloop` and `lsmod | grep snd_aloop`
(on Raspberry Pi OS the installer makes it persistent in
`/etc/modules-load.d/snd-aloop.conf`).

**Bluetooth pairing fails on the Pi**: check the adapter (`bluetoothctl list`)
and remove `dtoverlay=disable-wifi` from `config.txt` if it disabled Bluetooth.
On pCP, make sure Bluetooth was installed from the web interface before step 2
and that the install printed `pcp-bt.tcz rebuilt` (or `already contains these
two files`).

---

## Known limitations

- **TOSLINK probe**: it can only listen while a group-A config is active
  (CamillaDSP holds the device in group B), and all `_B-*` configs must use the
  same capture input.
- **Python 3.13+**: needs `audioop-lts`.
- **Shutdown**: `sudo poweroff` is not checked, and the Raspberry Pi OS
  installer's sudoers rule does not cover it (see above).
- **Default configs**: the installers' configs contain no tone filters, so the
  tone and loudness keys do nothing until you add them.
- **CamillaDSP 5 beta**: both installers use `5.0.0-beta.2` and the `next5`
  branch of pycamilladsp.
- **ESP32 firmware**: uses the legacy `driver/adc.h` API, deprecated in
  ESP-IDF 5.x. The remote hand-over between ESP32 and Pi is not guaranteed with
  every remote model.
- **Raspberry Pi OS installer**: only `/dev/sda1` is auto-detected for the USB
  drive, and `config.txt` is replaced rather than edited.

---

## License and credits

MIT License, see [LICENSE](LICENSE). Pull requests are welcome; for major
changes, please open an issue first.

This project is not affiliated with CamillaDSP, Lyrion Music Server, Logitech or
any of their owners.

- [CamillaDSP](https://github.com/HEnquist/camilladsp) by Henrik Enquist
- [piCoreCDSP](https://github.com/JWahle/piCoreCDSP) by JWahle (the pCP CamillaDSP installer is a fork)
- [Lyrion Music Server](https://lyrion.org/) and [Material Skin](https://github.com/CDrummond/lms-material) by Craig Drummond
- [python-evdev](https://github.com/gvalkov/python-evdev), [lgpio](https://github.com/joan2937/lg), [tm1637_lgpio](https://github.com/melomane63/tm1637_lgpio), [bluetuith](https://github.com/bluetuith-org/bluetuith)
- [pcp-ble-gatt](https://github.com/melomane63/pcp-ble-gatt) (BLE HID bridge for piCorePlayer)
- [RPi-CamillaDSP](https://github.com/mdsimon2/RPi-CamillaDSP) by mdsimon2
- Original LMS volume sync idea by [mbrennwa](https://github.com/mbrennwa/camilla_LMS_volume_sync)
