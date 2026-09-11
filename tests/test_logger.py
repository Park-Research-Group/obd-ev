"""Trip/part file layout and the durability behaviour it exists to provide.

On most cars the Pi loses power the instant the ignition goes off, so a trip
is in practice one power cycle and files are never closed cleanly. These tests
pin down the layout the uploader depends on and the flushing that bounds how
much an unclean shutdown costs.
"""

import csv
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from obd_ev.config import LoggerConfig  # noqa: E402
from obd_ev.logger import BOOT_ID, CsvLogger  # noqa: E402

FIELDS = ["speed_kph", "state_of_charge_pct"]
DICT = [
    {"column": "speed_kph", "name": "Vehicle speed", "unit": "kilometersPerHour",
     "group": "Movement", "source_id": "X_VSS"},
    {"column": "state_of_charge_pct", "name": "State of charge",
     "unit": "percent", "group": "Battery", "source_id": "X_SOC"},
]


def make_logger(tmp, **kw):
    cfg = LoggerConfig(output_dir=str(tmp), **kw)
    return CsvLogger(cfg, FIELDS, device_id="P003", dictionary=DICT)


class TestTripLayout(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_trip_folder_holds_parts_and_its_own_dictionary(self):
        lg = make_logger(self.tmp)
        try:
            lg.write({"speed_kph": 30})
            first = lg.path
            lg.rotate()
            lg.write({"speed_kph": 31})
            second = lg.path

            self.assertEqual(first.parent, second.parent,
                             "a rotation stays inside the same trip")
            self.assertEqual(first.parent.name, lg.trip_id)
            self.assertTrue((lg.trip_dir / "signals_P003.csv").exists(),
                            "the dictionary travels with the data it describes")
        finally:
            lg.close()

    def test_new_trip_creates_a_separate_folder(self):
        lg = make_logger(self.tmp)
        try:
            lg.write({"speed_kph": 30})
            first_trip = lg.trip_dir
            time.sleep(1.05)          # trip ids carry a second-resolution stamp
            lg.new_trip()
            lg.write({"speed_kph": 40})
            self.assertNotEqual(first_trip, lg.trip_dir)
            self.assertTrue((lg.trip_dir / "signals_P003.csv").exists())
            trips = sorted(p.name for p in self.tmp.iterdir() if p.is_dir())
            self.assertEqual(len(trips), 2)
        finally:
            lg.close()

    def test_trip_id_survives_a_clock_that_has_not_been_set(self):
        """Without an RTC two power cycles can report the same wall time. The
        boot id keeps the folders distinct, and a same-second collision inside
        one boot still gets a unique name."""
        lg = make_logger(self.tmp)
        try:
            self.assertIn(BOOT_ID, lg.trip_id)
            first = lg.trip_id
            lg.new_trip()             # immediately: same second, same boot id
            self.assertNotEqual(first, lg.trip_id)
            self.assertTrue(lg.trip_id.startswith(first))
        finally:
            lg.close()

    def test_current_marker_tracks_the_open_file(self):
        """upload.sh reads this to avoid shipping a half-written file."""
        marker = self.tmp / ".current"
        lg = make_logger(self.tmp)
        try:
            self.assertEqual(marker.read_text().strip(), str(lg.path))
            lg.rotate()
            self.assertEqual(marker.read_text().strip(), str(lg.path))
        finally:
            lg.close()
        self.assertFalse(marker.exists(), "cleared on a clean shutdown")

    def test_marker_names_the_part_before_the_file_exists(self):
        """upload.sh checks the marker right before touching a file. If the
        file could exist before the marker named it, a run in that window
        would ship it and move it out from under the open handle."""
        marker = self.tmp / ".current"
        lg = make_logger(self.tmp)
        seen = []
        real_open = Path.open

        def spy_open(path, *a, **kw):
            if path.suffix == ".csv" and "w" in (a[0] if a else kw.get("mode", "")):
                seen.append(marker.read_text().strip() == str(path))
            return real_open(path, *a, **kw)

        try:
            Path.open = spy_open
            lg.rotate()
        finally:
            Path.open = real_open
            lg.close()
        self.assertEqual(seen, [True])

    def test_names_are_utc_like_the_timestamp_column(self):
        import datetime as dt
        lg = make_logger(self.tmp)
        try:
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M")
            self.assertTrue(lg.trip_id.startswith(stamp[:-1]),
                            f"{lg.trip_id} should start with UTC {stamp}")
        finally:
            lg.close()

    def test_empty_part_is_discarded_on_rotate(self):
        """Rotating a part that never got a row must not leave a header-only
        file behind for the uploader to ship."""
        lg = make_logger(self.tmp)
        try:
            lg.rotate()
            lg.rotate()
            drives = list(lg.trip_dir.glob("drive_*.csv"))
            self.assertEqual(len(drives), 1, drives)
            self.assertEqual(drives[0], lg.path)

            # A part that did get rows is kept when the next rotation happens.
            lg.write({"speed_kph": 5})
            kept = lg.path
            lg.rotate()
            self.assertTrue(kept.exists())
            self.assertEqual(len(list(lg.trip_dir.glob("drive_*.csv"))), 2)
        finally:
            lg.close()

    def test_rows_are_readable_with_the_declared_schema(self):
        lg = make_logger(self.tmp)
        try:
            lg.write({"speed_kph": 42.5, "state_of_charge_pct": 71.5})
            lg.close()
            rows = list(csv.DictReader(lg.path.open()))
            self.assertEqual(rows[0]["speed_kph"], "42.5")
            self.assertTrue(rows[0]["timestamp"].endswith("+00:00"),
                            "timestamps must be timezone-aware")
            self.assertTrue(float(rows[0]["t_mono"]) > 0)
        finally:
            lg.close()


class TestDurability(unittest.TestCase):
    """Power is cut without warning, so unflushed rows are lost rows."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _rows_on_disk(self, path):
        with path.open() as fh:
            return max(sum(1 for _ in fh) - 1, 0)

    def test_time_based_flush_bounds_loss_at_low_row_rates(self):
        # flush_every=1000 would never trigger at these rates; the time-based
        # flush is what has to save the data.
        lg = make_logger(self.tmp, flush_every=1000, flush_seconds=0.05)
        try:
            lg.write({"speed_kph": 1})
            time.sleep(0.06)
            lg.write({"speed_kph": 2})
            self.assertGreaterEqual(
                self._rows_on_disk(lg.path), 2,
                "rows must reach disk on the time-based flush")
        finally:
            lg.close()

    def test_row_count_flush_still_applies(self):
        lg = make_logger(self.tmp, flush_every=2, flush_seconds=3600)
        try:
            for i in range(4):
                lg.write({"speed_kph": i})
            self.assertGreaterEqual(self._rows_on_disk(lg.path), 4)
        finally:
            lg.close()


if __name__ == "__main__":
    unittest.main()
