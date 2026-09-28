"""
Unit tests for the analysis math and report output in respeaker_analyzer.py.

Scope (per task): load_wav, rate_level, to_db, analyze, level_range, meter,
rating_line, report, volume_color, and the --file CLI path.

Run from the repo root with:
    python3 -m unittest tests.test_analysis -v

Only stdlib unittest + numpy are used (no pytest, matching the target Pi).
"""
import contextlib
import io
import math
import os
import subprocess
import sys
import tempfile
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import respeaker_analyzer as ra


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.join(REPO_ROOT, "respeaker_analyzer.py")


# --------------------------------------------------------------------------- #
# WAV helpers
# --------------------------------------------------------------------------- #
def make_tone(freq, rate, duration=1.0, amplitude=1.0, phase=0.0):
    n = int(rate * duration)
    t = np.arange(n) / rate
    return amplitude * np.sin(2 * np.pi * freq * t + phase)


def write_wav_16bit(path, samples, rate, channels=1):
    """samples: float array in [-1, 1]. If channels==2 and samples is 1-D,
    the same signal is duplicated to both channels."""
    samples = np.asarray(samples, dtype=np.float64)
    if channels == 2 and samples.ndim == 1:
        stereo = np.repeat(samples.reshape(-1, 1), 2, axis=1)
    else:
        stereo = samples
    ints = np.clip(np.round(stereo * 32767), -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(ints.tobytes())


def write_wav_stereo_channels(path, left, right, rate):
    left = np.asarray(left, dtype=np.float64)
    right = np.asarray(right, dtype=np.float64)
    stereo = np.stack([left, right], axis=1)
    ints = np.clip(np.round(stereo * 32767), -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(ints.tobytes())


def write_wav_width(path, int_values, rate, channels, sampwidth):
    """Write a WAV with an arbitrary sample width (1, 2, or 3 bytes) so we
    can test load_wav's rejection of non-16-bit files."""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        if sampwidth == 1:
            raw = np.asarray(int_values, dtype=np.uint8).tobytes()
        elif sampwidth == 2:
            raw = np.asarray(int_values, dtype=np.int16).tobytes()
        elif sampwidth == 3:
            raw = bytearray()
            for v in int_values:
                raw += int(v & 0xFFFFFF).to_bytes(3, "little", signed=False)
            raw = bytes(raw)
        else:
            raise ValueError("unsupported width")
        wf.writeframes(raw)


# --------------------------------------------------------------------------- #
# rate_level
# --------------------------------------------------------------------------- #
class RateLevelTests(unittest.TestCase):
    def test_freq_every_boundary_goes_to_next_level(self):
        # rate_level uses strict '<', so a value exactly at a bound belongs
        # to the NEXT tier, not the one the bound nominally caps.
        bounds_and_next_label = [
            (60, "LOW"),
            (250, "MID-LOW"),
            (1000, "MID"),
            (2000, "MID-HIGH"),
            (4000, "HIGH"),
            (6000, "VERY HIGH"),
        ]
        for bound, expected_label in bounds_and_next_label:
            idx, label = ra.rate_level(bound, ra.FREQ_LEVELS)
            self.assertEqual(label, expected_label,
                              f"value={bound} should land in {expected_label!r}")

    def test_freq_just_below_boundary_stays_in_current_level(self):
        cases = [
            (59.999, "VERY LOW"),
            (249.999, "LOW"),
            (999.999, "MID-LOW"),
            (1999.999, "MID"),
            (3999.999, "MID-HIGH"),
            (5999.999, "HIGH"),
        ]
        for value, expected_label in cases:
            _, label = ra.rate_level(value, ra.FREQ_LEVELS)
            self.assertEqual(label, expected_label)

    def test_freq_inf_top_level(self):
        idx, label = ra.rate_level(1e9, ra.FREQ_LEVELS)
        self.assertEqual(idx, 6)
        self.assertEqual(label, "VERY HIGH")

    def test_volume_every_boundary_goes_to_next_level(self):
        bounds_and_next_label = [
            (-60, "VERY QUIET"),
            (-45, "QUIET"),
            (-35, "MODERATE"),
            (-25, "LOUD"),
            (-15, "VERY LOUD"),
            (-6, "EXTREME / CLIPPING"),
        ]
        for bound, expected_label in bounds_and_next_label:
            idx, label = ra.rate_level(bound, ra.VOLUME_LEVELS)
            self.assertEqual(label, expected_label)

    def test_volume_inf_top_level(self):
        idx, label = ra.rate_level(1e6, ra.VOLUME_LEVELS)
        self.assertEqual(idx, 6)
        self.assertEqual(label, "EXTREME / CLIPPING")

    def test_volume_very_negative_is_silent(self):
        idx, label = ra.rate_level(-1000, ra.VOLUME_LEVELS)
        self.assertEqual(idx, 0)
        self.assertEqual(label, "SILENT")


# --------------------------------------------------------------------------- #
# to_db
# --------------------------------------------------------------------------- #
class ToDbTests(unittest.TestCase):
    def test_full_scale_is_zero_db(self):
        self.assertAlmostEqual(ra.to_db(1.0), 0.0, places=6)

    def test_half_scale_is_about_minus6(self):
        self.assertAlmostEqual(ra.to_db(0.5), -6.0206, places=3)

    def test_zero_floors_to_minus100(self):
        self.assertEqual(ra.to_db(0.0), -100.0)

    def test_tiny_value_floors_to_minus100(self):
        self.assertEqual(ra.to_db(1e-11), -100.0)

    def test_just_above_floor_threshold_uses_log(self):
        # 1e-10 is the exact floor cutoff (x > 1e-10 required); just above it
        # should use the real log10 formula, not the floor.
        val = ra.to_db(1e-9)
        self.assertNotEqual(val, -100.0)
        self.assertAlmostEqual(val, 20 * math.log10(1e-9), places=3)


# --------------------------------------------------------------------------- #
# analyze: amplitude -> dBFS
# --------------------------------------------------------------------------- #
class AnalyzeAmplitudeTests(unittest.TestCase):
    RATE = 16000

    def test_full_scale_sine_rms_and_peak(self):
        tone = make_tone(440, self.RATE, duration=1.0, amplitude=1.0)
        r = ra.analyze(tone, self.RATE)
        self.assertAlmostEqual(r["rms_db"], -3.0103, places=2)
        self.assertAlmostEqual(r["peak_db"], 0.0, places=2)

    def test_half_scale_sine_rms(self):
        tone = make_tone(440, self.RATE, duration=1.0, amplitude=0.5)
        r = ra.analyze(tone, self.RATE)
        self.assertAlmostEqual(r["rms_db"], -9.0309, places=2)

    def test_volume_label_thresholds_with_real_signals(self):
        # amplitude -> rms_db = 20*log10(amp) - 3.0103 (sine crest factor)
        cases = [
            (0.00001, "SILENT"),      # rms_db ~ -103.0 dBFS
            (0.003, "VERY QUIET"),    # rms_db ~  -53.5 dBFS
            (0.02, "QUIET"),          # rms_db ~  -37.0 dBFS
            (0.045, "MODERATE"),      # rms_db ~  -30.0 dBFS
            (0.14, "LOUD"),           # rms_db ~  -20.1 dBFS
            (0.45, "VERY LOUD"),      # rms_db ~  -10.0 dBFS
            (1.0, "EXTREME / CLIPPING"),  # rms_db ~ -3.0 dBFS
        ]
        for amp, expected_label in cases:
            tone = make_tone(440, self.RATE, duration=1.0, amplitude=amp)
            r = ra.analyze(tone, self.RATE)
            self.assertEqual(r["vol_label"], expected_label,
                              f"amp={amp} rms_db={r['rms_db']:.2f} "
                              f"expected {expected_label!r} got {r['vol_label']!r}")

    def test_spl_matches_rms_plus_offset(self):
        tone = make_tone(440, self.RATE, duration=1.0, amplitude=1.0)
        r = ra.analyze(tone, self.RATE)
        self.assertAlmostEqual(r["spl"], r["rms_db"] + ra.SPL_OFFSET, places=6)

    def test_spl_clamped_at_zero_for_silence(self):
        sil = np.zeros(self.RATE)
        r = ra.analyze(sil, self.RATE)
        self.assertGreaterEqual(r["spl"], 0.0)
        self.assertEqual(r["spl"], 0.0)


# --------------------------------------------------------------------------- #
# analyze: silence
# --------------------------------------------------------------------------- #
class AnalyzeSilenceTests(unittest.TestCase):
    def test_silence_all_zero_no_crash(self):
        sil = np.zeros(16000)
        r = ra.analyze(sil, 16000)
        self.assertTrue(r["silent"])
        self.assertEqual(r["vol_label"], "SILENT")
        self.assertEqual(r["dominant"], 0.0)
        self.assertEqual(r["rms_db"], -100.0)

    def test_silence_report_prints_too_quiet(self):
        sil = np.zeros(16000)
        r = ra.analyze(sil, 16000)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ra.report(r)
        out = buf.getvalue()
        self.assertIn("Too quiet", out)
        self.assertNotIn("Dominant freq", out)

    def test_silence_no_numpy_warnings_escalate(self):
        sil = np.zeros(16000)
        with np.errstate(all="raise"):
            # analyze() should not need errstate suppression to avoid
            # divide-by-zero / invalid-value floating point exceptions
            # for legitimate silent input.
            r = ra.analyze(sil, 16000)
        self.assertTrue(r["silent"])


# --------------------------------------------------------------------------- #
# analyze: DC offset / rumble
# --------------------------------------------------------------------------- #
class AnalyzeDcRumbleTests(unittest.TestCase):
    RATE = 16000

    def test_dc_offset_removed_dominant_is_tone_not_zero(self):
        tone = make_tone(440, self.RATE, duration=1.0, amplitude=0.1)
        with_dc = tone + 5.0  # large DC bias
        r = ra.analyze(with_dc, self.RATE)
        self.assertAlmostEqual(r["dominant"], 440.0, delta=2.0)
        self.assertNotEqual(r["dominant"], 0.0)

    def test_rumble_below_20hz_ignored(self):
        t = np.arange(self.RATE) / self.RATE
        rumble = 0.9 * np.sin(2 * np.pi * 10 * t) + 0.05 * np.sin(2 * np.pi * 440 * t)
        r = ra.analyze(rumble, self.RATE)
        self.assertAlmostEqual(r["dominant"], 440.0, delta=2.0)


# --------------------------------------------------------------------------- #
# analyze: dominant frequency across the range and bands / labels
# --------------------------------------------------------------------------- #
class AnalyzeFrequencyTests(unittest.TestCase):
    def test_pure_tones_16khz(self):
        rate = 16000
        cases = [
            (50, "VERY LOW"),
            (120, "LOW"),
            (440, "MID-LOW"),
            (1000, "MID"),          # exact boundary -> next tier
            (3000, "MID-HIGH"),
            (5000, "HIGH"),
            (7000, "VERY HIGH"),
        ]
        for freq, expected_label in cases:
            tone = make_tone(freq, rate, duration=1.0, amplitude=0.8)
            r = ra.analyze(tone, rate)
            self.assertAlmostEqual(r["dominant"], float(freq), delta=2.0,
                                    msg=f"freq={freq}")
            self.assertEqual(r["freq_label"], expected_label,
                              f"freq={freq} got {r['freq_label']!r}")
            total = sum(r["bands"].values())
            self.assertAlmostEqual(total, 100.0, delta=0.5,
                                    msg=f"band sum for freq={freq}")

    def test_pure_tone_44100(self):
        rate = 44100
        tone = make_tone(440, rate, duration=1.0, amplitude=0.8)
        r = ra.analyze(tone, rate)
        self.assertAlmostEqual(r["dominant"], 440.0, delta=2.0)

    def test_pure_tone_48000(self):
        rate = 48000
        tone = make_tone(440, rate, duration=1.0, amplitude=0.8)
        r = ra.analyze(tone, rate)
        self.assertAlmostEqual(r["dominant"], 440.0, delta=2.0)

    def test_band_membership_low_mid_high(self):
        rate = 16000
        low_tone = make_tone(100, rate, duration=1.0, amplitude=0.8)
        mid_tone = make_tone(1000, rate, duration=1.0, amplitude=0.8)
        high_tone = make_tone(5000, rate, duration=1.0, amplitude=0.8)
        r_low = ra.analyze(low_tone, rate)
        r_mid = ra.analyze(mid_tone, rate)
        r_high = ra.analyze(high_tone, rate)
        self.assertGreater(r_low["bands"]["Low"], 90)
        self.assertGreater(r_mid["bands"]["Mid"], 90)
        self.assertGreater(r_high["bands"]["High"], 90)


# --------------------------------------------------------------------------- #
# analyze: top peaks
# --------------------------------------------------------------------------- #
class AnalyzeTopPeaksTests(unittest.TestCase):
    def test_three_tone_chord_returns_three_distinct_peaks(self):
        rate = 16000
        t = np.arange(rate) / rate
        chord = (0.5 * np.sin(2 * np.pi * 300 * t)
                 + 0.3 * np.sin(2 * np.pi * 1000 * t)
                 + 0.2 * np.sin(2 * np.pi * 3000 * t))
        r = ra.analyze(chord, rate)
        self.assertEqual(len(r["peaks"]), 3)
        freqs = [f for f, _ in r["peaks"]]
        # strongest first
        self.assertAlmostEqual(freqs[0], 300.0, delta=2.0)
        self.assertAlmostEqual(freqs[1], 1000.0, delta=2.0)
        self.assertAlmostEqual(freqs[2], 3000.0, delta=2.0)
        # first peak is the reference -> 0.0 dB relative
        self.assertAlmostEqual(r["peaks"][0][1], 0.0, places=3)
        # peaks are strongest-first (non-increasing relative dB)
        dbs = [db for _, db in r["peaks"]]
        self.assertEqual(dbs, sorted(dbs, reverse=True))
        # peaks are >= 30 Hz apart
        for i in range(len(freqs)):
            for j in range(i + 1, len(freqs)):
                self.assertGreaterEqual(abs(freqs[i] - freqs[j]), 30)

    def test_single_tone_has_one_dominant_peak_at_zero_db(self):
        rate = 16000
        tone = make_tone(440, rate, duration=1.0, amplitude=0.8)
        r = ra.analyze(tone, rate)
        self.assertGreaterEqual(len(r["peaks"]), 1)
        self.assertAlmostEqual(r["peaks"][0][0], 440.0, delta=2.0)
        self.assertAlmostEqual(r["peaks"][0][1], 0.0, places=3)


# --------------------------------------------------------------------------- #
# analyze: short / empty input (robustness)
# --------------------------------------------------------------------------- #
class AnalyzeEdgeInputTests(unittest.TestCase):
    def test_very_short_input_does_not_crash(self):
        samples = np.random.RandomState(0).uniform(-0.5, 0.5, 10)
        r = ra.analyze(samples, 16000)
        self.assertIn("dominant", r)
        self.assertIn("bands", r)

    def test_single_sample_does_not_crash(self):
        samples = np.zeros(1)
        r = ra.analyze(samples, 16000)
        self.assertEqual(r["dominant"], 0.0)


class AnalyzeEmptyInputTest(unittest.TestCase):
    def test_empty_input_should_not_crash(self):
        # A 0-frame WAV (empty/truncated recording) must degrade to "silent"
        # instead of raising from the FFT.
        empty = np.array([], dtype=np.float64)
        result = ra.analyze(empty, 16000)  # should not raise
        self.assertTrue(result["silent"])


# --------------------------------------------------------------------------- #
# load_wav
# --------------------------------------------------------------------------- #
class LoadWavTests(unittest.TestCase):
    def test_mono_roundtrip_normalization(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "mono.wav")
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(np.array([32767, -32768, 0, 16384],
                                         dtype=np.int16).tobytes())
            data, rate = ra.load_wav(path)
            self.assertEqual(rate, 16000)
            np.testing.assert_allclose(
                data, [32767 / 32768.0, -1.0, 0.0, 0.5], atol=1e-9)

    def test_stereo_channels_averaged_to_mono(self):
        rate = 16000
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "stereo.wav")
            tone = make_tone(440, rate, duration=0.5, amplitude=1.0)
            write_wav_stereo_channels(path, tone, np.zeros_like(tone), rate)
            data, r2 = ra.load_wav(path)
            # left=tone, right=0 -> mono = tone/2 (approximately, modulo
            # int16 quantization)
            expected = tone / 2.0
            np.testing.assert_allclose(data, expected, atol=2e-4)

    def test_rejects_8bit_wav(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "eight.wav")
            write_wav_width(path, [0, 128, 255, 64], 16000, 1, 1)
            with self.assertRaises(ValueError):
                ra.load_wav(path)

    def test_rejects_24bit_wav(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tf.wav")
            write_wav_width(path, [0, 1000, -1000, 500000], 16000, 1, 3)
            with self.assertRaises(ValueError):
                ra.load_wav(path)

    def test_accepts_16bit_wav(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ok.wav")
            write_wav_width(path, [0, 100, -100], 16000, 1, 2)
            data, rate = ra.load_wav(path)  # should not raise
            self.assertEqual(len(data), 3)


# --------------------------------------------------------------------------- #
# level_range
# --------------------------------------------------------------------------- #
class LevelRangeTests(unittest.TestCase):
    def test_first_level_freq(self):
        self.assertEqual(ra.level_range(0, ra.FREQ_LEVELS, "Hz"), "below 60 Hz")

    def test_middle_level_freq(self):
        self.assertEqual(ra.level_range(3, ra.FREQ_LEVELS, "Hz"), "1000 to 2000 Hz")

    def test_last_level_freq(self):
        self.assertEqual(ra.level_range(6, ra.FREQ_LEVELS, "Hz"), "6000 Hz and above")

    def test_first_level_volume(self):
        self.assertEqual(ra.level_range(0, ra.VOLUME_LEVELS, "dBFS"), "below -60 dBFS")

    def test_last_level_volume(self):
        self.assertEqual(ra.level_range(6, ra.VOLUME_LEVELS, "dBFS"),
                          "-6 dBFS and above")

    def test_middle_level_volume(self):
        self.assertEqual(ra.level_range(3, ra.VOLUME_LEVELS, "dBFS"),
                          "-35 to -25 dBFS")


# --------------------------------------------------------------------------- #
# meter
# --------------------------------------------------------------------------- #
class MeterTests(unittest.TestCase):
    def test_clamps_below_zero(self):
        self.assertEqual(ra.meter(-5.0), ra.meter(0.0))

    def test_clamps_above_one(self):
        self.assertEqual(ra.meter(5.0), ra.meter(1.0))

    def test_zero_fraction_score(self):
        self.assertIn("(0/100)", ra.meter(0.0))

    def test_full_fraction_score(self):
        self.assertIn("(100/100)", ra.meter(1.0))

    def test_half_fraction_score(self):
        self.assertIn("(50/100)", ra.meter(0.5))

    def test_bar_width_default(self):
        text = ra.meter(0.5)
        bar = text.split("[")[1].split("]")[0]
        self.assertEqual(len(bar), 30)

    def test_bar_all_hash_at_one(self):
        text = ra.meter(1.0)
        bar = text.split("[")[1].split("]")[0]
        self.assertEqual(bar, "#" * 30)

    def test_bar_starts_with_hash_at_zero(self):
        text = ra.meter(0.0)
        bar = text.split("[")[1].split("]")[0]
        self.assertEqual(bar[0], "#")
        self.assertTrue(all(c == "-" for c in bar[1:]))


# --------------------------------------------------------------------------- #
# rating_line
# --------------------------------------------------------------------------- #
class RatingLineTests(unittest.TestCase):
    def test_contains_level_n_of_7(self):
        line = ra.rating_line(2, "MID-LOW", ra.FREQ_LEVELS, "Hz")
        self.assertIn("level 3 of 7", line)
        self.assertIn("MID-LOW", line)
        self.assertIn("250 to 1000 Hz", line)

    def test_volume_rating_line(self):
        line = ra.rating_line(0, "SILENT", ra.VOLUME_LEVELS, "dBFS")
        self.assertIn("level 1 of 7", line)
        self.assertIn("SILENT", line)
        self.assertIn("below -60 dBFS", line)


# --------------------------------------------------------------------------- #
# volume_color
# --------------------------------------------------------------------------- #
class VolumeColorTests(unittest.TestCase):
    def test_endpoints_green_to_red(self):
        self.assertEqual(ra.volume_color(0), (0, 255, 0))
        self.assertEqual(ra.volume_color(6), (255, 0, 0))

    def test_midpoint_is_yellowish(self):
        r, g, b = ra.volume_color(3)
        self.assertEqual(b, 0)
        self.assertGreater(r, 0)
        self.assertGreater(g, 0)

    def test_all_components_are_ints_in_range(self):
        for i in range(7):
            rgb = ra.volume_color(i)
            for c in rgb:
                self.assertIsInstance(c, int)
                self.assertGreaterEqual(c, 0)
                self.assertLessEqual(c, 255)


# --------------------------------------------------------------------------- #
# report (stdout content)
# --------------------------------------------------------------------------- #
class ReportOutputTests(unittest.TestCase):
    def test_report_contains_exact_numbers(self):
        rate = 16000
        tone = make_tone(440, rate, duration=1.0, amplitude=1.0)
        r = ra.analyze(tone, rate)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ra.report(r)
        out = buf.getvalue()
        self.assertIn(f"{r['rms_db']:7.1f} dBFS", out)
        self.assertIn(f"{r['peak_db']:7.1f} dBFS", out)
        self.assertIn(f"{r['spl']:7.1f} dB (approx.)", out)
        self.assertIn(f"{r['dominant']:7.1f} Hz", out)
        self.assertIn(f"{r['centroid']:7.1f} Hz", out)
        self.assertIn("level", out)
        self.assertIn("of 7", out)
        self.assertIn("/100)", out)
        self.assertIn("440.0 Hz", out)
        for name, lo, hi in ra.BANDS:
            self.assertIn(f"{r['bands'][name]:5.1f}%", out)

    def test_report_silent_case_has_no_frequency_numbers(self):
        r = ra.analyze(np.zeros(16000), 16000)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ra.report(r)
        out = buf.getvalue()
        self.assertIn("Too quiet", out)
        self.assertNotIn("Top peaks", out)


# --------------------------------------------------------------------------- #
# --file CLI path
# --------------------------------------------------------------------------- #
class CliFilePathTests(unittest.TestCase):
    def test_cli_file_flag_prints_expected_fields(self):
        rate = 16000
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t440.wav")
            tone = make_tone(440, rate, duration=1.0, amplitude=1.0)
            write_wav_16bit(path, tone, rate, channels=2)
            proc = subprocess.run(
                [sys.executable, SCRIPT_PATH, "--file", path],
                capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
            )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        out = proc.stdout
        self.assertIn("440.0 Hz", out)
        self.assertIn("level", out)
        self.assertIn("of 7", out)
        self.assertIn("/100)", out)
        self.assertIn("dBFS", out)

    def test_cli_file_flag_silence(self):
        rate = 16000
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "silence.wav")
            write_wav_16bit(path, np.zeros(rate), rate, channels=1)
            proc = subprocess.run(
                [sys.executable, SCRIPT_PATH, "--file", path],
                capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
            )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("Too quiet", proc.stdout)
        self.assertIn("SILENT", proc.stdout)

    def test_cli_file_flag_nonexistent_file_fails_gracefully(self):
        proc = subprocess.run(
            [sys.executable, SCRIPT_PATH, "--file", "/nonexistent/does_not_exist.wav"],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
        )
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
