"""The offline cleaner: what it repairs, and what it refuses to invent.

The kit has no RTC, so its wall clock is wrong until GPS or NTP corrects it --
in the field by as much as a day, with the correction landing *inside* the
recording as a step. Rows are also sparse by construction: the scheduler polls
commands round robin, so a row holds whatever answered in that cycle. These
tests pin down the repairs that follow from both, and that the cleaner reports
rather than guesses when the evidence is not there.
"""

import csv
import datetime as dt
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Loaded by path, not imported as a package: the script is deliberately
# standalone so it can be handed to collaborators on its own, and this keeps
# that honest -- if it ever grows an import from src/, this stops working.
_spec = importlib.util.spec_from_file_location(
    "clean_trips", REPO_ROOT / "scripts" / "clean_trips.py")
clean_trips = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(clean_trips)


BASE_COLUMNS = ["timestamp", "t_mono", "device_id", "obd_connected",
                "vehicle_speed_kph", "lat", "lon", "gps_time"]

DICTIONARY = [
    {"column": "vehicle_speed_kph", "name": "Vehicle speed",
     "unit": "kilometersPerHour", "group": "Movement", "category": "Movement",
     "source": "obdb", "source_id": "X_VSS", "command": "22000D",
     "period_s": "0.25", "min": "0.0", "max": "255"},
    {"column": "lat", "name": "Latitude", "unit": "degrees", "group": "GPS",
     "category": "GPS", "source": "gps", "source_id": "lat",
     "command": "", "period_s": "", "min": "-90", "max": "90"},
]

DICTIONARY_FIELDS = ["column", "name", "unit", "group", "category", "source",
                     "source_id", "command", "period_s", "min", "max"]


def iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


class TripBuilder:
    """Writes a trip folder the way a kit would have left it."""

    def __init__(self, root: Path, name: str, device: str = "P001"):
        self.dir = root / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.device = device
        self.columns = list(BASE_COLUMNS)

    def write_dictionary(self, rows=None) -> "TripBuilder":
        path = self.dir / f"signals_{self.device}.csv"
        with path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=DICTIONARY_FIELDS,
                                    extrasaction="ignore")
            writer.writeheader()
            for row in (DICTIONARY if rows is None else rows):
                writer.writerow(row)
        return self

    def write_part(self, stamp: str, rows, columns=None) -> "TripBuilder":
        columns = columns or self.columns
        path = self.dir / f"drive_{self.device}_{stamp}.csv"
        with path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns,
                                    extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c, "") for c in columns})
        return self


def read_csv(path: Path):
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def run(root: Path, out: Path, *extra, skip_without_obd: bool = False):
    """Drive the cleaner.

    Trips the car never answered are skipped by default, and most fixtures
    here are about something else entirely -- clock repair, ordering, output
    layout -- so they keep them. The tests that are about the skip ask for it.
    """
    argv = [str(root), "-o", str(out), "--quiet"]
    if not skip_without_obd:
        argv.append("--keep-without-obd")
    argv += list(extra)
    code = clean_trips.main(argv)
    assert code == 0, f"cleaner exited {code}"
    trips = sorted((out / "trips").glob("*.csv"))
    summary = read_csv(out / "trips_summary.csv")
    return trips, summary


class CleanerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "P001"
        self.root.mkdir(parents=True)
        self.out = Path(self._tmp.name) / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def rows_of(self, path: Path):
        return read_csv(path)


class TestClockRepair(CleanerTestCase):
    """`gps_time` is the only clock on the kit that was never wrong."""

    def test_rebuilds_timestamps_from_gps_when_the_pi_clock_is_wrong(self):
        # The Pi believes it is a day earlier than the receiver does.
        true_epoch = 1790715900.0
        skew = 25 * 3600.0
        rows = [{"timestamp": iso(true_epoch - skew + i), "t_mono": 100.0 + i,
                 "device_id": "P001", "obd_connected": "1",
                 "gps_time": iso(true_epoch + i).replace("+00:00", "Z")}
                for i in range(10)]
        TripBuilder(self.root, "20260928_195204_0b1dadf0") \
            .write_dictionary().write_part("20260928_195204", rows)

        trips, summary = run(self.root, self.out)
        cleaned = self.rows_of(trips[0])

        self.assertEqual(summary[0]["clock_source"], "gps")
        self.assertAlmostEqual(float(summary[0]["clock_correction_h"]),
                               25.0, places=2)
        first = dt.datetime.fromisoformat(cleaned[0]["timestamp"])
        self.assertAlmostEqual(first.timestamp(), true_epoch, delta=1.0)
        # The Pi's own reading is kept, so the repair can be audited.
        self.assertTrue(cleaned[0]["timestamp_pi"].startswith("2026-09-28"))

    def test_the_file_is_named_for_the_corrected_start_not_the_folder(self):
        """Folder names come from the same wrong clock, so they mislead."""
        true_epoch = 1790715900.0        # 2026-09-29T21:05Z
        rows = [{"timestamp": iso(true_epoch - 25 * 3600 + i),
                 "t_mono": 100.0 + i, "device_id": "P001",
                 "gps_time": iso(true_epoch + i).replace("+00:00", "Z")}
                for i in range(5)]
        TripBuilder(self.root, "20260928_195204_0b1dadf0") \
            .write_dictionary().write_part("20260928_195204", rows)

        trips, _ = run(self.root, self.out)
        self.assertIn("20260929T2105", trips[0].name)
        self.assertIn("0b1dadf0", trips[0].name)   # still traceable

    def test_carries_the_corrected_clock_back_over_a_mid_trip_step(self):
        """NTP lands seconds into a trip; the rows before it are on the old
        clock and the rows after it are on the good one."""
        good = 1790715900.0
        rows = [{"timestamp": iso(good - 59 * 3600 + i), "t_mono": 40.0 + i,
                 "device_id": "P001"} for i in range(3)]
        rows += [{"timestamp": iso(good + i), "t_mono": 43.0 + i,
                  "device_id": "P001"} for i in range(7)]
        TripBuilder(self.root, "20260907_235305_c9feb6d6") \
            .write_dictionary().write_part("20260907_235305", rows)

        trips, summary = run(self.root, self.out)
        cleaned = self.rows_of(trips[0])

        self.assertEqual(summary[0]["clock_source"], "pi_corrected")
        stamps = [dt.datetime.fromisoformat(r["timestamp"]) for r in cleaned]
        self.assertEqual(stamps, sorted(stamps))
        # The whole trip lands on the post-correction clock, so it lasts the
        # ten seconds t_mono says it did, not the 59 hours the step implied.
        self.assertLess((stamps[-1] - stamps[0]).total_seconds(), 60)
        self.assertLess(float(summary[0]["duration_min"]), 1.0)

    def test_duration_comes_from_t_mono_not_the_wall_clock(self):
        rows = [{"timestamp": iso(1790000000.0), "t_mono": 10.0,
                 "device_id": "P001"},
                {"timestamp": iso(1790000000.0 + 9 * 3600), "t_mono": 70.0,
                 "device_id": "P001"}]
        TripBuilder(self.root, "20260101_000000_abcd1234") \
            .write_dictionary().write_part("20260101_000000", rows)

        _, summary = run(self.root, self.out)
        self.assertAlmostEqual(float(summary[0]["duration_min"]), 1.0, places=2)

    def test_says_so_rather_than_guessing_when_there_is_no_reference(self):
        rows = [{"timestamp": iso(1790000000.0 + i), "t_mono": 10.0 + i,
                 "device_id": "P001"} for i in range(5)]
        TripBuilder(self.root, "20260101_000000_abcd1234") \
            .write_dictionary().write_part("20260101_000000", rows)

        trips, summary = run(self.root, self.out)
        self.assertEqual(summary[0]["clock_source"], "pi")
        self.assertIn("may be badly wrong", summary[0]["notes"])
        self.assertEqual(self.rows_of(trips[0])[0]["clock_source"], "pi")

    def test_ignores_gps_time_that_disagrees_with_itself(self):
        """Scattered `gps_time` against monotonic time is not a clock."""
        base = 1790000000.0
        rows = [{"timestamp": iso(base + i), "t_mono": 10.0 + i,
                 "device_id": "P001",
                 "gps_time": iso(base + i * 500).replace("+00:00", "Z")}
                for i in range(6)]
        TripBuilder(self.root, "20260101_000000_abcd1234") \
            .write_dictionary().write_part("20260101_000000", rows)

        _, summary = run(self.root, self.out)
        self.assertNotEqual(summary[0]["clock_source"], "gps")
        self.assertIn("did not trust it", summary[0]["notes"])


class TestRowHygiene(CleanerTestCase):
    def test_orders_parts_by_monotonic_time_and_drops_repeats(self):
        early = [{"timestamp": iso(1790000000.0 + i), "t_mono": 10.0 + i,
                  "device_id": "P001"} for i in range(3)]
        late = [{"timestamp": iso(1790000000.0 + 20 + i), "t_mono": 30.0 + i,
                 "device_id": "P001"} for i in range(3)]
        trip = TripBuilder(self.root, "20260101_000000_abcd1234") \
            .write_dictionary()
        # Written out of order, and with the boundary row repeated: the
        # uploader can ship a part twice, and a part can be reopened.
        trip.write_part("20260101_000100", late)
        trip.write_part("20260101_000000", early + [late[0]])

        trips, summary = run(self.root, self.out)
        cleaned = self.rows_of(trips[0])

        self.assertEqual([r["t_mono"] for r in cleaned],
                         ["10.0", "11.0", "12.0", "30.0", "31.0", "32.0"])
        self.assertEqual(int(summary[0]["rows"]), 6)
        self.assertIn("duplicate", summary[0]["notes"])

    def test_drops_rows_a_power_cut_left_without_a_monotonic_time(self):
        rows = [{"timestamp": iso(1790000000.0), "t_mono": 10.0,
                 "device_id": "P001"},
                {"timestamp": iso(1790000001.0), "t_mono": "",
                 "device_id": "P001"},
                {"timestamp": iso(1790000002.0), "t_mono": 12.0,
                 "device_id": "P001"}]
        TripBuilder(self.root, "20260101_000000_abcd1234") \
            .write_dictionary().write_part("20260101_000000", rows)

        trips, summary = run(self.root, self.out)
        self.assertEqual(len(self.rows_of(trips[0])), 2)
        self.assertIn("no usable t_mono", summary[0]["notes"])

    def test_skips_a_trip_with_nothing_in_it(self):
        TripBuilder(self.root, "20260101_000000_abcd1234") \
            .write_dictionary().write_part("20260101_000000", [])
        trips, summary = run(self.root, self.out)
        self.assertEqual(trips, [])
        self.assertEqual(summary, [])


class TestValueHygiene(CleanerTestCase):
    def test_blanks_values_the_signal_says_it_cannot_produce(self):
        """A decode outside the signal's own range is a decode that failed."""
        rows = [{"timestamp": iso(1790000000.0), "t_mono": 10.0,
                 "device_id": "P001", "vehicle_speed_kph": "50"},
                {"timestamp": iso(1790000001.0), "t_mono": 11.0,
                 "device_id": "P001", "vehicle_speed_kph": "9999"},
                {"timestamp": iso(1790000002.0), "t_mono": 12.0,
                 "device_id": "P001", "vehicle_speed_kph": "60"}]
        TripBuilder(self.root, "20260101_000000_abcd1234") \
            .write_dictionary().write_part("20260101_000000", rows)

        trips, summary = run(self.root, self.out)
        cleaned = self.rows_of(trips[0])
        self.assertEqual([r["vehicle_speed_kph"] for r in cleaned],
                         ["50", "", "60"])
        self.assertIn("out-of-range", summary[0]["notes"])

    def test_drops_columns_no_row_ever_filled(self):
        rows = [{"timestamp": iso(1790000000.0 + i), "t_mono": 10.0 + i,
                 "device_id": "P001", "vehicle_speed_kph": "50"}
                for i in range(3)]
        TripBuilder(self.root, "20260101_000000_abcd1234") \
            .write_dictionary().write_part("20260101_000000", rows)

        trips, _ = run(self.root, self.out)
        self.assertNotIn("lat", self.rows_of(trips[0])[0])

        run(self.root, self.out, "--keep-empty-columns")
        kept = sorted((self.out / "trips").glob("*.csv"))[0]
        self.assertIn("lat", self.rows_of(kept)[0])

    def test_runs_without_a_data_dictionary(self):
        """An upload can arrive with its signals.csv missing."""
        rows = [{"timestamp": iso(1790000000.0 + i), "t_mono": 10.0 + i,
                 "device_id": "P001", "vehicle_speed_kph": "9999"}
                for i in range(3)]
        TripBuilder(self.root, "20260101_000000_abcd1234") \
            .write_part("20260101_000000", rows)

        trips, summary = run(self.root, self.out)
        # Nothing to range-check against, so the value stands and is flagged.
        self.assertEqual(self.rows_of(trips[0])[0]["vehicle_speed_kph"], "9999")
        self.assertIn("no signals", summary[0]["notes"])


class TestForwardFill(CleanerTestCase):
    """Filling is opt-in: it repeats a value the car did not report."""

    def _sparse_trip(self):
        rows = []
        for i in range(12):
            row = {"timestamp": iso(1790000000.0 + i * 2),
                   "t_mono": 10.0 + i * 2, "device_id": "P001"}
            if i % 4 == 0:                      # sampled every 8 seconds
                row["vehicle_speed_kph"] = str(40 + i)
            rows.append(row)
        TripBuilder(self.root, "20260101_000000_abcd1234") \
            .write_dictionary().write_part("20260101_000000", rows)

    def test_off_by_default(self):
        self._sparse_trip()
        trips, _ = run(self.root, self.out)
        filled = [r["vehicle_speed_kph"] for r in self.rows_of(trips[0])]
        self.assertEqual(filled.count(""), 9)

    def test_fills_from_the_rate_the_signal_actually_achieved(self):
        """The declared period is the scheduler's target; over BLE it is not
        met, so a window built on it would fill nothing."""
        self._sparse_trip()
        trips, _ = run(self.root, self.out, "--fill")
        filled = [r["vehicle_speed_kph"] for r in self.rows_of(trips[0])]
        self.assertEqual(filled[:5], ["40", "40", "40", "40", "44"])
        self.assertEqual(filled.count(""), 0)

    def test_stops_rather_than_flatlining_when_a_signal_goes_away(self):
        rows = [{"timestamp": iso(1790000000.0), "t_mono": 10.0,
                 "device_id": "P001", "vehicle_speed_kph": "50"}]
        rows += [{"timestamp": iso(1790000000.0 + 30 + i * 10),
                  "t_mono": 40.0 + i * 10, "device_id": "P001"}
                 for i in range(5)]
        TripBuilder(self.root, "20260101_000000_abcd1234") \
            .write_dictionary().write_part("20260101_000000", rows)

        trips, _ = run(self.root, self.out, "--fill", "--max-hold", "20")
        filled = [r["vehicle_speed_kph"] for r in self.rows_of(trips[0])]
        self.assertEqual(filled[0], "50")
        self.assertEqual(filled[-1], "")

    def test_never_fills_the_columns_that_describe_their_own_row(self):
        rows = [{"timestamp": iso(1790000000.0), "t_mono": 10.0,
                 "device_id": "P001", "gps_time": "2026-09-01T00:00:00.000Z"},
                {"timestamp": iso(1790000001.0), "t_mono": 11.0,
                 "device_id": "P001"},
                {"timestamp": iso(1790000002.0), "t_mono": 12.0,
                 "device_id": "P001"}]
        TripBuilder(self.root, "20260101_000000_abcd1234") \
            .write_dictionary().write_part("20260101_000000", rows)

        trips, _ = run(self.root, self.out, "--fill")
        cleaned = self.rows_of(trips[0])
        self.assertEqual(cleaned[1].get("gps_time", ""), "")

    def test_does_not_let_filled_rows_inflate_the_summary(self):
        """A filled lat/lon is not a fix the receiver ever had."""
        rows = []
        for i in range(8):
            row = {"timestamp": iso(1790000000.0 + i), "t_mono": 10.0 + i,
                   "device_id": "P001"}
            if i % 4 == 0:
                row["lat"], row["lon"] = "38.0", "-78.0"
            rows.append(row)
        TripBuilder(self.root, "20260101_000000_abcd1234") \
            .write_dictionary().write_part("20260101_000000", rows)

        _, plain = run(self.root, self.out)
        _, filled = run(self.root, self.out, "--fill")
        self.assertEqual(plain[0]["gps_fix_rows"], "2")
        self.assertEqual(filled[0]["gps_fix_rows"], plain[0]["gps_fix_rows"])
        self.assertEqual(filled[0]["distance_km"], plain[0]["distance_km"])


class TestDatasetLevelOutputs(CleanerTestCase):
    def _two_schemas(self):
        """A kit reassigned to a profiled vehicle logs more columns than it
        did before, so a dataset legitimately holds two shapes of trip."""
        narrow = ["timestamp", "t_mono", "device_id", "vehicle_speed_kph"]
        wide = narrow + ["hv_battery_charge_pct"]
        a = TripBuilder(self.root, "20260101_000000_aaaaaaaa").write_dictionary()
        a.write_part("20260101_000000",
                     [{"timestamp": iso(1790000000.0 + i), "t_mono": 10.0 + i,
                       "device_id": "P001", "vehicle_speed_kph": "50"}
                      for i in range(3)], columns=narrow)
        b = TripBuilder(self.root, "20260102_000000_bbbbbbbb").write_dictionary(
            DICTIONARY + [{"column": "hv_battery_charge_pct",
                           "name": "State of charge", "unit": "percent",
                           "group": "Battery", "category": "Battery",
                           "source": "obdb", "source_id": "X_SOC",
                           "period_s": "10", "min": "0", "max": "100"}])
        b.write_part("20260102_000000",
                     [{"timestamp": iso(1790100000.0 + i), "t_mono": 10.0 + i,
                       "device_id": "P001", "vehicle_speed_kph": "60",
                       "hv_battery_charge_pct": "80"}
                      for i in range(3)], columns=wide)

    def test_merges_differing_schemas_into_one_table(self):
        self._two_schemas()
        run(self.root, self.out, "--merge")
        merged = read_csv(self.out / "all_trips.csv")

        self.assertEqual(len(merged), 6)
        self.assertIn("hv_battery_charge_pct", merged[0])
        # The trip that never had the column is empty there, not wrong there.
        self.assertEqual(merged[0]["hv_battery_charge_pct"], "")
        self.assertEqual(len({r["trip_id"] for r in merged}), 2)

    def test_writes_one_dictionary_covering_every_column_written(self):
        self._two_schemas()
        run(self.root, self.out, "--merge")
        signals = {r["column"]: r
                   for r in read_csv(self.out / "signals.csv")}
        self.assertIn("hv_battery_charge_pct", signals)
        self.assertEqual(signals["vehicle_speed_kph"]["source_id"], "X_VSS")
        self.assertEqual(signals["hv_battery_charge_pct"]["unit"], "percent")

    def test_summary_is_ordered_by_corrected_start_time(self):
        """Folder order is clock order, and the clock was wrong."""
        late_folder = TripBuilder(self.root, "20260101_000000_aaaaaaaa") \
            .write_dictionary()
        true_epoch = 1790715900.0
        late_folder.write_part("20260101_000000", [
            {"timestamp": iso(1700000000.0 + i), "t_mono": 10.0 + i,
             "device_id": "P001",
             "gps_time": iso(true_epoch + i).replace("+00:00", "Z")}
            for i in range(3)])
        early_folder = TripBuilder(self.root, "20260102_000000_bbbbbbbb") \
            .write_dictionary()
        early_folder.write_part("20260102_000000", [
            {"timestamp": iso(1700000000.0 + i), "t_mono": 10.0 + i,
             "device_id": "P001",
             "gps_time": iso(true_epoch - 86400 + i).replace("+00:00", "Z")}
            for i in range(3)])

        _, summary = run(self.root, self.out)
        starts = [r["start_utc"] for r in summary]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(summary[0]["trip_id"], "20260102_000000_bbbbbbbb")

    def test_dry_run_writes_nothing_at_all(self):
        rows = [{"timestamp": iso(1790000000.0 + i), "t_mono": 10.0 + i,
                 "device_id": "P001"} for i in range(3)]
        TripBuilder(self.root, "20260101_000000_abcd1234") \
            .write_dictionary().write_part("20260101_000000", rows)

        self.assertEqual(clean_trips.main(
            [str(self.root), "-o", str(self.out), "--dry-run", "--quiet"]), 0)
        self.assertFalse(self.out.exists())

    def test_never_touches_the_input(self):
        rows = [{"timestamp": iso(1790000000.0 + i), "t_mono": 10.0 + i,
                 "device_id": "P001"} for i in range(3)]
        TripBuilder(self.root, "20260101_000000_abcd1234") \
            .write_dictionary().write_part("20260101_000000", rows)
        before = {p: p.read_bytes()
                  for p in sorted(self.root.rglob("*")) if p.is_file()}

        run(self.root, self.out, "--fill", "--merge")

        after = {p: p.read_bytes()
                 for p in sorted(self.root.rglob("*")) if p.is_file()}
        self.assertEqual(before, after)


class TestTripsWithoutOBD(CleanerTestCase):
    """Most trips in the field are bench and GPS runs where the car never
    answered. They are skipped by default: the point of the dataset is the
    vehicle."""

    def _trip(self, name, rows, columns=None, dictionary=None):
        builder = TripBuilder(self.root, name).write_dictionary(dictionary)
        builder.write_part(name.split("_")[1] or "000000", rows,
                           columns=columns)
        return builder

    def _gps_only_rows(self):
        return [{"timestamp": iso(1790000000.0 + i), "t_mono": 10.0 + i,
                 "device_id": "P001", "obd_connected": "0",
                 "lat": "38.0", "lon": "-78.0"} for i in range(4)]

    def _with_obd_rows(self):
        rows = self._gps_only_rows()
        rows[2]["vehicle_speed_kph"] = "45"
        return rows

    def test_skips_a_trip_the_car_never_answered(self):
        self._trip("20260101_000000_aaaaaaaa", self._gps_only_rows())
        trips, summary = run(self.root, self.out, skip_without_obd=True)
        self.assertEqual(trips, [])
        self.assertEqual(summary, [])

    def test_keeps_a_trip_with_even_one_vehicle_value(self):
        self._trip("20260101_000000_bbbbbbbb", self._with_obd_rows())
        trips, summary = run(self.root, self.out, skip_without_obd=True)
        self.assertEqual(len(trips), 1)
        self.assertEqual(summary[0]["obd_signal_rows"], "1")

    def test_keeps_them_when_asked(self):
        self._trip("20260101_000000_aaaaaaaa", self._gps_only_rows())
        trips, summary = run(self.root, self.out)
        self.assertEqual(len(trips), 1)
        self.assertEqual(summary[0]["obd_signal_rows"], "0")

    def test_a_link_that_stayed_up_is_not_a_reading(self):
        """`obd_connected` says the ELM327 answered, not that the car did."""
        rows = self._gps_only_rows()
        for row in rows:
            row["obd_connected"] = "1"
        self._trip("20260101_000000_aaaaaaaa", rows)
        trips, _ = run(self.root, self.out, skip_without_obd=True)
        self.assertEqual(trips, [])

    def test_a_value_the_range_check_rejected_is_not_a_reading(self):
        rows = self._gps_only_rows()
        rows[1]["vehicle_speed_kph"] = "9999"     # max is 255
        self._trip("20260101_000000_aaaaaaaa", rows)
        trips, _ = run(self.root, self.out, skip_without_obd=True)
        self.assertEqual(trips, [])

    def test_gps_and_imu_columns_do_not_count_as_the_car(self):
        columns = ["timestamp", "t_mono", "device_id", "lat", "accel_x"]
        rows = [{"timestamp": iso(1790000000.0 + i), "t_mono": 10.0 + i,
                 "device_id": "P001", "lat": "38.0", "accel_x": "9.8"}
                for i in range(3)]
        self._trip("20260101_000000_aaaaaaaa", rows, columns=columns)
        trips, _ = run(self.root, self.out, skip_without_obd=True)
        self.assertEqual(trips, [])

    def test_falls_back_to_column_names_without_a_dictionary(self):
        columns = ["timestamp", "t_mono", "device_id", "lat",
                   "hv_battery_charge_pct"]
        rows = [{"timestamp": iso(1790000000.0 + i), "t_mono": 10.0 + i,
                 "device_id": "P001", "lat": "38.0"} for i in range(3)]
        rows[1]["hv_battery_charge_pct"] = "80"
        builder = TripBuilder(self.root, "20260101_000000_aaaaaaaa")
        builder.write_part("20260101_000000", rows, columns=columns)

        trips, summary = run(self.root, self.out, skip_without_obd=True)
        self.assertEqual(len(trips), 1)
        self.assertEqual(summary[0]["obd_signal_rows"], "1")

    def test_the_report_says_which_trips_went_and_why(self):
        self._trip("20260101_000000_aaaaaaaa", self._gps_only_rows())
        self._trip("20260102_000000_bbbbbbbb", self._with_obd_rows())
        run(self.root, self.out, skip_without_obd=True)

        report = (self.out / "cleaning_report.txt").read_text()
        self.assertIn("1 trip(s) written", report)
        self.assertIn("1 skipped", report)
        self.assertIn("20260101_000000_aaaaaaaa: no OBD readings", report)


class TestOBDTiming(CleanerTestCase):
    """How long between readings -- the question the polling interval is."""

    def _trip(self, name="20260101_000000_aaaaaaaa", speed_every=4, rows=24):
        """A signal sampled every `speed_every` rows, one row per second."""
        data = []
        for i in range(rows):
            row = {"timestamp": iso(1790000000.0 + i), "t_mono": 10.0 + i,
                   "device_id": "P001", "lat": "38.0"}
            if i % speed_every == 0:
                row["vehicle_speed_kph"] = "45"
            data.append(row)
        TripBuilder(self.root, name).write_dictionary() \
            .write_part("20260101_000000", data)

    def test_measures_the_interval_between_readings_of_one_signal(self):
        self._trip(speed_every=4)
        run(self.root, self.out)
        timing = {r["column"]: r for r in read_csv(self.out / "obd_timing.csv")}

        speed = timing["vehicle_speed_kph"]
        self.assertEqual(float(speed["median_s"]), 4.0)
        self.assertEqual(speed["samples"], "6")
        # Declared 0.25 s in the fixture dictionary, achieved 4 s.
        self.assertEqual(speed["times_slower_than_declared"], "16.0")

    def test_reports_the_row_gap_separately_from_the_signal_interval(self):
        """The kit can hear from the car often and still refresh any one
        signal rarely: a hundred commands share one link."""
        data = []
        for i in range(12):
            row = {"timestamp": iso(1790000000.0 + i), "t_mono": 10.0 + i,
                   "device_id": "P001"}
            # Something from the car every row, but each signal every third.
            row[["vehicle_speed_kph", "hv_battery_charge_pct",
                 "hv_battery_temperature_c"][i % 3]] = "10"
            data.append(row)
        TripBuilder(self.root, "20260101_000000_aaaaaaaa").write_dictionary(
            DICTIONARY + [
                {"column": "hv_battery_charge_pct", "name": "SOC",
                 "unit": "percent", "group": "Battery", "source": "obdb",
                 "source_id": "SOC", "period_s": "1", "min": "0", "max": "100"},
                {"column": "hv_battery_temperature_c", "name": "Pack temp",
                 "unit": "celsius", "group": "Battery", "source": "obdb",
                 "source_id": "TEMP", "period_s": "1", "min": "-40",
                 "max": "100"}]) \
            .write_part("20260101_000000", data,
                        columns=BASE_COLUMNS + ["hv_battery_charge_pct",
                                                "hv_battery_temperature_c"])

        _, summary = run(self.root, self.out)
        timing = read_csv(self.out / "obd_timing.csv")

        self.assertEqual(float(summary[0]["obd_row_gap_median_s"]), 1.0)
        self.assertEqual({float(r["median_s"]) for r in timing}, {3.0})

    def test_measured_before_any_fill(self):
        """Filling would report the cadence of the filling, not of the car."""
        self._trip(speed_every=4)
        run(self.root, self.out)
        plain = read_csv(self.out / "obd_timing.csv")

        run(self.root, self.out, "--fill")
        filled = read_csv(self.out / "obd_timing.csv")

        self.assertEqual([r["median_s"] for r in filled],
                         [r["median_s"] for r in plain])
        self.assertEqual(float(filled[0]["median_s"]), 4.0)

    def test_uses_monotonic_time_so_a_clock_step_cannot_distort_it(self):
        data = []
        for i in range(12):
            # The wall clock jumps an hour halfway through; t_mono does not.
            wall = 1790000000.0 + i + (3600 if i >= 6 else 0)
            row = {"timestamp": iso(wall), "t_mono": 10.0 + i,
                   "device_id": "P001"}
            if i % 2 == 0:
                row["vehicle_speed_kph"] = "45"
            data.append(row)
        TripBuilder(self.root, "20260101_000000_aaaaaaaa") \
            .write_dictionary().write_part("20260101_000000", data)

        run(self.root, self.out)
        timing = read_csv(self.out / "obd_timing.csv")
        self.assertEqual(float(timing[0]["median_s"]), 2.0)

    def test_covers_every_trip_and_names_them(self):
        self._trip("20260101_000000_aaaaaaaa", speed_every=2)
        self._trip("20260102_000000_bbbbbbbb", speed_every=6)
        run(self.root, self.out)

        timing = read_csv(self.out / "obd_timing.csv")
        by_trip = {r["trip_id"]: r for r in timing
                   if r["column"] == "vehicle_speed_kph"}
        self.assertEqual(float(by_trip["20260101_000000_aaaaaaaa"]["median_s"]),
                         2.0)
        self.assertEqual(float(by_trip["20260102_000000_bbbbbbbb"]["median_s"]),
                         6.0)

    def test_leaves_out_kit_columns(self):
        """GPS and IMU are not on the OBD link, so they are not its timing."""
        self._trip()
        run(self.root, self.out)
        columns = {r["column"] for r in read_csv(self.out / "obd_timing.csv")}
        self.assertEqual(columns, {"vehicle_speed_kph"})

    def test_no_timing_file_when_nothing_came_from_the_car(self):
        rows = [{"timestamp": iso(1790000000.0 + i), "t_mono": 10.0 + i,
                 "device_id": "P001", "lat": "38.0"} for i in range(4)]
        TripBuilder(self.root, "20260101_000000_aaaaaaaa") \
            .write_dictionary().write_part("20260101_000000", rows)
        run(self.root, self.out)
        self.assertFalse((self.out / "obd_timing.csv").exists())

    def test_the_report_quotes_both_rates(self):
        self._trip(speed_every=4)
        run(self.root, self.out)
        report = (self.out / "cleaning_report.txt").read_text()
        self.assertIn("a reading off the bus every", report)
        self.assertIn("any one signal refreshes every", report)


class TestOutputLocation(CleanerTestCase):
    def _a_trip(self, root: Path = None):
        builder = TripBuilder(root or self.root, "20260101_000000_abcd1234")
        builder.write_dictionary()
        builder.write_part("20260101_000000",
                           [{"timestamp": iso(1790000000.0 + i),
                             "t_mono": 10.0 + i, "device_id": "P001",
                             "vehicle_speed_kph": "45"}
                            for i in range(3)])
        return builder

    def test_defaults_to_a_cleaned_folder_inside_the_input(self):
        """It is uploaded with the raw data, so it lives with the raw data."""
        self._a_trip()
        self.assertEqual(clean_trips.main([str(self.root), "--quiet"]), 0)

        expected = self.root / f"{self.root.name}_cleaned"
        self.assertTrue((expected / "trips_summary.csv").is_file())
        self.assertEqual(len(list((expected / "trips").glob("*.csv"))), 1)

    def test_a_second_run_does_not_clean_the_first_run_s_output(self):
        self._a_trip()
        clean_trips.main([str(self.root), "--quiet"])
        out = self.root / f"{self.root.name}_cleaned"
        first = sorted(p.name for p in (out / "trips").glob("*.csv"))

        self.assertEqual(clean_trips.main([str(self.root), "--quiet"]), 0)

        summary = read_csv(out / "trips_summary.csv")
        self.assertEqual(len(summary), 1)
        self.assertEqual(sorted(p.name for p in (out / "trips").glob("*.csv")),
                         first)
        # Nothing the cleaner writes is named drive*.csv, and the output
        # folder is skipped outright; both keep a rerun idempotent.
        self.assertEqual(clean_trips.find_trip_dirs(self.root, exclude=out),
                         [self.root / "20260101_000000_abcd1234"])

    def test_an_explicit_out_still_wins(self):
        self._a_trip()
        chosen = Path(self._tmp.name) / "somewhere_else"
        self.assertEqual(
            clean_trips.main([str(self.root), "-o", str(chosen), "--quiet"]), 0)
        self.assertTrue((chosen / "trips_summary.csv").is_file())
        self.assertFalse((self.root / f"{self.root.name}_cleaned").exists())


class TestInputShapes(CleanerTestCase):
    def test_accepts_a_single_trip_folder(self):
        trip = TripBuilder(self.root, "20260101_000000_abcd1234") \
            .write_dictionary()
        trip.write_part("20260101_000000",
                        [{"timestamp": iso(1790000000.0 + i),
                          "t_mono": 10.0 + i, "device_id": "P001"}
                         for i in range(3)])
        trips, _ = run(trip.dir, self.out)
        self.assertEqual(len(trips), 1)

    def test_accepts_a_tree_of_device_folders(self):
        uploads = Path(self._tmp.name) / "obd-ev-uploads"
        for device in ("P001", "P002"):
            builder = TripBuilder(uploads / device,
                                  "20260101_000000_abcd1234", device=device)
            builder.write_dictionary()
            builder.write_part("20260101_000000",
                               [{"timestamp": iso(1790000000.0 + i),
                                 "t_mono": 10.0 + i, "device_id": device}
                                for i in range(3)])
        trips, summary = run(uploads, self.out)
        self.assertEqual(len(trips), 2)
        self.assertEqual({r["device_id"] for r in summary}, {"P001", "P002"})


if __name__ == "__main__":
    unittest.main()
