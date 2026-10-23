#!/bin/sh
# ---------------------------------------------------------------------------
# install_pcp.sh - All-in-one installer for piCorePlayer (menu)
#
#   1) CamillaDSP 5.x + CamillaGUI  (Squeezelite -> loopback -> CamillaDSP -> DAC)
#                                   installed as the CDSPpiCore.tcz extension
#   2) LyBriCa + BLE HID bridge     (TM1637 display, remote, LMS sync, BLE bridge)
#      + ESP32C3                    (UART + GPIO overlays in config.txt)
#
# Usage (SSH, user tc):
#   sh install_pcp.sh      -> interactive menu (no parameters)
#
# Each step runs in its own process: an error in one step does not stop
# the menu, and nothing is saved (pcp bu) if that step fails.
# Recommended order: 1, 2, then a single reboot at the end.
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
CDSP_VER="5.0.0-beta.2"          # CamillaDSP version
GUI_VER="5.0.0-beta.2"           # camillagui-backend version
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
wget -O camilladsp.tar.gz \
    "https://github.com/HEnquist/camilladsp/releases/download/v${CDSP_VER}/camilladsp-linux-${ARCH_SUFFIX}.tar.gz"
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
GUI_ASSET="camillagui_linux_${GUI_ARCH}.tar.gz"
GUI_URL="https://github.com/HEnquist/camillagui-backend/releases/download/v${GUI_VER}/${GUI_ASSET}"

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
TCZ="$TMPDIR_CDSP/CDSPpiCore.tcz"
mksquashfs "$EXT" "$TCZ" -noappend -all-root
mv -f "$TCZ" "$OPT/CDSPpiCore.tcz"

# Make sure the kernel modules (snd-aloop) are loaded before our script runs
ALSA_EXT="alsa-modules-$(uname -r).tcz"
if [ -f "$OPT/$ALSA_EXT" ]; then
    echo "$ALSA_EXT" > "$OPT/CDSPpiCore.tcz.dep"
else
    echo "!!! WARNING: $OPT/$ALSA_EXT not found: no .dep written, check that snd-aloop is loaded early enough."
fi

grep -qx "CDSPpiCore.tcz" /etc/sysconfig/tcedir/onboot.lst \
    || echo "CDSPpiCore.tcz" >> /etc/sysconfig/tcedir/onboot.lst

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
echo "CamillaDSP extension installed (CDSPpiCore.tcz). Reboot to start it."
echo "Web interface: http://pcp.local:5000/gui/index.html"
echo "After reboot, check:"
echo "  tce-status -i | grep CDSPpiCore"
echo "  pidof camilladsp camillagui"
echo "  for t in /proc/\$(pidof camilladsp)/task/*; do echo \"\$(cat \$t/comm): \$(chrt -p \$(basename \$t) | head -1)\"; done"
}

###########################################################################
# 2) LYBRICA + BLE HID BRIDGE  (+ ESP32C3 UART/GPIO overlays, merged)
#
# Layout:
#   lybrica.tcz (read-only, rebuilt at each run)
#     /usr/local/lib/python3.11/site-packages/
#         camilladsp/ websocket/ serial/ lgpio/ alsaaudio/
#         tm1637_lgpio.py
#     /usr/local/bin/lybrica.py             (read-only, factory copy)
#     /usr/local/bin/start_lybrica.sh       (launcher, applies the override)
#     /usr/local/bin/lybrica                (symlink -> start_lybrica.sh)
#     /usr/local/bin/hid_ble_bridge.py      (read-only, from the extension)
#     /usr/local/tce.installed/lybrica      (empty: bootlocal.sh launches it)
#   /home/tc (persistent)
#     lybrica-mod.py                    (OPTIONAL editable copy of lybrica.py;
#                                        not created by this installer, run
#                                        "lybrica reset" to create it)
#     lybrica.log  pcp_startup.log  lybrica.pid
#
# Modules in /usr/local are visible to both tc and root without PYTHONPATH,
# unlike the old pip --user install in /home/tc/.local.
#
# ESP32C3 (was step 3): edits config.txt on the FAT partition to add the
# gpio-poweroff (GPIO 26) / gpio-shutdown (GPIO 16) overlays and enable the
# UART. Interactive: the user can cancel it; a cancel or a "no change"
# does NOT fail this step.
###########################################################################
install_lybrica() {
    PYCAMILLA_REF="next5"   # branch, tag or commit of pycamilladsp
    TM1637_URL="https://raw.githubusercontent.com/melomane63/tm1637_lgpio/main/tm1637_lgpio.py"
    LYBRICA_URL="https://github.com/melomane63/LyBriCa/raw/refs/heads/main/lybrica.py"
    BRIDGE_URL="https://github.com/melomane63/LyBriCa/raw/refs/heads/main/hid_ble_bridge.py"

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
        "git+https://github.com/HEnquist/pycamilladsp.git@$PYCAMILLA_REF" \
        || fail "pycamilladsp install failed."

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

    # 5b. hid_ble_bridge.py -> inside the extension (read-only)
    say "Downloading hid_ble_bridge.py into the extension..."
    download_page_check "$BRIDGE_URL" "$EXT/usr/local/bin/hid_ble_bridge.py"

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
  start           Start LyBriCa if not already running (root required).
                  Idempotent: does nothing if already running. Used at boot.

  restart         Stop any running instance, then start (root required).
                  Use this after editing /home/tc/lybrica-mod.py.

  stop            Stop the running instance (root required).

  status          Show which launcher would be used and whether the
                  daemon is running (no root required).

  log [N]         Follow /home/tc/lybrica.log (default: last 50 lines).
                  No root required. Ctrl+C to stop following.

  reset           Restore /home/tc/lybrica-mod.py from the factory copy
                  shipped in the tcz. Creates the file if missing, asks
                  for confirmation before overwriting an existing file.
                  No root required.

  help            Show this help.

The "--" prefix is optional: "lybrica --status" == "lybrica status".
Short aliases: "lybrica -h" and "lybrica --help" both work.

Override workflow:
  1. lybrica reset               create or restore /home/tc/lybrica-mod.py
  2. vi /home/tc/lybrica-mod.py  edit it
  3. sudo lybrica restart        apply the new version
  4. pcp bu                      persist across reboots

Without /home/tc/lybrica-mod.py, the factory version from the tcz runs
(read-only, cannot be edited in place). Deleting the file falls back to
that factory version on the next start.

Files:
  /home/tc/lybrica-mod.py     Editable override (optional, not created by install)
  /home/tc/lybrica.log        Python output (follow with: lybrica log)
  /home/tc/pcp_startup.log    Launcher trace (starts, stops, overrides)
  /home/tc/lybrica.pid        Current process id

Shortcut: /usr/local/bin/lybrica is a symlink to start_lybrica.sh.
HELP
        exit 0 ;;

    status)
        if [ -f "$MOD" ]; then
            echo "launcher: $MOD (copied to $LYBRICA at start)"
        else
            echo "launcher: $LYBRICA (tcz version, no local copy)"
        fi
        if [ -n "$PID" ]; then echo "lybrica.py running (pid $PID)"; exit 0
        else                   echo "lybrica.py not running";        exit 1
        fi ;;

    log)
        [ -r "$LYBRICA_LOG" ] || { echo "No log file yet: $LYBRICA_LOG" >&2; exit 1; }
        LINES=${2:-50}
        exec tail -n "$LINES" -f "$LYBRICA_LOG" ;;

    reset)
        # Restore /home/tc/lybrica-mod.py from the factory copy shipped in
        # the extension. Reads /tmp/tcloop/... directly, never
        # /usr/local/bin/lybrica.py, because that file may currently hold
        # a previous mod (a broken one, for example).
        if [ ! -e "$FACTORY" ]; then
            echo "ERROR: factory copy not found: $FACTORY" >&2
            echo "       Is lybrica.tcz mounted? Check: tce-status -i | grep lybrica" >&2
            exit 1
        fi
        if [ -f "$MOD" ] && cmp -s "$MOD" "$FACTORY"; then
            echo "$MOD already matches the factory copy, nothing to do."
            exit 0
        fi
        if [ -f "$MOD" ]; then
            echo "About to overwrite $MOD with the factory copy."
            echo "  current : $(wc -c < "$MOD") bytes"
            echo "  factory : $(wc -c < "$FACTORY") bytes"
            printf "Continue? [y/N]: "
            read -r ANSWER
            case "$ANSWER" in
                y|Y) ;;
                *) echo "Cancelled, $MOD unchanged."; exit 0 ;;
            esac
        fi
        rm -f "$MOD"
        if cp "$FACTORY" "$MOD"; then
            echo "Restored: $MOD (from the factory copy)"
            echo "Apply it now with:  sudo lybrica restart"
        else
            echo "ERROR: cannot create $MOD" >&2
            exit 1
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
    chmod 755 "$EXT/usr/local/bin/start_lybrica.sh"
    sh -n "$EXT/usr/local/bin/start_lybrica.sh" || fail "Syntax error in start_lybrica.sh."

    # Shortcut: "lybrica status" instead of "start_lybrica.sh --status".
    # Relative symlink, resolved inside the mounted tcz.
    ln -s start_lybrica.sh "$EXT/usr/local/bin/lybrica"

    # 7. Empty tce.installed (nothing to launch at boot: bootlocal.sh does it)
    : > "$EXT/usr/local/tce.installed/lybrica"

    # 8. Build and register the extension
    say "Building lybrica.tcz..."
    TCZ="$TMPDIR_LY/lybrica.tcz"
    mksquashfs "$EXT" "$TCZ" -noappend -all-root || fail "mksquashfs failed."
    mv -f "$TCZ" "$OPT/lybrica.tcz"

    # Dependencies: Python, evdev, bleak, and CDSPpiCore if present
    {
        echo "python3.11.tcz"
        echo "python3.11-evdev.tcz"
        echo "python3.11-bleak.tcz"
        [ -f "$OPT/CDSPpiCore.tcz" ] && echo "CDSPpiCore.tcz"
    } > "$OPT/lybrica.tcz.dep"

    grep -qx "lybrica.tcz" /etc/sysconfig/tcedir/onboot.lst \
        || echo "lybrica.tcz" >> /etc/sysconfig/tcedir/onboot.lst

    # 9. Update bootlocal.sh: ensure the launcher is present
    NEW_LINE="/usr/local/bin/start_lybrica.sh &"
    if grep -qxF "$NEW_LINE" "$BOOTLOCAL"; then
        say "bootlocal.sh already points to $NEW_LINE"
    else
        say "Updating $BOOTLOCAL..."
        TMP=$(mktemp)
        grep -vxF "$NEW_LINE" "$BOOTLOCAL" > "$TMP" || true
        N=$(grep -n '^#pCPstop' "$TMP" | head -1 | cut -d: -f1)
        if [ -n "$N" ]; then
            TMP2=$(mktemp)
            head -n "$N" "$TMP" > "$TMP2"
            echo "$NEW_LINE" >> "$TMP2"
            tail -n +$((N+1)) "$TMP" >> "$TMP2"
            mv "$TMP2" "$TMP"
        else
            echo "$NEW_LINE" >> "$TMP"
        fi
        sudo sh -c "cat $TMP > $BOOTLOCAL" || fail "Cannot write $BOOTLOCAL."
        rm -f "$TMP"
    fi

    # 10. Check imports from the build dir (proves both tc and root see them)
    say "Checking imports from the build dir (tc)..."
    PYTHONPATH="$SP" python3 -c \
        "import camilladsp, websocket, serial, lgpio, alsaaudio, evdev, tm1637_lgpio, bleak" \
        || fail "A module cannot be imported from the build dir (tc)."
    say "Checking imports from the build dir (root)..."
    sudo PYTHONPATH="$SP" python3 -c \
        "import camilladsp, websocket, serial, lgpio, alsaaudio, evdev, tm1637_lgpio, bleak" \
        || fail "A module cannot be imported from the build dir (root)."

    # 11. ESP32C3 overlays (merged from old step 3, interactive)
    #
    # A cancel (SKIP_RC) or a "no change" (UNCHANGED_RC) must NOT fail this
    # step: LyBriCa is already installed and pcp bu must still run.
    ESP32_STATUS="unknown"
    say "Configuring ESP32C3 (config.txt overlays)..."
    configure_esp32c3
    ESP_RC=$?
    case "$ESP_RC" in
        0)                ESP32_STATUS="applied (reboot needed)" ;;
        "$SKIP_RC")       ESP32_STATUS="cancelled" ;;
        "$UNCHANGED_RC")  ESP32_STATUS="already up to date" ;;
        *)                ESP32_STATUS="FAILED (see messages above)" ;;
    esac
    say "ESP32C3: $ESP32_STATUS"

    # 12. Save
    say "Saving with pcp bu..."
    pcp bu

    rm -rf "$TMPDIR_LY"

    echo
    echo "LyBriCa + BLE bridge installed as lybrica.tcz. The temporary build"
    echo "tools disappear at the next reboot."
    echo "  /usr/local/bin/start_lybrica.sh   (launcher, from the extension)"
    echo "  /usr/local/bin/lybrica            (symlink, e.g. 'lybrica help')"
    echo "  /usr/local/bin/lybrica.py         (from the extension, read-only)"
    echo "  /usr/local/bin/hid_ble_bridge.py  (bridge, from the extension, read-only)"
    echo "  /home/tc/lybrica-mod.py           (optional, create with 'lybrica reset')"
    echo "ESP32C3 config.txt: $ESP32_STATUS"
    echo "Reboot to activate:  pcp reboot"
    echo "Logs: /home/tc/pcp_startup.log  /home/tc/lybrica.log"
    echo "Commands: lybrica help"
}

###########################################################################
# ESP32C3 helper (called from install_lybrica, formerly a standalone step)
#
# Edits config.txt (FAT partition, /mnt/mmcblk0p1) to add, under [all],
# the lines that are not already there:
#   dtoverlay=gpio-poweroff,gpiopin=26,active_low=1
#   dtoverlay=gpio-shutdown,gpio_pin=16,active_low=0,gpio_pull=down
#   enable_uart=1
#
# Return codes:
#   0              config.txt updated (reboot required)
#   SKIP_RC        user cancelled (config.txt unchanged)
#   UNCHANGED_RC   every line already present (no change, no reboot)
#   other          error (do not exit the parent step)
###########################################################################
configure_esp32c3() {
    echo
    echo '-----------------------------------------------------------------'
    echo ' ESP32C3 microcontroller support'
    echo '-----------------------------------------------------------------'
    echo ' This step edits config.txt (FAT partition, /mnt/mmcblk0p1).'
    echo ''
    echo ' It ADDS, under [all], the lines that are not already there:'
    echo '   dtoverlay=gpio-poweroff,gpiopin=26,active_low=1'
    echo '       (GPIO 26: "shutdown done" signal to the ESP32)'
    echo '   dtoverlay=gpio-shutdown,gpio_pin=16,active_low=0,gpio_pull=down'
    echo '       (GPIO 16: shutdown request from the ESP32)'
    echo '   enable_uart=1'
    echo '       (UART on GPIO 14/15, link with the ESP32)'
    echo ''
    echo ' Nothing is removed. Meant for a fresh install: on a bare Raspberry Pi'
    echo ' these GPIOs are free (GPIO 14/15 = UART, GPIO 16 and 26 = plain GPIOs).'
    echo ' If you use a HAT or another add-on board, check its pinout first.'
    echo ''
    echo ' Expected setup (ESP32C3 + relay):'
    echo '   1. A physical button is wired to the ESP32.'
    echo '   2. Pressing it while the Pi runs: the ESP32 requests a shutdown (GPIO 16).'
    echo '   3. The Pi shuts down and signals it (GPIO 26); the ESP32 then cuts'
    echo '      the power with a relay.'
    echo '   4. Pressing the button again: the ESP32 closes the relay and the Pi boots.'
    echo ''
    echo ' A backup is kept as config.txt.bak (created once, never overwritten).'
    echo ' A reboot is required to apply the changes.'
    echo '-----------------------------------------------------------------'
    echo
    printf "Apply the ESP32C3 configuration to config.txt? [y/N]: "
    read -r ESP_ANS
    case "$ESP_ANS" in
        y|Y) ;;
        *)
            say "Cancelled (config.txt unchanged)."
            return "$SKIP_RC" ;;
    esac

    ESP_SCRIPT=/tmp/fix_gpio_overlays.sh
    cat > "$ESP_SCRIPT" <<'GPIO_SCRIPT_EOF'
#!/bin/sh
# Add the gpio-poweroff (GPIO 26) and gpio-shutdown (GPIO 16) overlays and
# enable the UART, only for the lines that are not already in config.txt.

set -e

FAT_MNT=/mnt/mmcblk0p1
FAT_DEV=/dev/mmcblk0p1
CONFIG="$FAT_MNT/config.txt"

[ "$(id -u)" -eq 0 ] || { echo "ERROR: run as root (sudo $0)" >&2; exit 1; }

# Mount the FAT partition if not already mounted.
# MOUNTED_BY_US remembers it, so it is unmounted at the end only in that case.
MOUNTED_BY_US=0
if ! mount | grep -q " $FAT_MNT "; then
    mkdir -p "$FAT_MNT"
    mount "$FAT_DEV" "$FAT_MNT"
    MOUNTED_BY_US=1
    echo ">>> $FAT_DEV mounted on $FAT_MNT"
fi

[ -f "$CONFIG" ] || { echo "ERROR: $CONFIG not found" >&2; exit 1; }

# Backup of the original config.txt (created once, never overwritten, so the
# first untouched copy is always kept).
if [ ! -f "$CONFIG.bak" ]; then
    cp -p "$CONFIG" "$CONFIG.bak"
    echo ">>> Backup created: $CONFIG.bak"
else
    echo ">>> Backup already present: $CONFIG.bak (kept as is)"
fi

# Add a line only if it is not already in config.txt (exact line match).
# "[all]" is written once, before the first added line: without it, the
# lines would only apply to the conditional section (e.g. [pi4]) the file
# happens to end with.
ADDED=0
add_line() {
    if grep -qxF "$1" "$CONFIG"; then
        echo ">>> Already present: $1"
    else
        if [ "$ADDED" -eq 0 ]; then
            # Make sure the file ends with a newline before appending
            [ -z "$(tail -c1 "$CONFIG")" ] || echo >> "$CONFIG"
            echo "[all]" >> "$CONFIG"
            ADDED=1
        fi
        echo "$1" >> "$CONFIG"
        echo ">>> Added: $1"
    fi
}

add_line "dtoverlay=gpio-poweroff,gpiopin=26,active_low=1"
add_line "dtoverlay=gpio-shutdown,gpio_pin=16,active_low=0,gpio_pull=down"
add_line "enable_uart=1"

echo ">>> $CONFIG, last lines:"
tail -10 "$CONFIG"

if [ "$MOUNTED_BY_US" -eq 1 ]; then
    umount "$FAT_MNT"
    echo ">>> $FAT_MNT unmounted"
else
    echo ">>> $FAT_MNT was already mounted: left mounted"
fi

# Tell the caller whether anything was actually added.
# 20 matches UNCHANGED_RC in install_pcp.sh: "already up to date".
if [ "$ADDED" -eq 0 ]; then
    exit 20
fi
exit 0
GPIO_SCRIPT_EOF

    chmod 755 "$ESP_SCRIPT"
    sudo sh "$ESP_SCRIPT"
    ESP_RC=$?
    rm -f "$ESP_SCRIPT"
    case "$ESP_RC" in
        0)
            say "config.txt updated."
            echo ">>> Reboot required to apply:  pcp reboot"
            return 0 ;;
        "$UNCHANGED_RC")
            say "config.txt already up to date, no reboot needed."
            return "$UNCHANGED_RC" ;;
        *)
            echo "ERROR: config.txt update failed (see above)." >&2
            return 1 ;;
    esac
}

###########################################################################
# DISPATCH: one step = one process (sh "$SELF" --run <step>)
###########################################################################
if [ "$1" = "--run" ]; then
    case "$2" in
        camilla) install_camilladsp ;;
        lybrica) install_lybrica ;;
        *) fail "Unknown step: $2" ;;
    esac
    exit $?
fi

[ "$(id -u)" -eq 0 ] && fail "Run this script as user tc, not root."

DONE=""

run_step() {  # $1 = camilla|lybrica
    echo
    echo "############################################################"
    echo "# Step: $1"
    echo "############################################################"
    sh "$SELF" --run "$1"
    rc=$?
    case "$rc" in
        0)        DONE="$DONE $1:OK" ;;
        "$SKIP_RC") DONE="$DONE $1:SKIPPED" ;;
        *)        DONE="$DONE $1:FAILED"
                  echo "!!! Step '$1' failed (see messages above)." >&2 ;;
    esac
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
            2|lybrica) run_step lybrica ;;
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
    echo "  CamillaDSP installer for piCorePlayer "
    echo "=============================================="
    echo "  1) CamillaDSP + CamillaGUI"
    echo "  2) LyBriCa + ESP32C3 "
    echo "  R) Reboot now"
    echo "  Q) Quit"
    echo "----------------------------------------------"
    printf "Choice (several allowed, e.g. 1 2): "
    read -r CHOICES || break
    [ -z "$CHOICES" ] && continue
    # shellcheck disable=SC2086
    run_choice $CHOICES || break
done

summary
