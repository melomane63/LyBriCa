#!/bin/sh
# ---------------------------------------------------------------------------
# install_pcp.sh - All-in-one installer for piCorePlayer (menu)
#
#   1) CamillaDSP 5.x + CamillaGUI  (Squeezelite -> loopback -> CamillaDSP -> DAC)
#   2) Bridge BLE HID               (Bluetooth remote)
#   3) LyBriCa                      (modules Python, daemon)
#   4) ESP32C3                      (UART + GPIO overlays)
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

# Exit code a step returns when the user cancels it (shown as SKIPPED).
# Chosen so it cannot be confused with the exit codes of wget, tar, etc.
SKIP_RC=10

# Exit code the ESP32C3 subscript returns when config.txt already contains
# every line it wants to add (no change, no reboot needed).
UNCHANGED_RC=20

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
    echo "  - Pair the remote from the pCP web interface after this step."
    echo "-----------------------------------------------------------------"
    echo

    BRIDGE_URL="https://github.com/melomane63/LyBriCa/raw/refs/heads/main/hid_ble_bridge.py"

    # 1. Dependency: only bleak is needed by the patched bridge.
    say "Installing python3.11-bleak..."
    tce-load -wi python3.11-bleak.tcz || fail "tce-load failed."

    # 2. Download the patched bridge script to /home/tc/ (persistent).
    say "Downloading hid_ble_bridge.py..."
    wget -q -O "$HOME_DIR/hid_ble_bridge.py" "$BRIDGE_URL" \
        || fail "Download failed: $BRIDGE_URL"
    head -1 "$HOME_DIR/hid_ble_bridge.py" | grep -q '^#!' \
        || fail "$HOME_DIR/hid_ble_bridge.py is not a script (wrong URL? use the raw link)."
    chmod 755 "$HOME_DIR/hid_ble_bridge.py"

    # 3, 4, 5, 7: removed (scan / pairing / start_bridge.sh / init-script patch)

    # 6. Insert the symlink commands at the START of /opt/bootlocal.sh
    #    (right after the shebang, before #pCPstart).
    #    /usr/local/ is a tmpfs on piCorePlayer and .tcz content is mounted
    #    there at each boot, so the symlink must be recreated at every boot
    #    to redirect the stock script to our persistent copy in /home/tc/.
    #    Note: section 1 may have already inserted a line after the shebang
    #    (camilladsp-start); the block here is inserted just after the
    #    shebang, BEFORE that line. Order between the two blocks does not
    #    matter: hidbridge starts later, inside the #pCPstart block.
    SYMLINK_MARK="# hid_ble_bridge.py symlink (/usr/local -> /home/tc), do not remove"
    if grep -qF "$SYMLINK_MARK" "$BOOTLOCAL"; then
        say "$BOOTLOCAL already contains the symlink block."
    else
        say "Adding symlink block at the start of $BOOTLOCAL..."
        TMP=$(mktemp)
        BLOCK=$(mktemp)
        {
            echo "$SYMLINK_MARK"
            echo "sudo rm /usr/local/bin/hid_ble_bridge.py"
            echo "sudo ln -s /home/tc/hid_ble_bridge.py /usr/local/bin/hid_ble_bridge.py"
            echo ""
        } > "$BLOCK"

        if head -1 "$BOOTLOCAL" | grep -q '^#!'; then
            # Preserve the shebang as line 1, insert the block just after
            head -1 "$BOOTLOCAL" > "$TMP"
            cat "$BLOCK" >> "$TMP"
            tail -n +2 "$BOOTLOCAL" >> "$TMP"
        else
            cat "$BLOCK" > "$TMP"
            cat "$BOOTLOCAL" >> "$TMP"
        fi

        rm -f "$BLOCK"
        sudo sh -c "cat $TMP > $BOOTLOCAL" || fail "Cannot write $BOOTLOCAL."
        rm -f "$TMP"

        grep -q "opt/bootlocal.sh" /opt/.filetool.lst \
            || echo "opt/bootlocal.sh" >> /opt/.filetool.lst
    fi

    # 7. Save
    say "Saving with pcp bu..."
    pcp bu

    echo
    echo "Bridge installed."
    echo "Reboot to activate the symlink:  pcp reboot"
    echo "After reboot:"
    echo "  /usr/local/bin/hid_ble_bridge.py -> /home/tc/hid_ble_bridge.py"
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
            echo ">>> Reboot required to apply:  pcp reboot" ;;
        "$UNCHANGED_RC")
            say "config.txt already up to date, no reboot needed." ;;
        *)
            fail "config.txt update failed (see above)." ;;
    esac
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
            2|bridge)  run_step bridge ;;
            3|lybrica) run_step lybrica ;;
            4|esp32c3) run_step esp32c3 ;;
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
