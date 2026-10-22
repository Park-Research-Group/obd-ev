"""IMU sampling rate, against a stand-in for the sensor.

The rate matters because the CSV reports the strongest and weakest
acceleration seen between rows: sampling slower than asked means sharp
events -- a pothole, a hard brake -- land between samples and never appear.
"""

import sys
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from obd_ev.config import IMUConfig  # noqa: E402
from obd_ev.imu_reader import IMUReader  # noqa: E402


class FakeSensor:
    """An MPU-6050 whose reads cost real time, which is the whole point:
    the bug this guards was sleeping a full period on top of the read."""

    def __init__(self, read_seconds=0.005):
        self.read_seconds = read_seconds
        self.reads = 0

    def get_accel_data(self):
        time.sleep(self.read_seconds)
        self.reads += 1
        return {"x": 0.1, "y": 0.2, "z": 9.8}

    def get_gyro_data(self):
        return {"x": 0.0, "y": 0.0, "z": 0.0}


def install_fake_mpu6050(sensor):
    """The reader imports mpu6050 inside its thread, so the stand-in has to
    be in sys.modules before it starts."""
    module = types.ModuleType("mpu6050")
    module.mpu6050 = lambda address: sensor
    saved = sys.modules.get("mpu6050")
    sys.modules["mpu6050"] = module
    return saved


class TestSampleRate(unittest.TestCase):
    def setUp(self):
        self.sensor = FakeSensor(read_seconds=0.005)
        self.saved = install_fake_mpu6050(self.sensor)

    def tearDown(self):
        if self.saved is None:
            sys.modules.pop("mpu6050", None)
        else:
            sys.modules["mpu6050"] = self.saved

    def test_the_read_time_comes_out_of_the_period_not_on_top_of_it(self):
        """At 100 Hz with a 5 ms read, sleeping a whole period after each
        read gives about 66 Hz. That is what a kit actually measured."""
        reader = IMUReader(IMUConfig(enabled=True, sample_hz=100))
        reader.start()
        try:
            time.sleep(1.5)
        finally:
            reader.stop()
        rate = self.sensor.reads / 1.5
        self.assertGreater(rate, 85, f"only {rate:.0f} Hz of a requested 100")
        self.assertLess(rate, 115, f"{rate:.0f} Hz is faster than requested")

    def test_stop_is_not_delayed_by_the_sleep(self):
        """The wait has to be interruptible, or shutdown waits out a period
        -- and at 1 Hz that is a second of the logger not exiting."""
        reader = IMUReader(IMUConfig(enabled=True, sample_hz=1))
        reader.start()
        time.sleep(0.2)
        began = time.monotonic()
        reader.stop()
        self.assertLess(time.monotonic() - began, 0.5)


if __name__ == "__main__":
    unittest.main()
