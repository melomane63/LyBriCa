#!/bin/bash

# Exit on error
set -e

# =============================================================================
# Global download URLs
# =============================================================================
CDSP_URL="https://github.com/HEnquist/camilladsp/releases/download/v5.0.0-beta.2/camilladsp-linux-aarch64.tar.gz"
GUI_URL="https://github.com/HEnquist/camillagui-backend/releases/download/v5.0.0-beta.2/camillagui_linux_aarch64.tar.gz"
PYCAMILLADSP_URL="git+https://github.com/HEnquist/pycamilladsp.git@next5"
LMS_URL="https://downloads.lms-community.org/LyrionMusicServer_v9.1.1/lyrionmusicserver_9.1.1_arm.deb"

# Extract versions from URLs (for menu display)
CDSP_VERSION="${CDSP_URL#*/download/v}"
CDSP_VERSION="${CDSP_VERSION%%/*}"

LMS_VERSION="${LMS_URL#*/LyrionMusicServer_v}"
LMS_VERSION="${LMS_VERSION%%/*}"

# =============================================================================
# Function to update and upgrade the system
# =============================================================================
upgrade_system() {
    echo "🔄 Updating and upgrading system..."
    sudo apt update && sudo apt upgrade -y
    ###sudo apt install -y git wget curl python3-pip python3-venv python3-dev build-essential systemd libasound2-dev
    sudo apt install -y wget systemd
}

# =============================================================================
# Helper: write a systemd service file, protecting manual edits
# =============================================================================
write_service_protected() {
    local service_name="$1"
    local service_file="$2"
    local service_content="$3"

    if [ ! -f "$service_file" ]; then
        echo "📝 Creating ${service_name}..."
        echo "$service_content" | sudo tee "$service_file" > /dev/null
        echo "✅ ${service_name} written."
        return 0
    fi

    if [ "$(sudo cat "$service_file")" = "$service_content" ]; then
        echo "✅ ${service_name} already up to date, skipping."
        return 0
    fi

    echo "⚠  ${service_name} exists but differs from the script's version."
    read -p "Overwrite ${service_name}? (y/n): " confirm
    if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
        echo "↩  Keeping existing ${service_name} (manual edits preserved)."
        return 1
    fi

    echo "$service_content" | sudo tee "$service_file" > /dev/null
    echo "✅ ${service_name} overwritten."
    return 0
}

# =============================================================================
# Function to install Lybrica
# (Remote, Display, Trigger, AutoSetconfig, Volume synchronisation)
# =============================================================================
install_lybrica() {
    echo "🎮 Installing Lybrica (Remote, Display, Trigger, AutoSetconfig, Volume synchronisation)..."

    # -------------------------------------------------------------------------
    # 0. Lybrica-specific dependencies
    # -------------------------------------------------------------------------
    echo "📦 Installing Lybrica dependencies..."
    sudo apt install -y git curl python3-pip python3-venv python3-dev build-essential libasound2-dev

    # -------------------------------------------------------------------------
    # 1. Configure /boot/firmware/config.txt and cmdline.txt
    # -------------------------------------------------------------------------
    echo "⚙  Configuring /boot/firmware/config.txt..."

    CONFIG_FILE="/boot/firmware/config.txt"

    if sudo grep -q "^#installed-by-lybrica" "$CONFIG_FILE" 2>/dev/null; then
        echo "ℹ  Lybrica already installed (found #installed-by-lybrica in config.txt). Skipping config.txt rewrite."
    else
        sudo cp "$CONFIG_FILE" "${CONFIG_FILE}.bak"

        sudo tee "$CONFIG_FILE" > /dev/null <<'EOL'
# For more options and information see
# http://rptl.io/configtxt
# Some settings may impact device functionality. See link above for details

# Uncomment some or all of these to enable the optional hardware interfaces

# Enable audio (loads snd_bcm2835)

# Additional overlays and parameters are documented
# /boot/firmware/overlays/README

# Automatically load overlays for detected cameras
#camera_auto_detect=1

# Automatically load overlays for detected DSI displays
#display_auto_detect=1

# Automatically load initramfs files, if found
auto_initramfs=1

# Enable DRM VC4 V3D driver
max_framebuffers=2

# Don't have the firmware create an initial video= setting in cmdline.txt.
# Use the kernel's default instead.
disable_fw_kms_setup=1

# Run in 64-bit mode
arm_64bit=1

# Disable compensation for displays with overscan
disable_overscan=1

# Run as fast as firmware / board allows
arm_boost=1

[cm4]
# Enable host mode on the 2711 built-in XHCI USB controller.
# This line should be removed if the legacy DWC2 controller is required
# (e.g. for USB device mode) or if USB support is not required.
otg_mode=1

[cm5]

[all]
dtparam=audio=off
dtoverlay=gpio-poweroff,gpiopin=26,active_low=1
dtoverlay=gpio-shutdown,gpio_pin=16,active_low=0,gpio_pull=down
enable_uart=1

#installed-by-lybrica
EOL

        echo "✅ /boot/firmware/config.txt rewritten!"
    fi

    CMDLINE_FILE="/boot/firmware/cmdline.txt"
    if [ -f "$CMDLINE_FILE" ]; then
        if ! sudo grep -q "console=serial0" "$CMDLINE_FILE"; then
            echo "ℹ  cmdline.txt already clean (no serial console)."
        else
            sudo cp "$CMDLINE_FILE" "${CMDLINE_FILE}.bak"
            sudo sed -i 's/console=serial0,[0-9]* //' "$CMDLINE_FILE"
            echo "✅ cmdline.txt cleaned (serial console removed)."
        fi
    fi

    # -------------------------------------------------------------------------
    # 2. User groups & venv
    # -------------------------------------------------------------------------
    sudo usermod -aG dialout,gpio,input,spi,i2c,audio "$USER"

    # -------------------------------------------------------------------------
    # 2b. Sudoers rule (no protection, trivial content)
    # -------------------------------------------------------------------------
    echo "🔐 Adding sudoers rule for passwordless shutdown..."
    SUDOERS_TMP=$(mktemp)
    echo "$USER ALL=(ALL) NOPASSWD: /sbin/shutdown" > "$SUDOERS_TMP"
    if ! sudo visudo -c -f "$SUDOERS_TMP" > /dev/null; then
       rm -f "$SUDOERS_TMP"
       echo "❌ Invalid sudoers rule, nothing installed."
       exit 1
    fi
    sudo install -m 440 -o root -g root "$SUDOERS_TMP" /etc/sudoers.d/lybrica
    rm -f "$SUDOERS_TMP"

    if [ ! -d "/opt/venv" ]; then
        sudo mkdir -p /opt/venv
        sudo python3 -m venv --system-site-packages /opt/venv
    fi
    sudo /opt/venv/bin/pip install --upgrade pip

    # -------------------------------------------------------------------------
    # 3. Dependencies
    # -------------------------------------------------------------------------
    sudo /opt/venv/bin/pip install evdev==1.6.1 lgpio==0.2.2.0 pyserial==3.5 pyalsaaudio

    INSTALL_TM1637=false
    if /opt/venv/bin/pip show camilladsp &> /dev/null; then
        echo "✅ camilladsp (pycamilladsp) already installed, skipping."
    else
        echo "📦 Installing camilladsp from $PYCAMILLADSP_URL..."
        sudo /opt/venv/bin/pip install "$PYCAMILLADSP_URL"
        INSTALL_TM1637=true
    fi

    # -------------------------------------------------------------------------
    # 4. Download scripts
    # -------------------------------------------------------------------------
    SAVED_REMOTE_NAME=""
    if [ -f "$HOME/scripts/lybrica.py" ]; then
        SAVED_REMOTE_NAME=$(/opt/venv/bin/python3 - "$HOME/scripts/lybrica.py" <<'EOF' || true
import ast
import re
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    text = f.read()

m = re.search(
    r"""^REMOTE_NAME\s*=\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""",
    text,
    re.MULTILINE,
)
if m:
    print(ast.literal_eval(m.group(1)))
EOF
)
    fi

    echo "📥 Downloading lybrica.py from GitHub..."
    mkdir -p "$HOME/scripts"
    wget -q https://raw.githubusercontent.com/melomane63/LyBriCa/main/lybrica.py -O "$HOME/scripts/lybrica.py"

    if [ -n "$SAVED_REMOTE_NAME" ]; then
        if /opt/venv/bin/python3 - "$HOME/scripts/lybrica.py" "$SAVED_REMOTE_NAME" <<'EOF'
import json
import re
import sys

script, name = sys.argv[1], sys.argv[2]
with open(script, encoding="utf-8") as f:
    text = f.read()

pattern = re.compile(
    r"""^(REMOTE_NAME\s*=\s*)(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""",
    re.MULTILINE,
)
new_text, count = pattern.subn(
    lambda m: m.group(1) + json.dumps(name, ensure_ascii=False), text, count=1
)
if count == 0:
    sys.exit(1)

with open(script, "w", encoding="utf-8") as f:
    f.write(new_text)
EOF
        then
            echo "✅ REMOTE_NAME \"$SAVED_REMOTE_NAME\" kept from the previous lybrica.py."
        else
            echo "⚠  Could not restore REMOTE_NAME \"$SAVED_REMOTE_NAME\": run option 8 (Pair Bluetooth Remote)."
        fi
    fi

    if $INSTALL_TM1637; then
        PYVER=$(/opt/venv/bin/python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        echo "📥 Downloading tm1637_lgpio.py from GitHub..."
        wget -q https://raw.githubusercontent.com/melomane63/tm1637_lgpio/main/tm1637_lgpio.py -O /tmp/tm1637_lgpio.py
        sudo cp /tmp/tm1637_lgpio.py "/opt/venv/lib/python${PYVER}/site-packages/tm1637_lgpio.py"
        rm -f /tmp/tm1637_lgpio.py
    else
        echo "✅ tm1637_lgpio.py already installed (with camilladsp), skipping."
    fi

    # -------------------------------------------------------------------------
    # 5. systemd service (protected)
    # -------------------------------------------------------------------------
    SERVICE_FILE="/etc/systemd/system/lybrica.service"
    SERVICE_CONTENT=$(cat <<EOL
[Unit]
Description=Lybrica (Remote, Display, Trigger, AutoSetconfig, Volume synchronisation)
After=default.target

[Service]
User=$USER
Type=simple
WorkingDirectory=$HOME/scripts
ExecStart=/opt/venv/bin/python3 $HOME/scripts/lybrica.py
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
EOL
)

    write_service_protected "lybrica.service" "$SERVICE_FILE" "$SERVICE_CONTENT" || true

    sudo systemctl daemon-reload
    sudo systemctl enable lybrica.service
    sudo systemctl restart lybrica.service
    echo "✅ Lybrica service configured and restarted with /opt/venv/bin/python3 !"
}

# =============================================================================
# Function to install CamillaDSP & CamillaGUI (64-bit / aarch64, v5)
# =============================================================================
install_camilladsp() {
    echo "🎛 Installing CamillaDSP (aarch64, v${CDSP_VERSION})..."

    if [ "$(uname -m)" != "aarch64" ]; then
        echo "❌ Ce script ne fournit que des binaires aarch64 (64 bits)."
        echo "   Architecture détectée : $(uname -m). Utilise un OS Raspberry Pi OS 64 bits."
        return 1
    fi

    mkdir -p ~/camilladsp ~/camilladsp/coeffs ~/camilladsp/configs

    # --- CamillaDSP installed? --------------------------------------------
    if command -v camilladsp &> /dev/null; then
        CDSP_INSTALLED=$(camilladsp --version 2>/dev/null | awk '{print $2}')

        if dpkg --compare-versions "$CDSP_INSTALLED" eq "$CDSP_VERSION" 2>/dev/null; then
            VER_STATE="same as"
        elif dpkg --compare-versions "$CDSP_INSTALLED" lt "$CDSP_VERSION" 2>/dev/null; then
            VER_STATE="older than"
        else
            VER_STATE="newer than"
        fi

        echo "ℹ  CamillaDSP is already installed: v${CDSP_INSTALLED} (${VER_STATE} target v${CDSP_VERSION})."
        read -p "Reinstall CamillaDSP & CamillaGUI? (y/n): " confirm
        if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
            echo "↩  Cancelled."
            return 1
        fi
    fi

    # --- CamillaDSP binary -------------------------------------------------
    echo "📥 CamillaDSP : $CDSP_URL"
    wget "$CDSP_URL" -O ~/camilladsp/camilladsp-linux-aarch64.tar.gz \
        || { echo "❌ Téléchargement CamillaDSP échoué"; return 1; }
    sudo tar -xvf ~/camilladsp/camilladsp-linux-aarch64.tar.gz -C /usr/local/bin/
    rm -f ~/camilladsp/camilladsp-linux-aarch64.tar.gz

    # --- CamillaGUI binary -------------------------------------------------
    echo "🖥️  Installing CamillaGUI (aarch64, v${CDSP_VERSION})..."
    echo "📥 CamillaGUI : $GUI_URL"
    wget "$GUI_URL" -O /tmp/camillagui_linux_aarch64.tar.gz \
        || { echo "❌ Téléchargement CamillaGUI échoué"; return 1; }

    sudo mkdir -p /opt/camillagui_backend
    sudo tar -xzf /tmp/camillagui_linux_aarch64.tar.gz -C /opt/camillagui_backend/
    rm -f /tmp/camillagui_linux_aarch64.tar.gz

    if [ ! -f /opt/camillagui_backend/camillagui ]; then
        echo "❌ Binaire 'camillagui' introuvable dans /opt/camillagui_backend/."
        return 1
    fi
    sudo chmod +x /opt/camillagui_backend/camillagui
    sudo chown -R "$USER":"$USER" /opt/camillagui_backend 2>/dev/null || true

    # --- Service systemd CamillaDSP (protected) ---------------------------
    CDSP_SERVICE_FILE="/etc/systemd/system/camilladsp.service"
    CDSP_SERVICE_CONTENT=$(cat <<EOL
[Unit]
Description=CamillaDSP
After=default.target
StartLimitIntervalSec=10
StartLimitBurst=10

[Service]
Type=simple
User=$USER
WorkingDirectory=~
ExecStart=camilladsp -s camilladsp/statefile.yml -w -g-40 -o camilladsp/camilladsp.log -p 1234
Restart=always
RestartSec=1
StandardOutput=journal
StandardError=journal
SyslogIdentifier=camilladsp
CPUSchedulingPolicy=fifo
CPUSchedulingPriority=10

[Install]
WantedBy=default.target
EOL
)

    write_service_protected "camilladsp.service" "$CDSP_SERVICE_FILE" "$CDSP_SERVICE_CONTENT" || true

    # --- Service systemd CamillaGUI (protected) ---------------------------
    GUI_SERVICE_FILE="/etc/systemd/system/camillagui.service"
    GUI_SERVICE_CONTENT=$(cat <<EOL
[Unit]
Description=CamillaDSP Backend and GUI
After=default.target

[Service]
Type=idle
User=$USER
WorkingDirectory=/opt/camillagui_backend
ExecStart=/opt/camillagui_backend/camillagui

[Install]
WantedBy=default.target
EOL
)

    write_service_protected "camillagui.service" "$GUI_SERVICE_FILE" "$GUI_SERVICE_CONTENT" || true

    # --- Statefile CamillaDSP (protected) ---------------------------------
    STATEFILE="$HOME/camilladsp/statefile.yml"
    STATEFILE_CONTENT=$(cat <<EOL
config_path: "/home/$USER/camilladsp/configs/_Setup.yml"
mute:
- false
- false
volume:
- -40.0
- 0.0
EOL
)

    if [ ! -f "$STATEFILE" ]; then
        echo "📝 Creating statefile.yml..."
        echo "$STATEFILE_CONTENT" > "$STATEFILE"
        echo "✅ statefile.yml created."
    elif [ "$(cat "$STATEFILE")" = "$STATEFILE_CONTENT" ]; then
        echo "✅ statefile.yml already up to date, skipping."
    else
        echo "⚠  statefile.yml exists but differs from the script's version."
        read -p "Overwrite statefile.yml? (y/n): " confirm
        if [[ "$confirm" == "y" || "$confirm" == "Y" ]]; then
            echo "$STATEFILE_CONTENT" > "$STATEFILE"
            echo "✅ statefile.yml overwritten."
        else
            echo "↩  Keeping existing statefile.yml (manual edits preserved)."
        fi
    fi

    # --- Config par défaut CamillaDSP (protected) -------------------------
    SETUPFILE="$HOME/camilladsp/configs/_Setup.yml"
    SETUPFILE_CONTENT=$(cat <<EOL
devices:
  samplerate: 48000
  chunksize: 512
  queuelimit: 4
  capture:
    type: Alsa
    channels: 2
    device: "plughw:Loopback,1,0"
    format: S32_LE
  playback:
    type: Alsa
    channels: 2
    device: "default"
    format: S16_LE
title: Setup
EOL
)

    if [ ! -f "$SETUPFILE" ]; then
        echo "📝 Creating _Setup.yml..."
        echo "$SETUPFILE_CONTENT" > "$SETUPFILE"
        echo "✅ _Setup.yml created."
    elif [ "$(cat "$SETUPFILE")" = "$SETUPFILE_CONTENT" ]; then
        echo "✅ _Setup.yml already up to date, skipping."
    else
        echo "⚠  _Setup.yml exists but differs from the script's version."
        read -p "Overwrite _Setup.yml? (y/n): " confirm
        if [[ "$confirm" == "y" || "$confirm" == "Y" ]]; then
            echo "$SETUPFILE_CONTENT" > "$SETUPFILE"
            echo "✅ _Setup.yml overwritten."
        else
            echo "↩  Keeping existing _Setup.yml (manual edits preserved)."
        fi
    fi

    # --- Module ALSA loopback ----------------------------------------------
    if ! grep -q "^snd-aloop" /etc/modules-load.d/snd-aloop.conf 2>/dev/null; then
        echo "snd-aloop" | sudo tee -a /etc/modules-load.d/snd-aloop.conf
    fi
    sudo modprobe snd-aloop

    # --- Activation et démarrage -------------------------------------------
    sudo systemctl daemon-reload
    sudo systemctl enable camilladsp camillagui
    sudo systemctl restart camilladsp camillagui

    echo "✅ CamillaDSP v${CDSP_VERSION} (~/camilladsp) et CamillaGUI v${CDSP_VERSION} (/opt/camillagui_backend/camillagui) configurés et redémarrés !"
}

# =============================================================================
# Function to install Lyrion Media Server
# =============================================================================
install_lyrion() {
    INSTALLED=$(dpkg-query -W -f='${Version}' lyrionmusicserver 2>/dev/null || true)

    if [ -z "$INSTALLED" ]; then
        echo "🎵 Lyrion Media Server not installed, installing v${LMS_VERSION}..."
    else
        if dpkg --compare-versions "$INSTALLED" eq "$LMS_VERSION" 2>/dev/null; then
            echo "ℹ  Lyrion Media Server v${INSTALLED} is already installed (same as target)."
        elif dpkg --compare-versions "$INSTALLED" lt "$LMS_VERSION" 2>/dev/null; then
            echo "ℹ  Installed version (v${INSTALLED}) is OLDER than target (v${LMS_VERSION})."
        else
            echo "⚠  Installed version (v${INSTALLED}) is NEWER than target (v${LMS_VERSION})."
        fi
        read -p "Reinstall with v${LMS_VERSION}? (y/n): " confirm
        if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
            echo "↩  Cancelled, keeping v${INSTALLED}."
            return 1
        fi
    fi

    echo "🎵 Installing Lyrion Media Server (v${LMS_VERSION})..."
    wget "$LMS_URL" -O ~/lyrionmusicserver_arm.deb
    sudo dpkg -i ~/lyrionmusicserver_arm.deb || sudo apt --fix-broken install -y
    echo "✅ Lyrion Media Server v${LMS_VERSION} installed!"
}

# =============================================================================
# Function to install Squeezelite
# =============================================================================
install_squeezelite() {
    SQUEEZELITE_CONF="/etc/default/squeezelite"

    if dpkg -l squeezelite 2>/dev/null | grep -q "^ii"; then
        INSTALLED_VER=$(dpkg-query -W -f='${Version}' squeezelite 2>/dev/null || true)
        echo "ℹ  Squeezelite is already installed (v${INSTALLED_VER})."
        echo ""
        echo "📋 Current Squeezelite parameters (from ${SQUEEZELITE_CONF}):"
        if [ -f "$SQUEEZELITE_CONF" ]; then
            grep -E '^[A-Z_]+=' "$SQUEEZELITE_CONF" | sed 's/^/   /'
        else
            echo "   (file not found)"
        fi
        echo ""

        read -p "Reinstall Squeezelite? (y/n): " confirm
        if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
            echo "↩  Cancelled, keeping current installation."
            return 1
        fi
        REINSTALL_FLAG="--reinstall"
    else
        echo "🔊 Installing Squeezelite..."
        REINSTALL_FLAG=""
    fi

    sudo apt install -y $REINSTALL_FLAG squeezelite

    # Safely update Squeezelite output device
    sudo sed -i '/^SL_OPTIONS=/d;/^SL_SOUNDCARD=/d;/^SB_EXTRA_ARGS=/d' "$SQUEEZELITE_CONF"
    echo -e 'SL_SOUNDCARD="hw:Loopback,0"\nSB_EXTRA_ARGS="-W -C 5 -r 48000-48000 -R hLE"' | sudo tee -a "$SQUEEZELITE_CONF" > /dev/null

    sudo systemctl restart squeezelite

    echo ""
    echo "✅ Squeezelite installed and configured!"
    echo ""
    echo "📋 Current Squeezelite parameters (from ${SQUEEZELITE_CONF}):"
    grep -E '^[A-Z_]+=' "$SQUEEZELITE_CONF" | sed 's/^/   /'
    echo ""
}

# =============================================================================
# Function to mount USB drive
# =============================================================================
mount_usb_drive() {
    echo "💾 Setting up USB Drive auto-mount..."
    sudo mkdir -p /mnt/usb

    USB_UUID=$(sudo blkid -s UUID -o value /dev/sda1 2>/dev/null || true)

    if [ -z "$USB_UUID" ]; then
        echo "⚠  No USB drive automatically detected on /dev/sda1."
        read -p "Enter your USB drive UUID manually: " USB_UUID
    else
        echo "✅ USB drive automatically detected with UUID: $USB_UUID"
    fi

    if [ -n "$USB_UUID" ]; then
        sudo sed -i 's|.* /mnt/usb .*||g' /etc/fstab
        sudo sed -i '/^$/d' /etc/fstab

        echo "UUID=$USB_UUID /mnt/usb auto defaults,nofail,x-systemd.device-timeout=1,noatime 0 0" | sudo tee -a /etc/fstab

        sudo mount -a
        echo "✅ USB Drive mounted at /mnt/usb with UUID $USB_UUID!"
    else
        echo "❌ Error: No valid UUID could be configured."
    fi
}

# =============================================================================
# Function to set sound card output
# =============================================================================
set_sound_card() {
    echo "🔊 Launching alsamixer to configure the sound card..."
    echo "   (Press Esc or 'q' to exit once settings are done)"
    alsamixer
    echo "💾 Saving ALSA settings..."
    sudo alsactl store
    echo "✅ ALSA settings saved!"
}

# =============================================================================
# Function to pair Bluetooth Remote using bluetuith
# =============================================================================
pair_bluetooth_remote() {
    LYBRICA_SCRIPT="${LYBRICA_SCRIPT:-$HOME/scripts/lybrica.py}"
    VENV_PYTHON="${VENV_PYTHON:-/opt/venv/bin/python3}"

    echo "🔗 Preparing Bluetooth Remote pairing interface..."

    sudo rfkill unblock bluetooth
    sudo systemctl restart bluetooth
    sleep 2

    bluetoothctl power on

    if ! command -v bluetuith &> /dev/null; then
        echo "📦 Installing bluetuith..."
        wget https://github.com/bluetuith-org/bluetuith/releases/download/v0.2.7/bluetuith_0.2.7_Linux_arm64.tar.gz -O ~/bluetuith.tar.gz
        tar -xzf ~/bluetuith.tar.gz -C ~/ bluetuith
        sudo mv ~/bluetuith /usr/bin/bluetuith
        sudo chmod +x /usr/bin/bluetuith
        rm -f ~/bluetuith.tar.gz
    fi

    echo ""
    echo "💡 Instructions:"
    echo "   1. The bluetuith interface will open."
    echo "   2. Put your remote control into pairing mode (flashing LED)."
    echo "   3. Use the arrow keys to find your remote, press Enter to Pair, then Connect."
    echo "   4. Press 'q' to exit the interface when done."
    echo ""
    read -p "Press Enter to launch bluetuith..."

    bluetuith

    echo "✅ Bluetooth setup interface closed."

    echo ""
    echo "🔍 Detecting input devices..."
    echo "   The remote should now be connected. Press a key on it if needed."
    read -rp "Press Enter to scan..."

    local -a lines
    mapfile -t lines < <("$VENV_PYTHON" - <<'EOF'
import evdev
from evdev import ecodes as e

names = ['KEY_VOLUMEDOWN', 'KEY_VOLUMEUP', 'KEY_MUTE', 'KEY_PLAYPAUSE', 'KEY_PREVIOUSSONG',
         'KEY_NEXTSONG', 'KEY_UP', 'KEY_DOWN', 'KEY_LEFT', 'KEY_RIGHT', 'KEY_POWER',
         'KEY_ENTER', 'KEY_BACK', 'KEY_HOMEPAGE']
codes = {e.ecodes[n] for n in names}

rows = []
for p in evdev.list_devices():
    d = evdev.InputDevice(p)
    keys = d.capabilities().get(e.EV_KEY, [])
    rows.append((len(codes & set(keys)), p, d.name))

for score, p, name in sorted(rows, key=lambda r: (-r[0], r[1])):
    print(f"{p}\t{name}\t{score}/{len(codes)}")
EOF
)

    if [ "${#lines[@]}" -eq 0 ]; then
        echo "❌ No input device found (is the remote connected? is evdev installed in the venv?)"
        return 1
    fi

    echo ""
    echo "Input devices found (best match for the remote first):"
    local i dev_path dev_name dev_keys
    for i in "${!lines[@]}"; do
        IFS=$'\t' read -r dev_path dev_name dev_keys <<< "${lines[$i]}"
        printf "   %d) %-26s %-8s %s\n" "$((i + 1))" "$dev_name" "$dev_keys" "$dev_path"
    done
    echo ""

    local choice
    while true; do
        read -rp "Choose your remote [1-${#lines[@]}] (Enter = 1): " choice
        choice="${choice:-1}"
        if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#lines[@]} )); then
            break
        fi
        echo "Invalid choice."
    done

    IFS=$'\t' read -r dev_path dev_name dev_keys <<< "${lines[$((choice - 1))]}"
    echo "➡  Selected: $dev_name ($dev_keys keys)"
    if (( ${dev_keys%%/*} < 10 )); then
        echo "⚠  This device supports few of the remote keys: check that it is really the remote."
    fi

    if [ ! -f "$LYBRICA_SCRIPT" ]; then
        echo "⚠  $LYBRICA_SCRIPT not found. Set this line manually in lybrica.py:"
        echo "   REMOTE_NAME = \"$dev_name\""
        return 1
    fi

    if "$VENV_PYTHON" - "$LYBRICA_SCRIPT" "$dev_name" <<'EOF'
import json
import re
import sys

script, name = sys.argv[1], sys.argv[2]
with open(script, encoding="utf-8") as f:
    text = f.read()

pattern = re.compile(
    r"""^(REMOTE_NAME\s*=\s*)(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""",
    re.MULTILINE,
)
new_text, count = pattern.subn(
    lambda m: m.group(1) + json.dumps(name, ensure_ascii=False), text, count=1
)
if count == 0:
    sys.exit(1)

with open(script, "w", encoding="utf-8") as f:
    f.write(new_text)
EOF
    then
        echo "✅ REMOTE_NAME set to \"$dev_name\" in $LYBRICA_SCRIPT"
        echo "🔄 Restarting Lybrica service..."
        sudo systemctl restart lybrica
        echo "✅ Lybrica service restarted successfully."
    else
        echo "⚠  Could not update $LYBRICA_SCRIPT (no 'REMOTE_NAME = \"...\"' line, or no write permission)."
        echo "    Set this line manually in lybrica.py:"
        echo "    REMOTE_NAME = \"$dev_name\""
        return 1
    fi
}

# =============================================================================
# Function to shutdown (with confirmation)
# =============================================================================
shutdown_now() {
    read -p "Shutdown now? (y/n): " choice
    if [[ "$choice" == "y" || "$choice" == "Y" ]]; then
        sudo shutdown -h now
        return 0
    fi
    return 1
}

# =============================================================================
# Function to quit the script (with confirmation)
# =============================================================================
quit_now() {
    read -p "Quit the script? (y/n): " choice
    if [[ "$choice" == "y" || "$choice" == "Y" ]]; then
        echo "Quitting..."
        return 0
    fi
    return 1
}

# =============================================================================
# Main Menu
# =============================================================================
while true; do
    echo "============================================"
    echo "  RaspberryPi OS - Audio Setup Menu   "
    echo "============================================"
    echo "1) Upgrade System"
    echo "2) Install CamillaDSP & GUI (v${CDSP_VERSION})"
    echo "3) Install Lyrion Media Server (v${LMS_VERSION})"
    echo "4) Install Squeezelite"
    echo "5) Configure Sound Levels (alsamixer)"
    echo "6) Mount USB Drive"
    echo "7) Install Lybrica (Remote, Display, Trigger, AutoSetconfig, Volume synchronisation)"
    echo "8) Pair Bluetooth Remote"
    echo "S) Shutdown System"
    echo "Q) Quit"
    echo "============================================"
    echo "Tip: chain actions with spaces: '2 6 S' = '6 S 2' (numbers ascending, then S, then Q)."
    read -p "Choose an option [1-8, S, Q]: " -a choices

    if [ "${#choices[@]}" -eq 0 ]; then
        echo "⚠  No option entered."
        continue
    fi

    for option in "${choices[@]}"; do
        case $option in
            1|2|3|4|5|6|7|8) ;;
            S|s|Q|q) ;;
            *) echo "⚠  Invalid option: '$option'. Nothing executed."; continue 2 ;;
        esac
    done

    mapfile -t sorted_numeric < <(printf '%s\n' "${choices[@]}" | grep -E '^[1-8]$' | sort -n -u)

    has_shutdown=false
    has_quit=false
    for option in "${choices[@]}"; do
        case $option in
            S|s) has_shutdown=true ;;
            Q|q) has_quit=true ;;
        esac
    done

    for option in "${sorted_numeric[@]}"; do
        case $option in
            1) upgrade_system || true ;;
            2) install_camilladsp || true ;;
            3) install_lyrion || true ;;
            4) install_squeezelite || true ;;
            5) set_sound_card || true ;;
            6) mount_usb_drive || true ;;
            7) install_lybrica || true ;;
            8) pair_bluetooth_remote || true ;;
        esac
    done

    if $has_shutdown; then
        shutdown_now && exit 0
    fi
    if $has_quit; then
        quit_now && exit 0
    fi

    echo ""
    read -rp "Press Enter to return to the menu..." _
done
