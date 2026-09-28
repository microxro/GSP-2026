"""Fake ``RPi.GPIO`` for tests.

Real RPi.GPIO is not installed (and there is no real GPIO hardware) in the
test environment, so this stands in for it. Tests drive it by pushing a
scripted sequence of pin-read results with :func:`set_sequence`; each call
to :func:`input` pops the next value. Once the sequence is exhausted,
:func:`input` returns ``_default`` (HIGH, i.e. "button not pressed") on
every subsequent call, forever -- so a test **must** end its sequence with
the :data:`KEY_INTERRUPT` sentinel to make ``input()`` raise
``KeyboardInterrupt`` and stop an otherwise-infinite polling loop.
"""

BCM = "BCM"
BOARD = "BOARD"
IN = "IN"
OUT = "OUT"
PUD_UP = "PUD_UP"
PUD_DOWN = "PUD_DOWN"
PUD_OFF = "PUD_OFF"
LOW = 0
HIGH = 1

# Sentinel: when this value is popped from the scripted sequence, input()
# raises KeyboardInterrupt instead of returning a level. Used by tests to
# deterministically end a polling loop.
KEY_INTERRUPT = "KEY_INTERRUPT"

# Log of every setmode/setup/output/cleanup call, in order, for assertions.
calls = []

_sequence = []
_default = HIGH


def reset(default=HIGH):
    """Clear all state. Call at the start of every test that uses this fake."""
    global _default
    calls.clear()
    _sequence.clear()
    _default = default


def set_sequence(seq):
    """Set the scripted list of values input() will pop from, in order."""
    _sequence.clear()
    _sequence.extend(seq)


def setmode(mode):
    calls.append(("setmode", mode))


def setup(pin, direction, pull_up_down=None):
    calls.append(("setup", pin, direction, pull_up_down))


def input(pin):  # noqa: A001 - mirrors real RPi.GPIO's name
    if _sequence:
        val = _sequence.pop(0)
    else:
        val = _default
    if val == KEY_INTERRUPT:
        raise KeyboardInterrupt()
    return val


def output(pin, value):
    calls.append(("output", pin, value))


def cleanup():
    calls.append(("cleanup",))
