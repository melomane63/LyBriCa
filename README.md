markdown
# LyBriCa

**Ly**rion + **Bri**dge + **Ca**millaDSP

A bidirectional bridge between **Lyrion Music Server** and **CamillaDSP** for
Raspberry Pi, with HID remote control, TM1637 display, ambient light sensor,
power relay control and automatic power management.

The ESP32 companion firmware handles the physical power on/off of the Pi,
receives the remote control signals and reports the ambient light level.

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Hardware](#hardware)
- [Software components](#software-components)
- [Installation](#installation)
- [Configuration](#configuration)
- [Feature switches](#feature-switches)
- [Remote control keys](#remote-control-keys)
- [Display](#display)
- [Volume curve](#volume-curve)
- [Settings file](#settings-file)
- [ESP32 firmware](#esp32-firmware)
- [Systemd service](#systemd-service)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## What it does

LyBriCa keeps **Lyrion Music Server** (ex-LMS) and **CamillaDSP** in sync:

- **Bidirectional volume / mute sync** between Lyrion and CamillaDSP
- **Automatic DSP config switching** (TV / Squeezelite) via GPIO or TOSLINK
  detection
- **HID remote control** (volume, mute, transport, tone, tilt, loudness,
  power)
- **TM1637 display** with ambient light sensor for automatic brightness
- **Power relay control** : switch off after silence, back on with sound,
  halt the Pi after a long silence
- **Physical power on/off** handled by an ESP32 companion firmware
- **Per-config tone settings** saved in `settings.json`

---

## Architecture
┌──────────────────────┐
│ Lyrion Music Server │
│ (ex-LMS) │
└──────────┬───────────┘
│ CLI (port 9090)
│
┌──────────▼───────────┐
│ LyBriCa │
│ (Python, asyncio) │
└──────────┬───────────┘
│ WebSocket (port 1234)
│
┌──────────▼───────────┐
│ CamillaDSP │
└──────────────────────┘
▲
│ GPIO / I2C / Série / evdev
│
┌────────────────────────┼────────────────────────┐
│ │ │
┌──▼───┐ ┌─────▼─────┐ ┌──────▼──────┐
│ HID │ │ TM1637 │ │ Relais │
│remote│ │ display │ │ puissance │
└──────┘ └───────────┘ └──────┬──────┘
│
┌──────▼──────┐
│ ESP32 │
│ firmware │
└─────────────┘

text

The **ESP32** is the physical power manager: it cuts the power completely
when asked, and wakes the Pi back up on a remote control key press or a
sound event. It also reads the ambient light sensor and forwards the value
to the Pi over serial.

---

## Hardware

| Component | Role | Connection |
|---|---|---|
| Raspberry Pi | Runs LyBriCa | — |
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
| **ESP32 firmware** | C++ / Arduino | Power management, remote, light sensor |
| **CamillaDSP** | Rust | Audio DSP engine |
| **Lyrion Music Server** | Perl | Music server |

---

## Installation

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
Python dependencies
bash
pip install camilladsp evdev pyserial lgpio
Or, if you use a virtual environment:

bash
python3 -m venv venv
source venv/bin/activate
pip install camilladsp evdev pyserial lgpio
Install LyBriCa
bash
git clone https://github.com/<user>/lybrica.git
cd lybrica
cp settings.example.json settings.json
Permissions
The user running LyBriCa needs access to:

GPIO: add to the gpio group

Serial: add to the dialout group

Input devices: add to the input group

Audio: add to the audio group

bash
sudo usermod -aG gpio,dialout,input,audio $USER
And for the shutdown command:

bash
echo "$USER ALL=(ALL) NOPASSWD: /sbin/shutdown" | sudo tee /etc/sudoers.d/lybrica
Configuration
All configuration is at the top of lybrica.py:

python
# Feature switches
ENABLE_LMS_SYNC      = True
ENABLE_AUTO_POWEROFF = True
ENABLE_REMOTE        = True
ENABLE_DISPLAY       = True
ENABLE_CONFIG_SWITCH = "gpio"   # False | "gpio" | "toslink"
Network addresses
python
CAMILLA_ADDR = ("127.0.0.1", 1234)   # CamillaDSP WebSocket
LMS_ADDR     = ("127.0.0.1", 9090)   # Lyrion CLI
GPIO pins
python
POWER_GPIO = 4     # Power relay
TV_GPIO    = 12    # TV state input
CLK, DIO   = 23, 24 # TM1637 display
Timing
python
POWER_OFF_DELAY        = 2     # Minutes of silence before relay off
HALT_DELAY             = 48    # Hours of silence before Pi shutdown
SCREEN_RETURN_DELAY    = 30    # Seconds before returning to volume screen
TOSLINK_SILENCE_DELAY_S = 120  # Seconds before switching back to Squeezelite
Feature switches
Each feature can be disabled independently. When disabled, the corresponding
hardware library is not imported and not required.

Switch	Default	Effect
ENABLE_LMS_SYNC	True	Lyrion ⇄ CamillaDSP volume/mute sync
ENABLE_AUTO_POWEROFF	True	Power relay: off after silence, on with sound
ENABLE_REMOTE	True	HID remote control
ENABLE_DISPLAY	True	TM1637 display + ambient light sensor
ENABLE_CONFIG_SWITCH	"gpio"	Auto config switching (False, "gpio", "toslink")
Remote control keys
Key	Short press	Long press
VOLUMEUP / VOLUMEDOWN	Volume ±1 step	Continuous volume
MUTE	Mute / unmute	—
PLAYPAUSE	Pause	Stop (LMS)
PREVIOUSSONG	Previous track	Rewind (10 s steps)
NEXTSONG	Next track	Fast-forward (10 s steps)
UP / DOWN	Presence gain ± / treble ±	—
LEFT / RIGHT	Tilt gain ± / bass ±	—
ENTER	Reset / restore tone values	Switch tone / tilt screen
BACK	Next _* config (stereo)	—
HOMEPAGE	Next |* config (TV)	—
POWER	—	Toggle relay / shutdown
Display
The TM1637 display shows:

Volume screen : config label (3 chars) + volume (0–100)

Tone screen : bass / treble (3B 2T)

Tilt screen : tilt / presence (2T 1P)

Loudness screen : reference level (-30 Ld)

Power screen : PW ON / PW OFF / HALT

Special indicators:

Decimal point on the 3rd character: auto power-off active

Decimal point on the 4th character: power relay ON

Top segment on the 4th character: tone settings active

Brightness is adjusted automatically from the ambient light sensor
(4 levels with hysteresis).

Volume curve
The volume is mapped from the Lyrion 0–100 scale to CamillaDSP dB:

Level	dB	Step
0	−100 (mute)	—
1	−75	—
2–25	−71 to −37.5	~1.5 dB/step
25–100	−37.5 to 0	0.5 dB/step
The curve is strictly increasing, so the inverse conversion is exact.
Maximum deviation from the original Squeezelite curve: < 0.5 dB.

Settings file
settings.json stores:

Per-config tone values (bass, treble, tilt, loudness, presence)

Previous values for the ENTER restore function

Last used config per group (_ and |)

Last tone/tilt screen shown

The file is written atomically (no half-written file on power loss).

ESP32 firmware
The ESP32 companion firmware handles:

Physical power on/off of the Raspberry Pi (relay / MOSFET)

Remote control reception (IR or RF) and forwarding to the Pi

Ambient light sensor reading and forwarding over serial

Wake-up on remote key press or sound event

Communication protocol
The ESP32 and LyBriCa communicate over serial (/dev/serial0,
115200 baud):

Direction	Message	Meaning
ESP32 → Pi	<int>\n	Ambient light ADC value
ESP32 → Pi	POWER\n	Power key pressed
Pi → ESP32	OFF\n	Cut power
Pi → ESP32	ON\n	Power on
The firmware source, wiring diagram and flashing instructions are in the
firmware/ directory of this repository.

Systemd service
Copy lybrica.service to /etc/systemd/system/ and enable it:

bash
sudo cp lybrica.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lybrica
lybrica.service:

ini
[Unit]
Description=LyBriCa — Lyrion ⇄ CamillaDSP bridge
After=network-online.target camilladsp.service
Wants=network-online.target
Requires=camilladsp.service

[Service]
Type=simple
User=<user>
Group=<user>
WorkingDirectory=/home/<user>/lybrica
ExecStart=/usr/bin/python3 /home/<user>/lybrica/lybrica.py
Restart=on-failure
RestartSec=5
SupplementaryGroups=gpio dialout input audio

[Install]
WantedBy=multi-user.target
Troubleshooting
Remote not detected
bash
python3 -m evdev.evtest
Check that the remote name matches REMOTE_NAME in lybrica.py.

CamillaDSP unreachable
bash
camilladsp --check
Check CAMILLA_ADDR and that CamillaDSP is running.

Lyrion CLI unreachable
bash
telnet 127.0.0.1 9090
Check LMS_ADDR and that Lyrion is running.

Display stays blank
Check the TM1637 wiring (CLK, DIO)

Check the ambient light sensor value in the logs

Check that ENABLE_DISPLAY = True

ESP32 not responding
Check the serial port: ls -l /dev/serial0

Check the baud rate: 115200

Check the ESP32 logs (if available)

Logs
bash
journalctl -u lybrica -f
License
MIT License — see LICENSE for details.

Credits
CamillaDSP by Henrik Enquist

Lyrion Music Server (ex-Logitech Media Server)

python-evdev

lgpio

tm1637_lgpio

Original LMS volume sync idea by mbrennwa

Contributing
Pull requests are welcome. For major changes, please open an issue first
to discuss what you would like to change.

Disclaimer
This project is not affiliated with CamillaDSP, Lyrion Music Server,
Logitech or any of their respective owners.

text

---

## 🥈 Méthode 2 : depuis ton navigateur

1. **Sélectionne tout le bloc ci-dessus** (le bloc ```markdown … ```)
2. **Copie** (Ctrl+C / Cmd+C)
3. **Ouvre un éditeur de texte** (VS Code, nano, gedit, Notepad++)
4. **Colle** (Ctrl+V / Cmd+V)
5. **Enregistre** sous `README.md`

> ⚠️ Attention : la plupart des interfaces ne copient **qu'un seul bloc à la fois**. Si tu copies tout le message, tu récupéreras aussi les explications autour. C'est pourquoi je te donne le README dans **un seul bloc** ci-dessus.

---

## 🥉 Méthode 3 : depuis le terminal (la plus fiable)

Si tu es sur le Raspberry Pi ou en SSH, tu peux créer le fichier directement :

```bash
nano README.md
Puis :

Colle le contenu (Ctrl+Shift+V dans la plupart des terminaux)

Enregistre : Ctrl+O, Entrée

Quitte : Ctrl+X

Ou avec cat :

bash
cat > README.md << 'EOF'
# LyBriCa
...
EOF
Mais le heredoc est fragile avec les backticks et les caractères spéciaux du Markdown. Préfère nano ou VS Code.

✅ Ma recommandation
Copie le bloc ```markdown ci-dessus (méthode 1)

Colle-le dans un fichier README.md sur ton dépôt GitHub :

soit en local avec VS Code / nano

soit directement sur GitHub : bouton Add file → Create new file, nomme-le README.md, colle, commit

Vérifie le rendu : GitHub affiche automatiquement l'aperçu Markdown

🎯 Astuce pour GitHub directement
Si tu veux tout faire depuis l'interface GitHub :

Va sur ton dépôt

Clique sur Add file → Create new file

Nomme-le README.md

Colle le contenu dans l'éditeur

Clique sur Commit new file

C'est la méthode la plus simple si tu ne veux pas toucher au terminal.

Tu veux que je te génère aussi le settings.example.json, le lybrica.service ou le LICENSE ?

