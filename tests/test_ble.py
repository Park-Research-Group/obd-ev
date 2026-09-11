"""ELM327 framing and config hygiene: the parts of the adapter path that can
be checked without a radio."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from obd_ev import config as cfgmod  # noqa: E402
from obd_ev.ble_obd import parse_dpn, _clean_response  # noqa: E402
from obd_ev.provision.server import _is_sae_only, _is_open  # noqa: E402


class TestProtocolPinning(unittest.TestCase):
    def test_unsearched_adapter_is_not_pinned(self):
        """Straight after ATSP0 the adapter reports A0: nothing found yet.
        Pinning that would send ATSP0 and freeze nothing."""
        self.assertIsNone(parse_dpn("A0"))
        self.assertIsNone(parse_dpn("0"))

    def test_found_protocol_is_extracted(self):
        self.assertEqual(parse_dpn("A6"), "6")
        self.assertEqual(parse_dpn("6"), "6")
        self.assertEqual(parse_dpn("OK\nA7"), "7")

    def test_error_text_does_not_look_like_a_protocol(self):
        self.assertIsNone(parse_dpn("BUS ERROR"))
        self.assertIsNone(parse_dpn("?"))
        self.assertIsNone(parse_dpn(""))


class TestResponseCleaning(unittest.TestCase):
    def test_prompt_and_echo_are_stripped(self):
        self.assertEqual(_clean_response("220101\r7EC0462010164\r\r>", "220101"),
                         "7EC0462010164")


class TestConfigHygiene(unittest.TestCase):
    def _load(self, text):
        tmp = Path(tempfile.mkdtemp()) / "config.yaml"
        tmp.write_text(text)
        return cfgmod.load(tmp)

    def test_numeric_yaml_values_are_coerced_to_what_the_adapter_expects(self):
        cfg = self._load("obd:\n  response_timeout: 32\n  protocol: 6\n")
        self.assertEqual(cfg.obd.response_timeout, "32")
        self.assertEqual(cfg.obd.protocol, "6")

    def test_bad_protocol_falls_back_to_automatic(self):
        cfg = self._load("obd:\n  protocol: CAN\n")
        self.assertEqual(cfg.obd.protocol, "0")

    def test_zero_flush_every_cannot_divide_by_zero(self):
        cfg = self._load("logger:\n  flush_every: 0\n")
        self.assertEqual(cfg.logger.flush_every, 1)

    def test_non_mapping_config_falls_back_to_defaults(self):
        cfg = self._load("- just\n- a list\n")
        self.assertEqual(cfg.obd.ble_name, "VEEPEAK")


class TestSecurityParsing(unittest.TestCase):
    """nmcli names WPA3-Personal 'WPA3'; it never prints 'SAE'."""

    def test_wpa3_only_needs_the_passphrase(self):
        self.assertTrue(_is_sae_only("WPA3"))
        self.assertTrue(_is_sae_only("SAE"))

    def test_transition_mode_takes_a_pmk(self):
        self.assertFalse(_is_sae_only("WPA2 WPA3"))
        self.assertFalse(_is_sae_only("WPA1 WPA2"))
        self.assertFalse(_is_sae_only("WPA2"))

    def test_open_networks(self):
        self.assertTrue(_is_open(""))
        self.assertTrue(_is_open("--"))
        self.assertFalse(_is_open("WPA2"))
        self.assertFalse(_is_sae_only(""))


if __name__ == "__main__":
    unittest.main()
