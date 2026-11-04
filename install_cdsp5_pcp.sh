#!/bin/sh
# ---------------------------------------------------------------------------
# install_cdsp5_pcp.sh - CamillaDSP 5.x + CamillaGUI installer for piCorePlayer
#
# Fork of piCoreCDSP by JWahle (https://github.com/JWahle/piCoreCDSP)
# License: MIT
#
#   1) CamillaDSP 5.x + CamillaGUI  (Squeezelite -> loopback -> CamillaDSP -> DAC)
#                                   installed as the CDSPpiCore.tcz extension
#
# Usage (SSH, user tc):
#   sh install_cdsp5_pcp.sh      -> interactive menu (no parameters)
#
# The step runs in its own process: an error does not stop the menu,
# and nothing is saved (pcp bu) if that step fails.
# ---------------------------------------------------------------------------

SELF="$0"
HOME_DIR=/home/tc
# NOTE: /opt/bootlocal.sh is NOT read, written or used by this installer.

# Exit code a step returns when the user cancels it (shown as SKIPPED).
SKIP_RC=10

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

# --- pCP extension names (for reference) --------------------------------
EXT_CDSP="CDSPpiCore.tcz"

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
    # DAC already set in an existing _A-loopback.yml (script re-run)
    EXISTING_CFG=/mnt/mmcblk0p2/tce/camilladsp/configs/_A-loopback.yml
    EXISTING_DAC=""
    if [ -r "$EXISTING_CFG" ]; then
        EXISTING_DAC=$(awk '/^  playback:/{f=1;next} f && /^    device:/{gsub(/^    device:[ ]*"?/,""); gsub(/"?[ ]*$/,""); print; exit}' "$EXISTING_CFG")
        case "$EXISTING_DAC" in *Loopback*) EXISTING_DAC="" ;; esac
    fi
    case "$PCP_OUT" in
        *Loopback*|"")
            if [ -n "$EXISTING_DAC" ]; then
                DAC_DEVICE="$EXISTING_DAC"
                echo ">>> pcp.cfg OUTPUT is empty or already the loopback: DAC taken from _A-loopback.yml: $DAC_DEVICE"
            else
                echo ">>> pcp.cfg OUTPUT is empty or already the loopback: keeping DAC_DEVICE=$DAC_DEVICE"
            fi ;;
        hw:*)
            DAC_DEVICE="$PCP_OUT"
            echo ">>> DAC taken from pcp.cfg: $DAC_DEVICE" ;;
        *)
            if [ -n "$EXISTING_DAC" ]; then
                DAC_DEVICE="$EXISTING_DAC"
                echo "!!! WARNING: pcp.cfg OUTPUT ('$PCP_OUT') is not a hw: device: DAC taken from _A-loopback.yml: $DAC_DEVICE"
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
cat > "$BASE/configs/_A-loopback.yml" <<EOF
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
cp "$BASE/configs/_A-loopback.yml" "$BASE/default_config.yml"

if "$CDSP_BIN" -c "$BASE/configs/_A-loopback.yml"; then
    echo ">>> Config _A-loopback.yml: valid."
else
    echo "!!! WARNING: CamillaDSP considers _A-loopback.yml invalid (see message above)."
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
config_path: $BASE/configs/_A-loopback.yml
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

# Migration: remove the old loose files (bootlocal.sh is NOT touched:
# CamillaDSP is now started by the extension's tce.installed script).
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
# DISPATCH: one step = one process (sh "$SELF" --run <step>)
###########################################################################
if [ "$1" = "--run" ]; then
    case "$2" in
        camilla) install_camilladsp ;;
        *) fail "Unknown step: $2" ;;
    esac
    exit $?
fi

[ "$(id -u)" -eq 0 ] && fail "Run this script as user tc, not root."

DONE=""

run_step() {  # $1 = camilla
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
