"""
Hardware-facing tests for respeaker_analyzer.py.

Scope (per the task split): find_card, record, Leds, capture_and_analyze,
button_loop (single tap, double-tap sessions, TapDetector), the session
recorder (start_session/stop_session/finalize_wav), SAVE_DIR, and main()'s
argument handling. The pure analysis math (analyze/report/rate_level/...) is
out of scope here.

No real hardware is used or required:
  * arecord      -> tests/fakes/bin/arecord (a real, executable fake that
                     answers `-l`, writes real WAV files, and without `-d`
                     records until SIGINT/SIGTERM like the real one)
  * RPi.GPIO     -> tests/fakes/RPi/GPIO.py, injected via sys.modules
  * time         -> button_loop tests patch time.monotonic/time.sleep with a
                     FakeClock, so tap timing is exact and nothing really waits
  * spidev       -> tests/fakes/spidev.py, injected via sys.modules

Run with:  python3 -m unittest tests.test_hardware -v
"""
import contextlib
import importlib.util
import io
import os
import pathlib
import sys
import tempfile
import types
import unittest
import wave
from unittest import mock

TESTS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
FAKES_DIR = TESTS_DIR / "fakes"
FAKE_BIN_DIR = FAKES_DIR / "bin"

sys.path.insert(0, str(REPO_ROOT))
import respeaker_analyzer as ra  # noqa: E402


# --------------------------------------------------------------------------- #
# Fake-module helpers
# --------------------------------------------------------------------------- #
def _load_module_from_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@contextlib.contextmanager
def fake_gpio():
    """Install a fresh fake RPi.GPIO into sys.modules for the duration."""
    gpio_mod = _load_module_from_path("fake_rpi_gpio", FAKES_DIR / "RPi" / "GPIO.py")
    gpio_mod.reset()
    rpi_pkg = types.ModuleType("RPi")
    rpi_pkg.GPIO = gpio_mod
    saved = {k: sys.modules.get(k) for k in ("RPi", "RPi.GPIO")}
    sys.modules["RPi"] = rpi_pkg
    sys.modules["RPi.GPIO"] = gpio_mod
    try:
        yield gpio_mod
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


@contextlib.contextmanager
def fake_spidev(fail_open=False, fail_xfer=False):
    """Install a fresh fake spidev into sys.modules for the duration."""
    spidev_mod = _load_module_from_path("fake_spidev", FAKES_DIR / "spidev.py")
    spidev_mod.SpiDev.instances = []
    spidev_mod.SpiDev.fail_open = fail_open
    spidev_mod.SpiDev.fail_xfer = fail_xfer
    saved = sys.modules.get("spidev")
    sys.modules["spidev"] = spidev_mod
    try:
        yield spidev_mod
    finally:
        if saved is None:
            sys.modules.pop("spidev", None)
        else:
            sys.modules["spidev"] = saved


@contextlib.contextmanager
def no_spidev():
    """Ensure `import spidev` fails, as it would on a machine without it."""
    saved = sys.modules.pop("spidev", None)
    try:
        yield
    finally:
        if saved is not None:
            sys.modules["spidev"] = saved


@contextlib.contextmanager
def fake_arecord_on_path(**env):
    """Prepend the fake arecord to PATH; set/unset env vars for the duration."""
    old_path = os.environ.get("PATH", "")
    old_env = {k: os.environ.get(k) for k in env}
    os.environ["PATH"] = str(FAKE_BIN_DIR) + os.pathsep + old_path
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        os.environ["PATH"] = old_path
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def make_wav(path, seconds=1, rate=16000, channels=2, freq=440.0, amp=8000):
    import math
    import struct

    n = int(rate * seconds)
    buf = bytearray()
    for s in range(n):
        val = int(amp * math.sin(2 * math.pi * freq * s / rate))
        buf += struct.pack("<h", val) * channels
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(bytes(buf))


# --------------------------------------------------------------------------- #
# find_card
# --------------------------------------------------------------------------- #
class FindCardTests(unittest.TestCase):
    def _run_with_stdout(self, text):
        completed = mock.Mock(stdout=text)
        with mock.patch.object(ra.subprocess, "run", return_value=completed) as m:
            result = ra.find_card()
        return result, m

    def test_seeed_card_detected(self):
        listing = (
            "**** List of CAPTURE Hardware Devices ****\n"
            "card 0: b1 [bcm2835 HDMI 1], device 0: bcm2835 HDMI 1 [bcm2835 HDMI 1]\n"
            "card 1: seeed2micvoicec [seeed-2mic-voicecard], device 0: "
            "bcm2835-i2s-wm8960-hifi wm8960-hifi-0 [bcm2835-i2s-wm8960-hifi wm8960-hifi-0]\n"
        )
        result, m = self._run_with_stdout(listing)
        self.assertEqual(result, "plughw:1,0")
        m.assert_called_once_with(["arecord", "-l"], capture_output=True, text=True)

    def test_wm8960_card_detected(self):
        listing = (
            "**** List of CAPTURE Hardware Devices ****\n"
            "card 2: wm8960soundcard [wm8960-soundcard], device 0: "
            "bcm2835-i2s-wm8960-hifi wm8960-hifi-0 [bcm2835-i2s-wm8960-hifi wm8960-hifi-0]\n"
        )
        result, _ = self._run_with_stdout(listing)
        self.assertEqual(result, "plughw:2,0")

    def test_respeaker_literal_name_detected(self):
        listing = "card 3: respeaker [ReSpeaker 2Mic], device 0: foo\n"
        result, _ = self._run_with_stdout(listing)
        self.assertEqual(result, "plughw:3,0")

    def test_only_hdmi_falls_back_to_default(self):
        listing = (
            "**** List of CAPTURE Hardware Devices ****\n"
            "card 0: b1 [bcm2835 HDMI 1], device 0: bcm2835 HDMI 1 [bcm2835 HDMI 1]\n"
            "card 0: b1 [bcm2835 HDMI 2], device 1: bcm2835 HDMI 2 [bcm2835 HDMI 2]\n"
        )
        result, _ = self._run_with_stdout(listing)
        self.assertEqual(result, "default")

    def test_empty_output_falls_back_to_default(self):
        result, _ = self._run_with_stdout("")
        self.assertEqual(result, "default")

    def test_arecord_missing_exits_with_apt_hint(self):
        with mock.patch.object(ra.subprocess, "run", side_effect=FileNotFoundError()):
            with self.assertRaises(SystemExit) as ctx:
                ra.find_card()
        self.assertIn("apt install alsa-utils", str(ctx.exception))

    # End-to-end variants through the real fake `arecord -l` executable on PATH.
    def test_end_to_end_seeed_via_fake_executable(self):
        with fake_arecord_on_path(FAKE_ARECORD_CARDS="seeed"):
            self.assertEqual(ra.find_card(), "plughw:1,0")

    def test_end_to_end_wm8960_via_fake_executable(self):
        with fake_arecord_on_path(FAKE_ARECORD_CARDS="wm8960"):
            self.assertEqual(ra.find_card(), "plughw:2,0")

    def test_end_to_end_hdmi_only_via_fake_executable(self):
        with fake_arecord_on_path(FAKE_ARECORD_CARDS="hdmi"):
            self.assertEqual(ra.find_card(), "default")

    def test_end_to_end_empty_via_fake_executable(self):
        with fake_arecord_on_path(FAKE_ARECORD_CARDS="empty"):
            self.assertEqual(ra.find_card(), "default")


# --------------------------------------------------------------------------- #
# record
# --------------------------------------------------------------------------- #
class RecordTests(unittest.TestCase):
    def test_exact_argv(self):
        fake_result = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(ra.subprocess, "run", return_value=fake_result) as m:
            ra.record("/tmp/out.wav", "plughw:1,0", seconds=3)
        m.assert_called_once_with(
            ["arecord", "-q", "-D", "plughw:1,0", "-f", "S16_LE",
             "-r", "16000", "-c", "2", "-d", "3", "/tmp/out.wav"],
            capture_output=True, text=True,
        )

    def test_seconds_argument_is_forwarded(self):
        fake_result = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(ra.subprocess, "run", return_value=fake_result) as m:
            ra.record("/tmp/out.wav", "plughw:1,0", seconds=5)
        args = m.call_args[0][0]
        self.assertIn("-d", args)
        self.assertEqual(args[args.index("-d") + 1], "5")

    def test_nonzero_exit_raises_runtimeerror_with_stderr(self):
        fake_result = mock.Mock(returncode=1, stderr="  arecord: no such device  \n")
        with mock.patch.object(ra.subprocess, "run", return_value=fake_result):
            with self.assertRaises(RuntimeError) as ctx:
                ra.record("/tmp/out.wav", "plughw:9,0")
        self.assertIn("arecord: no such device", str(ctx.exception))

    def test_real_fake_executable_writes_valid_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.wav")
            with fake_arecord_on_path():
                ra.record(path, "plughw:1,0", seconds=1)
            self.assertTrue(os.path.exists(path))
            with wave.open(path, "rb") as wf:
                self.assertEqual(wf.getnchannels(), 2)
                self.assertEqual(wf.getframerate(), 16000)
                self.assertEqual(wf.getsampwidth(), 2)
                self.assertEqual(wf.getnframes(), 16000)

    def test_real_fake_executable_failure_raises_runtimeerror(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.wav")
            with fake_arecord_on_path(FAKE_ARECORD_FAIL="arecord: device busy"):
                with self.assertRaises(RuntimeError) as ctx:
                    ra.record(path, "plughw:1,0", seconds=1)
            self.assertIn("device busy", str(ctx.exception))
            self.assertFalse(os.path.exists(path))


# --------------------------------------------------------------------------- #
# Leds
# --------------------------------------------------------------------------- #
class LedsTests(unittest.TestCase):
    def test_no_spidev_methods_are_silent_noops(self):
        with no_spidev():
            leds = ra.Leds()
            self.assertIsNone(leds.spi)
            leds.show((255, 0, 0))   # must not raise
            leds.off()               # must not raise

    def test_spi_open_failure_falls_back_to_noop(self):
        with fake_spidev(fail_open=True):
            leds = ra.Leds()
            self.assertIsNone(leds.spi)
            leds.show((1, 2, 3))     # must not raise

    def test_open_called_with_bus0_device1(self):
        with fake_spidev() as spidev_mod:
            leds = ra.Leds()
            self.assertEqual(leds.spi.opened, (0, 1))
            self.assertEqual(leds.spi.max_speed_hz, 8000000)
            self.assertIs(leds.spi, spidev_mod.SpiDev.instances[-1])

    def test_show_frame_layout_and_byte_order(self):
        with fake_spidev() as spidev_mod:
            leds = ra.Leds(count=3)
            leds.show((10, 20, 30), brightness=5)
            frame = spidev_mod.SpiDev.instances[-1].transfers[-1]
            expected = [0x00] * 4 + [0xE0 | 5, 30, 20, 10] * 3 + [0xFF] * 4
            self.assertEqual(frame, expected)

    def test_default_brightness_is_8(self):
        with fake_spidev() as spidev_mod:
            leds = ra.Leds(count=1)
            leds.show((1, 2, 3))
            frame = spidev_mod.SpiDev.instances[-1].transfers[-1]
            self.assertEqual(frame[4], 0xE0 | 8)

    def test_off_sends_zero_colors(self):
        with fake_spidev() as spidev_mod:
            leds = ra.Leds(count=3)
            leds.off()
            frame = spidev_mod.SpiDev.instances[-1].transfers[-1]
            expected = [0x00] * 4 + [0xE0, 0, 0, 0] * 3 + [0xFF] * 4
            self.assertEqual(frame, expected)

    def test_xfer2_exception_is_swallowed(self):
        with fake_spidev(fail_xfer=True):
            leds = ra.Leds()
            leds.show((5, 5, 5))     # must not raise despite xfer2 failing


# --------------------------------------------------------------------------- #
# capture_and_analyze
# --------------------------------------------------------------------------- #
class CaptureAndAnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.save_dir = os.path.join(self.tmpdir.name, "recordings")
        self._save_dir_patch = mock.patch.object(ra, "SAVE_DIR", self.save_dir)
        self._save_dir_patch.start()
        self.addCleanup(self._save_dir_patch.stop)

    def _capture(self):
        leds = mock.Mock()
        with fake_arecord_on_path():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                result = ra.capture_and_analyze("plughw:1,0", leds)
        return result, leds, buf.getvalue()

    def test_savedir_autocreated(self):
        self.assertFalse(os.path.isdir(self.save_dir))
        self._capture()
        self.assertTrue(os.path.isdir(self.save_dir))

    def test_always_keeps_wav_and_prints_saved(self):
        result, leds, out = self._capture()
        files = os.listdir(self.save_dir)
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"^rec_\d{8}_\d{6}_\d{3}\.wav$")
        self.assertIn(f"Saved: {os.path.join(self.save_dir, files[0])}", out)
        with wave.open(os.path.join(self.save_dir, files[0]), "rb") as wf:
            self.assertEqual(wf.getnframes(), 3 * 16000)   # the full 3 s is kept

    def test_keep_parameter_is_gone(self):
        with self.assertRaises(TypeError):
            ra.capture_and_analyze("plughw:1,0", mock.Mock(), True)

    def test_two_recordings_in_same_second_both_kept(self):
        import datetime as real_dt
        times = iter([real_dt.datetime(2026, 9, 28, 3, 8, 21, 100000),
                      real_dt.datetime(2026, 9, 28, 3, 8, 21, 600000)])
        fake_dt = types.SimpleNamespace(
            datetime=types.SimpleNamespace(now=lambda: next(times)))
        with mock.patch.object(ra, "datetime", fake_dt):
            self._capture()
            self._capture()
        self.assertEqual(sorted(os.listdir(self.save_dir)),
                         ["rec_20260928_030821_100.wav",
                          "rec_20260928_030821_600.wav"])

    def test_report_is_printed(self):
        _, _, out = self._capture()
        self.assertIn("Recording 3s", out)
        self.assertIn("dBFS", out)
        self.assertIn("Hz", out)

    def test_led_sequence_red_then_blue_then_volume_color(self):
        result, leds, _ = self._capture()
        self.assertEqual(leds.show.call_count, 3)
        calls = leds.show.call_args_list
        self.assertEqual(calls[0].args[0], (255, 0, 0))     # red = recording
        self.assertEqual(calls[1].args[0], (0, 0, 255))     # blue = analyzing
        expected_color = ra.volume_color(result["vol_idx"])
        self.assertEqual(calls[2].args[0], expected_color)  # volume color

    def test_arecord_failure_raises_runtimeerror(self):
        leds = mock.Mock()
        with fake_arecord_on_path(FAKE_ARECORD_FAIL="arecord: device busy"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(RuntimeError):
                    ra.capture_and_analyze("plughw:1,0", leds)
        # Only the "recording" (red) LED state should have been reached.
        leds.show.assert_called_once_with((255, 0, 0))


# --------------------------------------------------------------------------- #
# button_loop helpers: a fake clock (patched over time.monotonic/time.sleep)
# and a press timeline read through the fake GPIO's level function.
# --------------------------------------------------------------------------- #
class FakeClock:
    def __init__(self, start=0.0):
        self.t = start

    def monotonic(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


def press_timeline(gpio_mod, now, presses, end, max_reads=200000):
    """Level function: LOW inside any [down, up) interval of `presses`,
    KEY_INTERRUPT from time `end` on (or after max_reads, so a bug can never
    hang the test run)."""
    reads = [0]

    def level():
        reads[0] += 1
        t = now()
        if t >= end or reads[0] > max_reads:
            return gpio_mod.KEY_INTERRUPT
        return gpio_mod.LOW if any(a <= t < b for a, b in presses) else gpio_mod.HIGH
    return level


def taps(*starts, length=0.1):
    """Press intervals of `length` seconds starting at each of `starts`."""
    return [(s, s + length) for s in starts]


class ButtonLoopTestBase(unittest.TestCase):
    """button_loop with fake GPIO + fake clock; recording functions mocked."""
    SESSION_PATH = "/fake/recordings/session_20260928_030821_100.wav"
    STARTED = "Session recording started - double-tap to stop and save."
    LOW, HIGH = 0, 1   # fake RPi.GPIO levels

    def setUp(self):
        self.clock = FakeClock()
        for name in ("sleep", "monotonic"):
            p = mock.patch.object(ra.time, name, getattr(self.clock, name))
            p.start()
            self.addCleanup(p.stop)
        self.capture_times = []
        self.capture_seconds = 3.0             # a real capture blocks ~3 s

        def fake_capture(device, leds):
            self.capture_times.append(self.clock.t)
            self.clock.t += self.capture_seconds
        self.capture = mock.Mock(side_effect=fake_capture)
        self.proc = mock.Mock(returncode=None)
        self.proc.poll.return_value = None
        self.proc.stderr = io.BytesIO(b"")
        self.start = mock.Mock(side_effect=lambda device: (self.proc, self.SESSION_PATH))
        self.stop = mock.Mock(return_value=2.5)
        for name, m in (("capture_and_analyze", self.capture),
                        ("start_session", self.start), ("stop_session", self.stop)):
            p = mock.patch.object(ra, name, m)
            p.start()
            self.addCleanup(p.stop)
        self.leds = mock.Mock()

    def run_loop(self, presses, end, sequence=()):
        with fake_gpio() as gpio_mod:
            self.gpio = gpio_mod
            gpio_mod.set_sequence(list(sequence))
            gpio_mod.set_level_function(
                press_timeline(gpio_mod, self.clock.monotonic, presses, end))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ra.button_loop("plughw:1,0", self.leds)
        return buf.getvalue()


# --------------------------------------------------------------------------- #
# button_loop: single tap / basics
# --------------------------------------------------------------------------- #
class ButtonLoopTests(ButtonLoopTestBase):
    def test_setmode_and_setup(self):
        self.run_loop([], end=0.0)
        self.assertIn(("setmode", "BCM"), self.gpio.calls)
        self.assertIn(("setup", 17, "IN", "PUD_UP"), self.gpio.calls)

    def test_banner_names_save_dir_and_double_tap(self):
        with mock.patch.object(ra, "SAVE_DIR", "/some/where/recordings"):
            out = self.run_loop([], end=0.0)
        self.assertIn("/some/where/recordings", out)
        self.assertIn("double-tap", out)

    def test_single_tap_one_capture_no_session(self):
        out = self.run_loop(taps(0.5), end=3.0)
        self.capture.assert_called_once_with("plughw:1,0", self.leds)
        self.start.assert_not_called()
        self.stop.assert_not_called()
        self.assertIn("Press the button to record again.", out)

    def test_single_tap_fires_only_after_the_double_tap_window(self):
        self.run_loop(taps(0.5), end=3.0)       # released at ~0.6
        (t,) = self.capture_times
        self.assertGreater(t, 0.6 + ra.DOUBLE_TAP_WINDOW)
        self.assertLess(t, 0.6 + ra.DOUBLE_TAP_WINDOW + 0.2)

    def test_held_then_released_triggers_exactly_one_capture(self):
        self.run_loop([(0.5, 3.0)], end=5.0)
        self.assertEqual(self.capture.call_count, 1)
        self.assertGreater(self.capture_times[0], 3.0)   # after release, not while held
        self.start.assert_not_called()

    def test_button_stuck_low_triggers_nothing_and_does_not_hang(self):
        out = self.run_loop([(0.5, 1000.0)], end=20.0)
        self.capture.assert_not_called()
        self.start.assert_not_called()
        self.assertIn("Bye.", out)
        self.assertIn(("cleanup",), self.gpio.calls)

    def test_one_poll_glitch_triggers_no_recording(self):
        # LOW at the first read, but HIGH again at the 30 ms debounce re-check.
        self.run_loop([], end=2.0, sequence=[self.LOW, self.HIGH])
        self.capture.assert_not_called()
        self.start.assert_not_called()

    def test_bounce_while_held_is_not_a_second_tap(self):
        # One HIGH read while the button is held (contact bounce) is rejected
        # by the debounce re-check, so it can't look like release + 2nd press.
        L, H = self.LOW, self.HIGH
        seq = [L, L, L, L, L, H, L, L, L, L, H, H]   # press, bounce, real release
        self.run_loop([], end=3.0, sequence=seq)
        self.start.assert_not_called()
        self.assertEqual(self.capture.call_count, 1)

    def test_two_separate_taps_trigger_two_captures(self):
        self.run_loop(taps(0.5, 5.0), end=10.0)
        self.assertEqual(self.capture.call_count, 2)
        self.start.assert_not_called()

    def test_press_held_across_end_of_capture_is_ignored(self):
        # tap at 0.5 -> capture at ~1.0 blocks until ~4.0; a press that began
        # during the capture and is still down afterwards must not count.
        self.run_loop(taps(0.5) + [(3.5, 4.5)], end=8.0)
        self.assertEqual(self.capture.call_count, 1)
        self.start.assert_not_called()

    def test_runtimeerror_is_printed_and_loop_continues(self):
        effects = [RuntimeError("boom: device busy"), None]

        def capture(device, leds):
            self.clock.t += 3.0
            e = effects.pop(0)
            if e:
                raise e
        self.capture.side_effect = capture
        out = self.run_loop(taps(0.5, 5.0), end=10.0)
        self.assertIn("boom: device busy", out)
        self.assertEqual(self.capture.call_count, 2)

    def test_keyboard_interrupt_prints_bye_and_cleans_up(self):
        out = self.run_loop([], end=0.0)
        self.assertIn("Bye.", out)
        self.leds.off.assert_called_once()
        self.assertIn(("cleanup",), self.gpio.calls)
        self.stop.assert_not_called()


# --------------------------------------------------------------------------- #
# button_loop: double-tap sessions
# --------------------------------------------------------------------------- #
class ButtonLoopSessionTests(ButtonLoopTestBase):
    def test_double_tap_starts_session(self):
        out = self.run_loop(taps(0.5, 0.75), end=3.0)
        self.start.assert_called_once_with("plughw:1,0")
        self.capture.assert_not_called()
        self.assertIn(self.STARTED, out)
        self.leds.show.assert_any_call((255, 0, 255))

    def test_double_tap_is_acted_on_at_the_second_press(self):
        started = []

        def start(device):
            started.append(self.clock.t)
            return self.proc, self.SESSION_PATH
        self.start.side_effect = start
        self.run_loop(taps(0.5, 0.75), end=3.0)
        self.assertGreaterEqual(started[0], 0.75)
        self.assertLess(started[0], 0.85)       # not after release + window

    def test_second_double_tap_stops_and_saves_without_analysis(self):
        out = self.run_loop(taps(0.5, 0.75, 3.0, 3.25), end=6.0)
        self.start.assert_called_once()
        self.stop.assert_called_once_with(self.proc, self.SESSION_PATH)
        self.capture.assert_not_called()
        saved = f"Saved: {self.SESSION_PATH} (2.5 s)"
        self.assertIn(saved, out)
        self.assertLess(out.index(saved), out.index("Bye."))   # not the Ctrl+C path
        self.assertNotIn("dBFS", out)
        self.assertNotIn("Recording 3s", out)
        self.assertEqual(self.leds.show.call_args_list[-1].args[0], (255, 0, 255))
        self.assertEqual(self.leds.off.call_count, 2)          # after save + at exit

    def test_after_session_single_tap_analyzes_again(self):
        self.run_loop(taps(0.5, 0.75, 3.0, 3.25, 6.0), end=10.0)
        self.assertEqual(self.start.call_count, 1)
        self.assertEqual(self.stop.call_count, 1)
        self.assertEqual(self.capture.call_count, 1)

    def test_saved_duration_is_formatted_to_one_decimal(self):
        self.stop.return_value = 12.345
        out = self.run_loop(taps(0.5, 0.75, 3.0, 3.25), end=6.0)
        self.assertIn(f"Saved: {self.SESSION_PATH} (12.3 s)", out)

    def test_gap_just_inside_window_is_double(self):
        # released at ~0.6, second press seen at ~0.96: gap ~0.36 s <= 0.4
        self.run_loop(taps(0.5, 0.96), end=3.0)
        self.start.assert_called_once()
        self.capture.assert_not_called()

    def test_gap_just_over_window_is_two_singles(self):
        # released at ~0.6, second press at ~1.06: gap ~0.46 s > 0.4.
        # Instant captures here so the second tap isn't swallowed by capture #1.
        self.capture_seconds = 0.0
        self.run_loop(taps(0.5, 1.06), end=8.0)
        self.start.assert_not_called()
        self.assertEqual(self.capture.call_count, 2)

    def test_triple_tap_from_idle_is_one_double(self):
        out = self.run_loop(taps(0.5, 0.75, 1.0), end=4.0)
        self.start.assert_called_once()
        self.capture.assert_not_called()
        self.assertNotIn("Recording... double-tap to stop.", out)   # 3rd tap absorbed

    def test_triple_tap_to_stop_does_not_start_an_analysis(self):
        out = self.run_loop(taps(0.5, 0.75, 3.0, 3.25, 3.5), end=7.0)
        self.assertEqual(self.start.call_count, 1)
        self.assertEqual(self.stop.call_count, 1)
        self.capture.assert_not_called()
        self.assertIn("Saved:", out)

    def test_quadruple_tap_quickly_is_one_double(self):
        self.run_loop(taps(0.5, 0.75, 1.0, 1.25), end=4.0)
        self.assertEqual(self.start.call_count, 1)
        self.stop.assert_called_once()           # only by the Ctrl+C path

    def test_single_tap_during_session_is_ignored_with_hint(self):
        out = self.run_loop(taps(0.5, 0.75, 3.0), end=6.0)
        self.start.assert_called_once()
        self.capture.assert_not_called()
        self.assertIn("Recording... double-tap to stop.", out)
        # still recording until Ctrl+C: stopped exactly once, after the hint
        self.stop.assert_called_once()
        self.assertLess(out.index("Recording... double-tap"), out.index("Saved:"))

    def test_ctrl_c_during_session_saves_then_cleans_up(self):
        out = self.run_loop(taps(0.5, 0.75), end=3.0)
        self.stop.assert_called_once_with(self.proc, self.SESSION_PATH)
        self.assertIn(f"Saved: {self.SESSION_PATH} (2.5 s)", out)
        self.assertLess(out.index("Saved:"), out.index("Bye."))
        self.leds.off.assert_called()
        self.assertEqual(self.gpio.calls[-1], ("cleanup",))

    def test_ctrl_c_session_save_failure_still_says_bye_and_cleans_up(self):
        self.stop.side_effect = RuntimeError("Session recording failed - no audio was saved.")
        out = self.run_loop(taps(0.5, 0.75), end=3.0)
        self.assertIn("no audio was saved", out)
        self.assertIn("Bye.", out)
        self.assertIn(("cleanup",), self.gpio.calls)

    def test_unexpected_exception_stops_session_before_propagating(self):
        calls = [0]

        def poll():
            calls[0] += 1
            if calls[0] == 5:
                raise ValueError("weird")
        self.proc.poll.side_effect = poll
        with self.assertRaises(ValueError):
            self.run_loop(taps(0.1, 0.25), end=5.0)
        self.stop.assert_called_once_with(self.proc, self.SESSION_PATH)
        self.assertIn(("cleanup",), self.gpio.calls)

    def test_arecord_dying_mid_session_recovers_to_idle(self):
        self.proc.poll.side_effect = lambda: None if self.clock.t < 2.0 else 1
        self.proc.returncode = 1
        self.proc.stderr = io.BytesIO(
            b"arecord: pcm_read:2178: read error: Input/output error\n")
        self.stop.return_value = 1.2
        out = self.run_loop(taps(0.5, 0.75, 4.0), end=9.0)
        self.assertIn("Session recording stopped unexpectedly (arecord exit code 1): "
                      "arecord: pcm_read:2178: read error: Input/output error", out)
        self.assertIn(f"Saved: {self.SESSION_PATH} (1.2 s)", out)
        self.stop.assert_called_once_with(self.proc, self.SESSION_PATH)
        self.leds.off.assert_called()
        # back to IDLE: the tap at 4.0 is a normal analysis, not a session hint
        self.assertEqual(self.capture.call_count, 1)
        self.assertNotIn("double-tap to stop.", out)
        self.assertIn("Bye.", out)

    def test_arecord_dying_with_no_audio_reports_nothing_saved(self):
        self.proc.poll.side_effect = lambda: None if self.clock.t < 1.0 else 1
        self.proc.returncode = 1
        self.proc.stderr = io.BytesIO(b"arecord: device busy")
        self.stop.side_effect = RuntimeError("no audio")
        out = self.run_loop(taps(0.5, 0.75, 3.0, 3.25), end=6.0)
        self.assertIn("device busy", out)
        self.assertIn("No audio was saved.", out)
        self.assertNotIn("Saved:", out)
        self.assertEqual(self.start.call_count, 2)   # a new double tap starts again

    def test_start_failure_stays_idle(self):
        self.start.side_effect = [FileNotFoundError(2, "No such file", "arecord"),
                                  (self.proc, self.SESSION_PATH)]
        out = self.run_loop(taps(0.5, 0.75, 3.0, 3.25), end=6.0)
        self.assertIn("Could not start recording", out)
        self.assertEqual(self.start.call_count, 2)
        self.assertIn(self.STARTED, out)
        self.stop.assert_called_once()           # the 2nd session, at Ctrl+C
        self.capture.assert_not_called()


# --------------------------------------------------------------------------- #
# TapDetector on exact timestamps (window boundary)
# --------------------------------------------------------------------------- #
class TapDetectorTests(unittest.TestCase):
    def feed(self, det, samples):
        return [e for e in (det.update(p, t) for p, t in samples) if e]

    def test_window_constant(self):
        self.assertEqual(ra.DOUBLE_TAP_WINDOW, 0.4)

    def test_gap_exactly_at_window_is_double(self):
        det = ra.TapDetector()
        events = self.feed(det, [(True, -0.1), (False, 0.0), (True, 0.4)])
        self.assertEqual(events, [ra.DOUBLE_TAP])

    def test_gap_just_over_window_is_single_then_new_tap(self):
        det = ra.TapDetector()
        events = self.feed(det, [(True, -0.1), (False, 0.0), (True, 0.4000001)])
        self.assertEqual(events, [ra.SINGLE_TAP])
        # that late press is the first press of a new tap
        self.assertEqual(self.feed(det, [(False, 0.5), (True, 0.6)]), [ra.DOUBLE_TAP])

    def test_single_reported_once_window_expires(self):
        det = ra.TapDetector()
        self.assertEqual(self.feed(det, [(True, 0.0), (False, 0.1), (False, 0.5)]), [])
        self.assertEqual(det.update(False, 0.5000001), ra.SINGLE_TAP)
        self.assertEqual(det.state, det.IDLE)
        self.assertIsNone(det.update(False, 10.0))

    def test_held_forever_reports_nothing(self):
        det = ra.TapDetector()
        self.assertEqual(self.feed(det, [(True, t / 10) for t in range(10000)]), [])

    def test_triple_and_quadruple_taps_are_one_double(self):
        for n in (3, 4, 5):
            det = ra.TapDetector()
            samples = []
            for i in range(n):
                samples += [(True, i * 0.3), (False, i * 0.3 + 0.1)]
            samples += [(False, 10.0)]
            self.assertEqual(self.feed(det, samples), [ra.DOUBLE_TAP], f"{n} taps")
            self.assertEqual(det.state, det.IDLE)

    def test_tap_after_burst_window_is_a_new_single(self):
        det = ra.TapDetector()
        samples = [(True, 0.0), (False, 0.1), (True, 0.2), (False, 0.3),   # double
                   (False, 0.71),                                           # burst over
                   (True, 1.0), (False, 1.1), (False, 1.6)]                 # single
        self.assertEqual(self.feed(det, samples), [ra.DOUBLE_TAP, ra.SINGLE_TAP])

    def test_reset_while_pressed_ignores_that_press(self):
        det = ra.TapDetector()
        det.update(True, 0.0)
        det.reset(True)
        self.assertEqual(self.feed(det, [(True, 0.1), (False, 0.2), (False, 5.0)]), [])
        self.assertEqual(det.state, det.IDLE)

    def test_reset_while_released_counts_the_next_press(self):
        det = ra.TapDetector()
        det.update(True, 0.0)
        det.update(False, 0.1)                  # a tap was pending
        det.reset(False)
        self.assertEqual(det.state, det.IDLE)
        self.assertEqual(self.feed(det, [(True, 0.2), (False, 0.3), (False, 1.0)]),
                         [ra.SINGLE_TAP])

    def test_custom_window(self):
        det = ra.TapDetector(window=1.0)
        self.assertEqual(self.feed(det, [(True, 0.0), (False, 0.1), (True, 1.0)]),
                         [ra.DOUBLE_TAP])


# --------------------------------------------------------------------------- #
# Session recorder: real subprocess (the fake arecord), nothing mocked
# --------------------------------------------------------------------------- #
def wait_for_exit(proc, timeout=10.0):
    import time
    deadline = time.monotonic() + timeout
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    return proc.poll()


class SessionRecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.save_dir = os.path.join(self.tmpdir.name, "recordings")
        p = mock.patch.object(ra, "SAVE_DIR", self.save_dir)
        p.start()
        self.addCleanup(p.stop)

    def _record(self, seconds, **env):
        import time
        with fake_arecord_on_path(**env):
            proc, path = ra.start_session("plughw:1,0")
            self.addCleanup(lambda: proc.poll() is None and proc.kill())
            t0 = time.monotonic()
            time.sleep(seconds)
            duration = ra.stop_session(proc, path)
            elapsed = time.monotonic() - t0
        return proc, path, duration, elapsed

    def assert_valid_wav(self, path, duration):
        with wave.open(path, "rb") as wf:
            self.assertEqual(wf.getnchannels(), 2)
            self.assertEqual(wf.getframerate(), 16000)
            self.assertEqual(wf.getsampwidth(), 2)
            n = wf.getnframes()
            self.assertEqual(len(wf.readframes(n)), n * 4)   # header matches data
        self.assertAlmostEqual(n / 16000, duration, places=6)
        self.assertEqual(os.path.getsize(path), 44 + n * 4)

    def test_g4_start_sleep_stop_gives_valid_wav_of_measured_length(self):
        proc, path, duration, elapsed = self._record(1.0)
        self.assertEqual(os.path.dirname(path), self.save_dir)
        self.assertRegex(os.path.basename(path), r"^session_\d{8}_\d{6}_\d{3}\.wav$")
        self.assert_valid_wav(path, duration)
        self.assertAlmostEqual(duration, elapsed, delta=0.3)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(os.listdir(self.save_dir), [os.path.basename(path)])

    def test_nonzero_exit_after_our_sigint_is_still_success(self):
        proc, path, duration, elapsed = self._record(0.6, FAKE_ARECORD_SIGINT_EXIT="1")
        self.assertEqual(proc.returncode, 1)     # fake printed "Aborted by signal Interrupt..."
        self.assert_valid_wav(path, duration)
        self.assertAlmostEqual(duration, elapsed, delta=0.3)

    def test_hung_arecord_is_terminated_and_unfinalized_header_repaired(self):
        with mock.patch.object(ra, "SESSION_STOP_TIMEOUT", 0.5):
            proc, path, duration, elapsed = self._record(
                0.6, FAKE_ARECORD_IGNORE_SIGINT="1")
        self.assertEqual(proc.returncode, 143)          # died on SIGTERM, not SIGINT
        self.assert_valid_wav(path, duration)
        # it kept recording during the 0.5 s SIGINT grace period
        self.assertGreater(duration, 0.5)
        self.assertLess(duration, elapsed + 0.3)

    def test_arecord_dying_on_its_own_keeps_audio(self):
        with fake_arecord_on_path(FAKE_ARECORD_DIE_AFTER="0.3"):
            proc, path = ra.start_session("plughw:1,0")
            self.assertEqual(wait_for_exit(proc), 1)
            self.assertIn("read error", ra.session_stderr(proc))
            duration = ra.stop_session(proc, path)
        self.assertAlmostEqual(duration, 0.3, places=6)
        self.assert_valid_wav(path, duration)

    def test_arecord_dying_before_any_audio_raises_and_removes_file(self):
        with fake_arecord_on_path(FAKE_ARECORD_DIE_AFTER="0",
                                  FAKE_ARECORD_DIE_MSG="arecord: overrun, giving up"):
            proc, path = ra.start_session("plughw:1,0")
            wait_for_exit(proc)
            with self.assertRaises(RuntimeError) as ctx:
                ra.stop_session(proc, path)
        self.assertIn("no audio was saved", str(ctx.exception))
        self.assertIn("overrun, giving up", str(ctx.exception))
        self.assertFalse(os.path.exists(path))

    def test_arecord_failing_to_open_device(self):
        with fake_arecord_on_path(FAKE_ARECORD_FAIL="arecord: main:830: audio open error: "
                                                    "Device or resource busy"):
            proc, path = ra.start_session("plughw:1,0")
            wait_for_exit(proc)
            with self.assertRaises(RuntimeError) as ctx:
                ra.stop_session(proc, path)
        self.assertIn("Device or resource busy", str(ctx.exception))
        self.assertFalse(os.path.exists(path))

    def test_argv_matches_record_without_duration(self):
        with mock.patch.object(ra.subprocess, "Popen") as popen:
            proc, path = ra.start_session("plughw:1,0")
        argv = popen.call_args.args[0]
        self.assertEqual(argv, ["arecord", "-q", "-D", "plughw:1,0", "-f", "S16_LE",
                                "-r", "16000", "-c", "2", path])
        self.assertNotIn("-d", argv)
        stderr = popen.call_args.kwargs["stderr"]
        self.assertTrue(hasattr(stderr, "read"))     # captured to a file, not a pipe
        self.assertIs(proc.stderr, stderr)
        stderr.close()

    def test_start_creates_save_dir(self):
        self.assertFalse(os.path.isdir(self.save_dir))
        with mock.patch.object(ra.subprocess, "Popen") as popen:
            ra.start_session("plughw:1,0")
        self.assertTrue(os.path.isdir(self.save_dir))
        popen.call_args.kwargs["stderr"].close()

    def test_missing_arecord_raises_oserror(self):
        with mock.patch.object(ra.subprocess, "Popen",
                               side_effect=FileNotFoundError(2, "No such file", "arecord")):
            with self.assertRaises(OSError):
                ra.start_session("plughw:1,0")

    def test_stop_never_hangs_on_an_unkillable_process(self):
        path = os.path.join(self.tmpdir.name, "s.wav")
        make_wav(path, seconds=1)
        proc = mock.Mock(stderr=None)
        proc.poll.return_value = None
        proc.wait.side_effect = ra.subprocess.TimeoutExpired("arecord", 5)
        self.assertEqual(ra.stop_session(proc, path), 1.0)
        proc.send_signal.assert_called_once_with(ra.signal.SIGINT)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        self.assertEqual(proc.wait.call_count, 3)
        for c in proc.wait.call_args_list:
            self.assertEqual(c.kwargs, {"timeout": ra.SESSION_STOP_TIMEOUT})

    def test_stop_sends_sigint_first_and_stops_there_if_it_works(self):
        path = os.path.join(self.tmpdir.name, "s.wav")
        make_wav(path, seconds=1)
        proc = mock.Mock(stderr=None)
        proc.poll.return_value = None
        self.assertEqual(ra.stop_session(proc, path), 1.0)
        proc.send_signal.assert_called_once_with(ra.signal.SIGINT)
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()


# --------------------------------------------------------------------------- #
# finalize_wav: repair / reject what a stopped or killed recorder left behind
# --------------------------------------------------------------------------- #
def raw_wav(path, data_bytes, declared, rate=16000, channels=2):
    import struct
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", min(36 + declared, 0xFFFFFFFF)) + b"WAVE"
                + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate,
                                        rate * channels * 2, channels * 2, 16)
                + b"data" + struct.pack("<I", declared) + b"\x01\x02" * (data_bytes // 2)
                + b"\x03" * (data_bytes % 2))


class FinalizeWavTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = os.path.join(self.tmpdir.name, "session.wav")

    def test_unfinalized_placeholder_header_is_repaired(self):
        raw_wav(self.path, 64000, declared=0x7FFFFFFF)       # 1 s of stereo 16 kHz
        size = os.path.getsize(self.path)
        self.assertEqual(ra.finalize_wav(self.path), 1.0)
        self.assertEqual((size - 44) / (16000 * 2 * 2), 1.0)
        with wave.open(self.path, "rb") as wf:
            self.assertEqual(wf.getnframes(), 16000)
        self.assertEqual(os.path.getsize(self.path), size)

    def test_partial_trailing_frame_is_dropped(self):
        raw_wav(self.path, 64000 + 3, declared=0x7FFFFFFF)
        self.assertEqual(ra.finalize_wav(self.path), 1.0)
        self.assertEqual(os.path.getsize(self.path), 44 + 64000)
        with wave.open(self.path, "rb") as wf:
            self.assertEqual(wf.getnframes(), 16000)

    def test_correct_file_is_left_byte_for_byte(self):
        make_wav(self.path, seconds=2)
        before = pathlib.Path(self.path).read_bytes()
        self.assertEqual(ra.finalize_wav(self.path), 2.0)
        self.assertEqual(pathlib.Path(self.path).read_bytes(), before)

    def test_header_only_file_is_removed(self):
        raw_wav(self.path, 0, declared=0x7FFFFFFF)
        self.assertIsNone(ra.finalize_wav(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_not_a_wav_is_removed(self):
        with open(self.path, "wb") as f:
            f.write(b"garbage" * 100)
        self.assertIsNone(ra.finalize_wav(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_truncated_header_is_removed(self):
        raw_wav(self.path, 0, declared=0)
        with open(self.path, "r+b") as f:
            f.truncate(30)
        self.assertIsNone(ra.finalize_wav(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_missing_file(self):
        self.assertIsNone(ra.finalize_wav(self.path))

    def test_mono_and_other_rates(self):
        raw_wav(self.path, 48000 * 2, declared=0x7FFFFFFF, rate=48000, channels=1)
        self.assertEqual(ra.finalize_wav(self.path), 1.0)


# --------------------------------------------------------------------------- #
# button_loop end to end: real time, real fake-arecord subprocess, fake GPIO
# --------------------------------------------------------------------------- #
class ButtonLoopEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.save_dir = os.path.join(self.tmpdir.name, "recordings")
        p = mock.patch.object(ra, "SAVE_DIR", self.save_dir)
        p.start()
        self.addCleanup(p.stop)

    def run_loop(self, presses, end, **env):
        import time
        t0 = time.monotonic()
        with fake_gpio() as gpio_mod, fake_arecord_on_path(**env), no_spidev():
            gpio_mod.set_level_function(
                press_timeline(gpio_mod, lambda: time.monotonic() - t0, presses, end))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ra.button_loop("plughw:1,0", ra.Leds())
        return buf.getvalue()

    def files(self, prefix):
        return sorted(f for f in os.listdir(self.save_dir) if f.startswith(prefix))

    def session_duration(self):
        (name,) = self.files("session_")
        with wave.open(os.path.join(self.save_dir, name), "rb") as wf:
            self.assertEqual((wf.getnchannels(), wf.getframerate()), (2, 16000))
            return name, wf.getnframes() / wf.getframerate()

    def test_double_tap_record_then_double_tap_save(self):
        out = self.run_loop(taps(0.1, 0.3, 1.5, 1.7), end=2.2)
        name, dur = self.session_duration()
        self.assertEqual(self.files("rec_"), [])                 # no analysis capture
        self.assertAlmostEqual(dur, 1.4, delta=0.35)              # 2nd press ~0.3 -> ~1.7
        self.assertIn(f"Saved: {os.path.join(self.save_dir, name)} ({dur:.1f} s)", out)
        self.assertNotIn("dBFS", out)

    def test_ctrl_c_during_session_saves_file(self):
        out = self.run_loop(taps(0.1, 0.3), end=1.2)
        name, dur = self.session_duration()
        self.assertAlmostEqual(dur, 0.9, delta=0.35)
        self.assertLess(out.index("Saved:"), out.index("Bye."))

    def test_arecord_dies_mid_session_then_single_tap_analyzes(self):
        out = self.run_loop(taps(0.1, 0.3, 1.5), end=2.4, FAKE_ARECORD_DIE_AFTER="0.5")
        self.assertIn("stopped unexpectedly (arecord exit code 1)", out)
        self.assertIn("read error", out)
        name, dur = self.session_duration()
        self.assertAlmostEqual(dur, 0.5, places=6)
        self.assertIn(f"({dur:.1f} s)", out)
        self.assertEqual(len(self.files("rec_")), 1)            # back to IDLE
        self.assertIn("dBFS", out)


# --------------------------------------------------------------------------- #
# main() argument handling
# --------------------------------------------------------------------------- #
class MainArgsTests(unittest.TestCase):
    def test_file_mode_never_touches_gpio_or_arecord(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = os.path.join(tmp, "in.wav")
            make_wav(wav_path, seconds=1)
            argv = ["respeaker_analyzer.py", "--file", wav_path]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(ra, "find_card") as find_card_mock, \
                 mock.patch.object(ra, "Leds") as leds_cls_mock, \
                 mock.patch.object(ra, "button_loop") as button_loop_mock, \
                 mock.patch.object(ra, "capture_and_analyze") as capture_mock:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    ra.main()
            find_card_mock.assert_not_called()
            leds_cls_mock.assert_not_called()
            button_loop_mock.assert_not_called()
            capture_mock.assert_not_called()
            self.assertIn("dBFS", buf.getvalue())

    def test_now_mode_calls_capture(self):
        argv = ["respeaker_analyzer.py", "--now"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(ra, "find_card", return_value="plughw:1,0") as find_card_mock, \
             mock.patch.object(ra, "Leds") as leds_cls_mock, \
             mock.patch.object(ra, "capture_and_analyze") as capture_mock, \
             mock.patch.object(ra, "button_loop") as button_loop_mock:
            leds_instance = mock.Mock()
            leds_cls_mock.return_value = leds_instance
            ra.main()
        find_card_mock.assert_called_once()
        capture_mock.assert_called_once_with("plughw:1,0", leds_instance)
        button_loop_mock.assert_not_called()

    def test_no_save_option_is_rejected(self):
        argv = ["respeaker_analyzer.py", "--now", "--no-save"]
        err = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(ra, "capture_and_analyze") as capture_mock, \
             contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                ra.main()
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("unrecognized arguments: --no-save", err.getvalue())
        capture_mock.assert_not_called()

    def test_help_describes_saving_and_double_tap(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["respeaker_analyzer.py", "--help"]), \
             contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                ra.main()
        self.assertEqual(ctx.exception.code, 0)
        text = out.getvalue()
        self.assertNotIn("--no-save", text)
        self.assertIn("Double-tap", text)
        self.assertIn("GSP_RECORDINGS_DIR", text)
        self.assertIn("session_YYYYmmdd_HHMMSS_mmm.wav", text)

    def test_device_flag_overrides_find_card(self):
        argv = ["respeaker_analyzer.py", "--now", "--device", "plughw:9,0"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(ra, "find_card") as find_card_mock, \
             mock.patch.object(ra, "Leds") as leds_cls_mock, \
             mock.patch.object(ra, "capture_and_analyze") as capture_mock:
            leds_instance = mock.Mock()
            leds_cls_mock.return_value = leds_instance
            ra.main()
        find_card_mock.assert_not_called()
        capture_mock.assert_called_once_with("plughw:9,0", leds_instance)

    def test_default_mode_calls_button_loop(self):
        argv = ["respeaker_analyzer.py"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(ra, "find_card", return_value="default") as find_card_mock, \
             mock.patch.object(ra, "Leds") as leds_cls_mock, \
             mock.patch.object(ra, "button_loop") as button_loop_mock, \
             mock.patch.object(ra, "capture_and_analyze") as capture_mock:
            leds_instance = mock.Mock()
            leds_cls_mock.return_value = leds_instance
            ra.main()
        find_card_mock.assert_called_once()
        button_loop_mock.assert_called_once_with("default", leds_instance)
        capture_mock.assert_not_called()


# --------------------------------------------------------------------------- #
# --now end to end (SAVE_DIR patched, fake arecord on PATH): always saves
# --------------------------------------------------------------------------- #
class NowEndToEndTest(unittest.TestCase):
    def test_now_end_to_end_prints_report_and_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_dir = os.path.join(tmp, "recordings")
            argv = ["respeaker_analyzer.py", "--now"]
            with mock.patch.object(ra, "SAVE_DIR", save_dir), \
                 fake_arecord_on_path(), \
                 no_spidev(), \
                 mock.patch.object(sys, "argv", argv):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    ra.main()
            out = buf.getvalue()
            self.assertIn("Recording 3s", out)
            self.assertIn("dBFS", out)
            self.assertIn("Hz", out)
            files = os.listdir(save_dir)
            self.assertEqual(len(files), 1)
            self.assertRegex(files[0], r"^rec_\d{8}_\d{6}_\d{3}\.wav$")
            self.assertIn(f"Saved: {os.path.join(save_dir, files[0])}", out)
            with wave.open(os.path.join(save_dir, files[0]), "rb") as wf:
                self.assertEqual(wf.getnframes(), 3 * 16000)


# --------------------------------------------------------------------------- #
# SAVE_DIR comes from GSP_RECORDINGS_DIR (exported by the gsp-keystudio wrapper)
# --------------------------------------------------------------------------- #
class SaveDirEnvTests(unittest.TestCase):
    def save_dir_with(self, value):
        import subprocess
        env = dict(os.environ)
        env.pop("GSP_RECORDINGS_DIR", None)
        if value is not None:
            env["GSP_RECORDINGS_DIR"] = value
        proc = subprocess.run(
            [sys.executable, "-c", "import respeaker_analyzer as ra; print(ra.SAVE_DIR)"],
            capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_env_var_sets_save_dir(self):
        self.assertEqual(self.save_dir_with("/tmp/xyz"), "/tmp/xyz")

    def test_unset_defaults_to_pranav_recordings(self):
        self.assertEqual(self.save_dir_with(None), "/home/pranav/recordings")

    def test_empty_defaults_to_pranav_recordings(self):
        self.assertEqual(self.save_dir_with(""), "/home/pranav/recordings")

    def test_both_kinds_of_recording_go_to_save_dir(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(ra, "SAVE_DIR", tmp):
            rec = ra.recording_path("rec")
            ses = ra.recording_path("session")
        self.assertEqual(os.path.dirname(rec), tmp)
        self.assertEqual(os.path.dirname(ses), tmp)
        self.assertRegex(os.path.basename(rec), r"^rec_\d{8}_\d{6}_\d{3}\.wav$")
        self.assertRegex(os.path.basename(ses), r"^session_\d{8}_\d{6}_\d{3}\.wav$")


class MainErrorTests(unittest.TestCase):
    """--file / --now failures exit with a short message, not a traceback."""

    def run_main(self, argv):
        with mock.patch.object(sys, "argv", ["respeaker_analyzer.py"] + argv):
            with self.assertRaises(SystemExit) as cm:
                ra.main()
        return str(cm.exception.code)

    def test_missing_file(self):
        msg = self.run_main(["--file", "/nonexistent/x.wav"])
        self.assertTrue(msg.startswith("Error:"), msg)
        self.assertIn("No such file", msg)

    def test_not_a_wav_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.wav")
            with open(path, "w") as f:
                f.write("not audio")
            msg = self.run_main(["--file", path])
        self.assertTrue(msg.startswith("Error:"), msg)

    def test_8bit_wav_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "8bit.wav")
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(1)
                wf.setframerate(16000)
                wf.writeframes(bytes(100))
            msg = self.run_main(["--file", path])
        self.assertIn("16-bit", msg)

    def test_now_recording_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ra, "SAVE_DIR", tmp), \
                 fake_arecord_on_path(FAKE_ARECORD_FAIL="1"), \
                 no_spidev(), \
                 contextlib.redirect_stdout(io.StringIO()):
                msg = self.run_main(["--now", "--device", "plughw:9,0"])
        self.assertIn("arecord failed", msg)


if __name__ == "__main__":
    unittest.main()
