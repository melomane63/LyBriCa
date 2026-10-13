LyBriCa
Lyrion + Bridge + CamillaDSP

A bidirectional bridge between Lyrion Music Server and CamillaDSP for Raspberry Pi, with HID remote control, TM1637 display, ambient light sensor, power relay control and automatic power management.

The ESP32 companion firmware handles the physical power on/off of the Pi, receives the remote control signals and reports the ambient light level.

Table of contents
What it does

Architecture

Hardware

Software components

Installation

Configuration

Feature switches

Remote control keys

Display

Volume curve

Settings file

ESP32 firmware

Systemd service

Troubleshooting

License

What it does
LyBriCa keeps Lyrion Music Server (ex-LMS) and CamillaDSP in sync:

Bidirectional volume / mute sync between Lyrion and CamillaDSP

Automatic DSP config switching (TV / Squeezelite) via GPIO or TOSLINK detection

HID remote control (volume, mute, transport, tone, tilt, loudness, power)

TM1637 display with ambient light sensor for automatic brightness

Power relay control: switch off after silence, back on with sound, halt the Pi after a long silence

Physical power on/off handled by an ESP32 companion firmware

Per-config tone settings saved in settings.json

Architecture
text
Lyrion Music Server (ex-LMS)
        |
        | CLI (port 9090)
        v
    LyBriCa (Python, asyncio)
        |
        | WebSocket (port 1234)
        v
    CamillaDSP
        ^
        | GPIO / I2C / Serial / evdev
        |
   +----+----+---------+
   |         |         |
 HID      TM1637    Power
remote   display    relay
                     |
                  ESP32
                 firmware
The ESP32 is the physical power manager: it cuts the power completely when asked, and wakes the Pi back up on a remote control key press or a sound event. It also reads the ambient light sensor and forwards the value to the Pi over serial.

Hardware
Component	Role	Connection
Raspberry Pi	Runs LyBriCa	—
ESP32	Power on/off, remote receiver, light sensor	Serial (/dev/serial0)
HID remote	User input	USB / Bluetooth (evdev)
TM1637 display	6-digit display	GPIO 23 (CLK), GPIO 24 (DIO)
Power relay	Cuts power to the amplifier	GPIO 4
TV state input	Detects TV on/off	GPIO 12 (optional)
Ambient light sensor	Auto brightness	Via ESP32 (serial)
TOSLINK input	Source detection	ALSA (optional)
Software components
Component	Language	Role
LyBriCa	Python 3	Main bridge, asyncio event loop
ESP32 firmware	C++ / Arduino	Power management, remote, light sensor
CamillaDSP	Rust	Audio DSP engine
Lyrion Music Server	Perl	Music server
Installation
Prerequisites
Raspberry Pi (any model with GPIO)

Raspberry Pi OS (Bookworm or later recommended)

Python 3.9+

CamillaDSP installed and running

Lyrion Music Server installed and running

ESP32 flashed with the companion firmware (see below)

System dependencies
bash
sudo apt update
sudo apt install -y python3-pip python3-dev python3-venv liblgpio-dev libasound2-dev
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
ENABLE_LMS_SYNC      = True
ENABLE_AUTO_POWEROFF = True
ENABLE_REMOTE        = True
ENABLE_DISPLAY       = True
ENABLE_CONFIG_SWITCH = "gpio"
Network addresses
python
CAMILLA_ADDR = ("127.0.0.1", 1234)
LMS_ADDR     = ("127.0.0.1", 9090)
GPIO pins
python
POWER_GPIO = 4
TV_GPIO    = 12
CLK, DIO   = 23, 24
Timing
python
POWER_OFF_DELAY         = 2
HALT_DELAY              = 48
SCREEN_RETURN_DELAY     = 30
TOSLINK_SILENCE_DELAY_S = 120
Feature switches
Switch	Default	Effect
ENABLE_LMS_SYNC	True	Lyrion ⇄ CamillaDSP volume/mute sync
ENABLE_AUTO_POWEROFF	True	Power relay: off after silence, on with sound
ENABLE_REMOTE	True	HID remote control
ENABLE_DISPLAY	True	TM1637 display + ambient light sensor
ENABLE_CONFIG_SWITCH	"gpio"	Auto config switching
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

Volume screen: config label + volume (0–100)

Tone screen: bass / treble

Tilt screen: tilt / presence

Loudness screen: reference level

Power screen: PW ON / PW OFF / HALT

Brightness is adjusted automatically from the ambient light sensor.

Volume curve
Level	dB	Step
0	−100 (mute)	—
1	−75	—
2–25	−71 to −37.5	~1.5 dB/step
25–100	−37.5 to 0	0.5 dB/step
Settings file
settings.json stores per-config tone values, previous values for the ENTER restore function, last used config per group, and last tone/tilt screen shown.

The file is written atomically.

ESP32 firmware
The ESP32 companion firmware handles:

Physical power on/off of the Raspberry Pi

Remote control reception (IR or RF) and forwarding to the Pi

Ambient light sensor reading and forwarding over serial

Wake-up on remote key press or sound event

Communication protocol
Direction	Message	Meaning
ESP32 → Pi	<int>\n	Ambient light ADC value
ESP32 → Pi	POWER\n	Power key pressed
Pi → ESP32	OFF\n	Cut power
Pi → ESP32	ON\n	Power on
The firmware source is in the firmware/ directory.

Systemd service
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
CamillaDSP unreachable
bash
camilladsp --check
Lyrion CLI unreachable
bash
telnet 127.0.0.1 9090
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
Pull requests are welcome.

Disclaimer
This project is not affiliated with CamillaDSP, Lyrion Music Server, Logitech or any of their respective owners.

📌 Fin du README
Le README s'arrête ici. Tout ce qui suit dans mes messages (explications, méthodes, recommandations) n'en fait pas partie.

🎯 La méthode la plus simple
Sélectionne tout le texte ci-dessus (de # LyBriCa à ...owners.)

Copie

Colle dans README.md

Vérifie sur GitHub

Si tu veux, je peux te donner le README en un seul bloc de code (comme la première fois) — mais tu auras le même problème de blocs imbriqués. La version en texte brut ci-dessus est la plus simple à copier.

Tu veux que je te génère aussi le settings.example.json, le lybrica.service ou le LICENSE ?


