#!/bin/sh
# Installer is adapted from https://github.com/JWahle/piCoreCDSP
# Copyright (c) 2023 Johannes Wahle
# MIT License
# ---------------------------------------------------------------------------
# install_lybrica_full_pcp.sh - Full installer for piCorePlayer:
#                               CamillaDSP 5 + LyBriCa (+ optional ESP32C3)
#
# Three independent phases, chosen from a menu (or all of them, in order):
#
#   1) CamillaDSP 5.x + CamillaGUI   Squeezelite -> loopback -> CamillaDSP -> DAC,
#                                    installed as the CDSPpiCore.tcz extension.
#                                    Writes the configs _A-loopback.yml (streamer,
#                                    group A) and _B-External.yml (external input,
#                                    group B).
#   2) LyBriCa                       Remote / display / automatic source switching /
#                                    LMS volume sync daemon, patched hid_ble_bridge.py,
#                                    Material Skin web files (custom.js, tone circle
#                                    page) and a TEMPORARY Material Skin patch.
#   3) ESP32C3 power button          config.txt GPIO lines, pcp.cfg GPIO options and
#                                    shutdown-monitor.tcz (optional hardware).
#
# Phase 1 is a fork of piCoreCDSP by JWahle (https://github.com/JWahle/piCoreCDSP)
# License: MIT
#
# Usage (SSH, user tc):
#   sh install_lybrica_full_pcp.sh      -> interactive menu (no parameters)
#
# Each phase runs in its own process: an error in one phase does not stop the
# menu, and nothing is saved (pcp bu) if that phase fails. The phases can also
# be launched directly with:  sh install_lybrica_full_pcp.sh --run cdsp|lybrica|esp32
# Reboot at the end to apply everything.
# ---------------------------------------------------------------------------

SELF="$0"
HOME_DIR=/home/tc
# NOTE: /opt/bootlocal.sh is NOT read, written or used by this installer.
# CamillaDSP is started by its extension (tce.installed), LyBriCa via
# USER_COMMAND_2 in pcp.cfg (pCP user command).

# Exit code a phase returns when the user cancels it (shown as SKIPPED).
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

# --- Phase 1: CamillaDSP 5.x + CamillaGUI --------------------------------
CDSP_VER="5.0.0-beta.2"
GUI_VER="5.0.0-beta.2"

CDSP_URL_BASE="https://github.com/HEnquist/camilladsp/releases/download/v${CDSP_VER}"
CDSP_URL_ARCH="camilladsp-linux-%s.tar.gz"

GUI_URL_BASE="https://github.com/HEnquist/camillagui-backend/releases/download/v${GUI_VER}"
GUI_URL_ARCH="camillagui_linux_%s.tar.gz"

# Squeezelite settings written to pcp.cfg (shown in the warning of phase 1)
SQ_RATES="48000-48000"
SQ_UPSAMPLE="hLE"
SQ_ALSA_PARAMS="::32:"
SQ_CLOSEOUT="3"

# --- Phase 2: LyBriCa + BLE HID bridge + web files -----------------------
PYCAMILLA_REF="next5"            # branch, tag or commit of pycamilladsp
PYCAMILLA_URL="git+https://github.com/HEnquist/pycamilladsp.git@${PYCAMILLA_REF}"

TM1637_URL="https://raw.githubusercontent.com/melomane63/tm1637_lgpio/main/tm1637_lgpio.py"
LYBRICA_URL="https://github.com/melomane63/LyBriCa/raw/refs/heads/main/lybrica.py"

# Material Skin button and tone circle page (raw URLs, no HTML)
CUSTOM_JS_URL="https://raw.githubusercontent.com/melomane63/LyBriCa/refs/heads/main/custom.js"
TONE_CIRCLE_HTML_URL="https://raw.githubusercontent.com/melomane63/LyBriCa/refs/heads/main/tone_circle.html"

# Patched hid_ble_bridge.py (raw URL, no HTML), hosted in the LyBriCa repo.
# The "hidbridge" init script of pcp-bt.tcz is left untouched.
PCP_BT_HID_BLE_BRIDGE_URL="https://raw.githubusercontent.com/melomane63/LyBriCa/refs/heads/main/hid_ble_bridge.py"

# --- Phase 3: ESP32C3 ------------------------------------------------------
# No remote URL: only local pCP files (config.txt, pcp.cfg) and
# shutdown-monitor.tcz from the pCP repository.

# --- pCP extension names (for reference) --------------------------------
EXT_CDSP="CDSPpiCore.tcz"
EXT_LYBRICA="lybrica.tcz"
EXT_PCP_BT="pcp-bt.tcz"
EXT_SHUTDOWN_MONITOR="shutdown-monitor.tcz"

###########################################################################
# PHASE 1 - CAMILLADSP 5.x + CAMILLAGUI
#
# Squeezelite -> loopback -> CamillaDSP -> DAC, as the CDSPpiCore.tcz extension.
#
# Configs written in /mnt/mmcblk0p2/tce/camilladsp/configs/:
#   _A-loopback.yml  group A: captures the loopback (streamer: LMS, Bluetooth)
#   _B-External.yml  group B: captures the external input (TOSLINK / S/PDIF /
#                    line-in: any ALSA capture card). Created only if a capture
#                    card is found and no other _B-*.yml exists yet.
# Both use the same DAC and the same filters (L_Bass, L_Treble, L_Compensation,
# L_Loudness), which LyBriCa drives. An existing config that differs is kept
# once as <file>.bak.
###########################################################################
install_camilladsp() {

echo
echo "This phase installs CamillaDSP + CamillaGUI and WRITES:"
echo "  - CamillaDSP configs: _A-loopback.yml (streamer) and _B-External.yml"
echo "    (external input, only if a capture card is found), camilladsp_statefile.yml"
echo "    (volume reset to -40 dB) and camillagui.yml (web port 5005)"
echo "  - the $EXT_CDSP extension (+ .dep) and its line in onboot.lst"
echo "  - pcp.cfg: squeezelite settings ALSA_PARAMS=\"$SQ_ALSA_PARAMS\", MAX_RATE=\"$SQ_RATES\","
echo "    UPSAMPLE=\"$SQ_UPSAMPLE\", CLOSEOUT=\"$SQ_CLOSEOUT\" (your current values are overwritten),"
echo "    USER_COMMAND_1 (overwritten), and OUTPUT only if it is a physical hw: device"
echo "  - /mnt/mmcblk0p2/tce/pcp_patch.sh and /etc/asound.conf"
echo "    (rate_converter in pCP_Mixer, subdevice 0 in dmixer)"
printf "Install CamillaDSP + CamillaGUI? [y/N]: "
read -r CDSP_ANS || CDSP_ANS=n
case "$CDSP_ANS" in
    y|Y) ;;
    *)   say "Cancelled (nothing was modified)."; return "$SKIP_RC" ;;
esac

set -e

### ======================= CONFIG ==========================================
SAMPLERATE=48000
CAPTURE_FORMAT="S32_LE"          # loopback capture (group A), matches Squeezelite
PLAYBACK_FORMAT="S16_LE"
EXT_CAPTURE_FORMAT="S32_LE"      # external input (group B): default, checked against the card if possible
### =========================================================================

normalize_fmt() {
    echo "$1" | sed -E 's/^([SF][0-9]+)(_[0-9]+)?LE$/\1\2_LE/'
}
CAPTURE_FORMAT=$(normalize_fmt "$CAPTURE_FORMAT")
PLAYBACK_FORMAT=$(normalize_fmt "$PLAYBACK_FORMAT")

PCP_CFG=/usr/local/etc/pcp/pcp.cfg

### ======================= DAC RESOLUTION ===================================
DAC_DEVICE="null"

PCP_OUT=""
if [ -r "$PCP_CFG" ]; then
    PCP_OUT=$(sed -n 's/^OUTPUT="\(.*\)"/\1/p' "$PCP_CFG" | head -n 1)
    PCP_OUT=$(echo "$PCP_OUT" | sed -E 's/^plughw:/hw:/; s/^hw:CARD=([^,]*),DEV=([0-9]+)$/hw:\1,\2/; s/^hw:CARD=([^,]*)$/hw:\1,0/')
fi

# First existing config named _A-*.yml (alphabetical order)
EXISTING_CFG=$(ls /mnt/mmcblk0p2/tce/camilladsp/configs/_A-*.yml 2>/dev/null | head -n 1)
EXISTING_DAC=""
if [ -r "$EXISTING_CFG" ]; then
    EXISTING_DAC=$(awk '/^  playback:/{f=1;next} f && /^    device:/{gsub(/^    device:[ ]*"?/,""); gsub(/"?[ ]*$/,""); print; exit}' "$EXISTING_CFG")
    case "$EXISTING_DAC" in *Loopback*) EXISTING_DAC="" ;; esac
fi

# Case 1: OUTPUT in pcp.cfg is a real physical DAC
case "$PCP_OUT" in
    hw:*)
        case "$PCP_OUT" in
            *Loopback*) ;;   # virtual -> look in the first _A-*.yml
            *)
                DAC_DEVICE="$PCP_OUT"
                echo ">>> DAC from pcp.cfg: $DAC_DEVICE"
                ;;
        esac
        ;;
esac

# Case 2: OUTPUT is virtual (loopback, dmix, empty) -> read the first _A-*.yml
if [ "$DAC_DEVICE" = "null" ]; then
    if [ -n "$EXISTING_DAC" ]; then
        DAC_DEVICE="$EXISTING_DAC"
        echo ">>> DAC from $EXISTING_CFG: $DAC_DEVICE"
    else
        echo "!!! WARNING: no physical DAC found (neither in pcp.cfg nor in an _A-*.yml config)."
        echo "!!! DAC_DEVICE stays null."
    fi
fi
### =========================================================================

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

install_temporarily_if_missing() {
    if ! tce-status -i | grep -q "$1" ; then
        pcp-load -wil -t /tmp "$1"
    fi
}

# Write one CamillaDSP config. Same DAC and same filters for every group; only
# the capture side and the title differ. An existing file that differs is kept
# once as <file>.bak.
#   $1 = file   $2 = title   $3 = capture device   $4 = capture format
write_cdsp_config() {
cat > "$1.new" <<EOF
---
devices:
  samplerate: ${SAMPLERATE}
  chunksize: 1024
  queuelimit: 4
  capture:
    type: Alsa
    channels: 2
    device: "$3"
    format: $4
  playback:
    type: Alsa
    channels: 2
    device: "${DAC_DEVICE}"
    format: ${PLAYBACK_FORMAT}
filters:
  L_Bass:
    description: null
    parameters:
      freq: 110.0
      gain: 2.0
      q: 0.7
      type: Lowshelf
    type: Biquad
  L_Compensation:
    description: null
    parameters:
      gain: -1.0
      inverted: false
      mute: false
      scale: dB
    type: Gain
  L_Loudness:
    description: null
    parameters:
      attenuate_mid: false
      fader: Main
      high_boost: 5.0
      high_freq: null
      high_q: null
      low_boost: 10.0
      low_freq: null
      low_q: null
      reference_level: -30.0
    type: Loudness
  L_Treble:
    description: null
    parameters:
      freq: 3500.0
      gain: 2.0
      q: 0.7
      type: Highshelf
    type: Biquad
mixers: null
pipeline:
- bypassed: false
  channels: null
  description: null
  names:
  - L_Bass
  - L_Compensation
  - L_Treble
  type: Filter
- bypassed: false
  channels: null
  description: null
  names:
  - L_Loudness
  type: Filter
title: $2
EOF
if [ -f "$1" ] && ! cmp -s "$1.new" "$1" && [ ! -f "$1.bak" ]; then
    cp -p "$1" "$1.bak"
    echo ">>> Existing $1 kept once as $1.bak"
fi
mv -f "$1.new" "$1"
}

### ======================= EXTERNAL INPUT (group B) =========================
# _B-External.yml captures the external input (TOSLINK, S/PDIF, line-in: any
# ALSA capture card) and sends it through the same filters to the DAC. LyBriCa
# switches between group A (streamer) and group B (this input) from the signal
# it detects. Asked here, before the verbose part of the installation.
EXT_DEVICE=""
EXT_CFG="$BASE/configs/_B-External.yml"
EXISTING_B=$(ls "$BASE"/configs/_B-*.yml 2>/dev/null | head -n 1)

if [ -n "$EXISTING_B" ]; then
    echo ">>> A group B config already exists ($EXISTING_B): _B-External.yml not created."
else
    # Capture cards other than the loopback, formatted hw:<card>,<device>
    EXT_CANDIDATES=$(arecord -l 2>/dev/null \
        | sed -n 's/^card [0-9]*: \([^ ]*\) \[.*\], device \([0-9]*\):.*/hw:\1,\2/p' \
        | grep -vi loopback || true)
    EXT_COUNT=$(printf '%s\n' "$EXT_CANDIDATES" | grep -c . || true)

    if [ "$EXT_COUNT" -eq 0 ]; then
        echo "!!! No capture card found besides the loopback: _B-External.yml will NOT be created."
        echo "    Add your external input later: copy _A-loopback.yml to _B-<name>.yml and set"
        echo "    devices.capture.device (CamillaGUI, port 5005, or any editor)."
    elif [ "$EXT_COUNT" -eq 1 ]; then
        echo
        printf "External input (group B): use %s as capture device? [Y/n]: " "$EXT_CANDIDATES"
        read -r EXT_ANS || EXT_ANS=n
        case "$EXT_ANS" in
            n|N) echo ">>> _B-External.yml will NOT be created." ;;
            *)   EXT_DEVICE="$EXT_CANDIDATES" ;;
        esac
    else
        echo
        echo "Capture cards found (external input, group B):"
        i=1
        for c in $EXT_CANDIDATES; do
            echo "  $i) $c"
            i=$((i + 1))
        done
        echo "  0) none: do not create _B-External.yml"
        printf "Choice [0-%s]: " "$EXT_COUNT"
        read -r EXT_NUM || EXT_NUM=0
        case "$EXT_NUM" in
            ""|*[!0-9]*) EXT_NUM=0 ;;
        esac
        if [ "$EXT_NUM" -ge 1 ] && [ "$EXT_NUM" -le "$EXT_COUNT" ]; then
            EXT_DEVICE=$(printf '%s\n' "$EXT_CANDIDATES" | sed -n "${EXT_NUM}p")
        else
            echo ">>> _B-External.yml will NOT be created."
        fi
    fi
fi

if [ -n "$EXT_DEVICE" ]; then
    # Capture format: the first one the card advertises (stream0 exists for USB
    # cards only), otherwise the default above, unchecked.
    EXT_CARD=${EXT_DEVICE#hw:}
    EXT_CARD=${EXT_CARD%%,*}
    if [ -r "/proc/asound/$EXT_CARD/stream0" ]; then
        CAP_SECTION=$(sed -n '/^Capture:/,$p' "/proc/asound/$EXT_CARD/stream0")
        EXT_FMT_FOUND=""
        for f in S32_LE S24_3LE S24_LE S16_LE; do
            if echo "$CAP_SECTION" | grep -q "Format: $f"; then
                case "$f" in
                    S24_3LE) EXT_CAPTURE_FORMAT="S24_3_LE" ;;
                    S24_LE)  EXT_CAPTURE_FORMAT="S24_4_LE" ;;
                    *)       EXT_CAPTURE_FORMAT="$f" ;;
                esac
                EXT_FMT_FOUND="yes"
                break
            fi
        done
        if [ -n "$EXT_FMT_FOUND" ]; then
            echo ">>> External input $EXT_DEVICE: capture format $EXT_CAPTURE_FORMAT."
        else
            echo "!!! WARNING: no known capture format advertised by '$EXT_CARD': using $EXT_CAPTURE_FORMAT, unchecked."
        fi
    else
        echo ">>> No stream0 for '$EXT_CARD' (non-USB card?): capture format not verified, using $EXT_CAPTURE_FORMAT."
    fi
fi
### =========================================================================

set -v

### --- 1. Directories and CamillaDSP binary --------------------------------
EXT="$TMPDIR_CDSP/ext"
mkdir -p "$EXT/usr/local/bin" "$EXT/usr/local/camillagui" "$EXT/usr/local/tce.installed"
CDSP_BIN="$EXT/usr/local/bin/camilladsp"

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

### --- 3. CamillaDSP configs and statefile ---------------------------------
# Group A: the streamer (Squeezelite -> loopback)
write_cdsp_config "$BASE/configs/_A-loopback.yml" "Setup" "hw:Loopback,1" "$CAPTURE_FORMAT"

if "$CDSP_BIN" -c "$BASE/configs/_A-loopback.yml"; then
    echo ">>> Config _A-loopback.yml: valid."
else
    echo "!!! WARNING: CamillaDSP considers _A-loopback.yml invalid (see message above)."
fi

# Group B: the external input (only if a capture card was chosen above)
if [ -n "$EXT_DEVICE" ]; then
    write_cdsp_config "$EXT_CFG" "External" "$EXT_DEVICE" "$EXT_CAPTURE_FORMAT"
    if "$CDSP_BIN" -c "$EXT_CFG"; then
        echo ">>> Config _B-External.yml ($EXT_DEVICE): valid."
    else
        echo "!!! WARNING: CamillaDSP considers _B-External.yml invalid (see message above)."
    fi
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

GUI_CFG_FOUND=$(find "$GUI_TMP" -type f -name gui-config.yml | head -n 1)
if [ -n "$GUI_CFG_FOUND" ] && [ ! -f "$BASE/camillagui/config/gui-config.yml" ]; then
    cp "$GUI_CFG_FOUND" "$BASE/camillagui/config/gui-config.yml"
fi

( cd "$BASE/camillagui" && "$GUI_BIN" --help > /dev/null ) \
    || die "The GUI executable does not run on this system."

cat > "$BASE/camillagui/config/camillagui.yml" <<EOF
---
camilla_host: "127.0.0.1"
camilla_port: 1234
bind_address: "0.0.0.0"
port: 5005
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

ln -s "$BASE/camillagui/config" "$EXT/usr/local/camillagui/config"

### --- 5. Startup script (run as root when the extension is loaded) --------
cat > "$EXT/usr/local/tce.installed/CDSPpiCore" <<'EOF'
#!/bin/sh
BASE=/mnt/mmcblk0p2/tce/camilladsp
modprobe snd-aloop
/usr/local/bin/camilladsp \
    -w -p 1234 -a 0.0.0.0 \
    -o /tmp/camilladsp.log \
    -s "$BASE/camilladsp_statefile.yml" &
sudo -u tc sh -c "cd '$BASE/camillagui' && /usr/local/camillagui/camillagui" &
exit 0
EOF
chmod 775 "$EXT/usr/local/tce.installed/CDSPpiCore"

### --- 6. Build and register the extension ---------------------------------
install_temporarily_if_missing squashfs-tools
TCZ="$TMPDIR_CDSP/$EXT_CDSP"
mksquashfs "$EXT" "$TCZ" -noappend -all-root
mv -f "$TCZ" "$OPT/$EXT_CDSP"

ALSA_EXT="alsa-modules-$(uname -r).tcz"
if [ -f "$OPT/$ALSA_EXT" ]; then
    echo "$ALSA_EXT" > "$OPT/$EXT_CDSP.dep"
else
    echo "!!! WARNING: $OPT/$ALSA_EXT not found: no .dep written, check that snd-aloop is loaded early enough."
fi

grep -qx "$EXT_CDSP" /etc/sysconfig/tcedir/onboot.lst \
    || echo "$EXT_CDSP" >> /etc/sysconfig/tcedir/onboot.lst

### --- 7. Squeezelite -> loopback ------------------------------------------
set_pcp_cfg() {
    if grep -q "^$1=" "$PCP_CFG"; then
        sed "s|^$1=.*|$1=\"$2\"|" -i "$PCP_CFG"
        echo ">>> pcp.cfg: $1=\"$2\""
    else
        echo "!!! WARNING: key $1 not found in pcp.cfg, not set."
    fi
}

# Only switch OUTPUT to the loopback if it is not already a virtual device
case "$PCP_OUT" in
    *Loopback*|dmix*|"")
        echo ">>> OUTPUT is already virtual ('$PCP_OUT'), leaving it unchanged."
        ;;
    hw:*)
        set_pcp_cfg OUTPUT "hw:Loopback,0"
        ;;
esac

set_pcp_cfg ALSA_PARAMS "$SQ_ALSA_PARAMS"
set_pcp_cfg MAX_RATE    "$SQ_RATES"
set_pcp_cfg UPSAMPLE    "$SQ_UPSAMPLE"
set_pcp_cfg CLOSEOUT    "$SQ_CLOSEOUT"

### --- 8. Cleanup (pcp backup runs only once, at the end of step 10) ------
set +e
rm -rf "$TMPDIR_CDSP"

### --- 9. pcp_patch.sh (dmix.cgi patches + DAC mixer restore) --------------
# Extract the card name from DAC_DEVICE
# (hw:CARD=Aureon51MkII,DEV=0 / plughw:Aureon51MkII,0 -> Aureon51MkII)
DAC_NAME=$(echo "$DAC_DEVICE" | sed -E 's/^(plug)?hw:(CARD=)?([^,]+).*/\3/')
[ "$DAC_DEVICE" = "null" ] && DAC_NAME=""

cat > /mnt/mmcblk0p2/tce/pcp_patch.sh <<'EOF'
#!/bin/sh
# pcp_patch.sh - piCorePlayer post-boot patches (run from Tweaks > User commands)

# Patch dmix.cgi: stop commenting out the format line (idempotent)
sudo sed -i '/Comment out format for now/{n;/^[[:blank:]]*busybox/s/^/#/}' /var/www/cgi-bin/dmix.cgi

# Patch dmix.cgi: the "rate" sed must not overwrite the rate_converter line (idempotent)
sudo sed -i 's|s/rate\.\*/rate |s/rate [0-9].*/rate |' /var/www/cgi-bin/dmix.cgi

# Restore the DAC mixer
sudo alsactl restore DAC_NAME_PLACEHOLDER
EOF

# If there is no DAC, remove the alsactl line (otherwise it would keep an empty name)
if [ -z "$DAC_NAME" ]; then
    sed -i '/sudo alsactl restore DAC_NAME_PLACEHOLDER/d' /mnt/mmcblk0p2/tce/pcp_patch.sh
else
    sed -i "s|DAC_NAME_PLACEHOLDER|${DAC_NAME}|g" /mnt/mmcblk0p2/tce/pcp_patch.sh
fi

chmod +x /mnt/mmcblk0p2/tce/pcp_patch.sh

set_pcp_cfg USER_COMMAND_1 "/mnt/mmcblk0p2/tce/pcp_patch.sh"

### --- 10. /etc/asound.conf: rate_converter (mixer only) + dmixer subdevice -
ASOUND=/etc/asound.conf

sudo touch "$ASOUND"

# 1) pcm.pCP_Mixer: add rate_converter "speexrate_medium" (idempotent)
if sudo grep -q '^pcm\.pCP_Mixer' "$ASOUND"; then
    if ! sudo awk '/^pcm\.pCP_Mixer[[:space:]]*\{/,/^\}/' "$ASOUND" | grep -q 'rate_converter'; then
        sudo awk '
            /^pcm\.pCP_Mixer[[:space:]]*\{/ { inblock=1 }
            inblock && /^\}/ && !done {
                print "   rate_converter \"speexrate_medium\""
                done=1
            }
            { print }
        ' "$ASOUND" > /tmp/asound.new && sudo cp /tmp/asound.new "$ASOUND" && rm -f /tmp/asound.new
    fi
else
    sudo sh -c "cat >> $ASOUND" <<'EOC'

pcm.pCP_Mixer {
   type plug
   slave.pcm "pcm.dmixer"
   rate_converter "speexrate_medium"
}
EOC
fi

# 2) pcm.dmixer: subdevice 0 always ACTIVE (uncommented if needed, added otherwise)
if sudo grep -q '^pcm\.dmixer' "$ASOUND"; then
    # uncomment any form of "# subdevice 0" / "##  subdevice 0" (inside the dmixer block only)
    sudo sed -i '/^pcm\.dmixer[[:space:]]*{/,/^}/ s/^[[:space:]]*#*[[:space:]]*subdevice 0[[:space:]]*$/      subdevice 0/' "$ASOUND"
    # if the line is missing, add it after "device 0"
    if ! sudo awk '/^pcm\.dmixer[[:space:]]*\{/,/^\}/' "$ASOUND" | grep -q '^[[:space:]]*subdevice 0'; then
        sudo awk '
            /^pcm\.dmixer[[:space:]]*\{/ { indmixer=1 }
            indmixer && /^      device 0/ && !done {
                print
                print "      subdevice 0"
                done=1
                next
            }
            { print }
        ' "$ASOUND" > /tmp/asound.new && sudo cp /tmp/asound.new "$ASOUND" && rm -f /tmp/asound.new
    fi
else
    sudo sh -c "cat >> $ASOUND" <<'EOC'

pcm.dmixer {
   type dmix
   ipc_key 1024
   ipc_perm 0666
   slave.pcm {
      type hw
      card Loopback
      device 0
      subdevice 0
   }
}
EOC
fi

# 3) Verification
echo ">>> /etc/asound.conf after patch:"
sudo cat "$ASOUND"
sudo grep -q 'rate_converter "speexrate_medium"' "$ASOUND" \
    && echo ">>> OK: speexrate_medium present." \
    || echo "!!! WARNING: speexrate_medium missing."
sudo grep -q '^[[:space:]]*subdevice 0' "$ASOUND" \
    && echo ">>> OK: subdevice 0 active." \
    || echo "!!! WARNING: subdevice 0 missing or commented out."

# 4) Final backup: USER_COMMAND_1 (pcp.cfg) and asound.conf
pcp backup || echo "!!! pcp backup failed (non-blocking)."

set -e

set +v
echo ""
echo "CamillaDSP extension installed ($EXT_CDSP). Reboot to start it."
echo "Configs: $BASE/configs/_A-loopback.yml (streamer)"
if [ -n "$EXT_DEVICE" ]; then
    echo "         $EXT_CFG (external input $EXT_DEVICE, $EXT_CAPTURE_FORMAT)"
else
    echo "         no group B config created (see the message above)"
fi
echo "Web interface: http://<address of the Pi>:5005/gui/index.html"
}

###########################################################################
# PHASE 2 - LYBRICA + BLE HID BRIDGE (patched pcp-bt.tcz) + WEB FILES
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
#   /mnt/mmcblk0p2/tce (persistent, written directly, no pcp bu needed)
#     slimserver/prefs/material-skin/custom.js
#     camilladsp/tone_circle.html
#
# Modules in /usr/local are visible to both tc and root without PYTHONPATH,
# unlike the old pip --user install in /home/tc/.local.
###########################################################################
install_lybrica() {
    # PYCAMILLA_REF, PYCAMILLA_URL, TM1637_URL, LYBRICA_URL and
    # PCP_BT_HID_BLE_BRIDGE_URL are defined globally.

    OPT=/etc/sysconfig/tcedir/optional
    TMPDIR_LY=/tmp/install_lybrica

    # LyBriCa drives the CamillaDSP configs written by phase 1: warn if they are missing
    CFG_DIR=/mnt/mmcblk0p2/tce/camilladsp/configs
    if ! ls "$CFG_DIR"/_A-*.yml >/dev/null 2>&1 || ! ls "$CFG_DIR"/_B-*.yml >/dev/null 2>&1; then
        echo "!!! WARNING: _A-*.yml and/or _B-*.yml not found in $CFG_DIR."
        echo "    LyBriCa expects an _A-*.yml, and a _B-*.yml for the automatic source"
        echo "    switching: run phase 1 first, or create them by hand."
    fi

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
# start_lybrica.sh - Launch LyBriCa (auto source switching / LMS / volume sync daemon)
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
    #     (see patch_pcp_bt). A failure here only warns: LyBriCa itself is
    #     already installed and pcp bu must still run.
    say "Patching pcp-bt.tcz (BLE HID bridge)..."
    patch_pcp_bt || echo "!!! WARNING: pcp-bt.tcz was not patched (see messages above)."

    # 8c. Web files: Material Skin custom.js, tone circle page, and the
    #     TEMPORARY material.min.js patch (see install_web_ui). A failure
    #     here only warns, for the same reason as above.
    say "Installing web files (custom.js, tone circle page, Material Skin patch)..."
    install_web_ui || echo "!!! WARNING: web files step incomplete (see messages above)."

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
    echo "  pcp-bt.tcz                        (rebuilt with the patched hid_ble_bridge.py)"
    echo "  /mnt/mmcblk0p2/tce/slimserver/prefs/material-skin/custom.js"
    echo "  /mnt/mmcblk0p2/tce/camilladsp/tone_circle.html"
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
# pCP's own pcp-bt.tcz ships hid_ble_bridge.py. This replaces it with the
# version from the LyBriCa repository
#   https://github.com/melomane63/LyBriCa
# by unpacking pcp-bt.tcz, swapping the file, rebuilding the squashfs and
# putting it back. The "hidbridge" init script is left untouched. The result
# takes effect at the next reboot.
#
# - Idempotent: nothing is rebuilt if the extension already has this file.
# - Safe: the file is downloaded and checked BEFORE anything is touched.
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

    # 1. Download and check the file before touching anything
    wget -q -O "$PB_TMP/hid_ble_bridge.py" "$PCP_BT_HID_BLE_BRIDGE_URL" \
        || { echo "!!! Download failed: $PCP_BT_HID_BLE_BRIDGE_URL"; return 1; }
    if head -c 300 "$PB_TMP/hid_ble_bridge.py" | grep -qiE '<!doctype|<html'; then
        echo "!!! hid_ble_bridge.py is a web page (use the raw URL)."
        return 1
    fi
    python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$PB_TMP/hid_ble_bridge.py" \
        || { echo "!!! hid_ble_bridge.py is not valid Python"; return 1; }

    # 2. Unpack the current pcp-bt.tcz
    unsquashfs -f -d "$PB_EXT" "$PB_TCZ" > /dev/null \
        || { echo "!!! unsquashfs failed on $PB_TCZ"; return 1; }
    [ -d "$PB_EXT/usr/local/bin" ] \
        || { echo "!!! usr/local/bin missing in pcp-bt.tcz: unexpected layout"; return 1; }

    # 3. Already patched? Then there is nothing to rebuild
    if cmp -s "$PB_TMP/hid_ble_bridge.py" "$PB_EXT/usr/local/bin/hid_ble_bridge.py"; then
        echo ">>> pcp-bt.tcz already contains this hid_ble_bridge.py: nothing to do."
        return 0
    fi

    # 4. Swap the file
    cp "$PB_TMP/hid_ble_bridge.py" "$PB_EXT/usr/local/bin/hid_ble_bridge.py"
    chmod 755 "$PB_EXT/usr/local/bin/hid_ble_bridge.py"

    # 5. Rebuild and put it back
    mksquashfs "$PB_EXT" "$PB_TMP/pcp-bt.tcz" -noappend -all-root > /dev/null \
        || { echo "!!! mksquashfs failed"; return 1; }

    # The original md5 no longer matches the rebuilt file: keep it as .orig
    if [ -f "$OPT/pcp-bt.tcz.md5.txt" ]; then
        [ -f "$OPT/pcp-bt.tcz.md5.txt.orig" ] || cp "$OPT/pcp-bt.tcz.md5.txt" "$OPT/pcp-bt.tcz.md5.txt.orig"
        rm -f "$OPT/pcp-bt.tcz.md5.txt"
    fi
    mv -f "$PB_TMP/pcp-bt.tcz" "$PB_TCZ"
    echo ">>> pcp-bt.tcz rebuilt with the patched hid_ble_bridge.py (active after reboot)."
    return 0
}

###########################################################################
# Web files: Material Skin custom.js, tone circle page, TEMPORARY JS patch
#
# Everything lives on the persistent partition (no pcp bu needed).
# - custom.js     -> slimserver/prefs/material-skin/
# - tone circle page -> camilladsp/ (next to settings.json, see WEB_PAGE_PATH
#                    in lybrica.py)
# - an existing different file is kept once as <file>.bak
# - Not fatal: returns non-zero with a message, the caller only warns.
#
# Uses TMPDIR_LY from install_lybrica.
###########################################################################
install_web_ui() {
    WU_TMP="$TMPDIR_LY/web"
    WU_BASE=/mnt/mmcblk0p2/tce
    WU_CUSTOM="$WU_BASE/slimserver/prefs/material-skin/custom.js"
    WU_HTML="$WU_BASE/camilladsp/tone_circle.html"
    mkdir -p "$WU_TMP" || return 1

    fetch_checked() {  # $1=url  $2=dest  $3=text that must be inside
        wget -q -O "$2" "$1" || { echo "!!! Download failed: $1"; return 1; }
        grep -qF "$3" "$2" || { echo "!!! $1 does not look right (missing '$3')"; return 1; }
    }
    put_file() {  # $1=source  $2=destination
        mkdir -p "$(dirname "$2")" || return 1
        if [ -f "$2" ] && ! cmp -s "$1" "$2" && [ ! -f "$2.bak" ]; then
            cp -p "$2" "$2.bak" && echo ">>> Existing $2 kept as $2.bak"
        fi
        cp "$1" "$2" && echo ">>> Installed: $2"
    }

    # Download and check both files before touching anything
    fetch_checked "$CUSTOM_JS_URL" "$WU_TMP/custom.js" "LyBriCa" || return 1
    fetch_checked "$TONE_CIRCLE_HTML_URL" "$WU_TMP/tone_circle.html" "api/tone" || return 1

    put_file "$WU_TMP/custom.js" "$WU_CUSTOM" || return 1
    put_file "$WU_TMP/tone_circle.html" "$WU_HTML" || return 1

    # Old name of the page (before the rename): the new lybrica.py no longer
    # serves it, so it is removed to avoid leaving a dead copy behind.
    if [ -f "$WU_BASE/camilladsp/beosonic_lybrica.html" ]; then
        rm -f "$WU_BASE/camilladsp/beosonic_lybrica.html" \
            && echo ">>> Removed the old page: $WU_BASE/camilladsp/beosonic_lybrica.html"
    fi

    # ---- TEMPORARY: Material Skin patch ---------------------------------
    # Remove this block once a Material Skin release contains the fix.
    # Idempotent. A Material Skin update replaces the file and removes the
    # patch (the next run of the installer applies it again).
    JS="$WU_BASE/slimserver/Cache/InstalledPlugins/Plugins/MaterialSkin/HTML/material/html/js/material.min.js"
    PATCH_FROM='if(void 0!=a){var e=[]'
    if [ ! -f "$JS" ]; then
        echo "!!! material.min.js not found: Material Skin not installed yet?"
        echo "    custom.js is installed; re-run this step after installing Material Skin."
        return 0
    fi
    if ! grep -qF "$PATCH_FROM" "$JS"; then
        echo ">>> material.min.js: pattern not found (already patched, or another version): unchanged."
        return 0
    fi
    [ -f "$JS.bak" ] || cp -p "$JS" "$JS.bak" || { echo "!!! Cannot back up $JS"; return 1; }
    sed -i 's/if(void 0!=a){var e=\[\]/{var e=[]/' "$JS" || { echo "!!! sed failed on $JS"; return 1; }
    if grep -qF "$PATCH_FROM" "$JS"; then
        echo "!!! Patch not applied (pattern still present)."
        return 1
    fi
    echo ">>> material.min.js patched (backup: $JS.bak)"
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
    echo '       (GPIO 12: EXT relay input, left floating at boot so the relay'
    echo '        board is not activated before lybrica.py takes over)'
    echo ''
    echo ' Nothing is removed. Meant for a fresh install: on a bare Raspberry Pi'
    echo ' these GPIOs are free (GPIO 14/15 = UART, GPIO 12, 16 and 26 = plain GPIOs).'
    echo ' If you use a HAT or another add-on board, check its pinout first.'
    echo ' The GPIO 12 line is harmless if no EXT relay is wired to it.'
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
# enable the UART and leave GPIO 12 floating at boot (EXT relay input),
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
# 20 matches UNCHANGED_RC in install_lybrica_pcp.sh: "already up to date".
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
# PHASE 3 - ESP32C3 (standalone phase)
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
# DISPATCH: one phase = one process (sh "$SELF" --run <phase>)
###########################################################################
if [ "$1" = "--run" ]; then
    case "$2" in
        cdsp)    install_camilladsp ;;
        lybrica) install_lybrica ;;
        esp32)   install_esp32 ;;
        *) fail "Unknown phase: $2 (use cdsp, lybrica or esp32)" ;;
    esac
    exit $?
fi

[ "$(id -u)" -eq 0 ] && fail "Run this script as user tc, not root."

DONE=""
LAST_RC=0

run_step() {  # $1 = cdsp|lybrica|esp32
    case "$1" in
        cdsp)    STEP_TITLE="PHASE 1/3 - CamillaDSP + CamillaGUI" ;;
        lybrica) STEP_TITLE="PHASE 2/3 - LyBriCa" ;;
        esp32)   STEP_TITLE="PHASE 3/3 - ESP32C3 power button" ;;
    esac
    echo
    echo "############################################################"
    echo "# $STEP_TITLE"
    echo "############################################################"
    sh "$SELF" --run "$1"
    rc=$?
    LAST_RC=$rc
    case "$rc" in
        0)          DONE="$DONE $1:OK" ;;
        "$SKIP_RC") DONE="$DONE $1:SKIPPED" ;;
        *)          DONE="$DONE $1:FAILED"
                    echo "!!! Phase '$1' failed (see messages above)." >&2 ;;
    esac
}

# All phases, in order. A declined phase (SKIPPED) does not stop the chain
# (CamillaDSP may already be installed); a failed one does.
run_all() {
    echo
    echo "All phases will run in order: 1) CamillaDSP, 2) LyBriCa, 3) ESP32C3."
    echo "Phases 1 and 3 ask for their own confirmation; answer n to skip one."
    for s in cdsp lybrica esp32; do
        run_step "$s"
        case "$LAST_RC" in
            0|"$SKIP_RC") ;;
            *) echo "!!! 'All' stopped after the failure of phase '$s'." >&2
               return ;;
        esac
    done
}

do_reboot() {
    case "$DONE" in
        *FAILED*) echo "!!! Some phases failed:$DONE" ;;
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
            1|cdsp)    run_step cdsp ;;
            2|lybrica) run_step lybrica ;;
            3|esp32)   run_step esp32 ;;
            a|A|all)   run_all ;;
            r|R)       do_reboot ;;
            q|Q)       return 1 ;;
            *) echo "Unknown choice: $c"; continue ;;
        esac
        # A failed phase cancels the remaining choices (e.g. "1 2 3")
        case "$LAST_RC" in
            0|"$SKIP_RC") ;;
            *) echo "!!! Remaining choices cancelled after a failure." >&2
               break ;;
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
    echo "=============================================================="
    echo "  LyBriCa full installer for piCorePlayer"
    echo "=============================================================="
    echo "  Installation phases (each one runs on its own):"
    echo
    echo "   1) CamillaDSP 5 + CamillaGUI   configs _A-loopback.yml (streamer)"
    echo "                                  and _B-External.yml (external input)"
    echo "   2) LyBriCa                     daemon, BLE HID bridge, web files"
    echo "   3) ESP32C3 power button        config.txt GPIO lines (optional hardware)"
    echo
    echo "   A) All phases, in order (1, 2, 3)"
    echo "  ------------------------------------------------------------"
    echo "   R) Reboot now"
    echo "   Q) Quit"
    echo "--------------------------------------------------------------"
    printf "Choice (several allowed, e.g. 1 2): "
    read -r CHOICES || break
    [ -z "$CHOICES" ] && continue
    # shellcheck disable=SC2086
    run_choice $CHOICES || break
done

summary
