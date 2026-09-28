#!/usr/bin/env bash
# One-shot installer for the Keyestudio ReSpeaker 2-Mic HAT analyzer.
#
#   sudo ./install.sh              # full install (packages, driver, command)
#   sudo ./install.sh --no-driver  # skip the ReSpeaker sound card driver
#
# Afterwards, run the analyzer from anywhere with:  gsp-keystudio
# (all script options pass through, e.g. gsp-keystudio --now)
set -euo pipefail

CMD_NAME="gsp-keystudio"
# GSP_INSTALL_DIR / GSP_BIN_PATH let tests (or an alternate prefix) override
# these locations; the defaults below are unchanged for a normal install.
INSTALL_DIR="${GSP_INSTALL_DIR:-/opt/gsp-keystudio}"
BIN_PATH="${GSP_BIN_PATH:-/usr/local/bin/${CMD_NAME}}"
DRIVER_REPO="https://github.com/HinTak/seeed-voicecard"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="${SUDO_USER:-pranav}"
INSTALL_DRIVER=1
REBOOT_NEEDED=0

for arg in "$@"; do
    case "$arg" in
        --no-driver) INSTALL_DRIVER=0 ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

step() { echo -e "\n\033[1;36m==> $*\033[0m"; }
warn() { echo -e "\033[1;33mWARNING: $*\033[0m"; }

if [[ $EUID -ne 0 ]]; then
    echo "Please run as root:  sudo $0"
    exit 1
fi
if [[ ! -f "${SRC_DIR}/respeaker_analyzer.py" ]]; then
    echo "respeaker_analyzer.py not found next to this installer (${SRC_DIR})."
    exit 1
fi

# --------------------------------------------------------------------------- #
step "Installing system packages"
apt-get update
apt-get install -y alsa-utils python3 python3-numpy python3-rpi.gpio \
    python3-spidev git dkms i2c-tools

# --------------------------------------------------------------------------- #
step "Enabling I2C and SPI (codec control + LEDs)"
if command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_i2c 0
    raspi-config nonint do_spi 0
else
    warn "raspi-config not found - enable I2C and SPI manually."
fi

# --------------------------------------------------------------------------- #
if [[ $INSTALL_DRIVER -eq 1 ]]; then
    step "Installing ReSpeaker 2-Mic (seeed-voicecard / WM8960) driver"
    if arecord -l 2>/dev/null | grep -qiE "seeed|wm8960"; then
        echo "Sound card already detected - skipping driver install."
    else
        apt-get install -y "linux-headers-$(uname -r)" 2>/dev/null \
            || apt-get install -y raspberrypi-kernel-headers \
            || warn "Could not install kernel headers; the driver build may fail."

        build_dir="$(mktemp -d)"
        git clone "$DRIVER_REPO" "${build_dir}/seeed-voicecard"
        cd "${build_dir}/seeed-voicecard"
        # The repo keeps one branch per kernel series, e.g. v6.6, v6.1, v5.15
        kbranch="v$(uname -r | cut -d. -f1,2)"
        if git ls-remote --exit-code --heads origin "$kbranch" >/dev/null; then
            git checkout "$kbranch"
        else
            warn "No driver branch for kernel ${kbranch}; using the default branch."
        fi
        ./install.sh
        cd "$SRC_DIR"
        rm -rf "$build_dir"
        REBOOT_NEEDED=1
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
step "Installing the ${CMD_NAME} command"
install -d -m 755 "$INSTALL_DIR"
install -m 755 "${SRC_DIR}/respeaker_analyzer.py" "${INSTALL_DIR}/respeaker_analyzer.py"

cat > "$BIN_PATH" <<EOF
#!/usr/bin/env bash
# Runs the ReSpeaker 2-Mic HAT recorder/analyzer. Installed by install.sh.
export GSP_RECORDINGS_DIR="${RECORDINGS_DIR}"
exec python3 "${INSTALL_DIR}/respeaker_analyzer.py" "\$@"
EOF
chmod 755 "$BIN_PATH"

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
step "Done"
echo "Command installed: ${BIN_PATH}"
echo "Recordings are saved to: ${RECORDINGS_DIR}"
echo
echo "Usage (from any folder):"
echo "  ${CMD_NAME}              tap the button once: record 3 s and show results"
echo "  ${CMD_NAME}              double-tap the button: start a session recording;"
echo "                          double-tap again to stop and save it"
echo "  ${CMD_NAME} --now        record right away"
echo "  ${CMD_NAME} --file x.wav analyze an existing recording"
echo
if [[ $REBOOT_NEEDED -eq 1 ]]; then
    warn "The sound card driver was just installed - reboot before first use:"
    echo "  sudo reboot"
else
    echo "Log out and back in (or reboot) once so the new group permissions apply."
fi
