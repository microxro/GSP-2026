# ReSpeaker 2-Mic HAT – Audio Recorder & Analyzer

After installing, run `man gsp-keystudio` for the full manual: button controls, the analysis report, LEDs, playback, files and troubleshooting.

`respeaker_analyzer.py` waits for the HAT's button (GPIO17). There are two ways to record, and every recording is saved.

| Button | What happens | Saved as |
|---|---|---|
| **Tap once** | Records 3 s, then shows the analysis below | `rec_<date>_<time>.wav` |
| **Double-tap** | Starts a session recording with no time limit and no analysis. The LEDs turn magenta. **Double-tap again** to stop and save it. | `session_<date>_<time>.wav` |

Both kinds go to the recordings folder that the installer creates (`/home/pranav/recordings/`). If you press Ctrl+C during a session, the session is saved before the program exits. A single tap starts about 0.4 s after you let go, because the program waits to see whether a second tap is coming. During a session a single tap does nothing, and a quick third tap after a double-tap is ignored.

The 3-second recording is made at 48 kHz (the chip's maximum, hears up to 24 kHz) and analyzed 200 times per second, so it can follow sweeps and changing sounds. The report shows:

- **Volume** in dB(A), like a sound level meter: the **average** of the whole recording, the **lowest** and **highest** 1/8-second levels (with when they happened), plus a rating and meter. Run `gsp-keystudio --calibrate <dB>` once with a sound meter (a phone app works) next to the HAT to make these exact; until then they're estimates.
- **Frequency:** the exact **lowest** and **highest** frequency heard (with when), the dominant frequency, spectral centre, the top 3 peaks and the share of energy in the low/mid/high bands.
- **LEDs:** a red countdown while recording (3 LEDs, then 2, then 1, one per second), blue while analyzing, then green→red to show loudness

## One-step install (Raspberry Pi)
Works on a fresh Raspberry Pi OS Lite, including the Pi Zero WH. You only need `git` to get the code:
```bash
cd ~
git clone https://github.com/microxro/GSP-2026
cd GSP-2026
sudo ./install.sh       # answers "Reboot now?" at the end - say yes the first time
```
The installer:
- installs the packages (alsa-utils, numpy, the GPIO and SPI libraries, man-db)
- turns on I2C and SPI
- sets up the HAT's sound card with the kernel's built-in WM8960 driver (`dtoverlay=wm8960-soundcard`), and removes the old seeed-voicecard driver if it's installed
- turns on the headphone output and microphones once the card appears (log: `/var/log/gsp-audio-levels.log`)
- adds your user to the audio/gpio/spi/i2c groups
- creates the recordings folder (`/home/pranav/recordings/`) and points the command at it
- makes the HAT the default sound device, so playback goes to the HAT's headphone jack
- creates the `gsp-keystudio` command and its manual page (`man gsp-keystudio`)

It's safe to run more than once. After you `git pull` new code, run `sudo ./install.sh` again to update the command. Use `--no-driver` to leave the sound card setup alone.

## Run (from any folder)
```bash
gsp-keystudio               # waits for the button: tap = 3 s + analysis, double-tap = session
gsp-keystudio --now         # records 3 s right away and analyzes (also saved)
gsp-keystudio --file x.wav  # analyzes an existing WAV
gsp-keystudio --play        # plays the newest recording through the HAT
gsp-keystudio --calibrate 65  # make volumes match a sound meter reading of 65 dB(A)
```
The script detects the sound card automatically; use `--device plughw:N,0` to override it.

Volumes are estimates until you calibrate once: hold a sound level meter (a phone app is fine) next to the HAT, make a steady sound, and run `gsp-keystudio --calibrate <the meter's dB(A) reading>`.

## Play back your recordings
Every recording is saved in the folder the installer made: `/home/pranav/recordings/`.

```bash
gsp-keystudio --play                                      # play the newest recording
ls -lt /home/pranav/recordings/                           # list them, newest first
gsp-keystudio --play session_20260928_033906_241.wav      # play a chosen one
```
Plug headphones or a speaker into the ReSpeaker HAT's headphone jack. `gsp-keystudio --play` always plays through the HAT; press Ctrl+C to stop.

You can also play files with `aplay <file>`. The installer makes the HAT the default sound device (it writes `~/.asoundrc`), so `aplay` also plays through the HAT without any extra options. To use the Pi's own audio output instead, add `-D plughw:Headphones`, or delete `~/.asoundrc`. Run `aplay -l` to see the card names.

To listen on another computer, copy the files over, for example `scp pranav@<pi-address>:recordings/*.wav .`, and open them in any media player.

