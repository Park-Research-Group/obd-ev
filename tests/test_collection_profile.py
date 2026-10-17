"""The signal selection that ships with a vehicle profile.

`config.yaml` is gitignored and written once per kit at imaging time, so a
selection kept only there can never be changed by an update -- while the
signalset it selects from is replaced by every `git pull`. The sidecar,
`vehicles/<Make-Model>.collect.json`, is tracked, so both travel together on
the `deploy` branch.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from obd_ev import config                                    # noqa: E402
from obd_ev.obdb import SignalSet                            # noqa: E402
from obd_ev.vehicle import VehicleReader                     # noqa: E402

BOLT = REPO_ROOT / "vehicles" / "Chevrolet-Bolt-EUV.json"
BOLT_COLLECT = REPO_ROOT / "vehicles" / "Chevrolet-Bolt-EUV.collect.json"

SIGNALSET = {"commands": [
    {"hdr": "7E4", "cmd": {"22": "0101"}, "freq": 1,
     "signals": [{"id": "WANTED", "name": "Wanted", "path": "Battery",
                  "fmt": {"len": 8, "max": 100, "unit": "percent"}}]},
    {"hdr": "7E4", "cmd": {"22": "0102"}, "freq": 1,
     "signals": [{"id": "BULK_01", "name": "Bulk one", "path": "Battery",
                  "fmt": {"len": 8, "max": 100, "unit": "percent"}}]},
]}


class CollectionProfileHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.signalset = self.tmp / "Test-Car.json"
        self.signalset.write_text(json.dumps(SIGNALSET))
        self._env = {k: os.environ.get(k) for k in
                     ("OBD_EV_VEHICLE", "OBD_EV_VEHICLE_YEAR")}
        for k in self._env:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def write_sidecar(self, doc):
        config.collection_profile_path(self.signalset).write_text(
            json.dumps(doc))

    def load(self, vehicle_yaml=None):
        """config.load() against a config.yaml the test writes."""
        body = {"vehicle": dict(vehicle_yaml or {},
                                signalset=str(self.signalset))}
        path = self.tmp / "config.yaml"
        path.write_text(json.dumps(body))      # JSON is a subset of YAML
        return config.load(path)


class TestSidecarIsApplied(CollectionProfileHarness):
    def test_selection_ships_with_the_profile(self):
        self.write_sidecar({"include_signals": ["WANTED"]})
        self.assertEqual(self.load().vehicle.include_signals, ["WANTED"])

    def test_absent_sidecar_leaves_the_selection_empty(self):
        self.assertEqual(self.load().vehicle.include_signals, [])

    def test_it_reaches_the_reader(self):
        """The point of the exercise: fewer commands actually polled."""
        self.write_sidecar({"include_signals": ["WANTED"]})
        reader = VehicleReader(self.load().obd, self.load().vehicle,
                               adapter=object())
        self.assertEqual(reader.field_names(), ["wanted_pct"])
        self.assertEqual(len(reader._schedule), 1)

    def test_exclude_signals_ship_too(self):
        self.write_sidecar({"exclude_signals": ["BULK_*"]})
        self.assertEqual(self.load().vehicle.exclude_signals, ["BULK_*"])


class TestConfigYamlWins(CollectionProfileHarness):
    def test_a_kit_can_override_the_shipped_list(self):
        """Tuning a kit in the field must not mean editing a tracked file --
        self_update refuses to update a repo with local modifications."""
        self.write_sidecar({"include_signals": ["WANTED"]})
        cfg = self.load({"include_signals": ["BULK_01"]})
        self.assertEqual(cfg.vehicle.include_signals, ["BULK_01"])

    def test_an_empty_list_is_not_an_override(self):
        """config.yaml.example ships both keys empty; a kit that copied it
        must still get the shipped selection."""
        self.write_sidecar({"include_signals": ["WANTED"]})
        cfg = self.load({"include_signals": [], "exclude_signals": []})
        self.assertEqual(cfg.vehicle.include_signals, ["WANTED"])

    def test_everything_can_be_collected_deliberately(self):
        self.write_sidecar({"include_signals": ["WANTED"]})
        cfg = self.load({"include_signals": ["*"]})
        reader = VehicleReader(cfg.obd, cfg.vehicle, adapter=object())
        self.assertEqual(len(reader._schedule), 2)

    def test_one_key_overridden_leaves_the_other_shipped(self):
        self.write_sidecar({"include_signals": ["WANTED"],
                            "exclude_signals": ["BULK_*"]})
        cfg = self.load({"include_signals": ["*"]})
        self.assertEqual(cfg.vehicle.include_signals, ["*"])
        self.assertEqual(cfg.vehicle.exclude_signals, ["BULK_*"])


class TestMalformedSidecarIsNotFatal(CollectionProfileHarness):
    """A bad file costs vehicle signals; it must never take a kit offline."""

    def test_unparseable_json(self):
        config.collection_profile_path(self.signalset).write_text("{nope")
        self.assertEqual(self.load().vehicle.include_signals, [])

    def test_not_a_mapping(self):
        self.write_sidecar(["BOLT_VSS"])
        self.assertEqual(self.load().vehicle.include_signals, [])

    def test_not_a_list_of_strings(self):
        self.write_sidecar({"include_signals": [1, 2, 3]})
        self.assertEqual(self.load().vehicle.include_signals, [])


class TestTheShippedBoltProfile(unittest.TestCase):
    def setUp(self):
        self.doc = json.loads(BOLT_COLLECT.read_text())

    def test_every_shipped_id_exists_in_the_shipped_signalset(self):
        """A typo here is a column silently missing from every trip of the
        study, and nothing about a running kit makes it visible."""
        import fnmatch
        available = SignalSet.load(BOLT).signal_ids()
        for pattern in self.doc["include_signals"]:
            self.assertTrue(
                any(fnmatch.fnmatchcase(i, pattern) for i in available),
                f"{pattern} matches nothing in {BOLT.name}")

    def test_the_per_module_voltages_are_not_in_it(self):
        self.assertFalse([p for p in self.doc["include_signals"]
                          if "CMU" in p])

    def test_what_p001_asked_for_is_covered(self):
        ids = self.doc["include_signals"]
        for want in ("BOLT_AIR_TEMP",          # temperature
                     "BOLT_CABIN_AC",          # A/C
                     "BOLT_CABIN_HEATER",      # heat
                     "BOLT_REGEN_BRAKING",     # regen
                     "BOLT_VSS"):              # speed
            self.assertTrue(any(i.startswith(want) for i in ids), want)

    def test_it_is_small_enough_to_be_the_point(self):
        """Each command is one BLE round trip; the whole exercise was the
        round-trip count, not the column count."""
        cfg = config.VehicleConfig(
            signalset=str(BOLT), year=2023,
            include_signals=self.doc["include_signals"],
            exclude_signals=self.doc["exclude_signals"])
        reader = VehicleReader(config.OBDConfig(), cfg, adapter=object())
        self.assertLessEqual(len(reader._schedule), 20)
        self.assertGreater(len(reader._schedule), 10)


class TestSelectedByEnv(unittest.TestCase):
    """OBD_EV_VEHICLE picks the profile; the sidecar has to follow it."""

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in
                     ("OBD_EV_VEHICLE", "OBD_EV_VEHICLE_YEAR")}
        os.environ["OBD_EV_VEHICLE"] = "Chevrolet-Bolt-EUV"
        os.environ["OBD_EV_VEHICLE_YEAR"] = "2023"

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_the_kit_gets_the_shipped_bolt_selection(self):
        cfg = config.load("/nonexistent.yaml")
        self.assertIn("BOLT_VSS", cfg.vehicle.include_signals)
        self.assertEqual(len(cfg.vehicle.include_signals), 15)


if __name__ == "__main__":
    unittest.main()
