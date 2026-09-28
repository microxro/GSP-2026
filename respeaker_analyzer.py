#!/usr/bin/env python3
"""
ReSpeaker 2-Mic Pi HAT (Keyestudio) - button-triggered audio recorder/analyzer.

Press the on-board button (GPIO17) -> record 3 seconds -> analyze:
  * Frequency: dominant pitch, spectral centroid, band energy (low/mid/high)
  * Volume:    RMS and peak level in dBFS, with an estimated dB SPL
Each result is rated on a LOW -> HIGH scale and printed as a bar meter.

Usage:
  python3 respeaker_analyzer.py               # wait for button presses
  python3 respeaker_analyzer.py --now         # record once immediately
  python3 respeaker_analyzer.py --file x.wav  # analyze an existing WAV
"""
import argparse
import datetime
import os
import re
import subprocess
import sys
import time
import wave

import numpy as np

BUTTON_PIN = 17            # ReSpeaker 2-Mic HAT user button (BCM numbering)
RECORD_SECONDS = 3
SAMPLE_RATE = 16000        # WM8960 codec supports 8k-48k; 16k covers voice + 8 kHz highs
CHANNELS = 2
SAVE_DIR = "/home/pranav/recordings"
# dBFS -> dB SPL offset. ~94 dB SPL for 0 dBFS is a rough default for this
# HAT at default capture gain; calibrate against a sound meter for accuracy.
SPL_OFFSET = 94.0

# (upper bound Hz, label) - rating of the dominant frequency
FREQ_LEVELS = [
    (60, "VERY LOW"),
    (250, "LOW"),
    (1000, "MID-LOW"),
    (2000, "MID"),
    (4000, "MID-HIGH"),
    (6000, "HIGH"),
    (float("inf"), "VERY HIGH"),
]
# (upper bound dBFS, label) - rating of the RMS loudness
VOLUME_LEVELS = [
    (-60, "SILENT"),
    (-45, "VERY QUIET"),
    (-35, "QUIET"),
    (-25, "MODERATE"),
    (-15, "LOUD"),
    (-6, "VERY LOUD"),
    (float("inf"), "EXTREME / CLIPPING"),
]
BANDS = [("Low", 20, 250), ("Mid", 250, 4000), ("High", 4000, 20000)]


# --------------------------------------------------------------------------- #
# LEDs (optional - 3x APA102 on SPI0). Silently disabled if spidev is missing.
# --------------------------------------------------------------------------- #
class Leds:
    def __init__(self, count=3):
        self.count = count
        self.spi = None
        try:
            import spidev
            self.spi = spidev.SpiDev()
            self.spi.open(0, 1)
            self.spi.max_speed_hz = 8000000
        except Exception:
            self.spi = None

    def show(self, rgb, brightness=8):
        if not self.spi:
            return
        r, g, b = rgb
        frame = [0x00] * 4
        for _ in range(self.count):
            frame += [0xE0 | brightness, b, g, r]
        frame += [0xFF] * 4
        try:
            self.spi.xfer2(frame)
        except Exception:
            pass

    def off(self):
        self.show((0, 0, 0), 0)


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def find_card():
    """Return the ALSA device for the ReSpeaker (seeed / wm8960), else default."""
    try:
        out = subprocess.run(["arecord", "-l"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        sys.exit("arecord not found - install with: sudo apt install alsa-utils")
    for line in out.splitlines():
        if re.search(r"seeed|wm8960|respeaker", line, re.I):
            m = re.match(r"card (\d+):", line)
            if m:
                return f"plughw:{m.group(1)},0"
    return "default"


def record(path, device, seconds=RECORD_SECONDS):
    cmd = ["arecord", "-q", "-D", device, "-f", "S16_LE", "-r", str(SAMPLE_RATE),
           "-c", str(CHANNELS), "-d", str(seconds), path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"arecord failed: {result.stderr.strip()}")


def load_wav(path):
    with wave.open(path, "rb") as wf:
        channels = wf.getnchannels()
        rate = wf.getframerate()
        width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise ValueError("Only 16-bit WAV files are supported")
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)  # mix both mics
    return data, rate


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def rate_level(value, levels):
    for i, (bound, label) in enumerate(levels):
        if value < bound:
            return i, label
    return len(levels) - 1, levels[-1][1]


def to_db(x):
    return 20 * np.log10(x) if x > 1e-10 else -100.0


def analyze(samples, rate):
    samples = samples - np.mean(samples)          # remove DC offset
    rms = np.sqrt(np.mean(samples ** 2))
    peak = np.max(np.abs(samples)) if samples.size else 0.0
    rms_db = to_db(rms)
    peak_db = to_db(peak)

    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    freqs = np.fft.rfftfreq(len(samples), d=1.0 / rate)
    power = spectrum ** 2
    audible = freqs >= 20                          # ignore rumble / DC

    if power[audible].sum() > 0:
        idx = np.argmax(spectrum * audible)
        dominant = float(freqs[idx])
        centroid = float(np.sum(freqs[audible] * power[audible]) / np.sum(power[audible]))
    else:
        dominant = centroid = 0.0

    total = power[audible].sum() or 1.0
    bands = {name: float(power[(freqs >= lo) & (freqs < hi)].sum() / total * 100)
             for name, lo, hi in BANDS}

    f_idx, f_label = rate_level(dominant, FREQ_LEVELS)
    v_idx, v_label = rate_level(rms_db, VOLUME_LEVELS)
    return {
        "rms_db": rms_db, "peak_db": peak_db, "spl": rms_db + SPL_OFFSET,
        "dominant": dominant, "centroid": centroid, "bands": bands,
        "freq_idx": f_idx, "freq_label": f_label,
        "vol_idx": v_idx, "vol_label": v_label,
        "silent": rms_db < VOLUME_LEVELS[0][0],
    }


def meter(idx, levels, width=21):
    pos = round(idx / (len(levels) - 1) * (width - 1))
    return "LOW [" + "".join("#" if i <= pos else "-" for i in range(width)) + "] HIGH"


def report(r):
    print("\n" + "=" * 52)
    print(" VOLUME")
    print(f"   RMS level      : {r['rms_db']:7.1f} dBFS")
    print(f"   Peak level     : {r['peak_db']:7.1f} dBFS")
    print(f"   Estimated SPL  : {r['spl']:7.1f} dB (approx.)")
    print(f"   Rating         : {r['vol_label']}")
    print(f"   {meter(r['vol_idx'], VOLUME_LEVELS)}")
    print(" FREQUENCY")
    if r["silent"]:
        print("   Too quiet to measure frequency reliably.")
    else:
        print(f"   Dominant freq  : {r['dominant']:7.1f} Hz")
        print(f"   Spectral center: {r['centroid']:7.1f} Hz")
        print(f"   Rating         : {r['freq_label']}")
        print(f"   {meter(r['freq_idx'], FREQ_LEVELS)}")
        print("   Band energy    : " +
              "  ".join(f"{k} {v:4.1f}%" for k, v in r["bands"].items()))
    print("=" * 52)


def volume_color(idx):
    """Green (quiet) -> yellow -> red (loud)."""
    t = idx / (len(VOLUME_LEVELS) - 1)
    return (int(255 * min(1, 2 * t)), int(255 * min(1, 2 * (1 - t))), 0)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def capture_and_analyze(device, leds, keep):
    os.makedirs(SAVE_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(SAVE_DIR, f"rec_{stamp}.wav")

    leds.show((255, 0, 0))                         # red = recording
    print(f"Recording {RECORD_SECONDS}s ...")
    record(path, device)
    leds.show((0, 0, 255))                         # blue = analyzing
    samples, rate = load_wav(path)
    result = analyze(samples, rate)
    report(result)
    leds.show(volume_color(result["vol_idx"]))
    if keep:
        print(f"Saved: {path}")
    else:
        os.remove(path)
    return result


def button_loop(device, leds, keep):
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    print(f"Using audio device {device}. Press the button to record "
          f"(Ctrl+C to quit).")
    try:
        while True:
            if GPIO.input(BUTTON_PIN) == GPIO.LOW:
                time.sleep(0.03)                   # debounce
                if GPIO.input(BUTTON_PIN) == GPIO.LOW:
                    try:
                        capture_and_analyze(device, leds, keep)
                    except RuntimeError as e:
                        print(e)
                    while GPIO.input(BUTTON_PIN) == GPIO.LOW:
                        time.sleep(0.02)           # wait for release
                    print("\nPress the button to record again.")
            time.sleep(0.02)
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        leds.off()
        GPIO.cleanup()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", help="analyze an existing 16-bit WAV instead of recording")
    p.add_argument("--now", action="store_true", help="record once without the button")
    p.add_argument("--device", help="ALSA device (default: auto-detect ReSpeaker)")
    p.add_argument("--no-save", action="store_true", help="delete recordings after analysis")
    args = p.parse_args()

    if args.file:
        report(analyze(*load_wav(args.file)))
        return

    device = args.device or find_card()
    leds = Leds()
    if args.now:
        capture_and_analyze(device, leds, not args.no_save)
    else:
        button_loop(device, leds, not args.no_save)


if __name__ == "__main__":
    main()
