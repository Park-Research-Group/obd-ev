"""Column naming, and the invariant that every column is documented."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from obd_ev.ble_obd import BleOBDReader  # noqa: E402
from obd_ev.config import OBDConfig, VehicleConfig  # noqa: E402
from obd_ev.naming import assign_columns, column_name, slug  # noqa: E402
from obd_ev.vehicle import VehicleReader  # noqa: E402


class TestColumnNames(unittest.TestCase):
    def test_readable_names_with_unit_suffixes(self):
        cases = [
            ("Lateral acceleration", "metersPerSecondSquared",
             "lateral_acceleration_mps2"),
            ("State of charge", "percent", "state_of_charge_pct"),
            ("Front left wheel speed, high resolution", "kilometersPerHour",
             "front_left_wheel_speed_high_resolution_kph"),
            ("Coolant temperature", "celsius", "coolant_temperature_c"),
            ("Battery current", "amps", "battery_current_a"),
        ]
        for name, unit, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(column_name(name, unit), expected)

    def test_enumerations_and_scalars_take_no_suffix(self):
        for unit in ("offon", "noyes", "scalar", "unknown", None):
            self.assertEqual(column_name("Charging", unit), "charging")

    def test_ambiguous_unit_abbreviations_are_kept_distinct(self):
        """metersPerSecond and milliseconds must not both become `_ms`, and
        coulombs must not collide with celsius."""
        self.assertEqual(column_name("Ground speed", "metersPerSecond"),
                         "ground_speed_mps")
        self.assertEqual(column_name("Response time", "milliseconds"),
                         "response_time_ms")
        self.assertEqual(column_name("Charge", "coulombs"), "charge_coulomb")
        self.assertEqual(column_name("Intake temp", "celsius"), "intake_temp_c")

    def test_name_already_ending_in_its_unit_is_not_doubled(self):
        self.assertEqual(column_name("Pack voltage v", "volts"), "pack_voltage_v")

    def test_leading_digit_is_made_identifier_safe(self):
        self.assertEqual(slug("12V battery"), "x12v_battery")

    def test_clash_falls_back_to_the_vendor_id(self):
        """Two ECUs can report the same display name; the second keeps a
        unique column rather than silently overwriting the first."""
        mapping = assign_columns([
            ("CAR_COOLANT_A", "Coolant temperature", "celsius"),
            ("CAR_COOLANT_B", "Coolant temperature", "celsius"),
        ])
        self.assertEqual(mapping["CAR_COOLANT_A"], "coolant_temperature_c")
        self.assertEqual(mapping["CAR_COOLANT_B"], "car_coolant_b")
        self.assertEqual(len(set(mapping.values())), 2)

    def test_repeated_key_maps_to_one_column(self):
        mapping = assign_columns([
            ("SOC", "State of charge", "percent"),
            ("SOC", "State of charge", "percent"),
        ])
        self.assertEqual(mapping, {"SOC": "state_of_charge_pct"})


class TestEveryColumnIsDocumented(unittest.TestCase):
    """signals.csv is what makes a 400-column file reviewable, so a column
    that appears in the CSV without a dictionary entry is a defect."""

    def assert_documented(self, reader):
        described = [row["column"] for row in reader.describe()]
        self.assertEqual(described, reader.field_names())
        for row in reader.describe():
            self.assertTrue(row["name"], f"{row['column']} has no readable name")
            self.assertTrue(row["source_id"], f"{row['column']} has no source id")
        self.assertEqual(len(set(described)), len(described),
                         "duplicate column names")

    def test_generic_mode01_reader(self):
        reader = BleOBDReader(OBDConfig())
        try:
            self.assert_documented(reader)
        finally:
            reader.adapter.close()

    def test_obdb_vehicle_reader(self):
        signalset = Path("vehicles/Hyundai-IONIQ-5.json")
        if not signalset.exists():
            self.skipTest("vehicle profile not vendored")
        reader = VehicleReader(
            OBDConfig(), VehicleConfig(signalset=str(signalset), year=2024),
            adapter=_NullAdapter())
        self.assert_documented(reader)
        # Spot-check that the vendor id is preserved for traceability.
        by_col = {r["column"]: r for r in reader.describe()}
        row = by_col["lateral_acceleration_mps2"]
        self.assertEqual(row["source_id"], "IONIQ5_LATERAL_ACCELERATION")
        self.assertEqual(row["command"], "22F010")
        self.assertEqual(row["category"], "Orientation")


class _NullAdapter:
    def connect(self): pass
    def pin_protocol(self): pass
    def command(self, cmd, timeout=None): return "NO DATA"
    def close(self): pass


class TestDecodedValuesUseColumns(unittest.TestCase):
    def test_read_returns_readable_column_keys(self):
        signalset = {"commands": [{
            "hdr": "7E4", "rax": "7EC", "cmd": {"22": "0101"}, "freq": 1.0,
            "signals": [{"id": "CAR_SOC", "name": "State of charge",
                         "path": "Battery",
                         "fmt": {"bix": 0, "len": 8, "max": 100, "div": 2,
                                 "unit": "percent"}}]}]}
        path = Path(tempfile.mkdtemp()) / "s.json"
        path.write_text(json.dumps(signalset))

        class Adapter(_NullAdapter):
            def command(self, cmd, timeout=None):
                return "7EC0462010164" if cmd == "220101" else "OK"

        reader = VehicleReader(OBDConfig(), VehicleConfig(signalset=str(path)),
                               adapter=Adapter())
        reader.connect()
        values = reader.read()
        self.assertEqual(list(values), ["state_of_charge_pct"])
        self.assertAlmostEqual(values["state_of_charge_pct"], 50.0)



class TestPreflightFailuresMode(unittest.TestCase):
    """--failures feeds self_update's before/after comparison, so it has to
    be names and nothing else -- a banner carrying the repo path would be
    compared as though it were a failing check."""

    def test_it_prints_only_failure_names(self):
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable,
             str(Path(__file__).resolve().parents[1] / "scripts" / "preflight.py"),
             "--quick", "--failures"],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, "--failures must always exit 0")
        for line in result.stdout.splitlines():
            self.assertFalse(line.startswith(" "), f"indented output: {line!r}")
            self.assertNotIn("preflight  (", line)
            self.assertNotIn("passed,", line)


class TestPreflightReadsTheKitEnvFile(unittest.TestCase):
    """A kit's identity lives in /etc/default/obd-ev, which systemd hands to
    the service and a shell does not."""

    def test_it_loads_obd_ev_keys_without_clobbering_the_environment(self):
        import os
        import sys
        import tempfile
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import preflight

        env_file = Path(tempfile.mkdtemp()) / "obd-ev"
        env_file.write_text(
            "# a comment\n\n"
            "OBD_EV_DEVICE_ID=P002\n"
            'OBD_EV_VEHICLE="Chevrolet-Bolt-EUV"\n'
            "NOT_OURS=ignored\n"
            "OBD_EV_ALREADY_SET=from-file\n")
        saved = {k: os.environ.get(k) for k in
                 ("OBD_EV_DEVICE_ID", "OBD_EV_VEHICLE", "NOT_OURS",
                  "OBD_EV_ALREADY_SET")}
        original_path = preflight.ENV_FILE
        os.environ["OBD_EV_ALREADY_SET"] = "from-environment"
        preflight.ENV_FILE = env_file
        try:
            preflight.load_kit_env()
            self.assertEqual(os.environ["OBD_EV_DEVICE_ID"], "P002")
            self.assertEqual(os.environ["OBD_EV_VEHICLE"], "Chevrolet-Bolt-EUV")
            self.assertNotIn("NOT_OURS", os.environ)
            self.assertEqual(os.environ["OBD_EV_ALREADY_SET"],
                             "from-environment")
        finally:
            preflight.ENV_FILE = original_path
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_a_missing_env_file_is_not_fatal(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import preflight
        original = preflight.ENV_FILE
        preflight.ENV_FILE = Path("/nonexistent/obd-ev")
        try:
            preflight.load_kit_env()
        finally:
            preflight.ENV_FILE = original


if __name__ == "__main__":
    unittest.main()


class TestVehicleSelection(unittest.TestCase):
    """The car is chosen per kit in /etc/default/obd-ev, not in the image."""

    def setUp(self):
        import os
        self._env = {k: os.environ.get(k) for k in
                     ("OBD_EV_VEHICLE", "OBD_EV_VEHICLE_YEAR")}
        for k in self._env:
            os.environ.pop(k, None)

    def tearDown(self):
        import os
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_env_selects_the_profile_and_resolves_the_path(self):
        import os
        from obd_ev import config
        os.environ["OBD_EV_VEHICLE"] = "Chevrolet-Bolt-EUV"
        os.environ["OBD_EV_VEHICLE_YEAR"] = "2023"
        cfg = config.load("/nonexistent.yaml")
        self.assertEqual(cfg.vehicle.make_model, "Chevrolet-Bolt-EUV")
        self.assertEqual(cfg.vehicle.year, 2023)
        self.assertTrue(Path(cfg.vehicle.signalset).is_absolute())
        self.assertTrue(cfg.vehicle.signalset.endswith(
            "vehicles/Chevrolet-Bolt-EUV.json"))

    def test_bad_year_is_ignored_not_fatal(self):
        import os
        from obd_ev import config
        os.environ["OBD_EV_VEHICLE_YEAR"] = "not-a-year"
        self.assertIsNone(config.load("/nonexistent.yaml").vehicle.year)

    def test_unknown_vehicle_falls_back_to_generic_mode01(self):
        """A typo in the kit's env file must cost vehicle signals, never the
        whole drive -- GPS and IMU still have to record."""
        import os
        from obd_ev import config
        from obd_ev.ble_obd import BleOBDReader
        from obd_ev.obd_reader import build_reader
        os.environ["OBD_EV_VEHICLE"] = "Chevrolet-Bolt-EUVV"
        reader = build_reader(config.load("/nonexistent.yaml"))
        try:
            self.assertIsInstance(reader, BleOBDReader)
            self.assertIn("vehicle_speed_kph", reader.field_names())
        finally:
            reader.adapter.close()


class TestVenvDetection(unittest.TestCase):
    """Scripts run by path start on the system interpreter, where bleak and
    mpu6050 are not installed. Getting this wrong reads as broken hardware."""

    def test_in_venv_compares_prefix_not_interpreter_path(self):
        import obd_ev.venv as v
        # .venv/bin/python is a symlink chain that ends at the system
        # interpreter, so comparing resolved interpreter paths reports
        # "already in the venv" when we are not. sys.prefix does not.
        original = v.venv_dir
        try:
            v.venv_dir = lambda: Path(sys.prefix)
            self.assertTrue(v.in_venv())
            v.venv_dir = lambda: Path(sys.prefix) / "definitely-not-here"
            self.assertFalse(v.in_venv())
        finally:
            v.venv_dir = original

    def test_reexec_is_a_noop_when_already_inside(self):
        import obd_ev.venv as v
        original = v.venv_dir
        try:
            v.venv_dir = lambda: Path(sys.prefix)
            v.reexec_if_needed()          # must return, not exec
        finally:
            v.venv_dir = original

    def test_reexec_respects_the_guard(self):
        import os
        import obd_ev.venv as v
        os.environ["OBD_EV_NO_REEXEC"] = "1"
        try:
            v.reexec_if_needed()          # must return, not exec
        finally:
            os.environ.pop("OBD_EV_NO_REEXEC", None)
