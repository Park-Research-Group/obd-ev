"""Scheduling and failure handling for the OBDb-driven reader.

Uses a scripted adapter rather than a radio: what matters here is which
requests get sent, how often, and what happens to commands the vehicle never
answers.
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from obd_ev.ble_obd import Elm327Timeout, OBDLinkDown  # noqa: E402
from obd_ev.config import OBDConfig, VehicleConfig  # noqa: E402
from obd_ev.vehicle import VehicleReader  # noqa: E402


SIGNALSET = {
    "commands": [
        {   # fast, answers correctly
            "hdr": "7E4", "rax": "7EC", "cmd": {"22": "0101"}, "freq": 0.25,
            "signals": [{"id": "SOC", "name": "State of charge", "path": "Battery",
                         "fmt": {"bix": 0, "len": 8, "max": 100, "div": 2,
                                 "unit": "percent"}}],
        },
        {   # different ECU, so a header switch is required
            "hdr": "7B3", "rax": "7BB", "cmd": {"22": "0100"}, "freq": 1.0,
            "signals": [{"id": "CABIN_T", "name": "Cabin temp", "path": "Climate",
                         "fmt": {"bix": 0, "len": 8, "max": 100, "add": -40,
                                 "unit": "celsius"}}],
        },
        {   # this vehicle never answers it
            "hdr": "7E4", "rax": "7EC", "cmd": {"22": "9999"}, "freq": 0.25,
            "signals": [{"id": "ABSENT", "name": "Absent", "path": "Battery",
                         "fmt": {"bix": 0, "len": 8, "max": 100, "unit": "percent"}}],
        },
    ]
}


class ScriptedAdapter:
    def __init__(self, replies=None, timeout_on=(), delay=0.0):
        self.sent = []
        self.replies = replies or {}
        self.timeout_on = set(timeout_on)
        self.connected = False
        self.delay = delay
        self.pins = 0

    def connect(self):
        self.connected = True

    def pin_protocol(self):
        self.pins += 1
        return True

    def command(self, cmd, timeout=None):
        self.sent.append(cmd)
        if self.delay and not cmd.startswith("AT"):
            import time
            time.sleep(self.delay)
        if cmd in self.timeout_on:
            raise Elm327Timeout(cmd)
        if cmd.startswith("AT"):
            return "OK"
        return self.replies.get(cmd, "NO DATA")

    def close(self):
        self.connected = False


def make_reader(adapter, signalset=None, obd=None, **vkw):
    tmp = Path(tempfile.mkdtemp()) / "signalset.json"
    tmp.write_text(json.dumps(signalset or SIGNALSET))
    vcfg = VehicleConfig(signalset=str(tmp), **vkw)
    reader = VehicleReader(obd or OBDConfig(), vcfg, adapter=adapter)
    reader.connect()
    return reader


def make_due(reader, include_disabled=False):
    for item in reader._schedule:
        if include_disabled or not item.disabled:
            item.due_at = 0.0


class TestSchedule(unittest.TestCase):
    def test_schema_lists_every_signal_regardless_of_support(self):
        reader = make_reader(ScriptedAdapter())
        self.assertEqual(sorted(reader.field_names()),
                         ["absent_pct", "cabin_temp_c", "state_of_charge_pct"])

    def test_min_period_floors_the_declared_frequency(self):
        reader = make_reader(ScriptedAdapter(), min_period=2.0)
        self.assertTrue(all(i.period >= 2.0 for i in reader._schedule))

    def test_decodes_answers_and_skips_silent_commands(self):
        adapter = ScriptedAdapter(replies={
            "220101": "7EC0462010164",       # 0x64 = 100 -> /2 = 50%
            "220100": "7BB0462010064",       # 0x64 = 100 -> -40 = 60C
        })
        reader = make_reader(adapter)
        values = reader.read()
        self.assertAlmostEqual(values["state_of_charge_pct"], 50.0)
        self.assertAlmostEqual(values["cabin_temp_c"], 60.0)
        self.assertNotIn("absent_pct", values)

    def test_header_is_only_reprogrammed_when_the_ecu_changes(self):
        adapter = ScriptedAdapter(replies={"220101": "7EC0462010164"})
        reader = make_reader(adapter)
        reader.read()
        # Two ECUs are addressed, so exactly two ATSH commands should appear.
        self.assertEqual([c for c in adapter.sent if c.startswith("ATSH")],
                         ["ATSH7E4", "ATSH7B3"])

    def test_unanswered_command_is_backed_off_after_repeated_no_data(self):
        """'NO DATA' is the normal reply for a PID the trim doesn't have, and
        it must back the command off -- otherwise it burns a round trip
        every cycle."""
        adapter = ScriptedAdapter(replies={"220101": "7EC0462010164"})
        reader = make_reader(adapter, disable_after=3, min_period=0.0,
                             retry_disabled_after=300)
        for _ in range(4):
            make_due(reader, include_disabled=True)
            reader.read()
        absent = [i for i in reader._schedule if i.command.pid == "9999"][0]
        self.assertTrue(absent.disabled)
        self.assertGreater(absent.due_at, time.monotonic() + 200,
                           "a backed-off command waits retry_disabled_after")
        before = len(adapter.sent)
        make_due(reader)                      # everything except the backed-off one
        reader.read()
        self.assertNotIn("229999", adapter.sent[before:])

    def test_backed_off_command_comes_back_when_it_answers(self):
        """Retirement is a backoff, not a verdict: a Bolt in accessory mode
        answers NO DATA on its HV commands and starts answering in READY."""
        adapter = ScriptedAdapter(replies={"220101": "7EC0462010164"})
        reader = make_reader(adapter, disable_after=2, min_period=0.0)
        for _ in range(3):
            make_due(reader, include_disabled=True)
            reader.read()
        absent = [i for i in reader._schedule if i.command.pid == "9999"][0]
        self.assertTrue(absent.disabled)

        adapter.replies["229999"] = "7EC0462999932"     # 0x32 = 50 -> 50%
        make_due(reader, include_disabled=True)           # retry time reached
        values = reader.read()
        self.assertAlmostEqual(values["absent_pct"], 50.0)
        self.assertFalse(absent.disabled)
        self.assertEqual(absent.failures, 0)

    def test_read_is_bounded_by_the_budget(self):
        """107 commands coming due at once must not hold up GPS/IMU rows for
        107 round trips; the remainder waits for the next cycle."""
        adapter = ScriptedAdapter(replies={"220101": "7EC0462010164",
                                           "220100": "7BB0462010064"},
                                  delay=0.05)
        reader = make_reader(adapter, min_period=0.0,
                             obd=OBDConfig(read_budget_seconds=0.06,
                                           command_timeout=0.05))
        make_due(reader)
        reader.read()
        obd_sent = [c for c in adapter.sent if not c.startswith("AT")]
        self.assertLess(len(obd_sent), 3, "budget should cut the cycle short")
        # The rest is still due and goes out next time.
        before = len(adapter.sent)
        reader.read()
        self.assertGreater(len(adapter.sent), before)

    def test_protocol_is_pinned_once_the_vehicle_has_answered(self):
        """The ELM327 only searches for a protocol on the first OBD request,
        so pinning must wait for data, and happen once."""
        adapter = ScriptedAdapter(replies={"220101": "7EC0462010164"})
        reader = make_reader(adapter, min_period=0.0)
        self.assertEqual(adapter.pins, 0, "nothing to pin before any data")
        for _ in range(3):
            make_due(reader)
            reader.read()
        self.assertEqual(adapter.pins, 1)

    def test_receive_filter_and_extended_address_are_reset_between_ecus(self):
        """A command without `rax` after one with it must not inherit the
        old filter, or its answers are silently dropped by the adapter."""
        signalset = {"commands": [
            {"hdr": "7E4", "rax": "7EC", "cmd": {"22": "0101"}, "freq": 1,
             "signals": [{"id": "A", "name": "a", "path": "X",
                          "fmt": {"len": 8, "max": 255}}]},
            {"hdr": "7DF", "cmd": {"01": "0D"}, "freq": 1, "eax": "F1",
             "signals": [{"id": "B", "name": "b", "path": "X",
                          "fmt": {"len": 8, "max": 255}}]},
        ]}
        adapter = ScriptedAdapter()
        reader = make_reader(adapter, signalset=signalset, min_period=0.0)
        make_due(reader)
        reader.read()
        at = [c for c in adapter.sent if c.startswith("AT")]
        self.assertIn("ATCRA7EC", at)
        self.assertIn("ATAR", at, "filter must be cleared for the 7DF command")
        self.assertIn("ATCEAF1", at)
        self.assertIn("ATCEA", at, "extended addressing off for the other")
        # Each ECU switch programs header, filter and addressing together.
        for sh in ("ATSH7E4", "ATSH7DF"):
            i = at.index(sh)
            self.assertEqual(at[i + 1], "ATCRA7EC" if sh == "ATSH7E4" else "ATAR")
            self.assertEqual(at[i + 2], "ATCEA" if sh == "ATSH7E4" else "ATCEAF1")

    def test_debug_commands_are_not_polled(self):
        signalset = {"commands": [
            {"hdr": "7E4", "rax": "7EC", "cmd": {"22": "0101"}, "freq": 1,
             "signals": [{"id": "A", "name": "a", "path": "X",
                          "fmt": {"len": 8, "max": 255}}]},
            {"hdr": "7E4", "rax": "7EC", "cmd": {"22": "0102"}, "freq": 1,
             "dbg": True,
             "signals": [{"id": "B", "name": "b", "path": "X",
                          "fmt": {"len": 8, "max": 255}}]},
        ]}
        reader = make_reader(ScriptedAdapter(), signalset=signalset)
        self.assertEqual(reader.field_names(), ["a"])

    def test_a_fully_silent_adapter_raises_link_down(self):
        adapter = ScriptedAdapter(timeout_on={"220101", "220100", "229999"})
        cfg = OBDConfig(max_read_failures=3)
        tmp = Path(tempfile.mkdtemp()) / "s.json"
        tmp.write_text(json.dumps(SIGNALSET))
        reader = VehicleReader(cfg, VehicleConfig(signalset=str(tmp),
                                                  min_period=0.0,
                                                  disable_after=0),
                               adapter=adapter)
        reader.connect()
        with self.assertRaises(OBDLinkDown):
            for _ in range(5):
                make_due(reader, include_disabled=True)
                reader.read()

    def test_asleep_vehicle_does_not_permanently_retire_everything(self):
        """A Pi that boots while the car is asleep sees NO DATA on every
        command. Retiring the whole set would mean collecting nothing for the
        rest of the study, so the schedule resets and the link is recycled."""
        adapter = ScriptedAdapter()          # answers NO DATA to everything
        reader = make_reader(adapter, disable_after=2, min_period=0.0)
        with self.assertRaises(OBDLinkDown):
            for _ in range(6):
                make_due(reader, include_disabled=True)
                reader.read()
        self.assertFalse(any(i.disabled for i in reader._schedule),
                         "schedule must be reset, not left dead")

        # After the reconnect the car is awake, and collection resumes.
        adapter.replies["220101"] = "7EC0462010164"
        reader.connect()
        self.assertAlmostEqual(reader.read()["state_of_charge_pct"], 50.0)

    def test_path_filters_narrow_collection(self):
        reader = make_reader(ScriptedAdapter(), include_paths=["Battery"])
        self.assertEqual(sorted(reader.field_names()),
                         ["absent_pct", "state_of_charge_pct"])
        reader = make_reader(ScriptedAdapter(), exclude_paths=["Battery"])
        self.assertEqual(reader.field_names(), ["cabin_temp_c"])


if __name__ == "__main__":
    unittest.main()
