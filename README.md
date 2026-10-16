# LyBriCa

**Ly**rion + **Bri**dge + **Ca**millaDSP

A bidirectional volume bridge between **Lyrion Music Server** and **CamillaDSP** for
Raspberry Pi, with HID remote control, TM1637 display with ambient light sensor,
power relay control, automatic trigger management and auto select config.

The ESP32 companion handles the physical power on/off of the Pi,
whit button and remote control and reports the ambient light level.

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Hardware](#hardware)
- [Software components](#software-components)
- [Quick install](#quick-install)
- [Manual installation](#manual-installation)
- [Configuration](#configuration)
- [Feature switches](#feature-switches)
- [Remote control keys](#remote-control-keys)
- [Display](#display)
- [Volume curve](#volume-curve)
- [Settings file](#settings-file)
- [ESP32 companion](#esp32-companion)
- [Systemd service](#systemd-service)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## What it does

LyBriCa keeps **Lyrion Music Server** (ex-LMS) and **CamillaDSP** in sync:

- **Bidirectional volume / mute sync** between Lyrion and CamillaDSP
- **Automatic DSP config switching** (TV / Squeezelite) via GPIO or TOSLINK
  detection
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

The **ESP32** is the physical power manager: it cuts the power completely
when asked, and wakes the Pi back up on a remote control key press or a
sound event. It also reads the ambient light sensor and forwards the value
to the Pi over serial.

---

## Hardware

| Component | Role | Connection |
|---|---|---|
| Raspberry Pi | Runs LyBriCa | - |
| ESP32 | Power on/off, remote receiver, light sensor | Serial (`/dev/serial0`) |
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
| **ESP32 companion** | C++ / Arduino | Power management, remote, light sensor |
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
1) Install Everything (Automated)
2) Upgrade System
3) Install CamillaDSP & GUI
4) Install Lyrion Media Server & Squeezelite
5) Mount USB Drive
6) Configure Sound Levels (alsamixer)
7) Install LyBriCa (Config + Script + Service)
8) Pair Bluetooth Remote
9) Reboot System
10) Exit
==================================
```

Choose **1) Install Everything (Automated)** for a full installation, or pick
individual steps.

### What the installer does

| Step | Action |
|---|---|
| **1. System** | `apt update`, `apt upgrade`, installs `git`, `wget`, `curl`, `python3-pip`, `python3-venv`, `systemd` |
| **2. CamillaDSP** | Downloads and installs CamillaDSP v4.1.3 + CamillaGUI v4.1.0, creates the systemd services, sets up the ALSA loopback |
| **3. Lyrion** | Downloads and installs Lyrion Music Server 9.1.1 + Squeezelite, configures the output device |
| **4. USB drive** | Detects the USB drive UUID and adds it to `/etc/fstab` |
| **5. Sound card** | Launches `alsamixer` for manual level adjustment |
| **6. LyBriCa** | Configures `/boot/firmware/config.txt`, creates the venv, installs dependencies, downloads `lybrica.py`, creates the systemd service |
| **7. Bluetooth remote** | Launches `bluetuith` for pairing, detects the remote, updates `REMOTE_NAME` in `lybrica.py` |
| **8. Reboot** | Optional reboot at the end |

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
| Python venv | `/opt/venv` |

To change any of these, edit `install-lybrica.sh` directly.

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

```bash
pip install evdev==1.6.1 lgpio==0.2.2.0 pyserial==3.5
pip install "git+https://github.com/HEnquist/pycamilladsp.git@v4.0.0"
```

Or, if you use a virtual environment:

```bash
python3 -m venv --system-site-packages /opt/venv
source /opt/venv/bin/activate
pip install evdev==1.6.1 lgpio==0.2.2.0 pyserial==3.5
pip install "git+https://github.com/HEnquist/pycamilladsp.git@v4.0.0"
```

### Install LyBriCa

```bash
git clone https://github.com/melomane63/LyBriCa.git
mkdir -p "$HOME/scripts"
cp LyBriCa/lybrica.py "$HOME/scripts/lybrica.py"

PYVER=$(/opt/venv/bin/python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
cp LyBriCa/tm1637_lgpio.py "/opt/venv/lib/python${PYVER}/site-packages/tm1637_lgpio.py"

cp LyBriCa/settings.example.json "$HOME/scripts/settings.json"
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

And for the shutdown command:

```bash
echo "$USER ALL=(ALL) NOPASSWD: /sbin/shutdown" | sudo tee /etc/sudoers.d/lybrica
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
POWER_OFF_DELAY         = 2     # Minutes of silence before relay off
HALT_DELAY              = 48    # Hours of silence before Pi shutdown
SCREEN_RETURN_DELAY     = 30    # Seconds before returning to volume screen
TOSLINK_SILENCE_DELAY_S = 120   # Seconds before switching back to Squeezelite
```

---

## Feature switches

Each feature can be disabled independently. When disabled, the corresponding
hardware library is **not imported** and **not required**.

| Switch | Default | Effect |
|---|---|---|
| `ENABLE_LMS_SYNC` | `True` | Lyrion <-> CamillaDSP volume/mute sync |
| `ENABLE_AUTO_POWEROFF` | `True` | Power relay: off after silence, on with sound |
| `ENABLE_REMOTE` | `True` | HID remote control |
| `ENABLE_DISPLAY` | `True` | TM1637 display + ambient light sensor |
| `ENABLE_CONFIG_SWITCH` | `"gpio"` | Auto config switching (`False`, `"gpio"`, `"toslink"`) |

---

## Remote control keys

| Key | Short press | Long press |
|---|---|---|
| `VOLUMEUP` / `VOLUMEDOWN` | Volume +/-1 step | Continuous volume |
| `MUTE` | Mute / unmute | - |
| `PLAYPAUSE` | Pause | Stop (LMS) |
| `PREVIOUSSONG` | Previous track | Rewind (10 s steps) |
| `NEXTSONG` | Next track | Fast-forward (10 s steps) |
| `UP` / `DOWN` | Presence gain +/- / treble +/- | - |
| `LEFT` / `RIGHT` | Tilt gain +/- / bass +/- | - |
| `ENTER` | Reset / restore tone values | Switch tone / tilt screen |
| `BACK` | Next `_*` config (stereo) | - |
| `HOMEPAGE` | Next `\|*` config (TV) | - |
| `POWER` | - | Toggle relay / shutdown |

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

The volume is mapped from the Lyrion 0-100 scale to CamillaDSP dB:

| Level | dB | Step |
|---|---|---|
| 0 | -100 (mute) | - |
| 1 | -75 | - |
| 2-25 | -71 to -37.5 | ~1.5 dB/step |
| 25-100 | -37.5 to 0 | 0.5 dB/step |

The curve is strictly increasing, so the inverse conversion is exact.
Maximum deviation from the original Squeezelite curve: < 0.5 dB.

---

## Settings file

`settings.json` stores:

- Per-config tone values (bass, treble, tilt, loudness, presence)
- Previous values for the ENTER restore function
- Last used config per group (`_` and `|`)
- Last tone/tilt screen shown

The file is written **atomically** (no half-written file on power loss).

---

## ESP32 companion

The ESP32 companion handles:

- **Physical power on/off** of the Raspberry Pi (relay / MOSFET)
- **Remote control reception** (IR or RF) and forwarding to the Pi
- **Ambient light sensor** reading and forwarding over serial
- **Wake-up** on remote key press or sound event

### Communication protocol

The ESP32 and LyBriCa communicate over **serial** (`/dev/serial0`,
115200 baud):

| Direction | Message | Meaning |
|---|---|---|
| ESP32 -> Pi | `<int>\n` | Ambient light ADC value |
| ESP32 -> Pi | `POWER\n` | Power key pressed |
| Pi -> ESP32 | `OFF\n` | Cut power |
| Pi -> ESP32 | `ON\n` | Power on |

> The firmware source, wiring diagram and flashing instructions are in the
> [`firmware/`](firmware/) directory of this repository.

---

## Systemd service

The installer creates `/etc/systemd/system/lybrica.service`:

```ini
[Unit]
Description=LyBriCa - Lyrion <-> CamillaDSP bridge
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

### Display stays blank

- Check the TM1637 wiring (CLK, DIO)
- Check the ambient light sensor value in the logs
- Check that `ENABLE_DISPLAY = True`

### ESP32 not responding

- Check the serial port: `ls -l /dev/serial0`
- Check the baud rate: `115200`
- Check the ESP32 logs (if available)

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
