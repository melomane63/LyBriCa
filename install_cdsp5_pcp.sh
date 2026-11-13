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
#   sh install_cdsp5_pcp.sh      -> asks for confirmation, then installs
#
# The install runs in its own process (--run camilla).
# ---------------------------------------------------------------------------

SELF="$0"

fail() { echo "ERROR: $*" >&2; exit 1; }

###########################################################################
# CENTRALIZED URLS AND VERSIONS
###########################################################################

CDSP_VER="5.0.0-beta.2"
GUI_VER="5.0.0-beta.2"

CDSP_URL_BASE="https://github.com/HEnquist/camilladsp/releases/download/v${CDSP_VER}"
CDSP_URL_ARCH="camilladsp-linux-%s.tar.gz"

GUI_URL_BASE="https://github.com/HEnquist/camillagui-backend/releases/download/v${GUI_VER}"
GUI_URL_ARCH="camillagui_linux_%s.tar.gz"

EXT_CDSP="CDSPpiCore.tcz"

# Squeezelite settings written to pcp.cfg (shown in the warning below)
SQ_RATES="48000-48000"
SQ_UPSAMPLE="hLE"
SQ_ALSA_PARAMS="::32:"
SQ_CLOSEOUT="3"

###########################################################################
# 1) CAMILLADSP
###########################################################################
install_camilladsp() {
set -e

### ======================= CONFIG ==========================================
SAMPLERATE=48000
CAPTURE_FORMAT="S32_LE"
PLAYBACK_FORMAT="S16_LE"
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
        echo "!!! DAC_DEVICE reste null."
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

### --- 3. CamillaDSP config and statefile ----------------------------------
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
title: Setup
EOF

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

rm -f "$BASE/start_camilladsp.sh" "$BASE/camilladsp" "$BASE/camillagui/camillagui"

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
echo "Web interface: http://pcp.local:5005/gui/index.html"
}

###########################################################################
# DISPATCH
###########################################################################
if [ "$1" = "--run" ]; then
    case "$2" in
        camilla) install_camilladsp ;;
        *) fail "Unknown step: $2" ;;
    esac
    exit $?
fi

[ "$(id -u)" -eq 0 ] && fail "Run this script as user tc, not root."

echo "This script installs CamillaDSP + CamillaGUI and WRITES:"
echo "  - CamillaDSP: _A-loopback.yml, camilladsp_statefile.yml"
echo "    (volume reset to -40 dB) and camillagui.yml (web port 5005)"
echo "  - the $EXT_CDSP extension (+ .dep) and its line in onboot.lst"
echo "  - pcp.cfg: squeezelite settings ALSA_PARAMS=\"$SQ_ALSA_PARAMS\", MAX_RATE=\"$SQ_RATES\","
echo "    UPSAMPLE=\"$SQ_UPSAMPLE\", CLOSEOUT=\"$SQ_CLOSEOUT\" (your current values are overwritten),"
echo "    USER_COMMAND_1 (overwritten), and OUTPUT only if it is a physical hw: device"
echo "  - /mnt/mmcblk0p2/tce/pcp_patch.sh and /etc/asound.conf"
echo "    (rate_converter in pCP_Mixer, subdevice 0 in dmixer)"
printf "Continue? [y/N]: "
read -r A
case "$A" in
    y|Y) ;;
    *)   echo "Cancelled."; exit 0 ;;
esac

if sh "$SELF" --run camilla; then
    echo "Installation finished."
else
    echo "!!! Installation failed (see messages above)." >&2
fi

printf "Reboot now? [y/N]: "
read -r R
case "$R" in
    y|Y) echo "Rebooting..."; pcp reboot ;;
    *)   echo "Reboot cancelled." ;;
esac
