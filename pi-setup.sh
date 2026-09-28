#!/usr/bin/env bash
# Fixes HAT playback ("Input/output error") on the Pi. Run once:
#
#   sudo ./pi-setup.sh              # apply the fix, then reboot
#   sudo ./pi-setup.sh --no-reboot  # apply the fix, reboot later yourself
#
# Replaces the seeed-voicecard driver (playback fails with "Input/output
# error" / "no PCM clock") with the kernel's built-in wm8960-soundcard
# overlay, keeping the card name seeed2micvoicec, and sets the mixer levels
# once on the next boot.
#
# Safe to run more than once.
set -uo pipefail

CARD="seeed2micvoicec"
CONFIG=/boot/firmware/config.txt
OVERLAY_LINE="dtoverlay=wm8960-soundcard,alsaname=${CARD}"
LEVELS_SCRIPT=/usr/local/sbin/gsp-audio-levels
LEVELS_UNIT=/etc/systemd/system/gsp-audio-levels.service
REBOOT=1

for arg in "$@"; do
    case "$arg" in
        --no-reboot) REBOOT=0 ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

step() { echo -e "\n\033[1;36m==> $*\033[0m"; }
warn() { echo -e "\033[1;33mWARNING: $*\033[0m"; }

if [[ $EUID -ne 0 ]]; then
    echo "Please run as root:  sudo $0"
    exit 1
fi
for f in "$CONFIG" /etc/modules; do
    [[ -f "$f" ]] || { echo "$f not found - is this Raspberry Pi OS?"; exit 1; }
done

# =========================================================================== #
# Built-in WM8960 driver instead of seeed-voicecard
# =========================================================================== #
step "Removing the seeed-voicecard driver"
if systemctl list-unit-files seeed-voicecard.service 2>/dev/null | grep -q "^seeed-voicecard"; then
    systemctl disable --now seeed-voicecard.service 2>/dev/null || true
    echo "seeed-voicecard service disabled."
fi
if command -v dkms >/dev/null && dkms status 2>/dev/null | grep -q seeed-voicecard; then
    dkms remove seeed-voicecard/0.3 --all || warn "dkms remove failed - continuing."
else
    echo "No seeed-voicecard DKMS module installed."
fi
sed -i '/^snd-soc-seeed-voicecard$/d;/^snd-soc-ac108$/d' /etc/modules

step "Enabling the kernel's built-in WM8960 overlay"
if grep -q "^dtoverlay=wm8960-soundcard" "$CONFIG"; then
    echo "Already in ${CONFIG}."
else
    # [all] makes sure the line isn't caught inside a model-specific section
    printf '\n[all]\n%s\n' "$OVERLAY_LINE" >> "$CONFIG"
    echo "Added to ${CONFIG}: ${OVERLAY_LINE}"
fi

step "Setting the HAT's volume levels once on the next boot"
cat > "$LEVELS_SCRIPT" <<EOF
#!/usr/bin/env bash
# Installed by GSP-2026 pi-setup.sh: turns on the WM8960's headphone output
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
echo "Will run once at the next boot (log: /var/log/gsp-audio-levels.log)."

# =========================================================================== #
step "Done"
echo "After the reboot, test playback with:  gsp-keystudio --play"
echo "To undo the audio change: remove '${OVERLAY_LINE}' from ${CONFIG},"
echo "then run sudo ./install.sh to reinstall the seeed driver."
if [[ $REBOOT -eq 1 ]]; then
    echo "Rebooting in 5 seconds (Ctrl+C to cancel)..."
    sleep 5
    reboot
else
    echo "Reboot when ready:  sudo reboot"
fi
