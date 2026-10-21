"""The touchscreen dashboard, driven against a real CsvLogger.

Two things are worth pinning down. The first is that it reads what the logger
writes -- running both halves here means a change to the trip layout breaks
this test rather than the screen in the car. The second is that it only reads:
the display is the one process allowed to look at a trip while it is being
written, and a bug in it that touched a file would cost data.
"""

import csv
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from obd_ev.config import LoggerConfig  # noqa: E402
from obd_ev.logger import CsvLogger  # noqa: E402


def _load_dashboard():
    """Import scripts/dashboard.py, which is a script rather than a module.

    The guard matters: dashboard.py re-execs into the project venv when it is
    not already there, and under a test runner that would hand the venv
    interpreter the runner's argv instead of the script's.
    """
    os.environ["OBD_EV_NO_REEXEC"] = "1"
    spec = importlib.util.spec_from_file_location(
        "obd_ev_dashboard", REPO_ROOT / "scripts" / "dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dash = _load_dashboard()

FIELDS = ["device_id", "obd_connected", "state_of_charge_pct",
          "cabin_temperature_c", "lat", "fix_mode", "gps_sats_used",
          "accel_x"]
DICT = [
    {"column": "timestamp", "source": "logger"},
    {"column": "t_mono", "source": "logger"},
    {"column": "device_id", "source": "logger"},
    {"column": "obd_connected", "source": "logger"},
    {"column": "state_of_charge_pct", "name": "State of charge",
     "unit": "percent", "group": "Battery", "source": "vehicle-obdb"},
    {"column": "cabin_temperature_c", "name": "Cabin temperature",
     "unit": "celsius", "group": "Climate", "source": "vehicle-obdb"},
    {"column": "lat", "name": "Latitude", "unit": "degrees", "group": "GPS",
     "source": "gps"},
    {"column": "fix_mode", "name": "Fix quality", "unit": "scalar",
     "group": "GPS", "source": "gps"},
    {"column": "gps_sats_used", "name": "Satellites used", "unit": "scalar",
     "group": "GPS", "source": "gps"},
    {"column": "accel_x", "name": "IMU acceleration, X axis",
     "unit": "metersPerSecondSquared", "group": "IMU", "source": "imu"},
]


def snapshot_tree(root: Path):
    """Every file under `root`, with its size and mtime."""
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


class DashboardTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = LoggerConfig(output_dir=str(self.root), flush_every=1)
        self.logger = CsvLogger(self.cfg, FIELDS, device_id="P001",
                                dictionary=DICT)
        self.mon = dash.Monitor(self.root, "P001")

    def tearDown(self):
        self.mon.tail.close()
        self.logger.close()
        self._tmp.cleanup()

    def write(self, **row):
        row.setdefault("device_id", "P001")
        row.setdefault("obd_connected", 1)
        self.logger.write(row)

    def raw(self, **row):
        """Append a line directly, bypassing the logger.

        For the cases the logger cannot produce on demand -- a wall-clock
        timestamp from before the GPS correction, or rows at a chosen rate.
        Built from the logger's own field list so it stays in step with the
        header already on disk.
        """
        row.setdefault("device_id", "P001")
        row.setdefault("obd_connected", 1)
        line = ",".join(str(row.get(f, "")) for f in self.logger.fieldnames)
        with self.logger.path.open("a") as fh:
            fh.write(line + "\n")
            fh.flush()


class TestFollowsTheLogger(DashboardTestCase):
    def test_reads_rows_the_logger_wrote(self):
        for i in range(5):
            self.write(state_of_charge_pct=61.5 - i, lat=38.03, accel_x=0.1)
        self.mon.poll()
        self.assertEqual(self.mon.rows, 5)
        self.assertEqual(self.mon.signals["state_of_charge_pct"].value, "57.5")

    def test_only_new_rows_are_parsed_on_each_poll(self):
        self.write(state_of_charge_pct=61.5)
        self.mon.poll()
        offset = self.mon.tail._fh.tell()
        self.write(state_of_charge_pct=61.4)
        self.mon.poll()
        self.assertEqual(self.mon.rows, 2)
        self.assertGreater(self.mon.tail._fh.tell(), offset)

    def test_a_half_written_line_is_held_until_it_is_complete(self):
        self.write(state_of_charge_pct=61.5)
        self.mon.poll()
        tail = ",".join(str({"timestamp": "2026-01-01T00:00:00+00:00",
                             "t_mono": 1.0, "device_id": "P001",
                             "obd_connected": 1,
                             "state_of_charge_pct": 60.0}.get(f, ""))
                        for f in self.logger.fieldnames)
        head, rest = tail[:20], tail[20:]
        with self.logger.path.open("a") as fh:
            fh.write(head)
            fh.flush()
        self.mon.poll()
        self.assertEqual(self.mon.rows, 1, "partial line must not count")
        with self.logger.path.open("a") as fh:
            fh.write(rest + "\n")
            fh.flush()
        self.mon.poll()
        self.assertEqual(self.mon.rows, 2)
        self.assertEqual(self.mon.signals["state_of_charge_pct"].value, "60.0")

    def test_rotation_is_followed_and_resets_the_part(self):
        self.write(state_of_charge_pct=61.5)
        self.mon.poll()
        first = self.mon.tail.path
        self.logger.rotate()
        self.write(state_of_charge_pct=60.0)
        self.mon.poll()
        self.assertNotEqual(self.mon.tail.path, first)
        self.assertEqual(self.mon.rows, 1, "row count is per part")

    def test_the_tail_of_a_closing_part_is_not_lost(self):
        # Rows written after the last poll but before the rotation still have
        # to be read out, or every rotation silently drops the screen's view
        # of the end of the part.
        self.write(state_of_charge_pct=61.5)
        self.mon.poll()
        self.write(state_of_charge_pct=59.9)
        self.logger.rotate()
        self.mon.poll()
        self.assertEqual(self.mon.signals["state_of_charge_pct"].value, "59.9")

    def test_units_and_names_come_from_the_trips_own_dictionary(self):
        self.write(state_of_charge_pct=61.5)
        self.mon.poll()
        body, _ = dash._rows_for(self.mon, "vehicle", time.monotonic())
        self.assertEqual([(r.label, r.unit) for r in body],
                         [("State of charge", "%")])


class TestItOnlyReads(DashboardTestCase):
    def test_polling_changes_nothing_on_disk(self):
        for i in range(3):
            self.write(state_of_charge_pct=61.5 - i)
        self.logger.rotate()          # close the part so mtimes settle
        before = snapshot_tree(self.root)
        for _ in range(5):
            self.mon.poll()
            dash.snapshot(self.mon, "vehicle", 0, 10)
        self.assertEqual(snapshot_tree(self.root), before)

    def test_the_uploaders_marker_files_are_left_alone(self):
        self.write(state_of_charge_pct=61.5)
        self.mon.poll()
        self.assertTrue((self.root / ".current").exists())
        self.assertFalse((self.root / ".flush-request").exists(),
                         "the display must never request a flush")


class TestAges(DashboardTestCase):
    def test_age_follows_t_mono_not_the_wall_clock(self):
        # The Pi has no RTC, so GPS moves the wall clock by hours at boot.
        # t_mono is what keeps an age honest across that.
        self.write(state_of_charge_pct=61.5)
        self.mon.poll()
        self.raw(timestamp="1980-01-01T00:00:00+00:00",
                 t_mono=round(time.monotonic() - 42, 3),
                 state_of_charge_pct=60.0)
        self.mon.poll()
        age = self.mon.signals["state_of_charge_pct"].age(time.monotonic())
        self.assertAlmostEqual(age, 42, delta=2)

    def test_a_signal_that_never_answered_has_no_age(self):
        self.write(state_of_charge_pct=61.5)   # cabin_temperature_c left out
        self.mon.poll()
        body, _ = dash._rows_for(self.mon, "vehicle-all", time.monotonic())
        ages = {r.label: r.age for r in body}
        self.assertIsNone(ages["Cabin temperature"])
        self.assertIsNotNone(ages["State of charge"])

    def test_the_answering_view_hides_what_never_answered(self):
        self.write(state_of_charge_pct=61.5)
        self.mon.poll()
        body, _ = dash._rows_for(self.mon, "vehicle", time.monotonic())
        self.assertEqual([r.label for r in body], ["State of charge"])


class TestState(DashboardTestCase):
    def state(self):
        self.mon.poll()
        return dash._state(self.mon, time.monotonic())[0]

    def test_logging_when_the_car_answers(self):
        self.write(state_of_charge_pct=61.5)
        self.assertEqual(self.state(), "LOGGING")

    def test_no_adapter_when_the_link_is_down(self):
        self.write(obd_connected=0)
        self.assertEqual(self.state(), "NO OBD ADAPTER")

    def test_car_not_answering_when_the_link_is_up_but_silent(self):
        self.write(obd_connected=1)        # no vehicle columns
        self.assertEqual(self.state(), "CAR NOT ANSWERING")

    def test_stalled_when_the_logger_stops_writing(self):
        self.write(state_of_charge_pct=61.5)
        self.mon.poll()
        self.mon.last_row_at -= dash.STALL_SECONDS + 5
        self.assertIn("NO ROWS FOR",
                      dash._state(self.mon, time.monotonic())[0])

    def test_waiting_before_the_first_row(self):
        self.assertEqual(self.state(), "WAITING FOR LOGGER")

    def test_the_empty_note_names_the_right_problem(self):
        self.write(obd_connected=0)
        self.mon.poll()
        self.assertIn("no BLE link", dash._empty_note(self.mon, polled=2))
        self.write(obd_connected=1)
        self.mon.poll()
        self.assertIn("READY", dash._empty_note(self.mon, polled=2))
        self.assertIn("no vehicle signals configured",
                      dash._empty_note(self.mon, polled=0))


class TestPaging(DashboardTestCase):
    def test_pages_are_clamped_to_what_exists(self):
        self.write(state_of_charge_pct=61.5, cabin_temperature_c=22.0)
        self.mon.poll()
        snap = dash.snapshot(self.mon, "vehicle", page=9, per_page=1)
        self.assertEqual(snap.pages, 2)
        self.assertEqual(snap.page, 1)
        self.assertEqual(len(snap.body), 1)

    def test_an_empty_view_is_still_one_page(self):
        self.write(obd_connected=0)
        self.mon.poll()
        snap = dash.snapshot(self.mon, "vehicle", page=0, per_page=10)
        self.assertEqual((snap.pages, snap.body), (1, []))
        self.assertTrue(snap.note)


class TestRateAndFacts(DashboardTestCase):
    def test_rate_is_measured_from_the_rows_own_timestamps(self):
        base = time.monotonic()
        for i in range(10):
            self.raw(timestamp="x", t_mono=round(base + i * 0.1, 3),
                     state_of_charge_pct=60.0)
        self.mon.poll()
        self.assertAlmostEqual(self.mon.rate, 10.0, delta=0.5)

    def test_facts_report_the_gps_fix(self):
        self.write(state_of_charge_pct=61.5, fix_mode=3, gps_sats_used=9)
        self.mon.poll()
        facts = dict((k, v) for k, v, _ in
                     dash._facts(self.mon, time.monotonic()))
        self.assertEqual(facts["GPS"], "3D 9sat")


if __name__ == "__main__":
    unittest.main()
