#!/bin/sh
# ---------------------------------------------------------------------------
# install_pcp.sh - All-in-one installer for piCorePlayer (menu)
#
#   1) CamillaDSP 5.x + CamillaGUI  (Squeezelite -> loopback -> CamillaDSP -> DAC)
#   2) Bridge BLE HID               (Bluetooth remote)
#   3) LyBriCa                      (modules Python, daemon)
#   4) ESP32C3                      (UART + GPIO overlays)
#                                    
#
# Usage (SSH, user tc):
#   sh install_pcp.sh      -> interactive menu (no parameters)
#
# Each step runs in its own process: an error in one step does not stop
# the menu, and nothing is saved (pcp bu) if that step fails.
# Recommended order: 1, 2, 3, 4, then a single reboot at the end.
# ---------------------------------------------------------------------------

SELF="$0"
BOOTLOCAL=/opt/bootlocal.sh
HOME_DIR=/home/tc

say()  { echo "==> $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

###########################################################################
# 1) CAMILLADSP
###########################################################################
install_camilladsp() {
set -e

### ======================= CONFIG ==========================================
CDSP_VER="5.0.0-beta.2"          # CamillaDSP version
GUI_VER="5.0.0-beta.2"           # camillagui-backend version (bundle)
DAC_FROM_PCP="yes"               # "yes": take the DAC from OUTPUT in pcp.cfg (read BEFORE it is switched to the loopback)
DAC_DEVICE="null"                # fallback DAC: ALSA "null" output (silence); replace with the real DAC (e.g. hw:CARDNAME,0, see /proc/asound/cards)
SAMPLERATE=48000                 # fixed pipeline sample rate
CAPTURE_FORMAT="S32_LE"          # loopback side (what Squeezelite writes)
PLAYBACK_FORMAT="S16_LE"         # DAC side (see /proc/asound/card0/stream0)
SQ_RATES="48000-48000"           # Squeezelite -r: fixed rate (same as SAMPLERATE)
SQ_UPSAMPLE="hLE"                # Squeezelite -R: resampling options
SQ_ALSA_PARAMS="::::"            # Squeezelite -a: all 5 fields empty (defaults)
### =========================================================================

normalize_fmt() {
    echo "$1" | sed -E 's/^([SF][0-9]+)(_[0-9]+)?LE$/\1\2_LE/'
}
CAPTURE_FORMAT=$(normalize_fmt "$CAPTURE_FORMAT")
PLAYBACK_FORMAT=$(normalize_fmt "$PLAYBACK_FORMAT")

PCP_CFG=/usr/local/etc/pcp/pcp.cfg
if [ "$DAC_FROM_PCP" = "yes" ] && [ -r "$PCP_CFG" ]; then
    PCP_OUT=$(sed -n 's/^OUTPUT="\(.*\)"/\1/p' "$PCP_CFG" | head -n 1)
    PCP_OUT=$(echo "$PCP_OUT" | sed -E 's/^plughw:/hw:/; s/^hw:CARD=([^,]*),DEV=([0-9]+)$/hw:\1,\2/; s/^hw:CARD=([^,]*)$/hw:\1,0/')
    # DAC already set in an existing loopback.yml (script re-run)
    EXISTING_CFG=/mnt/mmcblk0p2/tce/camilladsp/configs/loopback.yml
    EXISTING_DAC=""
    if [ -r "$EXISTING_CFG" ]; then
        EXISTING_DAC=$(awk '/^  playback:/{f=1;next} f && /^    device:/{gsub(/^    device:[ ]*"?/,""); gsub(/"?[ ]*$/,""); print; exit}' "$EXISTING_CFG")
        case "$EXISTING_DAC" in *Loopback*) EXISTING_DAC="" ;; esac
    fi
    case "$PCP_OUT" in
        *Loopback*|"")
            if [ -n "$EXISTING_DAC" ]; then
                DAC_DEVICE="$EXISTING_DAC"
                echo ">>> pcp.cfg OUTPUT is empty or already the loopback: DAC taken from loopback.yml: $DAC_DEVICE"
            else
                echo ">>> pcp.cfg OUTPUT is empty or already the loopback: keeping DAC_DEVICE=$DAC_DEVICE"
            fi ;;
        hw:*)
            DAC_DEVICE="$PCP_OUT"
            echo ">>> DAC taken from pcp.cfg: $DAC_DEVICE" ;;
        *)
            if [ -n "$EXISTING_DAC" ]; then
                DAC_DEVICE="$EXISTING_DAC"
                echo "!!! WARNING: pcp.cfg OUTPUT ('$PCP_OUT') is not a hw: device: DAC taken from loopback.yml: $DAC_DEVICE"
            else
                echo "!!! WARNING: pcp.cfg OUTPUT ('$PCP_OUT') is not a hw: device: keeping DAC_DEVICE=$DAC_DEVICE"
            fi ;;
    esac
fi

BASE=/mnt/mmcblk0p2/tce/camilladsp
GUI_DIR=$BASE/camillagui
TMPDIR_CDSP=/tmp/install_camilladsp

die() { echo "ERROR: $*" >&2; exit 1; }

### --- Sanity checks --------------------------------------------------------
REQUIRED_MB=150
AVAILABLE_MB=$(/bin/df -m /dev/mmcblk0p2 | awk 'NR==2 { print $4 }')
if [ "$AVAILABLE_MB" -le "$REQUIRED_MB" ]; then
    die "Not enough space (${AVAILABLE_MB} MB). Enlarge the partition: Main Page > Additional functions > Resize FS"
fi

if [ -f /etc/sysconfig/tcedir/optional/piCoreCDSP.tcz ]; then
    die "The piCoreCDSP extension is installed: uninstall it and reboot before continuing."
fi

if [ -d "$TMPDIR_CDSP" ]; then
    die "Reboot before re-running ($TMPDIR_CDSP already exists)."
fi

if [ "$(uname -m)" = "aarch64" ]; then
    ARCH_SUFFIX="aarch64"
else
    ARCH_SUFFIX="armv7"
fi
echo ">>> Architecture: $(uname -m) -> camilladsp-linux-$ARCH_SUFFIX binary"

mkdir -p "$TMPDIR_CDSP"

install_if_missing() {
    if ! tce-status -i | grep -q "$1" ; then
        pcp-load -wil "$1"
    fi
}

set -v

### --- 1. Directories and CamillaDSP binary --------------------------------
mkdir -p "$BASE/configs" "$BASE/coeffs"
cd "$BASE"
wget -O camilladsp.tar.gz \
    "https://github.com/HEnquist/camilladsp/releases/download/v${CDSP_VER}/camilladsp-linux-${ARCH_SUFFIX}.tar.gz"
tar -xzf camilladsp.tar.gz
rm -f camilladsp.tar.gz
chmod +x "$BASE/camilladsp"
"$BASE/camilladsp" --version

### --- 2. Module ALSA loopback ---------------------------------------------
if ! sudo modprobe snd-aloop 2>/dev/null; then
    install_if_missing "alsa-modules-$(uname -r)" || true
    sudo modprobe snd-aloop || die "snd-aloop not found. Look for the ALSA modules extension matching your kernel (tce-ab)."
fi
aplay -l | grep -qi loopback || die "Loopback card missing after modprobe."

### --- 3. CamillaDSP config and statefile ----------------------------------
# Minimal pass-through config: Loopback,1 (capture) -> 0 dB gain -> DAC.
# Capture on Loopback,1 because Squeezelite plays on Loopback,0.
cat > "$BASE/configs/loopback.yml" <<EOF
---
devices:
  samplerate: ${SAMPLERATE}
  chunksize: 1024
  queuelimit: 4
  capture:
    type: Alsa
    channels: 2
    device: "hw:Loopback,1"
    format: ${CAPTURE_FORMAT}
  playback:
    type: Alsa
    channels: 2
    device: "${DAC_DEVICE}"
    format: ${PLAYBACK_FORMAT}

filters:
  gain0:
    type: Gain
    parameters:
      gain: 0.0

pipeline:
  - type: Filter
    channels: [0, 1]
    names: [gain0]
EOF
cp "$BASE/configs/loopback.yml" "$BASE/default_config.yml"

if "$BASE/camilladsp" -c "$BASE/configs/loopback.yml"; then
    echo ">>> Config loopback.yml: valid."
else
    echo "!!! WARNING: CamillaDSP considers loopback.yml invalid (see message above)."
fi

DAC_CARD=${DAC_DEVICE#plughw:}
DAC_CARD=${DAC_CARD#hw:}
DAC_CARD=${DAC_CARD%%,*}
if [ -r "/proc/asound/$DAC_CARD/stream0" ]; then
    case "$PLAYBACK_FORMAT" in
        S24_3_LE) PB_FMT_PROC="S24_3LE" ;;
        S24_4_LE) PB_FMT_PROC="S24_LE" ;;
        *)        PB_FMT_PROC="$PLAYBACK_FORMAT" ;;
    esac
    PB_SECTION=$(sed -n '/^Playback:/,/^Capture:/p' "/proc/asound/$DAC_CARD/stream0")
    if echo "$PB_SECTION" | grep -q "Format: $PB_FMT_PROC"; then
        echo ">>> DAC: format $PLAYBACK_FORMAT accepted."
    else
        echo "!!! WARNING: DAC does not advertise $PLAYBACK_FORMAT. Advertised formats:"
        echo "$PB_SECTION" | grep "Format:" | sort -u
    fi
    if echo "$PB_SECTION" | grep "Rates:" | grep -q "$SAMPLERATE"; then
        echo ">>> DAC: sample rate $SAMPLERATE Hz accepted."
    else
        echo "!!! WARNING: DAC does not advertise $SAMPLERATE Hz. Advertised rates:"
        echo "$PB_SECTION" | grep "Rates:" | sort -u
    fi
else
    echo ">>> No stream0 for '$DAC_CARD' (non-USB card or null device?): DAC formats not checked."
fi

# Complete statefile: without mute/volume, CamillaDSP 4.x/5.x rejects the file
cat > "$BASE/camilladsp_statefile.yml" <<EOF
config_path: $BASE/configs/loopback.yml
mute:
- false
- false
- false
- false
- false
volume:
- -40.0
- 0.0
- 0.0
- 0.0
- 0.0
EOF

### --- 4. CamillaGUI (self-contained bundle) -------------------------------
# Upgrade: save the existing GUI config files before the folder is wiped.
# (Fresh install: nothing exists yet, so nothing is saved.)
if [ -d "$GUI_DIR" ]; then
    OLD_FILES=$(find "$GUI_DIR" -type f \( -path '*/config/camillagui.yml' -o -path '*/config/gui-config.yml' \) 2>/dev/null || true)
    if [ -n "$OLD_FILES" ]; then
        GUI_BACKUP="$BASE/camillagui_backup_$(date +%Y%m%d-%H%M%S)"
        mkdir -p "$GUI_BACKUP"
        for f in $OLD_FILES; do cp "$f" "$GUI_BACKUP/"; done
        echo ">>> Existing GUI config saved in $GUI_BACKUP"
    fi
fi

rm -rf "$GUI_DIR"
mkdir -p "$GUI_DIR"
cd "$GUI_DIR"

case "$ARCH_SUFFIX" in
    aarch64) GUI_ARCH="aarch64" ;;
    *)       GUI_ARCH="armv7" ;;
esac
BUNDLE_URL="https://github.com/HEnquist/camillagui-backend/releases/download/v${GUI_VER}/camillagui_linux_${GUI_ARCH}.tar.gz"

echo ">>> Bundle: $BUNDLE_URL"
wget -O camillagui_bundle.tar.gz "$BUNDLE_URL" || die "GUI bundle download failed: $BUNDLE_URL"
tar -xzf camillagui_bundle.tar.gz
rm -f camillagui_bundle.tar.gz

GUI_BIN=$(find "$GUI_DIR" -type f -name camillagui | head -n 1)
[ -n "$GUI_BIN" ] || die "camillagui executable not found in the bundle."
chmod +x "$GUI_BIN"
( cd "$(dirname "$GUI_BIN")" && ./camillagui --help > /dev/null ) \
    || die "The bundle does not run on this system (glibc?)."

GUI_ROOT=$(dirname "$GUI_BIN")
GUI_CFG="$GUI_ROOT/config/camillagui.yml"

# camillagui.yml and gui-config.yml shipped with the bundle are kept as-is.

### --- 5. Startup script ---------------------------------------------------
cat > "$BASE/start_camilladsp.sh" <<EOF
#!/bin/sh
# Starts CamillaDSP then CamillaGUI (called from /opt/bootlocal.sh, as root)

BASE=/mnt/mmcblk0p2/tce/camilladsp

# Load the ALSA loopback module before Squeezelite starts
modprobe snd-aloop

# CamillaDSP: runs as root (inherited from bootlocal.sh) so that its audio
# threads can get real-time priority (SCHED_FIFO).
"\$BASE/camilladsp" \\
    -w -p 1234 -a 0.0.0.0 \\
    -o /tmp/camilladsp.log \\
    -s "\$BASE/camilladsp_statefile.yml" &

# CamillaGUI: no real-time priority needed, runs as tc from its own directory
sudo -u tc sh -c "cd '\$BASE/camillagui' && ./camillagui" &

exit 0
EOF
chmod +x "$BASE/start_camilladsp.sh"

### --- 6. Auto-start via bootlocal.sh --------------------------------------
# snd-aloop must be loaded BEFORE Squeezelite starts: insert the line at the
# top of the file (right after the shebang), once.
MARK="# camilladsp-start"
if ! grep -q "$MARK" /opt/bootlocal.sh; then
    sudo sed -i "1a $BASE/start_camilladsp.sh $MARK" /opt/bootlocal.sh
fi
grep -q "opt/bootlocal.sh" /opt/.filetool.lst || echo "opt/bootlocal.sh" >> /opt/.filetool.lst

### --- 7. Squeezelite -> loopback ------------------------------------------
# Replace VAR="value" if the key exists in pcp.cfg; otherwise warn
# (so that a wrong variable name never silently creates a useless line).
set_pcp_cfg() {
    if grep -q "^$1=" "$PCP_CFG"; then
        sed "s|^$1=.*|$1=\"$2\"|" -i "$PCP_CFG"
        echo ">>> pcp.cfg: $1=\"$2\""
    else
        echo "!!! WARNING: key $1 not found in pcp.cfg, not set. Set it in the pCP web interface."
    fi
}

set_pcp_cfg OUTPUT      "hw:Loopback,0"
set_pcp_cfg ALSA_PARAMS "$SQ_ALSA_PARAMS"   # -a  all fields empty
set_pcp_cfg MAX_RATE    "$SQ_RATES"         # -r  min-max sample rate
set_pcp_cfg UPSAMPLE    "$SQ_UPSAMPLE"      # -R  resampling options

### --- 8. Cleanup and backup -----------------------------------------------
rm -rf "$TMPDIR_CDSP"
pcp backup

set +v
echo ""
echo "CamillaDSP installed."
echo "Web interface: http://pcp.local:5000/gui/index.html"
echo "After reboot, check real-time priority with:"
echo "  for t in /proc/\$(pidof camilladsp)/task/*; do echo \"\$(cat \$t/comm): \$(chrt -p \$(basename \$t) | head -1)\"; done"
}

###########################################################################
# 2) BRIDGE BLE HID
###########################################################################
install_bridge() {
    echo
    echo "-----------------------------------------------------------------"
    echo " BEFORE CONTINUING:"
    echo "  - Bluetooth must be installed and enabled in piCorePlayer"
    echo "    (web interface: Tweaks page, Bluetooth section)."
    echo "  - Do NOT pair the remote from the pCP web interface:"
    echo "    this step pairs it itself (le_auto_pair.py)."
    echo "-----------------------------------------------------------------"
    echo

    BRIDGE_URL="https://github.com/melomane63/LyBriCa/raw/refs/heads/main/hid_ble_bridge.py"
    PAIR_URL="https://github.com/paul-1/pcp-ble-gatt/raw/refs/heads/main/le_auto_pair.py"

    # 1. Dependencies
    # bleak: the bridge itself; dbus-python + gobject: needed by le_auto_pair.py
    say "Installing python3.11-bleak, dbus-python3.11, py3.11gobject-mini..."
    tce-load -wi python3.11-bleak.tcz dbus-python3.11.tcz py3.11gobject-mini.tcz || fail "tce-load failed."

    # 2. Download scripts
    download_script() {  # $1=url  $2=dest
        wget -q -O "$2" "$1" || fail "Download failed: $1"
        head -1 "$2" | grep -q '^#!' || fail "$2 is not a script (wrong URL? use the raw link)."
        chmod 755 "$2"
    }
    say "Downloading scripts..."
    download_script "$BRIDGE_URL" "$HOME_DIR/hid_ble_bridge.py"
    download_script "$PAIR_URL"   "$HOME_DIR/le_auto_pair.py"

    MAC=""
    NAME=""

    # Fresh install: the remote's MAC and name are unknown, so scan for it.
    echo "The remote can be found automatically with a Bluetooth scan (15 s):"
    echo "  1. Put the remote in pairing mode (see its manual)."
    echo "  2. Place it right on the Raspberry Pi: the device with the strongest"
    echo "     signal is taken as your remote."
    echo "  3. Keep other Bluetooth devices (phones...) away if possible."
    echo
    printf "Search for the remote with a Bluetooth scan? [Y/n]: "
    read -r ANS
    case "$ANS" in
        n|N) ;;
        *)
            echo "Remote in pairing mode and placed on the Raspberry Pi?"
            echo "Press Enter to start the scan (15 s)..."
            read -r _
            SCAN_OUT=/tmp/ble_scan.out
            DEV_NAMES=/tmp/ble_names.txt
            DEV_RSSI=/tmp/ble_rssi.txt
            DEVLIST=/tmp/ble_devices.txt
            DEV_RAW=/tmp/ble_rssi_raw.txt
            ESC=$(printf '\033')
            : > "$DEV_RAW"
            bluetoothctl power on > /dev/null 2>&1 || true

            # --- 1) Warm-up scan: primes the BLE cache, output discarded -------
            # BlueZ loses the first advertising reports of a cold StartDiscovery,
            # so the very first scan after idle captures almost nothing (0-1 RSSI).
            # A short throwaway scan fixes that: the real one then sees everything.
            echo "Warming up Bluetooth scan (5 s)..."
            bluetoothctl --timeout 5 scan on > /dev/null 2>&1

            # --- 2) Real scan ---------------------------------------------------
            echo "Scanning (15 s)..."
            bluetoothctl --timeout 15 scan on > "$SCAN_OUT" 2>&1

            N_RSSI=$(grep -c "RSSI:" "$SCAN_OUT" 2>/dev/null || echo 0)
            echo ">>> $N_RSSI RSSI captured"

            # --- 3) Names (after the scan, no concurrent bluetoothctl) ---------
            bluetoothctl devices 2>/dev/null \
                | awk '$1=="Device" { mac=$2; $1=""; $2=""; sub(/^ +/,""); print mac "\t" $0 }' > "$DEV_NAMES"

            # --- 4) Parse RSSI lines from the scan ------------------------------
            # Handles both "RSSI: -52" and "RSSI: 0xffffffcc (-52)".
            sed "s/${ESC}\[[0-9;]*[A-Za-z]//g; s/\r//g" "$SCAN_OUT" | awk '
                /Device [0-9A-Fa-f:]+ RSSI:/ {
                    mac=""
                    for (i=1; i<=NF; i++) if ($i=="Device") { mac=$(i+1); break }
                    if (!mac) next
                    if      (match($0, /\(-?[0-9]+\)/))        v=substr($0, RSTART+1, RLENGTH-2)+0
                    else if (match($0, /RSSI: -?[0-9]+/))      v=substr($0, RSTART+6, RLENGTH-6)+0
                    else next
                    if (v==0) next
                    print mac "\t" v
                }' > "$DEV_RAW"

            # Strongest RSSI seen per device
            awk -F'\t' '{ if (!($1 in best) || $2+0 > best[$1]+0) best[$1]=$2 }
                        END { for (m in best) print m "\t" best[m] }' "$DEV_RAW" > "$DEV_RSSI"

            # List format: RSSI<TAB>MAC<TAB>name, strongest signal first.
            # Without any RSSI info: all known devices ("?"), named ones first.
            if [ -s "$DEV_RSSI" ]; then
                awk -F'\t' 'NR==FNR { rssi[$1]=$2; next } ($1 in rssi) { print rssi[$1] "\t" $1 "\t" $2 }' \
                    "$DEV_RSSI" "$DEV_NAMES" | sort -n -r > "$DEVLIST"
            else
                awk -F'\t' '{
                    g=$1; gsub(":","-",g)
                    if ($2==g) u[++nu]="?\t" $1 "\t" $2; else k[++nk]="?\t" $1 "\t" $2
                } END { for (i=1;i<=nk;i++) print k[i]; for (i=1;i<=nu;i++) print u[i] }' "$DEV_NAMES" > "$DEVLIST"
            fi

            if [ -s "$DEVLIST" ]; then
                # Auto-detect: the strongest device is used if it is close
                # (>= -60 dBm) and clearly stronger (>= 10 dB) than the next one.
                BEST=$(sed -n 1p "$DEVLIST")
                BEST_RSSI=$(echo "$BEST" | cut -f1)
                NEXT_RSSI=$(sed -n 2p "$DEVLIST" | cut -f1)
                case "$BEST_RSSI" in
                    ''|'?'|*[!0-9-]*) ;;
                    *)
                        if [ "$BEST_RSSI" -ge -60 ] && { [ -z "$NEXT_RSSI" ] || [ $((BEST_RSSI - NEXT_RSSI)) -ge 10 ]; }; then
                            echo "Closest device: $(echo "$BEST" | cut -f2)  $(echo "$BEST" | cut -f3-)  (${BEST_RSSI} dBm)"
                            printf "Use it? [Y/n]: "
                            read -r OK
                            case "$OK" in
                                n|N) ;;
                                *)
                                    MAC=$(echo "$BEST" | cut -f2)
                                    NAME=$(echo "$BEST" | cut -f3-) ;;
                            esac
                        fi ;;
                esac

                # Otherwise: show the list, strongest signal first
                if [ -z "$MAC" ]; then
                    if [ "$BEST_RSSI" = "?" ]; then
                        echo "Devices found (no signal info available; named devices first):"
                    else
                        echo "Devices found (strongest signal first):"
                    fi
                    awk -F'\t' '{ s = ($1=="?") ? "signal unknown" : $1 " dBm"; printf "  %d) %s  %s  (%s)\n", NR, $2, $3, s }' "$DEVLIST"
                    printf "Number of your remote (Enter = enter MAC/name manually): "
                    read -r SEL
                    case "$SEL" in
                        ''|*[!0-9]*) ;;
                        *)
                            LINE_SEL=$(sed -n "${SEL}p" "$DEVLIST")
                            if [ -n "$LINE_SEL" ]; then
                                MAC=$(echo "$LINE_SEL" | cut -f2)
                                NAME=$(echo "$LINE_SEL" | cut -f3-)
                            else
                                echo "Invalid number."
                            fi ;;
                    esac
                fi
                [ -n "$MAC" ] && echo "Selected: $MAC  $NAME"
            else
                echo "No device found (is Bluetooth enabled? is the remote in pairing mode?)."
            fi
            rm -f "$SCAN_OUT" "$DEV_NAMES" "$DEV_RSSI" "$DEV_RAW" "$DEVLIST" ;;
    esac

    # Manual entry if still unknown: the MAC address OR the name is enough
    # (le_auto_pair.py accepts --device-mac or --device-name).
    if [ -z "$MAC" ] && [ -z "$NAME" ]; then
        echo
        echo "Manual entry: the MAC address (e.g. AA:BB:CC:DD:EE:FF) OR the device"
        echo "name is enough. The beginning of the name is accepted (e.g. HID Remote)."
        printf "Remote MAC address (Enter to use the device name instead): "
        read -r MAC
        if [ -z "$MAC" ]; then
            printf "Device name (a prefix is enough): "
            read -r NAME
        fi
    fi
    [ -n "$MAC" ] || [ -n "$NAME" ] || fail "A MAC address or a device name is required."

    # Find the MAC of a known device from its name (prefix match, as le_auto_pair.py does)
    resolve_mac_from_name() {  # $1 = name prefix
        bluetoothctl devices 2>/dev/null | awk -v n="$1" '
            $1=="Device" { mac=$2; $1=""; $2=""; sub(/^ +/,""); if (index($0,n)==1) { print mac; exit } }'
    }

    # 3. Pair the remote (skipped if already bonded)
    # Only the name known: try to find the MAC among the devices BlueZ already knows.
    if [ -z "$MAC" ]; then
        MAC=$(resolve_mac_from_name "$NAME")
    fi
    if [ -n "$MAC" ]; then
        say "Checking pairing for $MAC..."
    fi
    if [ -n "$MAC" ] && bluetoothctl info "$MAC" 2>/dev/null | grep -q "Bonded: yes"; then
        say "Already paired and bonded."
    else
        echo "Put the remote in pairing mode, then press Enter..."
        read -r _
        if [ -n "$MAC" ]; then
            "$HOME_DIR/le_auto_pair.py" --device-mac "$MAC"
        else
            "$HOME_DIR/le_auto_pair.py" --device-name "$NAME"
            MAC=$(resolve_mac_from_name "$NAME")
            [ -n "$MAC" ] || fail "Could not find the MAC address of '$NAME' after pairing."
        fi
        bluetoothctl info "$MAC" 2>/dev/null | grep -q "Bonded: yes" \
            || fail "Pairing failed. Retry the pairing, then rerun this step."
    fi

    # 4. start_bridge.sh
    say "Creating $HOME_DIR/start_bridge.sh..."
    cat > "$HOME_DIR/start_bridge.sh" << 'EOF'
#!/bin/sh
# start_bridge.sh - Bluetooth BLE HID bridge for piCorePlayer
# Logs [Xs] = seconds since boot (system clock is not synced yet at boot).

MAC="__MAC__"
LOG=/home/tc/pcp_startup.log
up() { cut -d' ' -f1 /proc/uptime; }

echo "=====================================" >> $LOG
echo "[$(up)s] Bridge script starting" >> $LOG

# 1. Stop the official hidbridge service (only if running)
if ps | grep -v grep | grep -q "hid_ble_bridge.py"; then
    echo "[$(up)s] Official service active, stopping..." >> $LOG
    /usr/local/etc/init.d/hidbridge stop "$MAC" 2>/dev/null
    sudo -n pkill -f "hid_ble_bridge.py --device-mac" 2>/dev/null
    for i in $(seq 1 10); do
        ps | grep -v grep | grep -q "hid_ble_bridge.py" || break
        sleep 1
    done
fi

# 2. Wait for the Bluetooth controller (max 60s)
echo "[$(up)s] Waiting for Bluetooth controller..." >> $LOG
for i in $(seq 1 30); do
    if bluetoothctl show 2>/dev/null | grep -q "Powered: yes"; then
        echo "[$(up)s] Controller ready after $((i*2))s" >> $LOG
        break
    fi
    sleep 2
done

# 3. Launch the bridge
nohup sudo -n -E python3 -u /home/tc/hid_ble_bridge.py --device-mac "$MAC" -vv > /dev/null 2>&1 &
echo "[$(up)s] Bridge launched (pid $!)" >> $LOG
EOF
    sed -i "s/__MAC__/$MAC/" "$HOME_DIR/start_bridge.sh"
    chmod 755 "$HOME_DIR/start_bridge.sh"
    sh -n "$HOME_DIR/start_bridge.sh" || fail "Syntax error in start_bridge.sh."

    # 5. bootlocal.sh (idempotent, placed BEFORE pcp_startup.sh)
    LINE="$HOME_DIR/start_bridge.sh &"
    if grep -qF "$LINE" "$BOOTLOCAL"; then
        say "bootlocal.sh already contains the bridge line."
    else
        say "Adding bridge to $BOOTLOCAL..."
        TMP=/tmp/bootlocal.new
        N=$(grep -n '^#pCPstart' "$BOOTLOCAL" | head -1 | cut -d: -f1)
        if [ -n "$N" ]; then
            head -n $((N-1)) "$BOOTLOCAL" > $TMP
            echo "$LINE" >> $TMP
            tail -n +"$N" "$BOOTLOCAL" >> $TMP
        else
            cp "$BOOTLOCAL" $TMP
            echo "$LINE" >> $TMP
        fi
        sudo sh -c "cat $TMP > $BOOTLOCAL" || fail "Cannot write $BOOTLOCAL."
        rm -f $TMP
    fi

    # 6. Save
    say "Saving with pcp bu..."
    pcp bu

    echo
    echo "Bridge installed. Startup log: tail /home/tc/pcp_startup.log"
    echo "Bridge log: /var/log/pcp_hidbridge-$MAC.log"
}

###########################################################################
# 3) LYBRICA
###########################################################################
install_lybrica() {
    PYCAMILLA_REF="next5"   # branch, tag or commit of pycamilladsp
    TM1637_URL="https://raw.githubusercontent.com/melomane63/tm1637_lgpio/main/tm1637_lgpio.py"
    # The TM1637 driver is installed in the user site-packages
    PYVER_INST=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    TM1637_DIR="$HOME_DIR/.local/lib/python$PYVER_INST/site-packages"
    LYBRICA_URL="https://github.com/melomane63/LyBriCa/raw/refs/heads/main/lybrica.py"

    download_page_check() {  # $1=url  $2=dest
        wget -q -O "$2" "$1" || fail "Download failed: $1"
        head -c 300 "$2" | grep -qiE '<!doctype|<html' && fail "$2 is a web page (use the raw URL)."
    }

    # 1. Permanent extensions (stay in onboot.lst)
    say "Installing permanent extensions..."
    tce-load -wi python3.11.tcz python3.11-evdev.tcz || fail "Permanent extensions failed."

    # 2. Temporary build tools (-w then -i: not meant for onboot.lst)
    TOOLS="python3.11-pip.tcz git.tcz compiletc.tcz libasound-dev.tcz python3.11-dev.tcz"
    say "Installing temporary build tools..."
    tce-load -w $TOOLS || fail "Download of build tools failed."
    tce-load -i $TOOLS || fail "Install of build tools failed."

    # 3. Python modules (pip --user, persistent)
    say "Installing pycamilladsp ($PYCAMILLA_REF)..."
    python3 -m pip install --user --no-cache-dir --force-reinstall \
        "git+https://github.com/HEnquist/pycamilladsp.git@$PYCAMILLA_REF" \
        || fail "pycamilladsp install failed."

    for pkg in websocket_client pyserial lgpio pyalsaaudio; do
        say "Installing $pkg..."
        python3 -m pip install --user --no-cache-dir "$pkg" || fail "$pkg install failed."
    done

    # 4. TM1637 driver
    say "Downloading TM1637 driver to $TM1637_DIR..."
    mkdir -p "$TM1637_DIR" || fail "Cannot create $TM1637_DIR."
    download_page_check "$TM1637_URL" "$TM1637_DIR/tm1637_lgpio.py"

    # 5. LyBriCa
    say "Downloading lybrica.py..."
    download_page_check "$LYBRICA_URL" "$HOME_DIR/lybrica.py"
    chmod 755 "$HOME_DIR/lybrica.py"

    say "Creating $HOME_DIR/start_lybrica.sh..."
    cat > "$HOME_DIR/start_lybrica.sh" << 'EOF'
#!/bin/sh
#
# start_lybrica.sh - Launch LyBriCa (TOSLINK/LMS/volume sync daemon)
#
# Usage:
#   start_lybrica.sh                  start if not running (idempotent, used at boot)
#   sudo start_lybrica.sh --restart   stop any running instance, then start
#   sudo start_lybrica.sh --stop      stop the running instance, do not start
#   start_lybrica.sh --status         print state and exit (exit 0 = running)
#
# Note: lybrica.py runs as root, so --restart and --stop require root too.
#       --start and --status work as user tc.
#
# Logs use uptime-based timestamps ([Xs] = seconds since boot).
#

LOG=/home/tc/pcp_startup.log
LYBRICA_LOG=/home/tc/lybrica.log
PYTHON3=/usr/local/bin/python3
LYBRICA=/home/tc/lybrica.py
PIDFILE=/home/tc/lybrica.pid

# bootlocal.sh runs as root, which does not see /home/tc/.local on its own.
PYVER=$("$PYTHON3" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
export PYTHONPATH=/home/tc/.local/lib/python$PYVER/site-packages

up()  { cut -d' ' -f1 /proc/uptime; }
log() { echo "[$(up)s] $*" >> "$LOG"; }

# Return the PID of the running lybrica.py, or nothing.
running_pid() {
    # Prefer the pidfile (exact PID)...
    if [ -r "$PIDFILE" ]; then
        p=$(cat "$PIDFILE" 2>/dev/null)
        if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
            # Verify the PID is really lybrica.py (PID may have been reused).
            if [ -r "/proc/$p/cmdline" ] && tr '\0' ' ' < "/proc/$p/cmdline" | grep -q 'lybrica\.py'; then
                echo "$p"; return
            fi
        fi
    fi
    # ...otherwise fall back to pgrep, which also finds instances started
    # by a previous version of this script (no pidfile).
    pgrep -f "python.*lybrica\.py" 2>/dev/null | head -1
}

stop_one() {  # $1 = pid
    p=$1
    log "Stopping lybrica.py (pid $p)..."
    kill -TERM "$p" 2>/dev/null
    i=0
    while [ "$i" -lt 10 ]; do
        kill -0 "$p" 2>/dev/null || { rm -f "$PIDFILE"; log "lybrica.py stopped"; return 0; }
        i=$((i + 1)); sleep 1
    done
    log "SIGTERM ignored after 10s, sending SIGKILL"
    kill -KILL "$p" 2>/dev/null
    sleep 1
    if kill -0 "$p" 2>/dev/null; then
        log "ERROR: lybrica.py still alive after SIGKILL"
        return 1
    fi
    rm -f "$PIDFILE"
    log "lybrica.py killed"
    return 0
}

start_one() {
    log "Launching lybrica.py"
    nohup "$PYTHON3" "$LYBRICA" >> "$LYBRICA_LOG" 2>&1 &
    p=$!
    echo "$p" > "$PIDFILE"
    sleep 1
    if kill -0 "$p" 2>/dev/null; then
        log "lybrica.py launched (pid $p)"
        return 0
    fi
    rm -f "$PIDFILE"
    log "ERROR: lybrica.py exited immediately after launch, see $LYBRICA_LOG"
    return 1
}

# --- argument parsing -----------------------------------------------------
case "$1" in
    ""|--start) MODE="start" ;;
    --restart)  MODE="restart" ;;
    --stop)     MODE="stop" ;;
    --status)   MODE="status" ;;
    *) echo "Usage: $0 [--restart|--stop|--status]" >&2; exit 2 ;;
esac

# --restart and --stop require root (lybrica.py runs as root).
case "$MODE" in
    restart|stop)
        if [ "$(id -u)" -ne 0 ]; then
            echo "ERROR: --$MODE requires root (lybrica.py runs as root)." >&2
            echo "       Try: sudo $0 --$MODE" >&2
            exit 1
        fi ;;
esac

# Log a header only for action modes (not for --status, a query).
case "$MODE" in
    start|restart|stop)
        echo "=====================================" >> "$LOG"
        log "LyBriCa: mode=$MODE"
        ;;
esac

PID=$(running_pid)

case "$MODE" in
    status)
        if [ -n "$PID" ]; then echo "lybrica.py running (pid $PID)"; exit 0
        else                   echo "lybrica.py not running";        exit 1
        fi ;;

    stop)
        if [ -z "$PID" ]; then log "Nothing to stop"; exit 0; fi
        stop_one "$PID" || exit 1
        exit 0 ;;

    start)
        if [ -n "$PID" ]; then
            log "lybrica.py already running (pid $PID), exiting."
            exit 0
        fi
        start_one || exit 1 ;;

    restart)
        if [ -n "$PID" ]; then
            log "Restart requested, stopping pid $PID first."
            stop_one "$PID" || exit 1
        else
            log "Restart requested, but nothing was running."
        fi
        start_one || exit 1 ;;
esac
EOF
    chmod 755 "$HOME_DIR/start_lybrica.sh"
    sh -n "$HOME_DIR/start_lybrica.sh" || fail "Syntax error in start_lybrica.sh."

    # 6. bootlocal.sh (idempotent, placed AFTER the pcp_startup.sh block)
    LINE="$HOME_DIR/start_lybrica.sh &"
    if grep -qF "$LINE" "$BOOTLOCAL"; then
        say "bootlocal.sh already contains the LyBriCa line."
    else
        say "Adding LyBriCa to $BOOTLOCAL..."
        TMP=/tmp/bootlocal.new
        N=$(grep -n '^#pCPstop' "$BOOTLOCAL" | head -1 | cut -d: -f1)
        if [ -n "$N" ]; then
            head -n "$N" "$BOOTLOCAL" > $TMP
            echo "$LINE" >> $TMP
            tail -n +$((N+1)) "$BOOTLOCAL" >> $TMP
        else
            cp "$BOOTLOCAL" $TMP
            echo "$LINE" >> $TMP
        fi
        sudo sh -c "cat $TMP > $BOOTLOCAL" || fail "Cannot write $BOOTLOCAL."
        rm -f $TMP
    fi

    # 7. Check imports, cleanup, save
    say "Checking imports..."
    python3 -c "import camilladsp, websocket, serial, lgpio, alsaaudio, evdev" \
        || fail "A module cannot be imported."

    rm -rf "$HOME_DIR/.cache/pip"
    say "Saving with pcp bu..."
    pcp bu

    echo
    echo "LyBriCa installed. The temporary build tools disappear at the next reboot."
    echo "Logs: /home/tc/pcp_startup.log  /home/tc/lybrica.log"
    echo "Note: modules in /home/tc/.local are NOT visible to scripts run with sudo."
}

###########################################################################
# 4) ESP32C3 (UART + GPIO overlays)
###########################################################################
install_esp32c3() {
    echo
    echo "-----------------------------------------------------------------"
    echo " ESP32C3 microcontroller support:"
    echo "   - Enable the UART on GPIO 14/15 (used by the ESP32C3)."
    echo "   - Move gpio-poweroff / gpio-shutdown from GPIO 20/21"
    echo "     (reserved by I2S) to GPIO 26/16, so that an I2S DAC can be"
    echo "     added later without a GPIO conflict."
    echo "   (config.txt lives on the FAT partition, reboot required)"
    echo "-----------------------------------------------------------------"
    echo
    printf "Apply the ESP32C3 configuration to config.txt? [y/N]: "
    read -r ESP_ANS
    case "$ESP_ANS" in
        y|Y) ;;
        *)
            say "Cancelled (config.txt unchanged)."
            return 0 ;;
    esac

    ESP_SCRIPT=/tmp/fix_gpio_overlays.sh
    cat > "$ESP_SCRIPT" <<'GPIO_SCRIPT_EOF'
#!/bin/sh
# Replace old gpio-poweroff/gpio-shutdown overlays with new ones on GPIO 26/16,
# and enable the UART.

set -e

FAT_MNT=/mnt/mmcblk0p1
FAT_DEV=/dev/mmcblk0p1
CONFIG="$FAT_MNT/config.txt"

[ "$(id -u)" -eq 0 ] || { echo "ERROR: run as root (sudo $0)" >&2; exit 1; }

# Mount the FAT partition if not already mounted
if ! mount | grep -q " $FAT_MNT "; then
    mkdir -p "$FAT_MNT"
    mount "$FAT_DEV" "$FAT_MNT"
    echo ">>> $FAT_DEV mounted on $FAT_MNT"
fi

[ -f "$CONFIG" ] || { echo "ERROR: $CONFIG not found" >&2; exit 1; }

# Remove any previous gpio-poweroff / gpio-shutdown lines (whatever GPIO)
sed -i -E '/^[[:space:]]*dtoverlay=gpio-(poweroff|shutdown)/d' "$CONFIG"

# Remove any previous enable_uart line (will be re-added below)
sed -i -E '/^[[:space:]]*enable_uart=/d' "$CONFIG"

# Append the new lines
cat >> "$CONFIG" <<'EOF'

dtoverlay=gpio-poweroff,gpiopin=26,active_low=1
dtoverlay=gpio-shutdown,gpio_pin=16,active_low=0,gpio_pull=down
enable_uart=1
EOF

echo ">>> $CONFIG updated, last lines:"
tail -8 "$CONFIG"

umount "$FAT_MNT"
echo ">>> $FAT_MNT unmounted"
GPIO_SCRIPT_EOF

    chmod 755 "$ESP_SCRIPT"
    if sudo sh "$ESP_SCRIPT"; then
        say "config.txt updated."
        echo ">>> Reboot required to apply:  pcp reboot"
    else
        rm -f "$ESP_SCRIPT"
        fail "config.txt update failed (see above)."
    fi
    rm -f "$ESP_SCRIPT"
}

###########################################################################
# DISPATCH: one step = one process (sh "$SELF" --run <step>)
###########################################################################
if [ "$1" = "--run" ]; then
    case "$2" in
        camilla) install_camilladsp ;;
        bridge)  install_bridge ;;
        lybrica) install_lybrica ;;
        esp32c3) install_esp32c3 ;;
        *) fail "Unknown step: $2" ;;
    esac
    exit $?
fi

[ "$(id -u)" -eq 0 ] && fail "Run this script as user tc, not root."

DONE=""

run_step() {  # $1 = camilla|bridge|lybrica|esp32c3
    echo
    echo "############################################################"
    echo "# Step: $1"
    echo "############################################################"
    if sh "$SELF" --run "$1"; then
        DONE="$DONE $1:OK"
    else
        DONE="$DONE $1:FAILED"
        echo "!!! Step '$1' failed (see messages above)." >&2
    fi
}

do_reboot() {
    case "$DONE" in
        *FAILED*) echo "!!! Some steps failed:$DONE" ;;
    esac
    printf "Reboot now? [y/N]: "
    read -r R
    case "$R" in
        y|Y) echo "Rebooting..."; pcp reboot; exit 0 ;;
        *)   echo "Reboot cancelled." ;;
    esac
}

run_choice() {
    for c in "$@"; do
        case "$c" in
            1|camilla) run_step camilla ;;
            2|bridge)  run_step bridge ;;
            3|lybrica) run_step lybrica ;;
            4|esp32c3) run_step esp32c3 ;;
            a|A|all)   run_step camilla; run_step bridge; run_step lybrica; run_step esp32c3 ;;
            r|R)       do_reboot ;;
            q|Q)       return 1 ;;
            *) echo "Unknown choice: $c" ;;
        esac
    done
    return 0
}

summary() {
    echo
    if [ -n "$DONE" ]; then
        echo "Result:$DONE"
        echo "Reboot to activate everything:  pcp reboot"
    fi
}

# Interactive menu
while true; do
    echo
    echo "=============================================="
    echo "  piCorePlayer installer"
    echo "=============================================="
    echo "  1) CamillaDSP + CamillaGUI"
    echo "  2) BLE HID bridge (remote)"
    echo "  3) LyBriCa (Python modules, TM1637, daemon)"
    echo "  4) ESP32C3 (UART + GPIO overlays)"
    echo "  A) Install everything (1, 2, 3 then 4)"
    echo "  R) Reboot now"
    echo "  Q) Quit"
    echo "----------------------------------------------"
    printf "Choice (several allowed, e.g. 1 3): "
    read -r CHOICES || break
    [ -z "$CHOICES" ] && continue
    # shellcheck disable=SC2086
    run_choice $CHOICES || break
done

summary
