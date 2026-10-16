# LyBriCa

**Ly**rion + **Bri**dge + **Ca**millaDSP

A Raspberry Pi audio-control project connecting **Lyrion Music Server** and
**CamillaDSP**, with bidirectional volume/mute synchronisation, a HID remote,
a TM1637 display, automatic brightness, amplifier power control and automatic
DSP configuration switching.

An optional **ESP32-C3 companion** manages the Raspberry Pi power relay, reads
a physical button and a BLE HID remote, and sends ambient-light readings to
the Pi over UART serial.

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
- [ESP32-C3 companion](#esp32-c3-companion)
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
| `UP` / `DOWN` | Increase / decrease bass on the tone screen; tilt on the tilt screen | - |
| `LEFT` / `RIGHT` | Decrease / increase treble on the tone screen; presence on the tilt screen | - |
| `ENTER` | Reset / restore tone values; toggle loudness on its screen | Switch between tone and tilt screens |
| `BACK` | Next config whose filename starts with `_` (stereo group) | - |
| `HOMEPAGE` | Next config whose filename starts with `\|` (TV group) | - |
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
- `ENABLE_CONFIG_SWITCH = False` disables automatic switching.
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

The installer creates `/etc/systemd/system/lybrica.service` (check the installer if your deployment uses a different service name or path):

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

### Automatic configuration switching does not work

- For GPIO mode, check `TV_GPIO`, the input wiring and the configured pull-up.
- For TOSLINK mode, check the ALSA capture device and the TOSLINK signal
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
