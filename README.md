# ReSpeaker 2-Mic HAT – Audio Recorder & Analyzer

`respeaker_analyzer.py` waits for the HAT's button (GPIO17). There are two ways to record, and every recording is saved.

| Button | What happens | Saved as |
|---|---|---|
| **Tap once** | Records 3 s, then shows the analysis below | `rec_<date>_<time>.wav` |
| **Double-tap** | Starts a session recording with no time limit and no analysis. The LEDs turn magenta. **Double-tap again** to stop and save it. | `session_<date>_<time>.wav` |

Both kinds go to the recordings folder that the installer creates (`/home/pranav/recordings/`). If you press Ctrl+C during a session, the session is saved before the program exits.

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
- creates the `gsp-keystudio` command

If the driver is already installed, use `--no-driver` to skip that step. After you pull new code, run the installer again to update the command.

## Run (from any folder)
```bash
gsp-keystudio               # waits for the button: tap = 3 s + analysis, double-tap = session
gsp-keystudio --now         # records 3 s right away and analyzes (also saved)
gsp-keystudio --file x.wav  # analyzes an existing WAV
```
The script detects the sound card automatically; use `--device plughw:N,0` to override it.

The SPL figure is approximate (`SPL_OFFSET` in the script). To get accurate numbers, calibrate it against a sound meter.

## Testing
The tests don't need the HAT. They use fake versions of the GPIO pins, the SPI LEDs, `arecord`, `apt-get` and the other system tools, so you can run them on the Pi or on any Linux machine:
```bash
python3 -m unittest tests.test_analysis tests.test_hardware   # analysis, button, recording, LEDs, CLI
sudo bash tests/test_install.sh                               # installer, run in a sandbox
```
