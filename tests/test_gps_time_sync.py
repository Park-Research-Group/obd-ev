"""Policy for stepping the clock from GPS.

Setting a clock from bad data is worse than a wrong clock, and gpsd hands out a
`time` field even with no fix -- it was observed reporting 2019-04-07 on a
receiver that had never locked. These tests pin the guards that stop that
reaching the system clock.
"""

import importlib.util
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location(
    "gps_time_sync", ROOT / "scripts" / "gps_time_sync.py")
gts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gts)

NOW = time.time()
DAY = 86400.0


class TestPlausible(unittest.TestCase):
    def test_rejects_the_gps_week_rollover_default(self):
        # The exact value seen from a receiver with no lock.
        bogus = time.mktime(time.strptime("2019-04-07", "%Y-%m-%d"))
        self.assertFalse(gts.plausible(bogus))

    def test_rejects_epoch_zero_and_far_future(self):
        self.assertFalse(gts.plausible(0))
        self.assertFalse(gts.plausible(time.mktime(
            time.strptime("2099-01-01", "%Y-%m-%d"))))

    def test_accepts_now(self):
        self.assertTrue(gts.plausible(NOW))


class TestDecide(unittest.TestCase):
    def test_no_readings_does_nothing(self):
        target, reason = gts.decide([])
        self.assertIsNone(target)
        self.assertIn("no trusted GPS time", reason)

    def test_a_single_reading_is_not_enough(self):
        target, reason = gts.decide([(NOW + DAY, NOW)])
        self.assertIsNone(target, "one reading must not move the clock")
        self.assertIn("two that agree", reason)

    def test_disagreeing_readings_are_rejected(self):
        target, reason = gts.decide([(NOW + DAY, NOW), (NOW + 5 * DAY, NOW)])
        self.assertIsNone(target)
        self.assertIn("disagree", reason)

    def test_steps_on_the_real_p001_error(self):
        """P001 was 1.05 days behind GPS; that must be corrected."""
        offset = 1.05 * DAY
        target, reason = gts.decide(
            [(NOW + offset, NOW), (NOW + offset + 0.3, NOW + 0.3)])
        self.assertIsNotNone(target)
        self.assertAlmostEqual(target, NOW + offset + 0.3, delta=1)
        self.assertIn("+90", reason.replace(",", ""))  # ~+90720s

    def test_leaves_a_good_clock_alone(self):
        target, reason = gts.decide([(NOW + 0.4, NOW), (NOW + 0.5, NOW + 0.1)])
        self.assertIsNone(target)
        self.assertIn("leaving it", reason)

    def test_force_steps_even_a_small_offset(self):
        target, _ = gts.decide([(NOW + 0.4, NOW), (NOW + 0.5, NOW + 0.1)],
                               force=True)
        self.assertIsNotNone(target)

    def test_threshold_is_honoured(self):
        pair = [(NOW + 3.0, NOW), (NOW + 3.1, NOW + 0.1)]
        self.assertIsNone(gts.decide(pair, threshold=10.0)[0])
        self.assertIsNotNone(gts.decide(pair, threshold=1.0)[0])

    def test_a_clock_ahead_of_gps_is_also_corrected(self):
        target, reason = gts.decide(
            [(NOW - DAY, NOW), (NOW - DAY + 0.2, NOW + 0.2)])
        self.assertIsNotNone(target)
        self.assertIn("-", reason)


if __name__ == "__main__":
    unittest.main()
