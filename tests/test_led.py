"""Status LED behaviour, against a stand-in for the sysfs LED directory.

The point of the LED is being readable in a car with no terminal, so what
matters is that the four states produce visibly different output and that a
missing or unwritable LED never takes the logger down.
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from obd_ev.config import LedConfig  # noqa: E402
from obd_ev.led import (CONNECTED, CONNECTED_IDLE, OFF, SEARCHING,  # noqa: E402
                        LedIndicator)


def fake_led(max_brightness=1):
    d = Path(tempfile.mkdtemp())
    (d / "brightness").write_text("0\n")
    (d / "trigger").write_text("[none]\n")
    return d


def brightness(d, timeout=1.0):
    """The current level, retrying past an empty read.

    The indicator rewrites the file on every half-cycle, and `write_text`
    truncates before it writes -- so a reader can catch it empty. On a kit
    that window does not exist: the real file is sysfs, where one write is
    one syscall the kernel applies whole. It is an artefact of standing the
    LED directory in on an ordinary filesystem, and it shows up on a slow
    one, so read past it rather than letting it fail a run at random.
    """
    path = Path(d) / "brightness"
    deadline = time.time() + timeout
    while True:
        value = path.read_text().strip()
        if value or time.time() >= deadline:
            return value
        time.sleep(0.01)


def samples(led, seconds, interval=0.02):
    """Watch the brightness file and return the distinct values seen."""
    path = led.path
    seen = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            v = path.read_text().strip()
        except OSError:
            v = None
        if not seen or seen[-1] != v:
            seen.append(v)
        time.sleep(interval)
    return seen


class TestPatterns(unittest.TestCase):
    def test_connected_is_steady_on(self):
        d = fake_led()
        led = LedIndicator(LedConfig(path=str(d)))
        led.start()
        self.addCleanup(led.stop)
        self.assertTrue(led.available)
        led.set_state(CONNECTED)
        time.sleep(0.4)
        self.assertEqual(brightness(d), "1")
        # and stays on
        time.sleep(0.4)
        self.assertEqual(brightness(d), "1")

    def test_off_is_steady_off(self):
        d = fake_led()
        led = LedIndicator(LedConfig(path=str(d)))
        led.start()
        self.addCleanup(led.stop)
        led.set_state(OFF)
        time.sleep(0.5)
        self.assertEqual(brightness(d), "0")

    def test_searching_and_idle_both_blink_at_different_rates(self):
        """The two blink states have to be tellable apart by eye, so the fast
        one must produce many more transitions in the same window."""
        d1, d2 = fake_led(), fake_led()
        slow = LedIndicator(LedConfig(path=str(d1)))
        fast = LedIndicator(LedConfig(path=str(d2)))
        slow.start(); fast.start()
        self.addCleanup(slow.stop); self.addCleanup(fast.stop)
        slow.set_state(SEARCHING)
        fast.set_state(CONNECTED_IDLE)
        n_slow = len(samples(slow, 1.2))
        n_fast = len(samples(fast, 1.2))
        self.assertGreater(n_fast, n_slow,
                           f"fast={n_fast} transitions, slow={n_slow}")
        self.assertGreaterEqual(n_fast, 3, "fast blink should be obvious")

    def test_invert_flips_the_level(self):
        d = fake_led()
        led = LedIndicator(LedConfig(path=str(d), invert=True))
        led.start()
        self.addCleanup(led.stop)
        led.set_state(CONNECTED)
        time.sleep(0.4)
        self.assertEqual(brightness(d), "0")

    def test_max_brightness_is_honoured(self):
        d = fake_led()
        led = LedIndicator(LedConfig(path=str(d), max_brightness=255))
        led.start()
        self.addCleanup(led.stop)
        led.set_state(CONNECTED)
        time.sleep(0.4)
        self.assertEqual(brightness(d), "255")


class TestDegradesQuietly(unittest.TestCase):
    """An LED must never be why a kit stops collecting data."""

    def test_missing_led_directory(self):
        led = LedIndicator(LedConfig(path="/nonexistent/leds/ACT"))
        led.start()
        self.assertFalse(led.available)
        led.set_state(CONNECTED)   # must not raise
        led.stop()

    def test_unwritable_brightness(self):
        d = fake_led()
        (d / "brightness").chmod(0o444)
        self.addCleanup(lambda: (d / "brightness").chmod(0o644))
        led = LedIndicator(LedConfig(path=str(d)))
        led.start()
        self.assertFalse(led.available)
        led.stop()

    def test_disabled_does_nothing(self):
        d = fake_led()
        led = LedIndicator(LedConfig(path=str(d), enabled=False))
        led.start()
        self.assertFalse(led.available)
        led.stop()
        self.assertEqual(brightness(d), "0")


if __name__ == "__main__":
    unittest.main()
