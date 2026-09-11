#!/usr/bin/env python3
"""Prove the BLE OBD adapter end to end, before trusting the logger to.

Uses the same BleElm327 class the logger uses, so a clean run here means the
logging path works -- discovery, GATT characteristic choice, the ELM327
handshake, and (in a car) a real request.

    scripts/obd_probe.py              # scan, connect, ATI / ATRV, then the
                                      # vehicle profile's first commands
    scripts/obd_probe.py --scan       # list nearby BLE devices and stop
    scripts/obd_probe.py --no-vehicle # adapter only; no OBD requests

The adapter is powered from the OBD port (pin 16 is battery +12V on nearly
every car, ignition off included), so the adapter checks work in a parked
car. The vehicle checks need the car in READY.

Only one BLE connection to the adapter is possible, so `obd-ev` is stopped
for the duration and restarted afterwards.
"""

import argparse
import asyncio
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from obd_ev.venv import reexec_if_needed  # noqa: E402
reexec_if_needed()

from obd_ev import config as cfgmod  # noqa: E402
from obd_ev.ble_obd import (BleElm327, Elm327Timeout, OBDLinkDown,  # noqa: E402
                            ELM_PROTOCOLS, parse_dpn)


def say(status, text):
    print(f"  {status:<5} {text}", flush=True)


def scan(seconds: float, wanted: str):
    try:
        from bleak import BleakScanner
    except ImportError:
        say("FAIL", "bleak is not importable; run under .venv/bin/python")
        return []
    print(f"\nScanning for BLE devices for {seconds:.0f}s...", flush=True)
    found = asyncio.run(BleakScanner.discover(timeout=seconds, return_adv=True))
    rows = []
    for address, (device, adv) in found.items():
        name = device.name or adv.local_name or ""
        rows.append((adv.rssi, address, name, list(adv.service_uuids or [])))
    rows.sort(key=lambda r: -(r[0] or -999))
    hits = []
    for rssi, address, name, uuids in rows:
        mark = ""
        if name and wanted.lower() in name.lower():
            mark = "   <-- matches obd.ble_name"
            hits.append((address, name))
        short = ", ".join(u[4:8] for u in uuids if u.startswith("0000"))
        print(f"    {rssi:>4} dBm  {address}  {name or '(no name)':<24} {short}{mark}")
    if not rows:
        say("WARN", "no BLE devices seen at all -- is the controller powered? "
                    "(sudo rfkill unblock bluetooth; bluetoothctl power on)")
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(REPO_ROOT / "config.yaml"))
    parser.add_argument("--scan", action="store_true",
                        help="only list nearby BLE devices")
    parser.add_argument("--scan-seconds", type=float, default=8.0)
    parser.add_argument("--no-vehicle", action="store_true",
                        help="stop after the adapter handshake")
    parser.add_argument("--commands", type=int, default=5,
                        help="how many profile commands to try")
    args = parser.parse_args()

    cfg = cfgmod.load(args.config)
    # The env file is what the service reads; mirror it so the vehicle
    # profile and any pinned address are the ones the logger will use.
    import os
    env_file = Path("/etc/default/obd-ev")
    if env_file.exists():
        try:
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
            cfg = cfgmod.load(args.config)
        except OSError:
            pass

    print(f"obd-ev adapter probe  ({REPO_ROOT})")
    print(f"  looking for name containing {cfg.obd.ble_name!r}"
          + (f", or address {cfg.obd.ble_address}" if cfg.obd.ble_address else ""))

    hits = scan(args.scan_seconds, cfg.obd.ble_name)
    if hits:
        for address, name in hits:
            say("PASS", f"adapter advertising as {name!r} at {address}")
        if not cfg.obd.ble_address:
            say("NOTE", f"pin it in config.yaml:  obd.ble_address: {hits[0][0]}")
    elif not cfg.obd.ble_address:
        say("FAIL", f"nothing advertising a name containing {cfg.obd.ble_name!r}. "
                    "Is the adapter plugged into a powered OBD port? Is it a "
                    "BLE model (OBDCheck BLE / BLE+), not a classic-only one?")
    if args.scan:
        return 0 if hits else 1

    stopped = False
    if subprocess.run(["systemctl", "is-active", "--quiet", "obd-ev"]).returncode == 0:
        print("\nStopping obd-ev for the duration (only one BLE connection is allowed)...")
        subprocess.run(["sudo", "systemctl", "stop", "obd-ev"])
        stopped = True
        time.sleep(2)

    rc = 1
    adapter = BleElm327(cfg.obd, raw_frames=bool(cfg.vehicle.signalset))
    try:
        print("\nConnecting and running the ELM327 handshake...", flush=True)
        t0 = time.monotonic()
        try:
            adapter.connect()
        except Exception as exc:
            say("FAIL", f"connect failed: {exc}")
            return 1
        say("PASS", f"connected in {time.monotonic() - t0:.1f}s; "
                    f"write={adapter.write_uuid} notify={adapter.notify_uuid}")

        for cmd, label in (("ATI", "adapter identifies as"),
                           ("ATRV", "battery voltage"),
                           ("ATDPN", "protocol")):
            try:
                reply = adapter.command(cmd, timeout=3)
            except Elm327Timeout:
                say("WARN", f"{cmd}: no reply")
                continue
            if cmd == "ATDPN":
                n = parse_dpn(reply)
                reply = (f"{reply.strip()} -> {ELM_PROTOCOLS.get(n, '?')}" if n
                         else f"{reply.strip()} (auto, not searched yet)")
            say("PASS", f"{label}: {reply.strip()}")
        rc = 0

        if args.no_vehicle:
            return rc

        print("\nVehicle (needs the car in READY)...", flush=True)
        if cfg.vehicle.signalset and Path(cfg.vehicle.signalset).exists():
            from obd_ev.vehicle import VehicleReader
            reader = VehicleReader(cfg.obd, cfg.vehicle, adapter=adapter)
            reader._schedule = reader._schedule[:args.commands]
            reader.connect()
            for item in reader._schedule:
                item.due_at = 0.0
            values = reader.read()
            if values:
                say("PASS", f"{len(values)} signals decoded from "
                            f"{len(reader._schedule)} commands")
                for k, v in list(values.items())[:12]:
                    print(f"          {k} = {v}")
                rc = 0
            else:
                say("FAIL", "no command answered (NO DATA). Car not in READY, "
                            "or wrong profile for this vehicle.")
                rc = 1
        else:
            from obd_ev.ble_obd import BleOBDReader
            reader = BleOBDReader(cfg.obd)
            reader.adapter = adapter
            reader.connect()
            values = reader.read()
            if values:
                say("PASS", f"{len(values)} Mode 01 values: "
                            + ", ".join(f"{k}={v}" for k, v in values.items()))
            else:
                say("FAIL", "no Mode 01 PID answered; is the ignition on?")
                rc = 1
    except OBDLinkDown as exc:
        say("FAIL", f"link down: {exc}")
        rc = 1
    finally:
        try:
            adapter.close()
        except Exception:
            pass
        if stopped:
            print("\nRestarting obd-ev...")
            subprocess.run(["sudo", "systemctl", "start", "obd-ev"])
    return rc


if __name__ == "__main__":
    sys.exit(main())
