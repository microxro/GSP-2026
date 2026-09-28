# ReSpeaker 2-Mic HAT – Audio Recorder & Analyzer

`respeaker_analyzer.py` waits for the HAT's button (GPIO17). When you press it, the script records 3 seconds of audio and reports:

- **Volume:** exact RMS and peak level (dBFS) and an estimated dB SPL. You also get a rating from SILENT to EXTREME showing its level (e.g. 5 of 7) and range, plus a LOW→HIGH meter with a 0–100 score.
- **Frequency:** the exact dominant frequency, the spectral centroid and the top 3 peaks (Hz). Band energy is shown as a % for Low 20–250, Mid 250–4000 and High 4000+ Hz. You also get a rating from VERY LOW to VERY HIGH showing its level and range, plus a meter with a 0–100 score.
- **LEDs (optional):** red while recording, blue while analyzing, then green→red to show loudness

## Setup (Raspberry Pi)
```bash
sudo apt install alsa-utils python3-numpy python3-rpi.gpio python3-spidev
arecord -l            # confirm the seeed/wm8960 card is listed
```

## Run
```bash
python3 /home/pranav/GSP-2026/respeaker_analyzer.py            # button mode
python3 /home/pranav/GSP-2026/respeaker_analyzer.py --now      # record once now
python3 /home/pranav/GSP-2026/respeaker_analyzer.py --file x.wav
```
Recordings are saved to `/home/pranav/recordings/`. Add `--no-save` to delete each recording after it's analyzed. The script detects the sound card automatically; use `--device plughw:N,0` to override it.

The SPL figure is approximate (`SPL_OFFSET` in the script). To get accurate numbers, calibrate it against a sound meter.
