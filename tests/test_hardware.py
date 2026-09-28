"""
Hardware-facing tests for respeaker_analyzer.py.

Scope (per the task split): find_card, record, Leds, capture_and_analyze,
button_loop, and main()'s argument handling. The pure analysis math
(analyze/report/rate_level/...) is out of scope here.

No real hardware is used or required:
  * arecord      -> tests/fakes/bin/arecord (a real, executable fake that
                     answers `-l` and writes real WAV files)
  * RPi.GPIO     -> tests/fakes/RPi/GPIO.py, injected via sys.modules
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

    def _capture(self, keep):
        leds = mock.Mock()
        with fake_arecord_on_path():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                result = ra.capture_and_analyze("plughw:1,0", leds, keep)
        return result, leds, buf.getvalue()

    def test_savedir_autocreated(self):
        self.assertFalse(os.path.isdir(self.save_dir))
        self._capture(keep=True)
        self.assertTrue(os.path.isdir(self.save_dir))

    def test_keep_true_leaves_wav_and_prints_saved(self):
        result, leds, out = self._capture(keep=True)
        files = os.listdir(self.save_dir)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].startswith("rec_") and files[0].endswith(".wav"))
        self.assertIn("Saved:", out)
        self.assertIn(os.path.join(self.save_dir, files[0]), out)

    def test_two_recordings_in_same_second_both_kept(self):
        import datetime as real_dt
        times = iter([real_dt.datetime(2026, 9, 28, 3, 8, 21, 100000),
                      real_dt.datetime(2026, 9, 28, 3, 8, 21, 600000)])
        fake_dt = types.SimpleNamespace(
            datetime=types.SimpleNamespace(now=lambda: next(times)))
        with mock.patch.object(ra, "datetime", fake_dt):
            self._capture(keep=True)
            self._capture(keep=True)
        self.assertEqual(sorted(os.listdir(self.save_dir)),
                         ["rec_20260928_030821_100.wav",
                          "rec_20260928_030821_600.wav"])

    def test_keep_false_deletes_wav(self):
        result, leds, out = self._capture(keep=False)
        self.assertEqual(os.listdir(self.save_dir), [])
        self.assertNotIn("Saved:", out)

    def test_report_is_printed(self):
        _, _, out = self._capture(keep=True)
        self.assertIn("Recording 3s", out)
        self.assertIn("dBFS", out)
        self.assertIn("Hz", out)

    def test_led_sequence_red_then_blue_then_volume_color(self):
        result, leds, _ = self._capture(keep=True)
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
                    ra.capture_and_analyze("plughw:1,0", leds, True)
        # Only the "recording" (red) LED state should have been reached.
        leds.show.assert_called_once_with((255, 0, 0))


# --------------------------------------------------------------------------- #
# button_loop
# --------------------------------------------------------------------------- #
class ButtonLoopTests(unittest.TestCase):
    def setUp(self):
        sleep_patch = mock.patch.object(ra.time, "sleep", return_value=None)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def test_setmode_and_setup(self):
        with fake_gpio() as gpio_mod:
            gpio_mod.set_sequence([gpio_mod.KEY_INTERRUPT])
            leds = mock.Mock()
            with mock.patch.object(ra, "capture_and_analyze", mock.Mock()):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    ra.button_loop("plughw:1,0", leds, True)
            self.assertIn(("setmode", "BCM"), gpio_mod.calls)
            self.assertIn(("setup", 17, "IN", "PUD_UP"), gpio_mod.calls)

    def test_held_press_triggers_exactly_one_recording(self):
        with fake_gpio() as gpio_mod:
            LOW, HIGH, KI = gpio_mod.LOW, gpio_mod.HIGH, gpio_mod.KEY_INTERRUPT
            gpio_mod.set_sequence([LOW, LOW, LOW, LOW, HIGH, KI])
            leds = mock.Mock()
            capture_mock = mock.Mock()
            with mock.patch.object(ra, "capture_and_analyze", capture_mock):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    ra.button_loop("plughw:1,0", leds, True)
            self.assertEqual(capture_mock.call_count, 1)
            capture_mock.assert_called_once_with("plughw:1,0", leds, True)

    def test_one_poll_glitch_triggers_no_recording(self):
        with fake_gpio() as gpio_mod:
            LOW, HIGH, KI = gpio_mod.LOW, gpio_mod.HIGH, gpio_mod.KEY_INTERRUPT
            # LOW at the outer check, but HIGH again at the debounce re-check.
            gpio_mod.set_sequence([LOW, HIGH, KI])
            leds = mock.Mock()
            capture_mock = mock.Mock()
            with mock.patch.object(ra, "capture_and_analyze", capture_mock):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    ra.button_loop("plughw:1,0", leds, True)
            self.assertEqual(capture_mock.call_count, 0)

    def test_two_separate_presses_trigger_two_recordings(self):
        with fake_gpio() as gpio_mod:
            LOW, HIGH, KI = gpio_mod.LOW, gpio_mod.HIGH, gpio_mod.KEY_INTERRUPT
            gpio_mod.set_sequence([
                LOW, LOW, HIGH,   # press #1: top-check, debounce-confirm -> capture, release
                LOW, LOW, HIGH,   # press #2: same shape -> capture again
                KI,
            ])
            leds = mock.Mock()
            capture_mock = mock.Mock()
            with mock.patch.object(ra, "capture_and_analyze", capture_mock):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    ra.button_loop("plughw:1,0", leds, True)
            self.assertEqual(capture_mock.call_count, 2)

    def test_runtimeerror_is_printed_and_loop_continues(self):
        with fake_gpio() as gpio_mod:
            LOW, HIGH, KI = gpio_mod.LOW, gpio_mod.HIGH, gpio_mod.KEY_INTERRUPT
            gpio_mod.set_sequence([
                LOW, LOW, HIGH,   # press #1 -> raises RuntimeError
                LOW, LOW, HIGH,   # press #2 -> succeeds
                KI,
            ])
            leds = mock.Mock()
            capture_mock = mock.Mock(side_effect=[RuntimeError("boom: device busy"), None])
            with mock.patch.object(ra, "capture_and_analyze", capture_mock):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    ra.button_loop("plughw:1,0", leds, True)
            out = buf.getvalue()
            self.assertIn("boom: device busy", out)
            self.assertEqual(capture_mock.call_count, 2)

    def test_keyboard_interrupt_prints_bye_and_cleans_up(self):
        with fake_gpio() as gpio_mod:
            gpio_mod.set_sequence([gpio_mod.KEY_INTERRUPT])
            leds = mock.Mock()
            with mock.patch.object(ra, "capture_and_analyze", mock.Mock()):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    ra.button_loop("plughw:1,0", leds, True)
            self.assertIn("Bye.", buf.getvalue())
            leds.off.assert_called_once()
            self.assertIn(("cleanup",), gpio_mod.calls)


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

    def test_now_mode_default_keep_true(self):
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
        capture_mock.assert_called_once_with("plughw:1,0", leds_instance, True)
        button_loop_mock.assert_not_called()

    def test_now_mode_no_save_keep_false(self):
        argv = ["respeaker_analyzer.py", "--now", "--no-save"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(ra, "find_card", return_value="plughw:1,0"), \
             mock.patch.object(ra, "Leds") as leds_cls_mock, \
             mock.patch.object(ra, "capture_and_analyze") as capture_mock:
            leds_instance = mock.Mock()
            leds_cls_mock.return_value = leds_instance
            ra.main()
        capture_mock.assert_called_once_with("plughw:1,0", leds_instance, False)

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
        capture_mock.assert_called_once_with("plughw:9,0", leds_instance, True)

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
        button_loop_mock.assert_called_once_with("default", leds_instance, True)
        capture_mock.assert_not_called()


# --------------------------------------------------------------------------- #
# G2 gate, run as a hermetic test (SAVE_DIR patched, fake arecord on PATH,
# real end-to-end main() --now --no-save call). See report for the literal
# shell-command form of this gate and why it is also run this way.
# --------------------------------------------------------------------------- #
class G2GateTest(unittest.TestCase):
    def test_now_no_save_end_to_end_prints_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_dir = os.path.join(tmp, "recordings")
            argv = ["respeaker_analyzer.py", "--now", "--no-save"]
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
            # --no-save: nothing left behind.
            self.assertEqual(os.listdir(save_dir), [])


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
