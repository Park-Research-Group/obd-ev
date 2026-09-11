"""Participant WiFi onboarding over a self-hosted access point.

The kit ships without home WiFi credentials. On first boot (or whenever no
home network has been provisioned) the Pi raises its own WPA2 access point and
serves a captive setup page. The participant joins it from a phone, picks their
home network and types the password.

What we store is not the password. The page derives the WPA2 pairwise master
key in the browser -- PMK = PBKDF2-HMAC-SHA1(passphrase, ssid, 4096, 256 bits)
-- and posts only that 64-hex digest. NetworkManager accepts a raw PMK wherever
it accepts a passphrase, so the Pi can join the network having never seen the
plaintext, and the plaintext is never transmitted, written to disk, or logged.

Two honest limits, both documented for participants in docs/participant.md:

  * The PMK is still a network credential. It cannot be reversed into the
    passphrase (so a password reused elsewhere stays protected, which is the
    point), but anyone holding it can join that network. Treat a returned SD
    card as sensitive.
  * WPA3-SAE derives its key differently and cannot be joined from a PMK. A
    WPA3-only network has to fall back to sending the passphrase, which the
    page asks about explicitly rather than doing silently.
"""

import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
STATE_DIR = Path(os.environ.get("OBD_EV_STATE_DIR", "/var/lib/obd-ev"))
MARKER = STATE_DIR / "provisioned.json"

AP_CONNECTION = "obd-ev-setup"
HOME_CONNECTION = "obd-ev-home"
PORTAL_ADDR = "10.42.0.1"

# URLs phones and laptops probe to decide whether they are behind a captive
# portal. Answering with a redirect is what makes the setup page pop up.
CAPTIVE_PROBES = (
    "/generate_204", "/gen_204", "/hotspot-detect.html", "/success.txt",
    "/ncsi.txt", "/connecttest.txt", "/canonical.html", "/redirect",
    "/library/test/success.html", "/mobile/status.php", "/kindle-wifi/wifistub.html",
)

HEX64 = re.compile(r"\A[0-9a-fA-F]{64}\Z")


def _is_sae_only(security: str) -> bool:
    """nmcli's SECURITY column names WPA3-Personal "WPA3" (never "SAE"):
    a transition-mode network shows "WPA2 WPA3" and can take a PMK, a
    WPA3-only one shows just "WPA3" and cannot."""
    tokens = set(security.upper().replace("-", " ").split())
    has_wpa3 = bool(tokens & {"WPA3", "SAE"})
    has_psk = bool(tokens & {"WPA2", "WPA1", "WPA"})
    return has_wpa3 and not has_psk


def _is_open(security: str) -> bool:
    return security.strip() in ("", "--")


def nmcli(*args: str, timeout: int = 30, check: bool = False) -> subprocess.CompletedProcess:
    cmd = ["nmcli"] + list(args)
    log.debug("running %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, check=check)


class Provisioner:
    """Owns the access point, the scan cache and the connection attempt."""

    def __init__(self, device_id: str, ap_password: str, iface: str = "wlan0"):
        self.device_id = device_id
        self.ap_password = ap_password
        self.iface = iface
        self.ap_ssid = f"OBD-EV-Setup-{device_id}"[:32]
        self.networks: List[Dict[str, object]] = []
        self.status: Dict[str, object] = {"state": "idle", "error": None,
                                          "ssid": None}
        self.done = threading.Event()
        self._lock = threading.Lock()

    # -- scanning -----------------------------------------------------------

    def scan(self) -> List[Dict[str, object]]:
        """Scan for nearby networks. Must run before the AP is raised: a
        single radio in AP mode cannot also scan, so the list the participant
        sees is the one captured here."""
        nmcli("device", "wifi", "rescan", timeout=45)
        time.sleep(3)
        result = nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list")
        seen: Dict[str, Dict[str, object]] = {}
        for line in result.stdout.splitlines():
            # nmcli terse output escapes colons inside fields as "\:".
            parts = re.split(r"(?<!\\):", line)
            if len(parts) < 3:
                continue
            ssid = parts[0].replace("\\:", ":").strip()
            if not ssid or ssid == self.ap_ssid:
                continue
            try:
                signal = int(parts[1])
            except ValueError:
                signal = 0
            security = parts[2].strip()
            if ssid not in seen or signal > seen[ssid]["signal"]:
                seen[ssid] = {
                    "ssid": ssid,
                    "signal": signal,
                    "security": security,
                    # SAE cannot be joined from a PMK; the page warns instead
                    # of silently failing after the AP has already come down.
                    "sae_only": _is_sae_only(security),
                    "open": _is_open(security),
                }
        self.networks = sorted(seen.values(), key=lambda n: -n["signal"])
        log.info("scan found %d networks", len(self.networks))
        return self.networks

    # -- access point -------------------------------------------------------

    def start_ap(self) -> None:
        nmcli("connection", "delete", AP_CONNECTION)
        add = nmcli(
            "connection", "add", "type", "wifi", "ifname", self.iface,
            "con-name", AP_CONNECTION, "autoconnect", "no",
            "ssid", self.ap_ssid,
            "802-11-wireless.mode", "ap",
            "802-11-wireless.band", "bg",
            "ipv4.method", "shared",
            "wifi-sec.key-mgmt", "wpa-psk",
            "wifi-sec.proto", "rsn",
            "wifi-sec.pairwise", "ccmp",
            "wifi-sec.psk", self.ap_password,
        )
        if add.returncode != 0:
            raise RuntimeError(f"could not create AP profile: {add.stderr.strip()}")
        up = nmcli("connection", "up", AP_CONNECTION, timeout=60)
        if up.returncode != 0:
            raise RuntimeError(f"could not start AP: {up.stderr.strip()}")
        log.info("access point %r is up on %s", self.ap_ssid, PORTAL_ADDR)

    def stop_ap(self) -> None:
        nmcli("connection", "down", AP_CONNECTION)

    # -- joining the participant's network ----------------------------------

    def _install_profile(self, ssid: str, psk: Optional[str],
                         hidden: bool, key_mgmt: str = "wpa-psk") -> None:
        nmcli("connection", "delete", HOME_CONNECTION)
        args = ["connection", "add", "type", "wifi", "ifname", self.iface,
                "con-name", HOME_CONNECTION, "ssid", ssid,
                "connection.autoconnect", "yes",
                "connection.autoconnect-priority", "10"]
        if hidden:
            args += ["802-11-wireless.hidden", "yes"]
        if psk:
            # wpa-psk takes either a passphrase or a 64-hex PMK and covers
            # WPA2 and WPA3 transition mode. A WPA3-only network needs `sae`,
            # which only works from the passphrase.
            args += ["wifi-sec.key-mgmt", key_mgmt, "wifi-sec.psk", psk]
        add = nmcli(*args)
        if add.returncode != 0:
            raise RuntimeError(f"could not save network: {add.stderr.strip()}")

    def connect(self, ssid: str, psk: Optional[str], hidden: bool,
                timeout: int = 45, key_mgmt: str = "wpa-psk") -> Dict[str, object]:
        """Bring the AP down and try the participant's network. On failure the
        AP comes back so they can correct the password."""
        self._install_profile(ssid, psk, hidden, key_mgmt)
        self.stop_ap()
        time.sleep(2)

        up = nmcli("connection", "up", HOME_CONNECTION, timeout=timeout + 15)
        if up.returncode != 0:
            error = up.stderr.strip() or "could not join that network"
            log.warning("join failed for %r: %s", ssid, error)
            nmcli("connection", "delete", HOME_CONNECTION)
            return {"ok": False, "error": _friendly(error)}

        connectivity = self._await_connectivity(timeout)
        record = {
            "ssid": ssid,
            "hidden": hidden,
            # Recorded so a returning kit can be audited without ever holding
            # the credential itself.
            "credential": "pmk" if psk and HEX64.match(psk) else
                          ("passphrase" if psk else "open"),
            "key_mgmt": key_mgmt if psk else "none",
            "connectivity": connectivity,
            "provisioned_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "device_id": self.device_id,
        }
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(json.dumps(record, indent=1) + "\n")
        log.info("joined %r (connectivity=%s)", ssid, connectivity)
        return {"ok": True, "connectivity": connectivity}

    def _await_connectivity(self, timeout: int) -> str:
        deadline = time.time() + timeout
        state = "unknown"
        while time.time() < deadline:
            state = nmcli("-t", "-f", "CONNECTIVITY", "general").stdout.strip()
            if state == "full":
                return state
            time.sleep(2)
        return state


def _friendly(error: str) -> str:
    lowered = error.lower()
    if "secrets" in lowered or "802-1x" in lowered or "key" in lowered:
        return "That password was not accepted by the network."
    if "not found" in lowered or "no network" in lowered:
        return ("That network was not found. If its name is hidden, tick "
                "“network is hidden” and try again.")
    if "timeout" in lowered or "timed out" in lowered:
        return "Timed out joining that network. Is the Pi in range of it?"
    return error


class PortalHandler(BaseHTTPRequestHandler):
    provisioner: Provisioner = None  # set by run_provisioning
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Never let a request line or body reach the journal: a fallback
        # passphrase would be in it.
        log.debug("portal %s", self.path.split("?")[0])

    # -- helpers ------------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _redirect_to_portal(self) -> None:
        self.send_response(302)
        self.send_header("Location", f"http://{PORTAL_ADDR}/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- routes -------------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            page = (HERE / "portal.html").read_bytes()
            js = (HERE / "wpa.js").read_bytes()
            page = page.replace(b"/*WPA_JS*/", js)
            page = page.replace(b"__AP_SSID__",
                                self.provisioner.ap_ssid.encode())
            self._send(200, page, "text/html; charset=utf-8")
        elif path == "/api/networks":
            self._json({"networks": self.provisioner.networks})
        elif path == "/api/status":
            self._json(self.provisioner.status)
        elif path in CAPTIVE_PROBES:
            self._redirect_to_portal()
        else:
            self._redirect_to_portal()

    def do_POST(self):
        if self.path.split("?")[0] != "/api/connect":
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json({"error": "malformed request"}, 400)
            return

        if not isinstance(payload, dict):
            self._json({"error": "malformed request"}, 400)
            return
        # The SSID is used verbatim: it is the PBKDF2 salt the page derived
        # the PMK with, and some networks really do have spaces at the ends.
        ssid = str(payload.get("ssid") or "")
        psk = (payload.get("psk") or "").strip() or None
        hidden = bool(payload.get("hidden"))
        key_mgmt = "sae" if payload.get("key_mgmt") == "sae" else "wpa-psk"
        if not ssid.strip():
            self._json({"error": "Choose a network first."}, 400)
            return
        if psk and not HEX64.match(psk) and len(psk) < 8:
            self._json({"error": "A WiFi password is at least 8 characters."},
                       400)
            return
        if key_mgmt == "sae" and (not psk or HEX64.match(psk)):
            self._json({"error": "A WPA3 network needs the password itself."},
                       400)
            return

        prov = self.provisioner
        prov.status = {"state": "connecting", "error": None, "ssid": ssid}

        # Answer before touching the radio: bringing the AP down drops this
        # very connection, so the reply has to be on the wire first.
        self._json({"accepted": True, "ssid": ssid})
        try:
            self.wfile.flush()
        except OSError:
            pass

        threading.Thread(target=_attempt,
                         args=(prov, ssid, psk, hidden, key_mgmt),
                         daemon=True, name="wifi-join").start()


def _attempt(prov: Provisioner, ssid: str, psk: Optional[str],
             hidden: bool, key_mgmt: str = "wpa-psk") -> None:
    time.sleep(2)  # let the HTTP response drain to the phone
    try:
        result = prov.connect(ssid, psk, hidden, key_mgmt=key_mgmt)
    except Exception as exc:
        log.exception("provisioning attempt failed")
        result = {"ok": False, "error": str(exc)}

    if result.get("ok"):
        prov.status = {"state": "connected", "error": None, "ssid": ssid,
                       "connectivity": result.get("connectivity")}
        prov.done.set()
        return

    prov.status = {"state": "failed", "error": result.get("error"), "ssid": ssid}
    try:
        prov.start_ap()
        log.info("setup network is back up so the password can be corrected")
    except Exception:
        log.exception("could not bring the setup AP back up")


def already_provisioned() -> bool:
    return MARKER.exists()


def run_provisioning(device_id: str, ap_password: str, iface: str = "wlan0",
                     port: int = 80, idle_timeout: float = 0.0) -> int:
    prov = Provisioner(device_id, ap_password, iface)
    try:
        prov.scan()
    except Exception:
        log.exception("pre-AP scan failed; the page will ask for a typed name")
    prov.start_ap()

    PortalHandler.provisioner = prov
    server = ThreadingHTTPServer(("0.0.0.0", port), PortalHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("setup portal listening on http://%s/ (join %r)",
             PORTAL_ADDR, prov.ap_ssid)

    finished = prov.done.wait(timeout=idle_timeout or None)
    server.shutdown()
    if not finished:
        log.warning("no network configured within %.0fs; giving up for now",
                    idle_timeout)
        prov.stop_ap()
        return 1
    log.info("provisioning complete")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="obd-ev-provision", description=__doc__)
    parser.add_argument("--device-id",
                        default=os.environ.get("OBD_EV_DEVICE_ID")
                        or socket.gethostname())
    parser.add_argument("--ap-password",
                        default=os.environ.get("OBD_EV_AP_PASSWORD", ""),
                        help="WPA2 password for the setup network (>=8 chars)")
    parser.add_argument("--iface", default=os.environ.get("OBD_EV_WIFI_IFACE",
                                                          "wlan0"))
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--timeout", type=float,
                        default=float(os.environ.get("OBD_EV_PROVISION_TIMEOUT",
                                                     "0")),
                        help="give up after this many seconds (0 = wait)")
    parser.add_argument("--force", action="store_true",
                        help="run even if a network is already provisioned")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if already_provisioned() and not args.force:
        log.info("%s exists; nothing to provision", MARKER)
        return 0
    if len(args.ap_password) < 8:
        log.error("set OBD_EV_AP_PASSWORD (at least 8 characters) in "
                  "/etc/default/obd-ev; it goes on the kit's label")
        return 2

    return run_provisioning(args.device_id, args.ap_password, args.iface,
                            args.port, args.timeout)
