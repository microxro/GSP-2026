# ReSpeaker 2-Mic HAT – Audio Recorder & Analyzer

After installing, run `man gsp-keystudio` for the full manual: button controls, the analysis report, LEDs, playback, files and troubleshooting.

`respeaker_analyzer.py` waits for the HAT's button (GPIO17). There are two ways to record, and every recording is saved.

| Button | What happens | Saved as |
|---|---|---|
| **Tap once** | Records 3 s, then shows the analysis below | `rec_<date>_<time>.wav` |
| **Double-tap** | Starts a session recording with no time limit and no analysis. The LEDs turn magenta. **Double-tap again** to stop and save it. | `session_<date>_<time>.wav` |

Both kinds go to the recordings folder that the installer creates (`/home/pranav/recordings/`). If you press Ctrl+C during a session, the session is saved before the program exits. A single tap starts about 0.4 s after you let go, because the program waits to see whether a second tap is coming. During a session a single tap does nothing, and a quick third tap after a double-tap is ignored.

The analysis after a single tap reports:

- **Volume:** exact RMS and peak level (dBFS) and an estimated dB SPL. You also get a rating from SILENT to EXTREME showing its level (e.g. 5 of 7) and range, plus a LOW→HIGH meter with a 0–100 score.
- **Frequency:** the exact dominant frequency, the spectral centroid and the top 3 peaks (Hz). Band energy is shown as a % for Low 20–250, Mid 250–4000 and High 4000+ Hz. You also get a rating from VERY LOW to VERY HIGH showing its level and range, plus a meter with a 0–100 score.
- **LEDs (optional):** red while recording, blue while analyzing, then green→red to show loudness

## One-step install (Raspberry Pi)
```bash
cd /home/pranav/GSP-2026
sudo ./install.sh
sudo reboot            # only needed the first time (sound card driver)
```
The installer:
- installs the packages
- turns on I2C and SPI
- builds the ReSpeaker driver for your kernel
- adds your user to the audio/gpio/spi groups
- creates the recordings folder (`/home/pranav/recordings/`) and points the command at it
- makes the HAT the default sound device, so playback goes to the HAT's headphone jack
- creates the `gsp-keystudio` command and its manual page (`man gsp-keystudio`)

If the driver is already installed, use `--no-driver` to skip that step. After you pull new code, run the installer again to update the command.

## Run (from any folder)
```bash
gsp-keystudio               # waits for the button: tap = 3 s + analysis, double-tap = session
gsp-keystudio --now         # records 3 s right away and analyzes (also saved)
gsp-keystudio --file x.wav  # analyzes an existing WAV
```
The script detects the sound card automatically; use `--device plughw:N,0` to override it.

The SPL figure is approximate (`SPL_OFFSET` in the script). To get accurate numbers, calibrate it against a sound meter.

## Play back your recordings
Every recording is saved in the folder the installer made: `/home/pranav/recordings/`.

```bash
ls -lt /home/pranav/recordings/                               # list them, newest first
aplay /home/pranav/recordings/session_20260928_033906_241.wav  # play one
aplay "$(ls -t /home/pranav/recordings/*.wav | head -1)"       # play the newest
```
Plug headphones or a speaker into the ReSpeaker HAT's headphone jack. The installer makes the HAT the default sound device (it writes `~/.asoundrc`), so `aplay` plays through the HAT without any extra options. To use the Pi's own audio output instead, add `-D plughw:Headphones`, or delete `~/.asoundrc`. Run `aplay -l` to see the card names.

To listen on another computer, copy the files over, for example `scp pranav@<pi-address>:recordings/*.wav .`, and open them in any media player.

