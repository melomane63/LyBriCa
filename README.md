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
