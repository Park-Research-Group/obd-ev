import logging
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

from .config import IMUConfig

log = logging.getLogger(__name__)

FIELDS = ["accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z",
          "accel_mag_max", "accel_mag_min", "imu_samples", "imu_age_s"]

# Column documentation for signals.csv.
DESCRIPTIONS = [
    ("accel_x", "IMU acceleration, X axis (low-pass filtered)", "metersPerSecondSquared"),
    ("accel_y", "IMU acceleration, Y axis (low-pass filtered)", "metersPerSecondSquared"),
    ("accel_z", "IMU acceleration, Z axis (low-pass filtered)", "metersPerSecondSquared"),
    ("gyro_x", "IMU angular rate, X axis (low-pass filtered)", "degreesPerSecond"),
    ("gyro_y", "IMU angular rate, Y axis (low-pass filtered)", "degreesPerSecond"),
    ("gyro_z", "IMU angular rate, Z axis (low-pass filtered)", "degreesPerSecond"),
    ("accel_mag_max", "Largest acceleration magnitude since the previous row",
     "metersPerSecondSquared"),
    ("accel_mag_min", "Smallest acceleration magnitude since the previous row",
     "metersPerSecondSquared"),
    ("imu_samples", "IMU samples averaged into this row", "scalar"),
    ("imu_age_s", "Age of the filtered sample when the row was written", "seconds"),
]


@dataclass
class IMUSample:
    accel_x: float = 0.0
    accel_y: float = 0.0
    accel_z: float = 0.0
    gyro_x: float = 0.0
    gyro_y: float = 0.0
    gyro_z: float = 0.0
    t: float = 0.0


class IMUReader:
    """Background MPU-6050 sampler with simple per-axis low-pass filter."""

    def __init__(self, cfg: IMUConfig):
        self.cfg = cfg
        self._sample = IMUSample()
        # Extremes seen since the last latest() call. The CSV row rate is set
        # by the OBD link, far below the IMU's 100Hz, so without these every
        # pothole and hard brake between rows is thrown away.
        self._mag_max = 0.0
        self._mag_min = 0.0
        self._count = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not self.cfg.enabled:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            from mpu6050 import mpu6050
        except ImportError:
            log.error("mpu6050-raspberrypi not importable under %s; IMU "
                      "disabled. It is installed in the project venv -- run "
                      "via .venv/bin/python, not the system interpreter.",
                      sys.executable)
            return

        period = 1.0 / max(1, self.cfg.sample_hz)
        a = self.cfg.lowpass_alpha
        f = IMUSample()
        primed = False
        sensor = None
        errors = 0
        last_warning = 0.0

        def warn(msg, *args):
            # A sensor that drops off the bus would otherwise log at
            # sample_hz for as long as it stays gone.
            nonlocal last_warning
            now = time.monotonic()
            if now - last_warning >= 60:
                log.warning(msg + " (repeating at most once a minute)", *args)
                last_warning = now

        while not self._stop.is_set():
            if sensor is None:
                try:
                    sensor = mpu6050(self.cfg.i2c_address)
                    errors = 0
                except Exception as e:
                    warn("MPU-6050 init failed: %s", e)
                    self._stop.wait(5.0)
                    continue
            try:
                ac = sensor.get_accel_data()
                gy = sensor.get_gyro_data()
            except Exception as e:
                errors += 1
                warn("IMU read error: %s", e)
                if errors >= 20:
                    sensor = None     # re-open the bus rather than spin
                    errors = 0
                self._stop.wait(period)
                continue
            errors = 0

            if not primed:
                # Seed the filter with the first real reading; starting from
                # zero otherwise takes seconds to converge at alpha=0.1.
                f.accel_x, f.accel_y, f.accel_z = ac["x"], ac["y"], ac["z"]
                f.gyro_x, f.gyro_y, f.gyro_z = gy["x"], gy["y"], gy["z"]
                primed = True

            f.accel_x = a * ac["x"] + (1 - a) * f.accel_x
            f.accel_y = a * ac["y"] + (1 - a) * f.accel_y
            f.accel_z = a * ac["z"] + (1 - a) * f.accel_z
            f.gyro_x  = a * gy["x"] + (1 - a) * f.gyro_x
            f.gyro_y  = a * gy["y"] + (1 - a) * f.gyro_y
            f.gyro_z  = a * gy["z"] + (1 - a) * f.gyro_z

            mag = (ac["x"] ** 2 + ac["y"] ** 2 + ac["z"] ** 2) ** 0.5
            with self._lock:
                f.t = time.monotonic()
                self._sample = IMUSample(**vars(f))
                self._mag_max = mag if not self._count else max(self._mag_max, mag)
                self._mag_min = mag if not self._count else min(self._mag_min, mag)
                self._count += 1
            time.sleep(period)

    def latest(self) -> Dict[str, float]:
        """Filtered sample plus the extremes seen since the previous call.
        Reading resets the extremes, so each CSV row covers one interval."""
        with self._lock:
            s = self._sample
            mag_max, mag_min, count = self._mag_max, self._mag_min, self._count
            self._mag_max = self._mag_min = 0.0
            self._count = 0
        out = {k: v for k, v in vars(s).items() if k != "t"}
        out["accel_mag_max"] = round(mag_max, 4) if count else None
        out["accel_mag_min"] = round(mag_min, 4) if count else None
        out["imu_samples"] = count
        out["imu_age_s"] = round(time.monotonic() - s.t, 2) if s.t else None
        return out

    def stop(self) -> None:
        self._stop.set()
