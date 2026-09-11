"""Portal tests: routing, page assembly, and the credential contract.

The radio is never touched here -- `Provisioner`'s nmcli-facing methods are
stubbed. What is being checked is that the page carries the derivation code,
that the portal answers captive-portal probes, and above all that a plaintext
passphrase never reaches disk or the log when the browser derived a PMK.
"""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from obd_ev.provision import server as ps  # noqa: E402


class FakeProvisioner(ps.Provisioner):
    def __init__(self):
        super().__init__("P007", "labelpassword")
        self.networks = [
            {"ssid": "Home Net", "signal": 82, "security": "WPA2",
             "sae_only": False, "open": False},
            {"ssid": "Neighbour", "signal": 31, "security": "WPA3",
             "sae_only": True, "open": False},
        ]
        self.attempts = []
        self.ap_running = False

    def start_ap(self):
        self.ap_running = True

    def stop_ap(self):
        self.ap_running = False

    def connect(self, ssid, psk, hidden, timeout=45, key_mgmt="wpa-psk"):
        self.attempts.append({"ssid": ssid, "psk": psk, "hidden": hidden,
                              "key_mgmt": key_mgmt})
        if psk == "f" * 64 or psk is None or key_mgmt == "sae":
            return {"ok": True, "connectivity": "full"}
        return {"ok": False, "error": "That password was not accepted."}


class PortalTestCase(unittest.TestCase):
    def setUp(self):
        self.prov = FakeProvisioner()
        ps.PortalHandler.provisioner = self.prov
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ps.PortalHandler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path, allow_redirects=True):
        opener = urllib.request.build_opener()
        if not allow_redirects:
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **kw):
                    return None
            opener = urllib.request.build_opener(NoRedirect)
        try:
            return opener.open(self.url(path), timeout=10)
        except urllib.error.HTTPError as exc:
            return exc

    def post(self, path, obj):
        req = urllib.request.Request(
            self.url(path), data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    # -- page ---------------------------------------------------------------

    def test_page_embeds_derivation_code_and_ap_name(self):
        body = self.get("/").read().decode()
        self.assertIn("function wpaPsk", body)
        self.assertIn("function pbkdf2Sha1", body)
        self.assertNotIn("/*WPA_JS*/", body)
        self.assertIn("OBD-EV-Setup-P007", body)
        self.assertNotIn("__AP_SSID__", body)

    def test_page_states_the_password_is_not_sent(self):
        body = self.get("/").read().decode()
        self.assertIn("not sent to us", body)

    def test_networks_endpoint_lists_the_cached_scan(self):
        data = json.loads(self.get("/api/networks").read())
        self.assertEqual([n["ssid"] for n in data["networks"]],
                         ["Home Net", "Neighbour"])
        self.assertTrue(data["networks"][1]["sae_only"])

    def test_captive_probes_redirect_to_the_portal(self):
        for probe in ("/generate_204", "/hotspot-detect.html", "/ncsi.txt",
                      "/anything-else"):
            resp = self.get(probe, allow_redirects=False)
            self.assertEqual(resp.status, 302, probe)
            self.assertEqual(resp.headers["Location"],
                             f"http://{ps.PORTAL_ADDR}/")

    # -- connect ------------------------------------------------------------

    def test_connect_accepts_a_derived_pmk_and_reports_success(self):
        status, body = self.post("/api/connect",
                                 {"ssid": "Home Net", "psk": "f" * 64})
        self.assertEqual(status, 200)
        self.assertTrue(body["accepted"])
        self.assertTrue(self.prov.done.wait(timeout=10))
        self.assertEqual(self.prov.status["state"], "connected")
        self.assertEqual(self.prov.attempts[0]["psk"], "f" * 64)

    def test_failed_join_brings_the_setup_network_back(self):
        self.post("/api/connect", {"ssid": "Home Net", "psk": "0" * 64})
        for _ in range(100):
            if self.prov.status.get("state") == "failed":
                break
            threading.Event().wait(0.1)
        self.assertEqual(self.prov.status["state"], "failed")
        self.assertTrue(self.prov.ap_running,
                        "AP must come back so the password can be corrected")

    def test_rejects_empty_ssid_and_short_password(self):
        status, body = self.post("/api/connect", {"ssid": "", "psk": "x" * 64})
        self.assertEqual(status, 400)
        status, body = self.post("/api/connect",
                                 {"ssid": "Home Net", "psk": "short"})
        self.assertEqual(status, 400)
        self.assertIn("8 characters", body["error"])
        self.assertEqual(self.prov.attempts, [])

    def test_open_network_is_joined_without_a_credential(self):
        status, body = self.post("/api/connect", {"ssid": "Cafe", "psk": None})
        self.assertEqual(status, 200)
        self.assertTrue(self.prov.done.wait(timeout=10))
        self.assertEqual(self.prov.attempts[0]["psk"], None)

    def test_wpa3_only_network_sends_the_passphrase_with_sae(self):
        """A WPA3-only network cannot be joined from a PMK; the page sends the
        passphrase and asks for the `sae` key management the profile needs."""
        status, body = self.post("/api/connect",
                                 {"ssid": "Neighbour", "psk": "a real passphrase",
                                  "key_mgmt": "sae"})
        self.assertEqual(status, 200)
        self.assertTrue(self.prov.done.wait(timeout=10))
        self.assertEqual(self.prov.attempts[0]["key_mgmt"], "sae")
        self.assertEqual(self.prov.attempts[0]["psk"], "a real passphrase")

    def test_sae_with_a_pmk_is_refused(self):
        status, body = self.post("/api/connect",
                                 {"ssid": "Neighbour", "psk": "f" * 64,
                                  "key_mgmt": "sae"})
        self.assertEqual(status, 400)

    def test_ssid_is_kept_verbatim(self):
        """The SSID is the PBKDF2 salt; a trailing space is part of it."""
        self.post("/api/connect", {"ssid": "Home Net ", "psk": "f" * 64})
        self.assertTrue(self.prov.done.wait(timeout=10))
        self.assertEqual(self.prov.attempts[0]["ssid"], "Home Net ")

    def test_page_supports_open_networks(self):
        body = self.get("/").read().decode()
        self.assertIn('id="manualOpen"', body)
        self.assertIn("key_mgmt", body)

    def test_malformed_body_is_rejected(self):
        req = urllib.request.Request(
            self.url("/api/connect"), data=b"{not json",
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)


def setattr_many(module, values):
    module.STATE_DIR, module.MARKER, module.nmcli = values


class CredentialRecordTest(unittest.TestCase):
    def test_marker_records_the_method_but_never_the_secret(self):
        """The provisioning record is what a returning kit is audited from.
        It must say how the network was joined without containing the key."""
        import tempfile

        # The real Provisioner, with only its nmcli calls stubbed out.
        prov = ps.Provisioner("P007", "labelpassword")
        original = (ps.STATE_DIR, ps.MARKER, ps.nmcli)
        self.addCleanup(lambda: setattr_many(ps, original))

        with tempfile.TemporaryDirectory() as tmp:
            ps.STATE_DIR = Path(tmp)
            ps.MARKER = Path(tmp) / "provisioned.json"
            ps.nmcli = lambda *a, **kw: type(
                "R", (), {"returncode": 0, "stdout": "full", "stderr": ""})()

            secret = "e" * 64
            result = prov.connect("Home Net", secret, hidden=False, timeout=1)
            self.assertTrue(result["ok"])
            written = ps.MARKER.read_text()
            record = json.loads(written)
            self.assertEqual(record["ssid"], "Home Net")
            self.assertEqual(record["credential"], "pmk")
            self.assertEqual(record["connectivity"], "full")
            self.assertNotIn(secret, written)

    def test_a_typed_passphrase_is_recorded_as_such(self):
        """WPA3 fallback still works, but the record has to be honest that a
        passphrase -- not a PMK -- is what reached the device."""
        import tempfile

        prov = ps.Provisioner("P007", "labelpassword")
        original = (ps.STATE_DIR, ps.MARKER, ps.nmcli)
        self.addCleanup(lambda: setattr_many(ps, original))
        with tempfile.TemporaryDirectory() as tmp:
            ps.STATE_DIR = Path(tmp)
            ps.MARKER = Path(tmp) / "provisioned.json"
            ps.nmcli = lambda *a, **kw: type(
                "R", (), {"returncode": 0, "stdout": "full", "stderr": ""})()
            prov.connect("Home Net", "a real passphrase", hidden=False, timeout=1,
                         key_mgmt="sae")
            record = json.loads(ps.MARKER.read_text())
            self.assertEqual(record["credential"], "passphrase")
            self.assertEqual(record["key_mgmt"], "sae")

    def test_wpa3_profile_uses_sae_key_management(self):
        import tempfile

        prov = ps.Provisioner("P007", "labelpassword")
        original = (ps.STATE_DIR, ps.MARKER, ps.nmcli)
        self.addCleanup(lambda: setattr_many(ps, original))
        calls = []

        def fake_nmcli(*a, **kw):
            calls.append(a)
            return type("R", (), {"returncode": 0, "stdout": "full", "stderr": ""})()

        with tempfile.TemporaryDirectory() as tmp:
            ps.STATE_DIR = Path(tmp)
            ps.MARKER = Path(tmp) / "provisioned.json"
            ps.nmcli = fake_nmcli
            prov.connect("Neighbour", "a real passphrase", hidden=False,
                         timeout=1, key_mgmt="sae")
        add = [c for c in calls if c[:2] == ("connection", "add")][0]
        self.assertIn("sae", add)
        self.assertNotIn("wpa-psk", add)


if __name__ == "__main__":
    unittest.main()
