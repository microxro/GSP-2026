#!/usr/bin/env bash
# One-step installer for the Keyestudio ReSpeaker 2-Mic HAT recorder/analyzer.
# Works on a fresh Raspberry Pi OS Lite (written for the Pi Zero WH):
#
#   sudo ./install.sh              # full install, asks to reboot at the end
#   sudo ./install.sh --no-driver  # don't touch the sound card setup
#
# Afterwards, run the program from anywhere with:  gsp-keystudio
# Full manual:                                     man gsp-keystudio
set -euo pipefail

CMD_NAME="gsp-keystudio"
CARD="seeed2micvoicec"          # ALSA name for the HAT (kept from the old driver)
OVERLAY_LINE="dtoverlay=wm8960-soundcard,alsaname=${CARD}"
# GSP_INSTALL_DIR / GSP_BIN_PATH / GSP_MAN_DIR can override
# these locations; the defaults below are unchanged for a normal install.
INSTALL_DIR="${GSP_INSTALL_DIR:-/opt/gsp-keystudio}"
BIN_PATH="${GSP_BIN_PATH:-/usr/local/bin/${CMD_NAME}}"
MAN_DIR="${GSP_MAN_DIR:-/usr/local/share/man/man1}"
LEVELS_SCRIPT=/usr/local/sbin/gsp-audio-levels
LEVELS_UNIT=/etc/systemd/system/gsp-audio-levels.service
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="${SUDO_USER:-pranav}"
SETUP_DRIVER=1
REBOOT_NEEDED=0
export DEBIAN_FRONTEND=noninteractive

for arg in "$@"; do
    case "$arg" in
        --no-driver) SETUP_DRIVER=0 ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

step() { echo -e "\n\033[1;36m==> $*\033[0m"; }
warn() { echo -e "\033[1;33mWARNING: $*\033[0m"; }

if [[ $EUID -ne 0 ]]; then
    echo "Please run as root:  sudo $0"
    exit 1
fi
for f in respeaker_analyzer.py "${CMD_NAME}.1"; do
    if [[ ! -f "${SRC_DIR}/${f}" ]]; then
        echo "${f} not found next to this installer (${SRC_DIR})."
        exit 1
    fi
done
CONFIG=/boot/firmware/config.txt              # Bookworm and newer
[[ -f "$CONFIG" ]] || CONFIG=/boot/config.txt # older releases
if [[ -r /proc/device-tree/model ]]; then
    echo "Board: $(tr -d '\0' < /proc/device-tree/model)"
fi

# --------------------------------------------------------------------------- #
step "Installing system packages"
# A single slow mirror shouldn't stop the install: retry, then carry on with
# whatever package lists did download.
for attempt in 1 2 3; do
    if apt-get update; then
        break
    fi
    warn "apt-get update failed (attempt ${attempt} of 3)."
    if [[ $attempt -lt 3 ]]; then sleep 5; fi
done
if ! apt-get install -y alsa-utils python3 python3-numpy python3-spidev man-db; then
    echo "Package install failed. Check the Pi's internet connection, run"
    echo "  sudo apt-get update"
    echo "and then run this installer again."
    exit 1
fi
# RPi.GPIO: keep whatever already provides it; newer releases ship the
# drop-in replacement python3-rpi-lgpio instead of python3-rpi.gpio.
if python3 -c "import RPi.GPIO" 2>/dev/null; then
    echo "RPi.GPIO is already installed."
else
    apt-get install -y python3-rpi.gpio || apt-get install -y python3-rpi-lgpio \
        || warn "Could not install RPi.GPIO - the button won't work until it is installed."
fi

# --------------------------------------------------------------------------- #
step "Enabling I2C and SPI (codec control + LEDs)"
if command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_i2c 0
    raspi-config nonint do_spi 0
else
    for param in i2c_arm spi; do
        if ! grep -q "^dtparam=${param}=on" "$CONFIG"; then
            printf '\n[all]\ndtparam=%s=on\n' "$param" >> "$CONFIG"
        fi
    done
    REBOOT_NEEDED=1
fi

# --------------------------------------------------------------------------- #
if [[ $SETUP_DRIVER -eq 1 ]]; then
    # The HAT's WM8960 chip is supported by the kernel itself. The old
    # seeed-voicecard driver never starts the chip's clock on newer kernels
    # (playback fails with "Input/output error"), so it is removed if present.
    step "Setting up the HAT's sound card (built-in WM8960 driver)"
    if systemctl list-unit-files seeed-voicecard.service 2>/dev/null | grep -q "^seeed-voicecard"; then
        systemctl disable --now seeed-voicecard.service 2>/dev/null || true
        echo "Disabled the old seeed-voicecard service."
    fi
    if command -v dkms >/dev/null && dkms status 2>/dev/null | grep -q seeed-voicecard; then
        dkms remove seeed-voicecard/0.3 --all || warn "Could not remove the seeed DKMS module."
    fi
    if [[ -f /etc/modules ]]; then
        sed -i '/^snd-soc-seeed-voicecard$/d;/^snd-soc-ac108$/d' /etc/modules
    fi

    overlay_dir="$(dirname "$CONFIG")/overlays"
    if [[ ! -f "${overlay_dir}/wm8960-soundcard.dtbo" ]]; then
        warn "This kernel has no wm8960-soundcard overlay. Update the Pi with"
        warn "  sudo apt full-upgrade && sudo reboot   then run this installer again."
    fi
    if grep -q "^dtoverlay=wm8960-soundcard" "$CONFIG"; then
        echo "Already enabled in ${CONFIG}."
    else
        # [all] makes sure the line isn't caught inside a model-specific section
        printf '\n[all]\n%s\n' "$OVERLAY_LINE" >> "$CONFIG"
        echo "Added to ${CONFIG}: ${OVERLAY_LINE}"
        REBOOT_NEEDED=1
    fi

    # Headphone output and microphone boost start switched off with this
    # driver. This one-time service turns them on once the card exists
    # (right away if it already does, otherwise at the next boot).
    step "Setting the HAT's volume levels"
    cat > "$LEVELS_SCRIPT" <<EOF
#!/usr/bin/env bash
# Installed by GSP-2026 install.sh: turns on the WM8960's headphone output
# and microphones, saves the levels, then disables its own service.
exec >>/var/log/gsp-audio-levels.log 2>&1
echo "--- \$(date)"
for _ in \$(seq 30); do
    grep -q "\\[${CARD}" /proc/asound/cards && break
    sleep 1
done
if ! grep -q "\\[${CARD}" /proc/asound/cards; then
    echo "Card ${CARD} not found - leaving the service enabled to retry next boot."
    exit 0
fi
set_ctl() { amixer -q -c ${CARD} sset "\$@" || echo "could not set: \$*"; }
set_ctl 'Left Output Mixer PCM' on
set_ctl 'Right Output Mixer PCM' on
set_ctl 'Playback' 100%
set_ctl 'Headphone' 90%
set_ctl 'Speaker' 90%
set_ctl 'Left Boost Mixer LINPUT1' on
set_ctl 'Right Boost Mixer RINPUT1' on
set_ctl 'Left Input Mixer Boost' on
set_ctl 'Right Input Mixer Boost' on
set_ctl 'Capture' 80% cap
alsactl store
systemctl disable gsp-audio-levels.service
echo "Levels set and saved."
EOF
    chmod 755 "$LEVELS_SCRIPT"
    cat > "$LEVELS_UNIT" <<EOF
[Unit]
Description=GSP-2026: set ReSpeaker HAT volume levels once
After=sound.target alsa-restore.service

[Service]
Type=oneshot
ExecStart=${LEVELS_SCRIPT}

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable gsp-audio-levels.service
    if grep -q "\[${CARD}" /proc/asound/cards 2>/dev/null; then
        "$LEVELS_SCRIPT" && echo "Levels set now (log: /var/log/gsp-audio-levels.log)."
    else
        echo "Levels will be set at the next boot (log: /var/log/gsp-audio-levels.log)."
    fi
fi

# --------------------------------------------------------------------------- #
step "Resolving ${TARGET_USER}'s recordings folder"
if id "$TARGET_USER" >/dev/null 2>&1; then
    TARGET_USER_EXISTS=1
    TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
else
    TARGET_USER_EXISTS=0
    TARGET_HOME="/home/${TARGET_USER}"
    warn "User ${TARGET_USER} not found - skipping group setup."
fi
RECORDINGS_DIR="${TARGET_HOME}/recordings"
echo "Recordings folder: ${RECORDINGS_DIR}"

# --------------------------------------------------------------------------- #
step "Installing the ${CMD_NAME} command and manual"
install -d -m 755 "$INSTALL_DIR"
install -m 755 "${SRC_DIR}/respeaker_analyzer.py" "${INSTALL_DIR}/respeaker_analyzer.py"

cat > "$BIN_PATH" <<EOF
#!/usr/bin/env bash
# Runs the ReSpeaker 2-Mic HAT recorder/analyzer. Installed by install.sh.
export GSP_RECORDINGS_DIR="${RECORDINGS_DIR}"
exec python3 "${INSTALL_DIR}/respeaker_analyzer.py" "\$@"
EOF
chmod 755 "$BIN_PATH"

install -d -m 755 "$MAN_DIR"
install -m 644 "${SRC_DIR}/${CMD_NAME}.1" "${MAN_DIR}/${CMD_NAME}.1"
mandb -q >/dev/null 2>&1 || true      # refresh the index for man -k / apropos

# --------------------------------------------------------------------------- #
step "Giving ${TARGET_USER} access to audio, GPIO, SPI and the recordings folder"
if [[ "$TARGET_USER_EXISTS" -eq 1 ]]; then
    for group in audio gpio spi i2c; do
        if getent group "$group" >/dev/null; then
            usermod -aG "$group" "$TARGET_USER"
        fi
    done
    target_group="$(id -gn "$TARGET_USER")"
    install -d -m 755 -o "$TARGET_USER" -g "$target_group" "$RECORDINGS_DIR"
fi

# --------------------------------------------------------------------------- #
step "Making the HAT the default sound device for ${TARGET_USER}"
if [[ "$TARGET_USER_EXISTS" -eq 1 ]]; then
    asoundrc="${TARGET_HOME}/.asoundrc"
    if [[ -f "$asoundrc" ]] && ! grep -q "GSP-2026" "$asoundrc"; then
        cp -p "$asoundrc" "${asoundrc}.bak-gsp"
        echo "Kept your old ${asoundrc} as ${asoundrc}.bak-gsp"
    fi
    cat > "$asoundrc" <<EOF
# Written by GSP-2026 install.sh: play and record through the ReSpeaker HAT
# (card "${CARD}") by default. Delete this file to go back to the Pi's
# own audio output.
pcm.!default {
    type plug
    slave.pcm "hw:${CARD}"
}
ctl.!default {
    type hw
    card "${CARD}"
}
EOF
    chown "$TARGET_USER:$target_group" "$asoundrc"
    echo "Sound now plays through the HAT for ${TARGET_USER}."
else
    warn "User ${TARGET_USER} not found - default sound device not changed."
fi

# --------------------------------------------------------------------------- #
step "Done"
echo "Command installed: ${BIN_PATH}"
echo "Recordings are saved to: ${RECORDINGS_DIR}"
echo "Full manual: man ${CMD_NAME}"
echo
echo "Usage (from any folder):"
echo "  ${CMD_NAME}              tap the button once: record 3 s and show results"
echo "  ${CMD_NAME}              double-tap the button: start a session recording;"
echo "                          double-tap again to stop and save it"
echo "  ${CMD_NAME} --now        record right away"
echo "  ${CMD_NAME} --file x.wav analyze an existing recording"
echo "  ${CMD_NAME} --play       play the newest recording through the HAT"
echo
if [[ $REBOOT_NEEDED -eq 1 ]]; then
    warn "A reboot is needed before the HAT's sound card appears."
    if [[ -t 0 ]]; then
        read -r -p "Reboot now? [Y/n] " answer || answer=n
        if [[ ! "$answer" =~ ^[Nn] ]]; then
            reboot
        fi
    fi
    echo "Reboot when ready:  sudo reboot"
else
    echo "Log out and back in (or reboot) once so the new group permissions apply."
fi
