#!/usr/bin/env python3
"""Check a kit before it leaves the bench.

Most ways this kit fails in the field are silent: a mistyped vehicle name falls
back to generic Mode 01, a shared upload token stops working weeks later, a
loose IMU just logs zeros. Every check here is something that has no visible
symptom until the study is over.

    scripts/preflight.py            # check everything
    scripts/preflight.py --quick    # skip the cloud round trip

Exit status is 0 when nothing FAILed, so it can gate an imaging script.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from obd_ev.venv import reexec_if_needed  # noqa: E402
reexec_if_needed()   # otherwise bleak/mpu6050 are reported missing wrongly

GREEN, YELLOW, RED, DIM, RESET = (
    ("\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m")
    if sys.stdout.isatty() else ("", "", "", "", ""))

results = []


def record(status, name, detail=""):
    results.append((status, name, detail))
    colour = {"PASS": GREEN, "WARN": YELLOW, "FAIL": RED}[status]
    print(f"  {colour}{status:<4}{RESET}  {name}"
          + (f"\n        {DIM}{detail}{RESET}" if detail else ""))


def run(*cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def section(title):
    print(f"\n{title}")


# -- configuration ----------------------------------------------------------

def check_config():
    section("Configuration")
    from obd_ev import config as cfgmod

    cfg_path = REPO_ROOT / "config.yaml"
    if not cfg_path.exists():
        record("WARN", "config.yaml missing",
               "using built-in defaults; cp config.yaml.example config.yaml")
    try:
        cfg = cfgmod.load(cfg_path)
    except Exception as exc:
        record("FAIL", "config.yaml does not load", str(exc))
        return None

    if cfg.device.id and cfg.device.id != os.uname().nodename:
        record("PASS", f"device id = {cfg.device.id}")
    else:
        record("WARN", f"device id falls back to hostname ({cfg.device.id})",
               "set OBD_EV_DEVICE_ID in /etc/default/obd-ev")
    return cfg


def check_vehicle(cfg):
    section("Vehicle profile")
    if not cfg.vehicle.signalset:
        record("WARN", "no vehicle profile; generic Mode 01 will be used",
               "scripts/select_vehicle.py  (far more data with a profile)")
        return
    path = Path(cfg.vehicle.signalset)
    if not path.exists():
        record("FAIL", f"profile not found: {path}",
               "the kit would silently fall back to generic Mode 01. "
               "Fix with scripts/select_vehicle.py")
        return
    try:
        from obd_ev.obdb import SignalSet
        ss = SignalSet.load(path).for_year(cfg.vehicle.year)
    except Exception as exc:
        record("FAIL", f"profile will not parse: {path}", str(exc))
        return
    signals = sum(len(c.signals) for c in ss.commands)
    if not ss.commands:
        record("FAIL", "profile has no commands for this model year",
               f"year={cfg.vehicle.year}; try clearing OBD_EV_VEHICLE_YEAR")
        return
    record("PASS", f"{cfg.vehicle.make_model or path.stem} "
                   f"{cfg.vehicle.year or '(any year)'}",
           f"{len(ss.commands)} commands, {signals} signals")

    meta_path = path.with_suffix(".meta.json")
    if meta_path.exists() and cfg.vehicle.year:
        years = json.loads(meta_path.read_text()).get("years") or []
        if years and cfg.vehicle.year not in years:
            record("WARN", f"model year {cfg.vehicle.year} outside "
                           f"{years[0]}-{years[-1]} for this model")
    if len(ss.commands) > 40:
        record("WARN", f"{len(ss.commands)} commands is a lot for a BLE link",
               "expect a low row rate; consider vehicle.exclude_paths")


# -- python and hardware ----------------------------------------------------

def check_dependencies():
    section("Python dependencies")
    for module, why in (("yaml", "config parsing"),
                        ("bleak", "BLE OBD adapter"),
                        ("gps", "gpsd client"),
                        ("mpu6050", "IMU")):
        try:
            __import__(module)
            record("PASS", f"{module} importable")
        except ImportError:
            status = "FAIL" if module in ("yaml", "bleak") else "WARN"
            record(status, f"{module} not importable", f"needed for {why}")


def check_gps(cfg):
    section("GPS")
    if not cfg.gps.enabled:
        record("WARN", "GPS disabled in config")
        return
    # The serial device first: if it is missing, gpsd will look "running" and
    # simply never produce a fix, which is indistinguishable from bad sky view.
    device = "/dev/ttyAMA0"
    gpsd_defaults = Path("/etc/default/gpsd")
    if gpsd_defaults.exists():
        for line in gpsd_defaults.read_text().splitlines():
            if line.startswith("DEVICES="):
                found = line.split("=", 1)[1].strip().strip('"').split()
                if found:
                    device = found[0]
    if Path(device).exists():
        record("PASS", f"serial device {device} present")
    else:
        record("FAIL", f"{device} does not exist",
               "on a Pi 5 the header UART needs dtparam=uart0=on in "
               "/boot/firmware/config.txt, then a reboot")
        return

    active = run("systemctl", "is-active", "--quiet", "gpsd").returncode == 0
    record("PASS" if active else "FAIL", "gpsd running",
           "" if active else "sudo systemctl enable --now gpsd")
    if not active:
        return
    out = run("gpspipe", "-w", "-n", "8", timeout=25)
    if out.returncode != 0:
        record("WARN", "could not read from gpsd",
               "install gpsd-clients, or check DEVICES in /etc/default/gpsd")
        return
    # A clock silently a day out poisons every timestamp in the dataset, and
    # "System clock synchronized: yes" was reported on a kit 1.05 days behind.
    # GPS time is the only reference that does not need a network.
    import re as _re
    gps_times = _re.findall(r'"time":"([0-9T:\-\.]+)Z?"', out.stdout)
    if gps_times and '"mode":[23]' in out.stdout.replace(" ", "") or gps_times:
        try:
            import datetime as _dt
            gt = _dt.datetime.strptime(gps_times[-1].split(".")[0],
                                       "%Y-%m-%dT%H:%M:%S")
            skew = (_dt.datetime.utcnow() - gt).total_seconds()
            if abs(skew) < 5:
                record("PASS", f"clock agrees with GPS ({skew:+.1f}s)")
            elif abs(skew) < 3600:
                record("WARN", f"clock is {skew:+.0f}s off GPS",
                       "obd-ev-timesync.timer corrects this within 15 min")
            else:
                record("FAIL", f"clock is {skew/86400:+.2f} days off GPS",
                       "every timestamp in the data is wrong by this much. "
                       "Fix now: sudo scripts/gps_time_sync.py --force")
        except (ValueError, IndexError):
            pass

    if '"class":"TPV"' in out.stdout:
        has_fix = '"lat"' in out.stdout
        record("PASS" if has_fix else "WARN",
               "gpsd reporting position" if has_fix
               else "gpsd responding but no fix yet",
               "" if has_fix else "cold start takes 30s-2min; needs sky view")
    else:
        record("WARN", "gpsd produced no position reports",
               "check the receiver is wired to the UART and powered")


def check_imu(cfg):
    section("IMU")
    if not cfg.imu.enabled:
        record("WARN", "IMU disabled in config")
        return
    # i2c-tools installs into /usr/sbin, which is not on a non-login shell's
    # PATH -- so a plain which() reports it missing on a kit that has it.
    i2cdetect = shutil.which("i2cdetect") or shutil.which(
        "i2cdetect", path="/usr/sbin:/sbin:/usr/local/sbin")
    if not i2cdetect:
        record("WARN", "i2cdetect not installed", "sudo apt install i2c-tools")
        return
    out = run(i2cdetect, "-y", "1")
    addr = f"{cfg.imu.i2c_address:02x}"
    if addr in out.stdout:
        record("PASS", f"IMU present at 0x{addr}")
    else:
        record("FAIL", f"no I2C device at 0x{addr}",
               "check wiring (SDA=pin 3, SCL=pin 5) and that I2C is enabled")


def check_bluetooth(cfg):
    section("Bluetooth / OBD adapter")
    active = run("systemctl", "is-active", "--quiet", "bluetooth").returncode == 0
    record("PASS" if active else "FAIL", "bluetooth service running")

    # The service can be perfectly healthy while the radio is rfkill-blocked or
    # unpowered, in which case the OBD adapter never appears and the only
    # symptom is a connect error buried in the journal.
    blocked = run("rfkill", "list", "bluetooth")
    if "Soft blocked: yes" in blocked.stdout or "Hard blocked: yes" in blocked.stdout:
        record("FAIL", "Bluetooth radio is rfkill-blocked",
               "sudo rfkill unblock bluetooth  -- the OBD adapter cannot be "
               "seen until this is cleared")
    elif blocked.returncode == 0 and blocked.stdout.strip():
        record("PASS", "Bluetooth radio not blocked")

    conf = Path("/etc/bluetooth/main.conf")
    if conf.exists():
        le_only = any(line.strip().replace(" ", "").lower() == "controllermode=le"
                      for line in conf.read_text().splitlines())
        if le_only:
            record("PASS", "controller is LE-only")
        else:
            record("WARN", "controller is in dual mode",
                   "BlueZ may try classic Bluetooth and fail with "
                   "br-connection-profile-unavailable. Fix: ./scripts/setup_pi.sh")

    powered = run("bluetoothctl", "show")
    if "Powered: yes" in powered.stdout:
        record("PASS", "Bluetooth controller powered")
    elif powered.returncode == 0 and powered.stdout.strip():
        record("FAIL", "Bluetooth controller not powered",
               "sudo bluetoothctl power on")
    if cfg.obd.ble_address:
        record("PASS", f"adapter pinned to {cfg.obd.ble_address}")
    else:
        record("WARN", f"adapter discovered by name ({cfg.obd.ble_name})",
               "pinning obd.ble_address is faster and more reliable")


def check_led(cfg):
    section("Status LED")
    if not cfg.led.enabled:
        record("WARN", "status LED disabled in config")
        return
    led = Path(cfg.led.path)
    if not led.is_dir():
        record("WARN", f"no LED at {led}",
               "set led.path, or led.enabled: false")
        return
    brightness = led / "brightness"
    if os.access(brightness, os.W_OK):
        record("PASS", f"LED writable: {brightness}")
    else:
        record("WARN", f"{brightness} not writable by this user",
               "the service fixes this itself via led_prepare.sh at start; "
               "only a problem if it persists after a restart")
    trigger = (led / "trigger")
    if trigger.exists():
        active = trigger.read_text()
        if "[none]" in active:
            record("PASS", "LED trigger detached")
        else:
            record("WARN", "LED still owned by a kernel trigger",
                   "led_prepare.sh detaches it at service start")


# -- upload path ------------------------------------------------------------

def check_upload(quick):
    section("Cloud upload")
    remote = os.environ.get("OBD_EV_REMOTE", "obd-ev:obd-ev-uploads")
    conf = os.environ.get("OBD_EV_RCLONE_CONF",
                          str(Path.home() / ".config/rclone/rclone.conf"))
    if not shutil.which("rclone"):
        record("FAIL", "rclone not installed")
        return
    if not Path(conf).exists():
        record("FAIL", f"no rclone config at {conf}",
               "scripts/image_setup.sh")
        return

    name = remote.split(":", 1)[0]
    if run("rclone", "--config", conf, "config", "show", name).returncode != 0:
        record("FAIL", f"no rclone remote named {name!r}")
        return
    record("PASS", f"rclone remote {name!r} configured")

    marker = Path("/var/lib/obd-ev/upload-authorized.json")
    if marker.exists():
        try:
            rec = json.loads(marker.read_text())
            record("PASS", "kit has its own upload authorization",
                   f"fingerprint {rec.get('token_fingerprint')}, "
                   f"{rec.get('authorized_at')}")
        except ValueError:
            record("WARN", "upload-authorized.json is unreadable")
    else:
        record("WARN", "kit still using the master image's credential",
               "run scripts/authorize_kit.sh, or uploads may stop when "
               "another kit refreshes")

    if quick:
        return
    probe = f"{remote}/_preflight_{os.getpid()}"
    written = subprocess.run(["rclone", "--config", conf, "rcat", probe],
                             input="preflight\n", text=True,
                             capture_output=True, timeout=90)
    if written.returncode == 0:
        run("rclone", "--config", conf, "delete", probe)
        record("PASS", "test upload succeeded")
    else:
        record("FAIL", "test upload failed",
               (written.stderr or "").strip().splitlines()[-1:] and
               (written.stderr or "").strip().splitlines()[-1] or "")


# -- services and storage ---------------------------------------------------

def check_services():
    section("Services")
    for unit, required in (("obd-ev.service", True),
                           ("obd-ev-upload.timer", True),
                           ("obd-ev-provision.service", False)):
        enabled = run("systemctl", "is-enabled", unit).stdout.strip()
        ok = enabled in ("enabled", "enabled-runtime", "static")
        record("PASS" if ok else ("FAIL" if required else "WARN"),
               f"{unit} {enabled or 'not installed'}",
               "" if ok else "./scripts/setup_pi.sh")


def check_storage(cfg):
    section("Storage")
    log_dir = Path(cfg.logger.output_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        probe = log_dir / ".preflight"
        probe.write_text("ok")
        probe.unlink()
        record("PASS", f"log directory writable: {log_dir}")
    except OSError as exc:
        record("FAIL", f"cannot write to {log_dir}", str(exc))
        return
    usage = shutil.disk_usage(log_dir)
    free_gb = usage.free / 1e9
    record("PASS" if free_gb > 1 else "WARN",
           f"{free_gb:.1f} GB free",
           "" if free_gb > 1 else "a full card stops collection entirely")


def check_provisioning():
    section("Participant WiFi")
    marker = Path("/var/lib/obd-ev/provisioned.json")
    lock = Path("/var/lib/obd-ev/setup-in-progress")
    if marker.exists():
        record("WARN", "a home network is already provisioned",
               "the setup portal will NOT run for the participant. Remove "
               f"{marker} before shipping.")
    elif lock.exists():
        record("PASS", "setup portal armed, held off while setup runs",
               "setup_kit.sh removes the hold when it finishes cleanly")
    else:
        record("PASS", "setup portal will run on first boot")
    if len(os.environ.get("OBD_EV_AP_PASSWORD", "")) >= 8:
        record("PASS", "setup-AP password set")
    else:
        record("WARN", "OBD_EV_AP_PASSWORD unset or too short",
               "the provisioning service will refuse to start")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--quick", action="store_true",
                        help="skip the cloud round trip")
    args = parser.parse_args()

    # /etc/default/obd-ev is what the services read; mirror it here.
    env_file = Path("/etc/default/obd-ev")
    try:
        env_text = env_file.read_text() if env_file.exists() else ""
    except OSError as exc:
        # Root-owned and unreadable: report it rather than crashing, since
        # every later check depends on what is in here.
        print(f"  {YELLOW}WARN{RESET}  cannot read {env_file}: {exc}")
        env_text = ""
    if env_text:
        for line in env_text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

    print(f"obd-ev preflight  ({REPO_ROOT})")
    cfg = check_config()
    if cfg is None:
        return 1
    check_vehicle(cfg)
    check_dependencies()
    check_gps(cfg)
    check_imu(cfg)
    check_bluetooth(cfg)
    check_led(cfg)
    check_storage(cfg)
    check_upload(args.quick)
    check_services()
    check_provisioning()

    failed = sum(1 for s, _, _ in results if s == "FAIL")
    warned = sum(1 for s, _, _ in results if s == "WARN")
    passed = sum(1 for s, _, _ in results if s == "PASS")
    print(f"\n{passed} passed, {warned} warnings, {failed} failures")
    if failed:
        print(f"{RED}Not ready to ship.{RESET} Fix the FAIL items above.")
    elif warned:
        print(f"{YELLOW}Ready, with warnings.{RESET} Read them before shipping.")
    else:
        print(f"{GREEN}Ready to ship.{RESET}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
