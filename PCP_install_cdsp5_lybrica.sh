#!/bin/sh
# ---------------------------------------------------------------------------
# install_pcp.sh - All-in-one installer for piCorePlayer (menu)
#
#   1) CamillaDSP 5.x + CamillaGUI  (Squeezelite -> loopback -> CamillaDSP -> DAC)
#                                   installed as the CDSPpiCore.tcz extension
#   2) LyBriCa + BLE HID bridge     (TM1637 display, remote, LMS sync; patches pcp-bt.tcz)
#   3) ESP32C3                      (UART + GPIO overlays, pcp.cfg, shutdown monitor)
#
# Usage (SSH, user tc):
#   sh install_pcp.sh      -> interactive menu (no parameters)
#
# Each step runs in its own process: an error in one step does not stop
# the menu, and nothing is saved (pcp bu) if that step fails.
# Recommended order: 1, 2, 3, then a single reboot at the end.
# ---------------------------------------------------------------------------

SELF="$0"
BOOTLOCAL=/opt/bootlocal.sh
HOME_DIR=/home/tc

# Exit code a step returns when the user cancels it (shown as SKIPPED).
# Chosen so it cannot be confused with the exit codes of wget, tar, etc.
SKIP_RC=10

# Exit code the ESP32C3 subscript returns when config.txt already contains
# every line it wants to add (no change, no reboot needed).
UNCHANGED_RC=20

say()  { echo "==> $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

###########################################################################
# CENTRALIZED URLS AND VERSIONS
# All remote resources fetched by this script are listed here.
# To upgrade a component, change ONLY the matching variable below.
###########################################################################

# --- 1) CamillaDSP + CamillaGUI -----------------------------------------
CDSP_VER="5.0.0-beta.2"          # CamillaDSP release tag
GUI_VER="5.0.0-beta.2"           # camillagui-backend release tag

# Binary archives (arch suffix is appended at runtime: aarch64|armv7)
CDSP_URL_BASE="https://github.com/HEnquist/camilladsp/releases/download/v${CDSP_VER}"
CDSP_URL_ARCH="camilladsp-linux-%s.tar.gz"      # %s = aarch64|armv7

GUI_URL_BASE="https://github.com/HEnquist/camillagui-backend/releases/download/v${GUI_VER}"
GUI_URL_ARCH="camillagui_linux_%s.tar.gz"       # %s = aarch64|armv7

# --- 2) LyBriCa + BLE HID bridge ----------------------------------------
PYCAMILLA_REF="next5"            # branch, tag or commit of pycamilladsp
PYCAMILLA_URL="git+https://github.com/HEnquist/pycamilladsp.git@${PYCAMILLA_REF}"

TM1637_URL="https://raw.githubusercontent.com/melomane63/tm1637_lgpio/main/tm1637_lgpio.py"
LYBRICA_URL="https://github.com/melomane63/LyBriCa/raw/refs/heads/main/lybrica.py"

# Patched BLE HID bridge files (raw URLs, no HTML)
PCP_BT_RAW_BASE="https://raw.githubusercontent.com/melomane63/pcp-ble-gatt/refs/heads/main"
PCP_BT_HID_BLE_BRIDGE_URL="${PCP_BT_RAW_BASE}/hid_ble_bridge.py"
PCP_BT_HIDBRIDGE_URL="${PCP_BT_RAW_BASE}/hidbridge"

# --- 3) ESP32C3 ---------------------------------------------------------
# No remote URL: only local pCP files (config.txt, pcp.cfg) and
# shutdown-monitor.tcz from the pCP repository.

# --- pCP extension names (for reference) --------------------------------
EXT_CDSP="CDSPpiCore.tcz"
EXT_LYBRICA="lybrica.tcz"
EXT_PCP_BT="pcp-bt.tcz"
EXT_SHUTDOWN_MONITOR="shutdown-monitor.tcz"

###########################################################################
# 1) CAMILLADSP  (extension version)
#
# Layout:
#   CDSPpiCore.tcz (read-only, rebuilt at each run)
#     /usr/local/bin/camilladsp
#     /usr/local/camillagui/camillagui        (+ symlink config -> $BASE/camillagui/config)
#     /usr/local/tce.installed/CDSPpiCore     (start script, run as root)
#   /mnt/mmcblk0p2/tce/camilladsp (persistent data, outside the extension)
#     configs/ coeffs/ default_config.yml camilladsp_statefile.yml
#     camillagui/config/ (camillagui.yml, gui-config.yml)
###########################################################################
install_camilladsp() {
set -e

### ======================= CONFIG ==========================================
# CDSP_VER and GUI_VER are defined globally at the top of this file.
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
TMPDIR_CDSP=/tmp/install_camilladsp
OPT=/etc/sysconfig/tcedir/optional

die() { echo "ERROR: $*" >&2; exit 1; }

### --- Sanity checks --------------------------------------------------------
REQUIRED_MB=150
AVAILABLE_MB=$(/bin/df -m /dev/mmcblk0p2 | awk 'NR==2 { print $4 }')
if [ "$AVAILABLE_MB" -le "$REQUIRED_MB" ]; then
    die "Not enough space (${AVAILABLE_MB} MB). Enlarge the partition: Main Page > Additional functions > Resize FS"
fi

# (No more refusal if CDSPpiCore.tcz exists: it is simply replaced.)

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

# Same, but temporary: downloaded to /tmp, gone after the next reboot.
install_temporarily_if_missing() {
    if ! tce-status -i | grep -q "$1" ; then
        pcp-load -wil -t /tmp "$1"
    fi
}

set -v

### --- 1. Directories and CamillaDSP binary --------------------------------
EXT="$TMPDIR_CDSP/ext"
mkdir -p "$EXT/usr/local/bin" "$EXT/usr/local/camillagui" "$EXT/usr/local/tce.installed"
CDSP_BIN="$EXT/usr/local/bin/camilladsp"

# Persistent data (outside the extension)
mkdir -p "$BASE/configs" "$BASE/coeffs" "$BASE/camillagui/config"

cd "$TMPDIR_CDSP"
CDSP_ASSET=$(printf "$CDSP_URL_ARCH" "$ARCH_SUFFIX")
CDSP_URL="${CDSP_URL_BASE}/${CDSP_ASSET}"
echo ">>> CamillaDSP: $CDSP_URL"
wget -O camilladsp.tar.gz "$CDSP_URL"
tar -xzf camilladsp.tar.gz -C "$EXT/usr/local/bin"
rm -f camilladsp.tar.gz
chmod +x "$CDSP_BIN"
"$CDSP_BIN" --version

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
title: Setup
EOF
cp "$BASE/configs/loopback.yml" "$BASE/default_config.yml"

if "$CDSP_BIN" -c "$BASE/configs/loopback.yml"; then
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

### --- 4. CamillaGUI (single executable) -----------------------------------
GUI_BIN="$EXT/usr/local/camillagui/camillagui"
case "$ARCH_SUFFIX" in
    aarch64) GUI_ARCH="aarch64" ;;
    *)       GUI_ARCH="armv7" ;;
esac
GUI_ASSET=$(printf "$GUI_URL_ARCH" "$GUI_ARCH")
GUI_URL="${GUI_URL_BASE}/${GUI_ASSET}"

echo ">>> GUI: $GUI_URL"
GUI_TMP="$TMPDIR_CDSP/gui"
mkdir -p "$GUI_TMP"
wget -O "$TMPDIR_CDSP/camillagui.tar.gz" "$GUI_URL" || die "GUI download failed: $GUI_URL"
tar -xzf "$TMPDIR_CDSP/camillagui.tar.gz" -C "$GUI_TMP"
rm -f "$TMPDIR_CDSP/camillagui.tar.gz"

GUI_FOUND=$(find "$GUI_TMP" -type f -name camillagui | head -n 1)
[ -n "$GUI_FOUND" ] || die "camillagui executable not found in the archive."
cp "$GUI_FOUND" "$GUI_BIN"
chmod +x "$GUI_BIN"

# If the archive ships a default gui-config.yml, use it only when none exists yet
GUI_CFG_FOUND=$(find "$GUI_TMP" -type f -name gui-config.yml | head -n 1)
if [ -n "$GUI_CFG_FOUND" ] && [ ! -f "$BASE/camillagui/config/gui-config.yml" ]; then
    cp "$GUI_CFG_FOUND" "$BASE/camillagui/config/gui-config.yml"
fi

( cd "$BASE/camillagui" && "$GUI_BIN" --help > /dev/null ) \
    || die "The GUI executable does not run on this system."

# camillagui.yml is rewritten each time (fixed paths).
# gui-config.yml is NOT touched: the GUI settings survive an update.
cat > "$BASE/camillagui/config/camillagui.yml" <<EOF
---
camilla_host: "127.0.0.1"
camilla_port: 1234
bind_address: "0.0.0.0"
port: 5000
ssl_certificate: null
ssl_private_key: null
gui_config_file: null
config_dir: "$BASE/configs"
coeff_dir: "$BASE/coeffs"
default_config: "$BASE/default_config.yml"
statefile_path: "$BASE/camilladsp_statefile.yml"
log_file: "/tmp/camilladsp.log"
on_set_active_config: null
on_get_active_config: null
supported_capture_types: ["Alsa", "Stdin"]
supported_playback_types: ["Alsa"]
EOF

# Symlink inside the extension: works whether the GUI looks for config/
# next to its executable or in the current directory.
ln -s "$BASE/camillagui/config" "$EXT/usr/local/camillagui/config"

### --- 5. Startup script (run as root when the extension is loaded) --------
cat > "$EXT/usr/local/tce.installed/CDSPpiCore" <<'EOF'
#!/bin/sh
# Starts CamillaDSP then CamillaGUI (run as root by the extension loader)

BASE=/mnt/mmcblk0p2/tce/camilladsp

# Load the ALSA loopback module before Squeezelite starts
modprobe snd-aloop

# CamillaDSP: runs as root so that its audio threads can get real-time
# priority (SCHED_FIFO).
/usr/local/bin/camilladsp \
    -w -p 1234 -a 0.0.0.0 \
    -o /tmp/camilladsp.log \
    -s "$BASE/camilladsp_statefile.yml" &

# CamillaGUI: no real-time priority needed, runs as tc from the persistent folder
sudo -u tc sh -c "cd '$BASE/camillagui' && /usr/local/camillagui/camillagui" &

exit 0
EOF
chmod 775 "$EXT/usr/local/tce.installed/CDSPpiCore"

### --- 6. Build and register the extension ---------------------------------
install_temporarily_if_missing squashfs-tools
TCZ="$TMPDIR_CDSP/$EXT_CDSP"
mksquashfs "$EXT" "$TCZ" -noappend -all-root
mv -f "$TCZ" "$OPT/$EXT_CDSP"

# Make sure the kernel modules (snd-aloop) are loaded before our script runs
ALSA_EXT="alsa-modules-$(uname -r).tcz"
if [ -f "$OPT/$ALSA_EXT" ]; then
    echo "$ALSA_EXT" > "$OPT/$EXT_CDSP.dep"
else
    echo "!!! WARNING: $OPT/$ALSA_EXT not found: no .dep written, check that snd-aloop is loaded early enough."
fi

grep -qx "$EXT_CDSP" /etc/sysconfig/tcedir/onboot.lst \
    || echo "$EXT_CDSP" >> /etc/sysconfig/tcedir/onboot.lst

# Migration: remove the old bootlocal.sh start and the old loose files
sudo sed -i '/# camilladsp-start/d' /opt/bootlocal.sh
rm -f "$BASE/start_camilladsp.sh" "$BASE/camilladsp" "$BASE/camillagui/camillagui"

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
echo "CamillaDSP extension installed ($EXT_CDSP). Reboot to start it."
echo "Web interface: http://pcp.local:5000/gui/index.html"
echo "After reboot, check:"
echo "  tce-status -i | grep CDSPpiCore"
echo "  pidof camilladsp camillagui"
echo "  for t in /proc/\$(pidof camilladsp)/task/*; do echo \"\$(cat \$t/comm): \$(chrt -p \$(basename \$t) | head -1)\"; done"
}

###########################################################################
# 2) LYBRICA + BLE HID BRIDGE (patched pcp-bt.tcz)
#
# Layout:
#   lybrica.tcz (read-only, rebuilt at each run)
#     /usr/local/lib/python3.11/site-packages/
#         camilladsp/ websocket/ serial/ lgpio/ alsaaudio/
#         tm1637_lgpio.py
#     /usr/local/bin/lybrica.py             (read-only, factory copy)
#     /usr/local/bin/start_lybrica.sh       (launcher, applies the override)
#     /usr/local/bin/lybrica                (symlink -> start_lybrica.sh)
#     /usr/local/tce.installed/lybrica      (empty: bootlocal.sh launches it)
#   /home/tc (persistent)
#     lybrica-mod.py                    (OPTIONAL editable copy of lybrica.py;
#                                        not created by this installer, run
#                                        "lybrica reset" to create it)
#     lybrica.log  pcp_startup.log  lybrica.pid
#
# Modules in /usr/local are visible to both tc and root without PYTHONPATH,
# unlike the old pip --user install in /home/tc/.local.
###########################################################################
install_lybrica() {
    # PYCAMILLA_REF, PYCAMILLA_URL, TM1637_URL, LYBRICA_URL,
    # PCP_BT_HID_BLE_BRIDGE_URL and PCP_BT_HIDBRIDGE_URL are defined globally.

    OPT=/etc/sysconfig/tcedir/optional
    TMPDIR_LY=/tmp/install_lybrica

    if [ -d "$TMPDIR_LY" ]; then
        fail "Reboot before re-running ($TMPDIR_LY already exists)."
    fi

    EXT="$TMPDIR_LY/ext"
    PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    SP="$EXT/usr/local/lib/python$PYVER/site-packages"
    mkdir -p "$SP" "$EXT/usr/local/bin" "$EXT/usr/local/tce.installed"

    download_page_check() {  # $1=url  $2=dest
        wget -q -O "$2" "$1" || fail "Download failed: $1"
        head -c 300 "$2" | grep -qiE '<!doctype|<html' && fail "$2 is a web page (use the raw URL)."
    }

    # 1. Permanent extensions (stay in onboot.lst)
    say "Installing permanent extensions..."
    tce-load -wi python3.11.tcz python3.11-evdev.tcz python3.11-bleak.tcz \
        || fail "Permanent extensions failed."

    # 2. Temporary build tools (-w then -i: not meant for onboot.lst)
    TOOLS="python3.11-pip.tcz git.tcz compiletc.tcz libasound-dev.tcz python3.11-dev.tcz squashfs-tools.tcz"
    say "Installing temporary build tools..."
    tce-load -w $TOOLS || fail "Download of build tools failed."
    tce-load -i $TOOLS || fail "Install of build tools failed."

    # 3. Python modules (pip --target, into the extension, not --user)
    say "Installing pycamilladsp ($PYCAMILLA_REF)..."
    python3 -m pip install --target "$SP" --no-cache-dir --force-reinstall \
        "$PYCAMILLA_URL" \
        || fail "pycamilladsp install failed ($PYCAMILLA_URL)."

    for pkg in websocket_client pyserial lgpio pyalsaaudio; do
        say "Installing $pkg..."
        python3 -m pip install --target "$SP" --no-cache-dir "$pkg" \
            || fail "$pkg install failed."
    done

    # 4. TM1637 driver (into the extension, not in /home/tc)
    say "Downloading TM1637 driver..."
    download_page_check "$TM1637_URL" "$SP/tm1637_lgpio.py"

    # 5a. LyBriCa script -> inside the extension (read-only, factory copy).
    #     No editable copy is created in /home/tc here: the user creates it
    #     on demand with "lybrica reset", edits it, then "sudo lybrica restart".
    say "Downloading lybrica.py into the extension..."
    download_page_check "$LYBRICA_URL" "$EXT/usr/local/bin/lybrica.py"

    # 6. start_lybrica.sh -> in the extension (/usr/local/bin)
    #    PYTHONPATH export removed: modules are in /usr/local, visible to root
    #    and to tc without any extra environment variable.
    #
    #    Modes accept both "start" and "--start", so a "lybrica status"
    #    symlink (created below) works without typing the dashes.
    say "Creating start_lybrica.sh (in the extension)..."
    cat > "$EXT/usr/local/bin/start_lybrica.sh" << 'EOF'
#!/bin/sh
#
# start_lybrica.sh - Launch LyBriCa (TOSLINK/LMS/volume sync daemon)
#
# Usage (also available as the "lybrica" symlink, see "lybrica help"):
#   start_lybrica.sh                  start if not running (idempotent, used at boot)
#   sudo start_lybrica.sh --restart   stop any running instance, then start
#   sudo start_lybrica.sh --stop      stop the running instance, do not start
#   start_lybrica.sh --status         print state and exit (exit 0 = running)
#   start_lybrica.sh --log [N]        follow the log (default: last 50 lines)
#   start_lybrica.sh --reset          restore lybrica-mod.py from the factory copy
#   start_lybrica.sh --help           list all commands
#
# The "--" prefix is optional: "lybrica status" == "start_lybrica.sh --status".
#
# Note: --start, --restart and --stop require root: they write to
#       /usr/local/bin (override copy) and to root-owned log files.
#       --status, --log, --reset and --help work as tc.
#
# Logs use uptime-based timestamps ([Xs] = seconds since boot).
#

LOG=/home/tc/pcp_startup.log
LYBRICA_LOG=/home/tc/lybrica.log
PYTHON3=/usr/local/bin/python3
LYBRICA=/usr/local/bin/lybrica.py
MOD=/home/tc/lybrica-mod.py
FACTORY=/tmp/tcloop/lybrica/usr/local/bin/lybrica.py
PIDFILE=/home/tc/lybrica.pid

# Modules are in /usr/local/lib/python3.11/site-packages (from lybrica.tcz),
# visible to both tc and root without PYTHONPATH.

up()  { cut -d' ' -f1 /proc/uptime; }
log() { echo "[$(up)s] $*" >> "$LOG"; }

running_pid() {
    if [ -r "$PIDFILE" ]; then
        p=$(cat "$PIDFILE" 2>/dev/null)
        if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
            if [ -r "/proc/$p/cmdline" ] && tr '\0' ' ' < "/proc/$p/cmdline" | grep -q 'lybrica\.py'; then
                echo "$p"; return
            fi
        fi
    fi
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
    # Local override: if /home/tc/lybrica-mod.py exists, copy it over the
    # tcz version before launching.
    #
    # piCore mounts extensions in linkfs mode: $LYBRICA is a symlink to
    # /tmp/tcloop/lybrica/... which lives on a read-only squashfs, so a
    # plain "cp" would follow the link and fail with "Read-only file system".
    # The copy therefore goes to $LYBRICA.new first (an ordinary file in the
    # tmpfs), then the symlink is removed and replaced by the new file with
    # an atomic "mv" in the same directory.
    #
    # A failed copy (e.g. no root) is not fatal: the symlink to the tcz copy
    # is left untouched and the daemon still starts.
    if [ -f "$MOD" ]; then
        if cp "$MOD" "$LYBRICA.new" 2>>"$LYBRICA_LOG"; then
            rm -f "$LYBRICA"
            mv "$LYBRICA.new" "$LYBRICA"
            log "Override: $MOD copied to $LYBRICA"
        else
            rm -f "$LYBRICA.new"
            log "WARNING: cannot copy $MOD to $LYBRICA, using the existing copy"
        fi
    elif [ ! -L "$LYBRICA" ]; then
        # No mod: make sure $LYBRICA is the tcz symlink again. If a previous
        # run left a regular file here (e.g. a broken mod), restore the link
        # so the factory version is used.
        rm -f "$LYBRICA"
        if [ -e "$FACTORY" ]; then
            ln -s "$FACTORY" "$LYBRICA"
            log "No mod: $LYBRICA restored to the tcz symlink"
        else
            log "ERROR: no mod and $FACTORY missing (tcz not mounted?), cannot restore $LYBRICA"
            return 1
        fi
    fi
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

case "$1" in
    ""|--start|start)     MODE="start" ;;
    --restart|restart)    MODE="restart" ;;
    --stop|stop)          MODE="stop" ;;
    --status|status)      MODE="status" ;;
    --log|log)            MODE="log" ;;
    --reset|reset)        MODE="reset" ;;
    --help|-h|help)       MODE="help" ;;
    *) echo "Usage: $0 [start|restart|stop|status|log [N]|reset|help]" >&2; exit 2 ;;
esac

case "$MODE" in
    start|restart|stop)
        if [ "$(id -u)" -ne 0 ]; then
            echo "ERROR: $MODE requires root (writes to /usr/local/bin and to root-owned logs)." >&2
            echo "       Try: sudo $0 $MODE" >&2
            exit 1
        fi ;;
esac

case "$MODE" in
    start|restart|stop)
        echo "=====================================" >> "$LOG"
        log "LyBriCa: mode=$MODE"
        ;;
esac

PID=$(running_pid)

case "$MODE" in
    help)
        cat <<'HELP'
Usage: lybrica [command] [args]

Commands:
  help            Show this help.

  start           Start LyBriCa if not already running (root required).
                  Idempotent: does nothing if already
