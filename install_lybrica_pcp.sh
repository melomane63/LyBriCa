#!/bin/sh
# ---------------------------------------------------------------------------
# install_pcp.sh - All-in-one installer for piCorePlayer (menu)
#
#   1) LyBriCa + BLE HID bridge + ESP32C3   (chains the two steps below
#      in a single user flow)
#
# The two steps remain separate in the script (install_lybrica and
# install_esp32): they can be launched independently via --run.
#
# Usage (SSH, user tc):
#   sh install_pcp.sh      -> interactive menu (no parameters)
#
# Each step runs in its own process: an error in one step does not stop
# the menu, and nothing is saved (pcp bu) if that step fails.
# Reboot at the end to apply everything.
# ---------------------------------------------------------------------------

SELF="$0"
HOME_DIR=/home/tc
# NOTE: /opt/bootlocal.sh is NOT read, written or used by this installer.
# LyBriCa is started via USER_COMMAND_2 in pcp.cfg (pCP user command).

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

# --- LyBriCa + BLE HID bridge ------------------------------------------
PYCAMILLA_REF="next5"            # branch, tag or commit of pycamilladsp
PYCAMILLA_URL="git+https://github.com/HEnquist/pycamilladsp.git@${PYCAMILLA_REF}"

TM1637_URL="https://raw.githubusercontent.com/melomane63/tm1637_lgpio/main/tm1637_lgpio.py"
LYBRICA_URL="https://github.com/melomane63/LyBriCa/raw/refs/heads/main/lybrica.py"

# Patched BLE HID bridge files (raw URLs, no HTML)
PCP_BT_RAW_BASE="https://raw.githubusercontent.com/melomane63/pcp-ble-gatt/refs/heads/main"
PCP_BT_HID_BLE_BRIDGE_URL="${PCP_BT_RAW_BASE}/hid_ble_bridge.py"
PCP_BT_HIDBRIDGE_URL="${PCP_BT_RAW_BASE}/hidbridge"

# --- ESP32C3 -----------------------------------------------------------
# No remote URL: only local pCP files (config.txt, pcp.cfg) and
# shutdown-monitor.tcz from the pCP repository.

# --- pCP extension names (for reference) --------------------------------
EXT_LYBRICA="lybrica.tcz"
EXT_PCP_BT="pcp-bt.tcz"
EXT_SHUTDOWN_MONITOR="shutdown-monitor.tcz"

###########################################################################
# 1) LYBRICA + BLE HID BRIDGE (patched pcp-bt.tcz)
#
# Layout:
#   lybrica.tcz (read-only, rebuilt at each run)
#     /usr/local/lib/python3.11/site-packages/
#         camilladsp/ websocket/ serial/ lgpio/ alsaaudio/
#         tm1637_lgpio.py
#     /usr/local/bin/lybrica.py             (read-only, factory copy)
#     /usr/local/bin/start_lybrica.sh       (launcher, applies the override)
#     /usr/local/bin/lybrica                (symlink -> start_lybrica.sh)
#     /usr/local/tce.installed/lybrica      (empty: pCP user command launches it)
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

    # 7. Empty tce.installed (nothing to launch at boot: the pCP user
    #    command launches it)
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

    # 8b. BLE HID bridge: rebuild pcp-bt.tcz with the patched hid_ble_bridge.py
    #     and hidbridge (see patch_pcp_bt). A failure here only warns: LyBriCa
    #     itself is already installed and pcp bu must still run.
    say "Patching pcp-bt.tcz (BLE HID bridge)..."
    patch_pcp_bt || echo "!!! WARNING: pcp-bt.tcz was not patched (see messages above)."

    # 9. Register the launcher as a pCP user command.
    #    On a fresh pCP install, USER_COMMAND_2 exists and is empty:
    #    write the launcher there. bootlocal.sh is NOT touched.
    #    Spaces are encoded as '+' (same format as the pCP web UI).
    PCP_CFG=/usr/local/etc/pcp/pcp.cfg
    CMD_RAW="/usr/local/bin/start_lybrica.sh"
    CMD_ENC=$(echo "$CMD_RAW" | tr ' ' '+')

    say "Registering $CMD_RAW in USER_COMMAND_2..."
    awk -v v="$CMD_ENC" '
        /^USER_COMMAND_2=/ { print "USER_COMMAND_2=\"" v "\""; next }
        { print }
    ' "$PCP_CFG" > "$PCP_CFG.new" && mv "$PCP_CFG.new" "$PCP_CFG"
    say "pcp.cfg: USER_COMMAND_2=\"$CMD_RAW\""

    # 10. Check imports from the build dir (proves both tc and root see them)
    say "Checking imports from the build dir (tc)..."
    PYTHONPATH="$SP" python3 -c \
        "import camilladsp, websocket, serial, lgpio, alsaaudio, evdev, tm1637_lgpio, bleak" \
        || fail "A module cannot be imported from the build dir (tc)."
    say "Checking imports from the build dir (root)..."
    sudo PYTHONPATH="$SP" python3 -c \
        "import camilladsp, websocket, serial, lgpio, alsaaudio, evdev, tm1637_lgpio, bleak" \
        || fail "A module cannot be imported from the build dir (root)."

    # 11. Save
    say "Saving with pcp bu..."
    pcp bu

    rm -rf "$TMPDIR_LY"

    echo
    echo "LyBriCa installed as lybrica.tcz. The temporary build"
    echo "tools disappear at the next reboot."
    echo "  /usr/local/bin/start_lybrica.sh   (launcher, from the extension)"
    echo "  /usr/local/bin/lybrica            (symlink, e.g. 'lybrica help')"
    echo "  /usr/local/bin/lybrica.py         (from the extension, read-only)"
    echo "  pcp-bt.tcz                        (rebuilt with the patched hid_ble_bridge.py + hidbridge)"
    echo "  /home/tc/lybrica-mod.py           (optional, create with 'lybrica reset')"
    echo
    echo "NOTE: the launcher is registered as USER_COMMAND_2 in pcp.cfg:"
    echo "        $CMD_RAW"
    echo "      Check Tweaks > User commands in the pCP web interface."
    echo
    echo "Reboot to activate:  pcp reboot"
    echo "Logs: /home/tc/pcp_startup.log  /home/tc/lybrica.log"
    echo "Commands: lybrica help"
}

###########################################################################
# BLE HID bridge: patch pcp-bt.tcz
#
# pCP's own pcp-bt.tcz ships hid_ble_bridge.py and the "hidbridge" init
# script. This replaces both with the versions from
#   https://github.com/melomane63/pcp-ble-gatt
# by unpacking pcp-bt.tcz, swapping the two files, rebuilding the squashfs
# and putting it back. The result takes effect at the next reboot.
#
# - Idempotent: nothing is rebuilt if the extension already has these files.
# - Safe: both files are downloaded and checked BEFORE anything is touched.
# - Not fatal: returns non-zero with a message, the caller only warns.
#
# Uses OPT and TMPDIR_LY from install_lybrica; needs unsquashfs / mksquashfs
# (squashfs-tools.tcz, already loaded with the temporary build tools).
###########################################################################
patch_pcp_bt() {
    PB_TCZ="$OPT/pcp-bt.tcz"
    PB_TMP="$TMPDIR_LY/pcp-bt"
    PB_EXT="$PB_TMP/ext"

    if [ ! -f "$PB_TCZ" ]; then
        echo "!!! pcp-bt.tcz not found in $OPT."
        echo "    Install Bluetooth from the pCP web interface first, then re-run this step."
        return 1
    fi
    if ! command -v unsquashfs >/dev/null 2>&1; then
        echo "!!! unsquashfs not found (squashfs-tools.tcz not loaded?)"
        return 1
    fi

    mkdir -p "$PB_TMP"

    # 1. Download and check both files before touching anything
    wget -q -O "$PB_TMP/hid_ble_bridge.py" "$PCP_BT_HID_BLE_BRIDGE_URL" \
        || { echo "!!! Download failed: $PCP_BT_HID_BLE_BRIDGE_URL"; return 1; }
    wget -q -O "$PB_TMP/hidbridge" "$PCP_BT_HIDBRIDGE_URL" \
        || { echo "!!! Download failed: $PCP_BT_HIDBRIDGE_URL"; return 1; }
    for f in hid_ble_bridge.py hidbridge; do
        if head -c 300 "$PB_TMP/$f" | grep -qiE '<!doctype|<html'; then
            echo "!!! $f is a web page (use the raw URL)."
            return 1
        fi
    done
    python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$PB_TMP/hid_ble_bridge.py" \
        || { echo "!!! hid_ble_bridge.py is not valid Python"; return 1; }
    sh -n "$PB_TMP/hidbridge" \
        || { echo "!!! hidbridge has a syntax error"; return 1; }

    # 2. Unpack the current pcp-bt.tcz
    unsquashfs -f -d "$PB_EXT" "$PB_TCZ" > /dev/null \
        || { echo "!!! unsquashfs failed on $PB_TCZ"; return 1; }
    for d in usr/local/bin usr/local/etc/init.d; do
        [ -d "$PB_EXT/$d" ] || { echo "!!! $d missing in pcp-bt.tcz: unexpected layout"; return 1; }
    done

    # 3. Already patched? Then there is nothing to rebuild
    if cmp -s "$PB_TMP/hid_ble_bridge.py" "$PB_EXT/usr/local/bin/hid_ble_bridge.py" \
       && cmp -s "$PB_TMP/hidbridge" "$PB_EXT/usr/local/etc/init.d/hidbridge"; then
        echo ">>> pcp-bt.tcz already contains these two files: nothing to do."
        return 0
    fi

    # 4. Swap the two files
    cp "$PB_TMP/hid_ble_bridge.py" "$PB_EXT/usr/local/bin/hid_ble_bridge.py"
    cp "$PB_TMP/hidbridge"         "$PB_EXT/usr/local/etc/init.d/hidbridge"
    chmod 755 "$PB_EXT/usr/local/bin/hid_ble_bridge.py" "$PB_EXT/usr/local/etc/init.d/hidbridge"

    # 5. Rebuild and put it back
    mksquashfs "$PB_EXT" "$PB_TMP/pcp-bt.tcz" -noappend -all-root > /dev/null \
        || { echo "!!! mksquashfs failed"; return 1; }

    # The original md5 no longer matches the rebuilt file: keep it as .orig
    if [ -f "$OPT/pcp-bt.tcz.md5.txt" ]; then
        [ -f "$OPT/pcp-bt.tcz.md5.txt.orig" ] || cp "$OPT/pcp-bt.tcz.md5.txt" "$OPT/pcp-bt.tcz.md5.txt.orig"
        rm -f "$OPT/pcp-bt.tcz.md5.txt"
    fi
    mv -f "$PB_TMP/pcp-bt.tcz" "$PB_TCZ"
    echo ">>> pcp-bt.tcz rebuilt with the patched hid_ble_bridge.py and hidbridge (active after reboot)."
    return 0
}

###########################################################################
# ESP32C3 helper (called from install_esp32)
#
# Edits config.txt (FAT partition, /mnt/mmcblk0p1) to add, under [all],
# the lines that are not already there:
#   dtoverlay=gpio-poweroff,gpiopin=26,active_low=1
#   dtoverlay=gpio-shutdown,gpio_pin=16,active_low=0,gpio_pull=down
#   enable_uart=1
#   gpio=12=ip,np
# Then sets the matching GPIO options in pcp.cfg and installs
# shutdown-monitor.tcz.
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
    echo '   gpio=12=ip,np'
    echo '       (GPIO 12: TV relay input, left floating at boot so the relay'
    echo '        board is not activated before lybrica.py takes over)'
    echo ''
    echo ' Nothing is removed. Meant for a fresh install: on a bare Raspberry Pi'
    echo ' these GPIOs are free (GPIO 14/15 = UART, GPIO 12, 16 and 26 = plain GPIOs).'
    echo ' If you use a HAT or another add-on board, check its pinout first.'
    echo ' The GPIO 12 line is harmless if no TV relay is wired to it.'
    echo ''
    echo ' Expected setup (ESP32C3 + relay):'
    echo '   1. A physical button is wired to the ESP32.'
    echo '   2. Pressing it while the Pi runs: the ESP32 requests a shutdown (GPIO 16).'
    echo '   3. The Pi shuts down and signals it (GPIO 26); the ESP32 then cuts'
    echo '      the power with a relay.'
    echo '   4. Pressing the button again: the ESP32 closes the relay and the Pi boots.'
    echo ''
    echo ' It also sets the matching GPIO options in pcp.cfg (so the pCP web'
    echo ' interface does not overwrite them) and installs shutdown-monitor.tcz'
    echo ' (REQUIRED for GPIO 16 to actually shut the Pi down).'
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
# Add the gpio-poweroff (GPIO 26) and gpio-shutdown (GPIO 16) overlays,
# enable the UART and leave GPIO 12 floating at boot (TV relay input),
# only for the lines that are not already in config.txt.

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
add_line "gpio=12=ip,np"

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

    if [ "$ESP_RC" -eq 0 ] || [ "$ESP_RC" -eq "$UNCHANGED_RC" ]; then
        PCP_CFG=/usr/local/etc/pcp/pcp.cfg
        set_pcp_cfg() {
            if grep -q "^$1=" "$PCP_CFG"; then
                sed "s|^$1=.*|$1=\"$2\"|" -i "$PCP_CFG"
                echo ">>> pcp.cfg: $1=\"$2\""
            else
                echo "$1=\"$2\"" >> "$PCP_CFG"
                echo ">>> pcp.cfg: $1=\"$2\" (added)"
            fi
        }
        set_pcp_cfg GPIOPOWEROFF      "yes"
        set_pcp_cfg GPIOPOWEROFF_GPIO "26"
        set_pcp_cfg GPIOPOWEROFF_HI   "no"
        set_pcp_cfg GPIOSHUTDOWN      "yes"
        set_pcp_cfg GPIOSHUTDOWN_GPIO "16"
        set_pcp_cfg GPIOSHUTDOWN_HI   "yes"
        set_pcp_cfg GPIOSHUTDOWN_PU   "down"

        say "Installing the shutdown monitor..."
        pcp-load -wil shutdown-monitor.tcz \
            || echo "!!! WARNING: shutdown-monitor.tcz install failed: GPIO 16 will not shut the Pi down."
    fi

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
# 2) ESP32C3 (standalone step)
#
# Runs configure_esp32c3, then saves with pcp bu. "Already up to date" is
# not an error here: pcp.cfg may still have been corrected, so we save.
###########################################################################
install_esp32() {
    configure_esp32c3
    rc=$?
    rm -f /home/tc/pcp-powerbutton.sh
    case "$rc" in
        0|"$UNCHANGED_RC")
            say "Saving with pcp bu..."
            pcp bu
            return 0 ;;
        "$SKIP_RC")
            return "$SKIP_RC" ;;
        *)
            return 1 ;;
    esac
}

###########################################################################
# 1) ALL-IN-ONE : LyBriCa then ESP32C3 in a single user flow
#
# This function chains the two steps in order. Both steps remain
# separately callable via --run lybrica / --run esp32.
#
# Chaining rules:
#   - If LyBriCa fails (FAILED), ESP32C3 is NOT chained: nothing is
#     saved (pcp bu) and the user must fix it first.
#   - If the user cancels the ESP32C3 step (SKIPPED), the whole run is
#     reported as SKIPPED (LyBriCa itself was installed and saved).
#   - If config.txt is already up to date (UNCHANGED_RC), that is a
#     success: install_esp32 still runs pcp bu to persist pcp.cfg.
###########################################################################
install_all() {
    echo
    echo "============================================================="
    echo "  WARNING: uses GPIO 12/14/15/16/26."
    echo "  Check that no HAT / add-on board (HiFiBerry, etc.)"
    echo "  already occupies these pins before continuing."
    echo "============================================================="
    echo
    printf "Continue with the full installation? [y/N]: "
    read -r ALL_ANS
    case "$ALL_ANS" in
        y|Y) ;;
        *) say "Cancelled (nothing was modified)."; return "$SKIP_RC" ;;
    esac

    # --- Step 1: LyBriCa ---
    echo
    echo "############################################################"
    echo "# Sub-step: lybrica"
    echo "############################################################"
    sh "$SELF" --run lybrica
    rc_ly=$?
    case "$rc_ly" in
        0)        say "LyBriCa: OK" ;;
        "$SKIP_RC") say "LyBriCa: SKIPPED (nothing was saved)"
                    return "$SKIP_RC" ;;
        *)        echo "!!! LyBriCa failed: ESP32C3 is not chained." >&2
                  return 1 ;;
    esac

    # --- Step 2: ESP32C3 ---
    echo
    echo "############################################################"
    echo "# Sub-step: esp32"
    echo "############################################################"
    sh "$SELF" --run esp32
    rc_esp=$?
    case "$rc_esp" in
        0)             say "ESP32C3: OK" ;;
        "$SKIP_RC")    say "ESP32C3: SKIPPED (LyBriCa remains installed)"
                       return "$SKIP_RC" ;;
        *)             echo "!!! ESP32C3 failed (LyBriCa remains installed)." >&2
                       return 1 ;;
    esac

    return 0
}

###########################################################################
# DISPATCH: one step = one process (sh "$SELF" --run <step>)
###########################################################################
if [ "$1" = "--run" ]; then
    case "$2" in
        all)     install_all ;;
        lybrica) install_lybrica ;;
        esp32)   install_esp32 ;;
        *) fail "Unknown step: $2" ;;
    esac
    exit $?
fi

[ "$(id -u)" -eq 0 ] && fail "Run this script as user tc, not root."

DONE=""

run_step() {  # $1 = all|lybrica|esp32
    # For "all", no generic header: install_all prints its GPIO warning
    # directly, then each sub-step prints its own header.
    if [ "$1" != "all" ]; then
        echo
        echo "############################################################"
        echo "# Step: $1"
        echo "############################################################"
    fi
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
            1|all)     run_step all ;;
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
    echo "  Lybrica installer for piCorePlayer"
    echo "=============================================="
    echo "  1) LyBriCa "
    echo "  R) Reboot now"
    echo "  Q) Quit"
    echo "----------------------------------------------"
    printf "Choice (several allowed, e.g. 1): "
    read -r CHOICES || break
    [ -z "$CHOICES" ] && continue
    # shellcheck disable=SC2086
    run_choice $CHOICES || break
done

summary
