#!/usr/bin/env python3
"""
ReSpeaker 2-Mic Pi HAT (Keyestudio) - button-triggered audio recorder/analyzer.

Tap the on-board button (GPIO17) once -> record 3 seconds at 48 kHz -> analyze
200 times per second:
  * Volume:    average, lowest and highest level in dB(A), like a sound meter
               (run --calibrate once against a real meter for exact values)
  * Frequency: lowest and highest frequency heard (tracks sweeps), dominant
               pitch, spectral centre, top peaks, band energy

Double-tap the button -> start recording (LEDs magenta, no time limit).
Double-tap again      -> stop and save that recording (no analysis).

Every recording is saved as a WAV in $GSP_RECORDINGS_DIR (set by the
gsp-keystudio command), or /home/pranav/recordings if that is not set:
  rec_YYYYmmdd_HHMMSS_mmm.wav      3-second recordings that were analyzed
  session_YYYYmmdd_HHMMSS_mmm.wav  double-tap recordings

Usage:
  python3 respeaker_analyzer.py               # wait for button taps
  python3 respeaker_analyzer.py --now         # record once immediately
  python3 respeaker_analyzer.py --file x.wav  # analyze an existing WAV
  python3 respeaker_analyzer.py --play        # play the newest recording on the HAT
  python3 respeaker_analyzer.py --play x.wav  # play a chosen recording
  python3 respeaker_analyzer.py --calibrate 65  # match a sound meter reading
"""
import argparse
import datetime
import os
import re
import signal
import struct
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np

BUTTON_PIN = 17            # ReSpeaker 2-Mic HAT user button (BCM numbering)
RECORD_SECONDS = 3
SAMPLE_RATE = 48000        # WM8960 maximum: captures frequencies up to 24 kHz
CHANNELS = 2
# Where every recording is saved. The installed gsp-keystudio wrapper exports
# GSP_RECORDINGS_DIR (the folder the installer created).
SAVE_DIR = os.environ.get("GSP_RECORDINGS_DIR") or "/home/pranav/recordings"
# Every analysis report is appended here (the installer creates this file).
RESULTS_FILE = (os.environ.get("GSP_RESULTS_FILE")
                or os.path.join(SAVE_DIR, "analysis_results.txt"))
DOUBLE_TAP_WINDOW = 0.4    # s: a 2nd press this soon after a release = double tap
DEBOUNCE = 0.03            # s: a level change must still be there this much later
POLL_INTERVAL = 0.02       # s: button polling period
SESSION_STOP_TIMEOUT = 5   # s: wait for arecord to exit after each stop signal
SESSION_COLOR = (255, 0, 255)  # magenta = double-tap recording in progress
# Analysis: a 2048-point FFT (about 43 ms at 48 kHz) every 5 ms
FRAME_RATE = 200           # analyses per second
WINDOW_SECONDS = 0.04      # FFT length is the next power of 2 above this
FAST_SECONDS = 0.125       # sound-meter "fast" averaging for lowest/highest volume
GATE_DB = 30               # frequency tracking ignores frames this far below the loudest
PROMINENCE_DB = 10         # ...and frames whose peak isn't this far above the frame median
SILENCE_DBFS = -75         # a recording whose loudest frame is below this is silent
HIGHPASS_HZ = 20           # analysis ignores DC drift and rumble below about this
# dB(A) SPL = A-weighted dBFS + offset. "gsp-keystudio --calibrate <meter
# reading>" measures the offset exactly. Without it, the offset is worked out
# from the mic's typical sensitivity and the WM8960's current gain settings:
#   offset = 94 - mic sensitivity - total gain + ADC full scale + 3.01
# (3.01: a full-scale sine reads -3.01 dBFS RMS). The HAT's mic part isn't
# published, so the sensitivity is the typical value for analog MEMS mics.
MIC_SENSITIVITY_DBV = -38.0    # mic output at 94 dB SPL (1 Pa), dBV
ADC_FULL_SCALE_DBV = 0.0       # WM8960 at AVDD 3.3 V: 1.0 Vrms in = 0 dBFS
HAT_CARD = "seeed2micvoicec"
# Gain stages between mic and ADC, with the installer's settings as fallback
GAIN_CONTROLS = [
    ("Capture", 20.25),                        # input PGA (installer: 80%)
    ("Left Input Boost Mixer LINPUT1", 0.0),   # mic boost: 0 / +13 / +20 / +29 dB
    ("ADC PCM", 0.0),                          # ADC digital volume
]
CALIBRATION_FILE = os.path.expanduser(
    os.environ.get("GSP_CALIBRATION_FILE") or "~/.config/gsp-keystudio/spl_offset")

# (upper bound dB SPL, label) - rating of the average volume
VOLUME_LEVELS = [
    (35, "SILENT"),
    (50, "VERY QUIET"),
    (60, "QUIET"),
    (70, "MODERATE"),
    (80, "LOUD"),
    (90, "VERY LOUD"),
    (float("inf"), "EXTREME"),
]
BANDS = [("Low", 20, 250), ("Mid", 250, 4000), ("High", 4000, float("inf"))]  # High: up to rate/2


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

    def show(self, rgb, brightness=8, lit=None):
        """Show a color on the first `lit` LEDs (default: all); the rest are off."""
        if not self.spi:
            return
        r, g, b = rgb
        lit = self.count if lit is None else lit
        frame = [0x00] * 4
        for i in range(self.count):
            frame += [0xE0 | brightness, b, g, r] if i < lit else [0xE0, 0, 0, 0]
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


def recording_path(prefix):
    """New file path in SAVE_DIR (created if missing), e.g. rec_20260928_030821_100.wav."""
    os.makedirs(SAVE_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]  # ms: no overwrites
    return os.path.join(SAVE_DIR, f"{prefix}_{stamp}.wav")


def arecord_cmd(device, path, seconds=None):
    cmd = ["arecord", "-q", "-D", device, "-f", "S16_LE", "-r", str(SAMPLE_RATE),
           "-c", str(CHANNELS)]
    if seconds is not None:
        cmd += ["-d", str(seconds)]
    return cmd + [path]


def record(path, device, seconds=RECORD_SECONDS, on_tick=None):
    """Record `seconds` of audio; on_tick(elapsed_s) is called ~20x/s meanwhile."""
    proc = subprocess.Popen(arecord_cmd(device, path, seconds), stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
    start = time.monotonic()
    while True:
        try:
            _, err = proc.communicate(timeout=0.05)
            break
        except subprocess.TimeoutExpired:
            if on_tick:
                on_tick(time.monotonic() - start)
    if proc.returncode != 0:
        raise RuntimeError(f"arecord failed: {err.strip()}")


# --------------------------------------------------------------------------- #
# Session recording (double tap): open-ended arecord, stopped on request
# --------------------------------------------------------------------------- #
def start_session(device):
    """Start an open-ended recording. Returns (proc, path).

    arecord's stderr goes to a temp file (readable via session_stderr) rather
    than a pipe, so a long session can never block on a full pipe buffer.
    """
    path = recording_path("session")
    err = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(arecord_cmd(device, path), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=err)
    except OSError:
        err.close()
        raise
    proc.stderr = err
    return proc, path


def session_stderr(proc):
    """Whatever arecord wrote to stderr so far, as stripped text."""
    f = getattr(proc, "stderr", None)
    if f is None:
        return ""
    try:
        f.seek(0)
    except (OSError, ValueError):
        pass
    try:
        data = f.read()
    except (OSError, ValueError):
        return ""
    if isinstance(data, bytes):
        data = data.decode(errors="replace")
    return data.strip()


def stop_session(proc, path):
    """Stop a session recording and return the saved audio's duration (s).

    SIGINT makes arecord finalize the WAV header. Some alsa-utils versions then
    exit non-zero ("Aborted by signal Interrupt..."), so the exit code is not
    used: success is judged by the file. If arecord ignores SIGINT it is
    terminated, then killed; a header it never finalized is repaired by
    finalize_wav. Raises RuntimeError if no audio was saved.
    """
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        for escalate in (proc.terminate, proc.kill, None):
            try:
                proc.wait(timeout=SESSION_STOP_TIMEOUT)
                break
            except subprocess.TimeoutExpired:
                if escalate is None:
                    break          # unkillable (stuck in the driver): don't hang
                escalate()
    err = session_stderr(proc)
    if getattr(proc, "stderr", None) is not None:
        try:
            proc.stderr.close()
        except (OSError, ValueError):
            pass
    duration = finalize_wav(path)
    if duration is None:
        raise RuntimeError("Session recording failed - no audio was saved"
                           + (f" (arecord: {err})" if err else "."))
    return duration


def _wav_layout(f):
    """(channels, rate, sample_width, data_offset, declared_data_bytes) or None."""
    head = f.read(12)
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    fmt = None
    while True:
        chunk = f.read(8)
        if len(chunk) < 8:
            return None
        cid, size = chunk[:4], struct.unpack("<I", chunk[4:])[0]
        if cid == b"fmt ":
            body = f.read(size)
            if len(body) < 16:
                return None
            _, channels, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
            fmt = (channels, rate, bits // 8)
            f.seek(size & 1, 1)
        elif cid == b"data":
            if fmt is None:
                return None
            return fmt + (f.tell(), size)
        else:
            f.seek(size + (size & 1), 1)


def finalize_wav(path):
    """Make a recorder-written WAV consistent and return its duration in s.

    If the header claims more audio than the file holds (arecord was killed
    before it could finalize, so the length is still a huge placeholder), the
    length fields are rewritten from the file size. A missing file, or one with
    no valid header or no audio frames, is removed and None is returned.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "r+b") as f:
            layout = _wav_layout(f)
            if layout is None:
                frames = 0
            else:
                channels, rate, width, offset, declared = layout
                frame = channels * width
                available = size - offset
                frames = min(declared, available) // frame if frame and rate else 0
                if frames and declared > available:         # un-finalized header
                    data_bytes = frames * frame
                    f.truncate(offset + data_bytes)          # drop a partial frame
                    f.seek(4)
                    f.write(struct.pack("<I", offset + data_bytes - 8))
                    f.seek(offset - 4)
                    f.write(struct.pack("<I", data_bytes))
    except OSError:
        return None
    if not frames:
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    return frames / rate


def load_wav(path):
    """Mono float32 samples (both mics mixed) and the sample rate.
    float32 and per-channel mixing keep a long session small enough for a Pi Zero."""
    with wave.open(path, "rb") as wf:
        channels = wf.getnchannels()
        rate = wf.getframerate()
        width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise ValueError("Only 16-bit WAV files are supported")
    pcm = np.frombuffer(raw, dtype=np.int16).reshape(-1, channels)
    data = pcm[:, 0].astype(np.float32)
    for ch in range(1, channels):
        data += pcm[:, ch]                         # mix both mics
    data /= 32768.0 * channels
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


def power_db(p):
    return 10 * np.log10(np.maximum(p, 1e-20))


def mixer_gain_db(card=HAT_CARD):
    """(total mic gain in dB, read live?) from the WM8960's mixer controls."""
    total, live = 0.0, True
    for control, default in GAIN_CONTROLS:
        try:
            out = subprocess.run(["amixer", "-c", card, "sget", control],
                                 capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        m = re.search(r"\[(-?\d+(?:\.\d+)?)dB\]", out)
        if m:
            total += float(m.group(1))
        else:
            total, live = total + default, False
    return total, live


def spl_offset():
    """(offset, how it was obtained) - from --calibrate if it was run, else
    estimated from the mic sensitivity and the current gain settings."""
    try:
        with open(CALIBRATION_FILE) as f:
            return float(f.read().strip()), "calibrated"
    except (OSError, ValueError):
        pass
    gain, live = mixer_gain_db()
    offset = 94.0 - MIC_SENSITIVITY_DBV - gain + ADC_FULL_SCALE_DBV + 3.01
    how = f"estimated from mic gain {gain:+.1f} dB" if live else "estimated from typical gain"
    return offset, how + ", about +/-5 dB - see --calibrate"


def a_weighting(freqs):
    """A-weighting power gains (IEC 61672), as used by sound level meters."""
    f2 = np.maximum(freqs, 1e-3) ** 2
    ra = (12194.0 ** 2 * f2 ** 2) / ((f2 + 20.6 ** 2) * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2))
                                    * (f2 + 12194.0 ** 2))
    return (ra * 10 ** (2.0 / 20)) ** 2


def interpolated_peaks(power, k, rate, n):
    """Frequencies of peak bins k (one per row of power), refined between bins
    with a log-parabolic fit - accurate to well under one bin for a clear tone."""
    power = np.atleast_2d(power)
    k = np.atleast_1d(k)
    kk = np.clip(k, 1, power.shape[1] - 2)
    rows = np.arange(len(k))
    a, b, c = (np.log(power[rows, kk + d] + 1e-30) for d in (-1, 0, 1))
    denom = a - 2 * b + c
    offset = np.divide(0.5 * (a - c), denom, out=np.zeros_like(denom), where=denom < 0)
    offset[kk != k] = 0.0                          # peak at the very edge: no refinement
    return (k + np.clip(offset, -0.5, 0.5)) * rate / n   # a true peak moves < half a bin


def _box(x, length):
    """Centred moving average (edges padded with the end values)."""
    xp = np.pad(x.astype(np.float64), (length // 2, length - 1 - length // 2), mode="edge")
    c = np.concatenate(([0.0], np.cumsum(xp)))
    return (c[length:] - c[:-length]) / length


def highpass(x, rate):
    """Remove DC drift and rumble below ~HIGHPASS_HZ (subtracts a twice-applied
    moving average: -8 dB at 20 Hz, -19 dB at 10 Hz, flat from 50 Hz up).
    Real mics pick up slow pressure changes far stronger than speech, and
    without this they swamp the lowest frequency bins."""
    length = max(3, round(rate * 900 / 48000 * 20 / HIGHPASS_HZ))
    return (x - _box(_box(x, length), length)).astype(np.float32)


def analyze(samples, rate):
    """Volume over time and frequency tracking, FRAME_RATE times per second."""
    samples = np.asarray(samples, dtype=np.float32) if samples.size else np.zeros(1, np.float32)
    samples -= samples.mean()                      # remove DC offset (in place: saves memory)
    duration = samples.size / rate
    n = int(2 ** np.ceil(np.log2(rate * WINDOW_SECONDS)))    # FFT size: 2048 at 48 kHz
    hop = max(1, round(rate / FRAME_RATE))
    if samples.size < n:
        samples = np.pad(samples, (0, n - samples.size))
    window = np.hanning(n)
    norm = n * np.sum(window ** 2)                 # power spectrum -> mean square of frame
    freqs = np.fft.rfftfreq(n, d=1.0 / rate)
    weight = np.full(freqs.size, 2.0)              # one-sided spectrum: double inner bins
    weight[0] = weight[-1] = 1.0
    a_weight = weight * a_weighting(freqs)
    k_min = int(np.searchsorted(freqs, 20.0))      # ignore DC and rumble below 20 Hz

    n_frames = 1 + (samples.size - n) // hop
    margin = 2 * round(rate * 900 / 48000 * 20 / HIGHPASS_HZ)   # filter settling room
    level_z, level_a, peak_f, prominent = [], [], [], []
    spec_sum = np.zeros(freqs.size)
    for f0 in range(0, n_frames, 256):             # in chunks: long files fit in memory
        f1 = min(n_frames, f0 + 256)
        s0, s1 = f0 * hop, (f1 - 1) * hop + n
        a, b = max(0, s0 - margin), min(samples.size, s1 + margin)
        seg = highpass(samples[a:b], rate)[s0 - a:s1 - a]
        frames = np.lib.stride_tricks.sliding_window_view(seg, n)[::hop]
        power = np.abs(np.fft.rfft(frames * window, axis=1)) ** 2
        spec_sum += power.sum(axis=0)
        level_z.append(power @ weight / norm)
        level_a.append(power @ a_weight / norm)
        audible = power[:, k_min:]
        k = np.argmax(audible, axis=1) + k_min
        rows = np.arange(len(k))
        is_peak = (power[rows, k] >= power[rows, k - 1]) & \
                  (power[rows, k] >= power[rows, np.minimum(k + 1, power.shape[1] - 1)])
        peak_f.append(interpolated_peaks(power, k, rate, n))
        prominent.append(is_peak & (power[rows, k] >= np.median(audible, axis=1)
                                    * 10 ** (PROMINENCE_DB / 10)))
    level_z = np.concatenate(level_z)              # mean square per frame (full scale = 1)
    level_a = np.concatenate(level_a)
    peak_f = np.concatenate(peak_f)
    prominent = np.concatenate(prominent)
    t = (np.arange(level_z.size) * hop + n / 2) / rate   # time of each frame's centre

    # --- volume (dB(A) SPL) ---
    offset, level_source = spl_offset()
    leq_a = float(power_db(level_a.mean()))        # energy average of the whole recording
    k_fast = max(1, round(FAST_SECONDS * FRAME_RATE))
    if level_a.size >= k_fast:
        fast = np.convolve(level_a, np.ones(k_fast) / k_fast, mode="valid")
        fast_t = t[:fast.size] + (k_fast - 1) / (2 * FRAME_RATE)
    else:
        fast, fast_t = np.array([level_a.mean()]), t[:1]   # shorter than 1/8 s
    i_lo, i_hi = int(np.argmin(fast)), int(np.argmax(fast))
    peak_dbfs = to_db(float(np.max(np.abs(samples))))

    # --- frequency ---
    loudest = float(power_db(level_z.max()))
    silent = loudest < SILENCE_DBFS
    spectrum = spec_sum / max(1, level_z.size)     # average spectrum of the recording
    audible = spectrum[k_min:]
    # top 3 distinct peaks (true local maxima, at least 50 Hz apart), strongest first;
    # the strongest one is the dominant frequency
    peaks = []
    is_max = np.r_[False, (spectrum[1:-1] > spectrum[:-2]) & (spectrum[1:-1] >= spectrum[2:]), False]
    is_max[:k_min] = False
    for k in np.argsort(spectrum)[::-1]:
        if len(peaks) == 3 or spectrum[k] <= 0 or silent:
            break
        f = float(interpolated_peaks(spectrum, k, rate, n)[0])
        if is_max[k] and f >= HIGHPASS_HZ and all(abs(f - pf) >= 50 for pf, _, _ in peaks):
            peaks.append((f, 0.0, spectrum[k]))
    peaks = [(f, float(power_db(p / peaks[0][2])), p) for f, _, p in peaks]
    dominant = peaks[0][0] if peaks else 0.0
    centroid = float(np.sum(freqs[k_min:] * audible) / np.sum(audible)) \
        if not silent and audible.sum() > 0 else 0.0
    peaks = [(f, db) for f, db, _ in peaks]

    total = audible.sum() or 1.0
    bands = {name: float(spectrum[(freqs >= lo) & (freqs < hi)].sum() / total * 100)
             for name, lo, hi in BANDS}

    # lowest / highest frequency: the main pitch of every loud, tonal, steady frame.
    # "Steady" skips frames that straddle a sudden start/stop (within 1 dB of the
    # loudest frame overlapping them), where the cut smears the pitch estimate.
    reach = max(1, n // (2 * hop))
    padded = np.pad(level_z, reach, mode="edge")
    local_max = np.lib.stride_tricks.sliding_window_view(padded, 2 * reach + 1).max(axis=1)
    steady = level_z >= local_max * 10 ** (-1 / 10)
    tracked = (power_db(level_z) >= max(loudest - GATE_DB, SILENCE_DBFS)) & prominent & steady \
        & (peak_f >= HIGHPASS_HZ)
    if silent or not tracked.any():
        low = high = (0.0, 0.0)
    else:
        idx = np.flatnonzero(tracked)
        lo_i, hi_i = idx[np.argmin(peak_f[idx])], idx[np.argmax(peak_f[idx])]
        low, high = (float(peak_f[lo_i]), float(t[lo_i])), (float(peak_f[hi_i]), float(t[hi_i]))

    avg_spl = max(0.0, leq_a + offset)
    v_idx, v_label = rate_level(avg_spl, VOLUME_LEVELS)
    return {
        "avg_spl": avg_spl, "leq_dbfs": leq_a,
        "min_spl": max(0.0, float(power_db(fast[i_lo])) + offset), "min_t": float(fast_t[i_lo]),
        "max_spl": max(0.0, float(power_db(fast[i_hi])) + offset), "max_t": float(fast_t[i_hi]),
        "peak_dbfs": peak_dbfs, "clipping": peak_dbfs > -0.5, "level_source": level_source,
        "low_freq": low[0], "low_t": low[1], "high_freq": high[0], "high_t": high[1],
        "dominant": dominant, "centroid": centroid, "bands": bands, "peaks": peaks,
        "vol_idx": v_idx, "vol_label": v_label, "silent": silent,
        "rate": rate, "fft_size": n, "frame_rate": rate / hop, "duration": duration,
    }


def level_range(idx, levels, unit):
    """Text for the value range of a rating, e.g. '60 to 70 dB'."""
    lo = levels[idx - 1][0] if idx > 0 else None
    hi = levels[idx][0]
    if lo is None:
        return f"below {hi:g} {unit}"
    if hi == float("inf"):
        return f"{lo:g} {unit} and above"
    return f"{lo:g} to {hi:g} {unit}"


def meter(fraction, width=30):
    """Bar positioned by the exact value (0.0 = lowest, 1.0 = highest)."""
    fraction = min(max(fraction, 0.0), 1.0)
    pos = round(fraction * (width - 1))
    bar = "".join("#" if i <= pos else "-" for i in range(width))
    return f"LOW [{bar}] HIGH  ({fraction * 100:.0f}/100)"


def report(r):
    """Print the analysis and return it as text."""
    lines = []
    out = lines.append
    vol_frac = (r["avg_spl"] - 20) / 90            # meter: 20 dB (silence) .. 110 dB
    nyquist = r["rate"] / 2

    out("\n" + "=" * 64)
    out(" VOLUME  (dB(A) sound level, like a sound meter)")
    out(f"   Accuracy                 : {r['level_source']}")
    out(f"   Average (whole recording): {r['avg_spl']:6.1f} dB")
    out(f"   Lowest  (quietest 1/8 s) : {r['min_spl']:6.1f} dB   at {r['min_t']:.2f} s")
    out(f"   Highest (loudest 1/8 s)  : {r['max_spl']:6.1f} dB   at {r['max_t']:.2f} s")
    out(f"   Rating                   : {r['vol_label']}  (level {r['vol_idx'] + 1} of "
        f"{len(VOLUME_LEVELS)}, {level_range(r['vol_idx'], VOLUME_LEVELS, 'dB')})")
    out(f"   {meter(vol_frac)}  {r['avg_spl']:.1f} dB")
    if r["clipping"]:
        out("   WARNING: the sound was too loud for the mics (clipping) - readings are low.")
    out(f" FREQUENCY  ({r['frame_rate']:.0f} analyses/s, {r['fft_size']}-point FFT, "
        f"{r['rate']} Hz sample rate)")
    if r["silent"]:
        out("   Too quiet to measure frequency reliably.")
    else:
        out(f"   Lowest freq    : {r['low_freq']:8.1f} Hz   at {r['low_t']:.2f} s")
        out(f"   Highest freq   : {r['high_freq']:8.1f} Hz   at {r['high_t']:.2f} s")
        out(f"   Dominant freq  : {r['dominant']:8.1f} Hz")
        out(f"   Spectral center: {r['centroid']:8.1f} Hz")
        out("   Top peaks      : " +
            ", ".join(f"{f:.1f} Hz ({db:+.1f} dB)" for f, db in r["peaks"]))
        out("   Band energy    :")
        for name, lo, hi in BANDS:
            hi = min(hi, nyquist)
            out(f"     {name:<5}({lo:>5}-{hi:<5g} Hz): {r['bands'][name]:5.1f}%")
    out("=" * 64)
    text = "\n".join(lines)
    print(text)
    return text


def save_results(text, source):
    """Append a report to RESULTS_FILE with the time and the analyzed file."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(os.path.dirname(RESULTS_FILE) or ".", exist_ok=True)
        with open(RESULTS_FILE, "a") as f:
            f.write(f"\n### {stamp}  {source}{text}\n")
    except OSError as e:
        print(f"Could not add the results to {RESULTS_FILE}: {e}")
        return
    print(f"Results added to: {RESULTS_FILE}")


def volume_color(idx):
    """Green (quiet) -> yellow -> red (loud)."""
    t = idx / (len(VOLUME_LEVELS) - 1)
    return (int(255 * min(1, 2 * t)), int(255 * min(1, 2 * (1 - t))), 0)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def capture_and_analyze(device, leds):
    path = recording_path("rec")

    # Red countdown while recording: 3 LEDs lit, then 2, then 1
    count = leds.count
    lit_now = [count]

    def countdown(elapsed):
        lit = max(1, count - int(elapsed * count / RECORD_SECONDS))
        if lit != lit_now[0]:
            lit_now[0] = lit
            leds.show((255, 0, 0), lit=lit)

    leds.show((255, 0, 0), lit=count)              # red = recording
    print(f"Recording {RECORD_SECONDS}s ...")
    try:
        record(path, device, on_tick=countdown)
    except BaseException:
        leds.off()                                 # don't leave the LEDs stuck on red
        raise
    leds.show((0, 0, 255))                         # blue = analyzing
    samples, rate = load_wav(path)
    result = analyze(samples, rate)
    text = report(result)
    leds.show(volume_color(result["vol_idx"]))
    print(f"Saved: {path}")
    save_results(text, path)
    return result


SINGLE_TAP = "single"
DOUBLE_TAP = "double"


class TapDetector:
    """Turns debounced button levels + time.monotonic() stamps into taps.

    Feed it every poll with update(pressed, now); it returns SINGLE_TAP,
    DOUBLE_TAP or None.

      IDLE     --press-->                               DOWN1
      DOWN1    --release-->                             WAIT2 (window starts)
      WAIT2    --press, gap <= window--> DOUBLE_TAP,    DOWN2
      WAIT2    --no press, gap > window--> SINGLE_TAP,  IDLE
      DOWN2    --release-->                             AFTER2 (window restarts)
      AFTER2   --press, gap <= window-->                DOWN2  (extra tap absorbed)
      AFTER2   --no press, gap > window-->              IDLE
      HELD     --release-->                             IDLE   (see reset(True))

    A double tap is reported as soon as the second press starts. Extra taps
    in the same quick burst (a triple tap) are absorbed, so they can't
    trigger a stray single tap. A press held down (even stuck LOW forever)
    produces nothing until it is released.
    """
    IDLE, DOWN1, WAIT2, DOWN2, AFTER2, HELD = (
        "IDLE", "DOWN1", "WAIT2", "DOWN2", "AFTER2", "HELD")

    def __init__(self, window=DOUBLE_TAP_WINDOW):
        self.window = window
        self.state = self.IDLE
        self.released_at = None

    def reset(self, pressed):
        """Forget any tap in progress. If the button is down right now, that
        press is ignored until it is released."""
        self.state = self.HELD if pressed else self.IDLE
        self.released_at = None

    def update(self, pressed, now):
        st = self.state
        if st in (self.WAIT2, self.AFTER2):
            within = now - self.released_at <= self.window
            if pressed and within:
                self.state = self.DOWN2
                return DOUBLE_TAP if st == self.WAIT2 else None
            if not within:                         # window expired
                self.state = self.DOWN1 if pressed else self.IDLE
                return SINGLE_TAP if st == self.WAIT2 else None
        elif st == self.IDLE:
            if pressed:
                self.state = self.DOWN1
        elif not pressed:                          # DOWN1 / DOWN2 / HELD released
            self.state = {self.DOWN1: self.WAIT2, self.DOWN2: self.AFTER2,
                          self.HELD: self.IDLE}[st]
            self.released_at = now
        return None


def _read_pressed(GPIO, was_pressed):
    """Debounced button state: a change only counts if it is still there 30 ms later."""
    pressed = GPIO.input(BUTTON_PIN) == GPIO.LOW
    if pressed != was_pressed:
        time.sleep(DEBOUNCE)
        pressed = GPIO.input(BUTTON_PIN) == GPIO.LOW
    return pressed


def _begin_session(device, leds):
    try:
        session = start_session(device)
    except OSError as e:
        print(f"Could not start recording: {e}")
        return None
    leds.show(SESSION_COLOR)
    print("Session recording started - double-tap to stop and save.")
    return session


def _end_session(session, leds):
    proc, path = session
    try:
        duration = stop_session(proc, path)
        print(f"Saved: {path} ({duration:.1f} s)")
    except (RuntimeError, OSError) as e:
        print(e)
    leds.off()


def _session_died(session, leds):
    """arecord exited without being asked to (e.g. device error)."""
    proc, path = session
    err = session_stderr(proc)
    print(f"Session recording stopped unexpectedly (arecord exit code {proc.returncode})"
          + (f": {err}" if err else "."))
    try:
        duration = stop_session(proc, path)       # already exited: just finalize the file
        print(f"Saved: {path} ({duration:.1f} s)")
    except (RuntimeError, OSError):
        print("No audio was saved.")
    leds.off()


def button_loop(device, leds):
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    print(f"Using audio device {device}. Tap the button to record {RECORD_SECONDS}s "
          f"and analyze; double-tap to start/stop a recording without analysis "
          f"(Ctrl+C to quit).\nRecordings are saved to {SAVE_DIR}")
    taps = TapDetector()
    pressed = False
    session = None                                 # (proc, path) while recording
    try:
        while True:
            if session is not None and session[0].poll() is not None:
                _session_died(session, leds)
                session = None
                print("\nPress the button to record again.")
            pressed = _read_pressed(GPIO, pressed)
            tap = taps.update(pressed, time.monotonic())
            if tap == DOUBLE_TAP:
                if session is None:
                    session = _begin_session(device, leds)
                else:
                    _end_session(session, leds)
                    session = None
                    print("\nPress the button to record again.")
            elif tap == SINGLE_TAP:
                if session is not None:
                    print("Recording... double-tap to stop.")
                else:
                    try:
                        capture_and_analyze(device, leds)
                    except RuntimeError as e:
                        print(e)
                    print("\nPress the button to record again.")
                    # taps during the recording don't count; a press still
                    # down now is ignored until released
                    pressed = GPIO.input(BUTTON_PIN) == GPIO.LOW
                    taps.reset(pressed)
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        if session is not None:
            _end_session(session, leds)            # save the session before quitting
            session = None
        print("\nBye.")
    finally:
        if session is not None:                    # unexpected error: don't orphan arecord
            _end_session(session, leds)
        leds.off()
        GPIO.cleanup()


# --------------------------------------------------------------------------- #
# Playback
# --------------------------------------------------------------------------- #
def newest_recording():
    """Path of the most recently saved WAV in SAVE_DIR."""
    try:
        files = [os.path.join(SAVE_DIR, f) for f in os.listdir(SAVE_DIR) if f.endswith(".wav")]
    except FileNotFoundError:
        files = []
    if not files:
        raise RuntimeError(f"No recordings in {SAVE_DIR} yet.")
    return max(files, key=os.path.getmtime)


def play_recording(name, device):
    """Play a recording (a path, a file name in SAVE_DIR, or the newest one)."""
    path = name or newest_recording()
    if not os.path.isfile(path) and os.path.isfile(os.path.join(SAVE_DIR, path)):
        path = os.path.join(SAVE_DIR, path)
    if not os.path.isfile(path):
        raise RuntimeError(f"No such recording: {name}")
    with wave.open(path, "rb") as wf:
        seconds = wf.getnframes() / wf.getframerate()
    print(f"Playing {path} ({seconds:.1f} s) - Ctrl+C to stop")
    try:
        result = subprocess.run(["aplay", "-q", "-D", device, path],
                                stderr=subprocess.PIPE, text=True)
    except KeyboardInterrupt:
        print("\nStopped.")
        return
    if result.returncode != 0:
        raise RuntimeError(f"aplay failed: {result.stderr.strip()}")


def calibrate(meter_db, device, leds, wav=None):
    """Make reports match a sound level meter: record (or load) a steady sound
    the meter reads as `meter_db` dB(A) and save the dBFS -> dB SPL offset."""
    if wav:
        samples, rate = load_wav(wav)
    else:
        print("Hold a sound level meter next to the HAT and make a steady sound")
        print(f"(e.g. music or a fan) that the meter reads as {meter_db:g} dB(A).")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "calibrate.wav")
            leds.show((255, 0, 0))
            print(f"Recording {RECORD_SECONDS}s ...")
            try:
                record(path, device)
            finally:
                leds.off()
            samples, rate = load_wav(path)
    result = analyze(samples, rate)
    if result["silent"]:
        raise RuntimeError("Calibration sound was too quiet - make it louder and try again.")
    if result["clipping"]:
        raise RuntimeError("Calibration sound was too loud for the mics - make it quieter.")
    offset = meter_db - result["leq_dbfs"]
    os.makedirs(os.path.dirname(CALIBRATION_FILE), exist_ok=True)
    with open(CALIBRATION_FILE, "w") as f:
        f.write(f"{offset:.2f}\n")
    print(f"Calibrated: offset {offset:.1f} dB saved to {CALIBRATION_FILE}.")
    print("Volume readings now match your meter.")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", help="analyze an existing 16-bit WAV instead of recording")
    p.add_argument("--now", action="store_true", help="record once without the button")
    p.add_argument("--play", nargs="?", const="", metavar="FILE",
                   help="play a recording through the HAT (default: the newest one)")
    p.add_argument("--calibrate", type=float, metavar="DB",
                   help="match a sound level meter: record a steady sound the meter "
                        "reads as DB dB(A) (or use --file) and save the correction")
    p.add_argument("--device", help="ALSA device (default: auto-detect ReSpeaker)")
    args = p.parse_args()

    try:
        if args.play is not None:
            play_recording(args.play, args.device or find_card())
            return

        if args.calibrate is not None:
            device = None if args.file else (args.device or find_card())
            calibrate(args.calibrate, device, Leds(), wav=args.file)
            return

        if args.file:
            save_results(report(analyze(*load_wav(args.file))), os.path.abspath(args.file))
            return

        device = args.device or find_card()
        leds = Leds()
        if args.now:
            capture_and_analyze(device, leds)
        else:
            button_loop(device, leds)
    except (RuntimeError, OSError, ValueError, EOFError, wave.Error) as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
