import logging
import os
import socket
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import List, Optional
import yaml

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
VEHICLE_DIR = REPO_ROOT / "vehicles"


@dataclass
class OBDConfig:
    ble_address: Optional[str] = None
    ble_name: str = "VEEPEAK"
    ble_write_uuid: Optional[str] = None
    ble_notify_uuid: Optional[str] = None
    # Time budget for the initial BLE link + ELM327 handshake.
    timeout: int = 30
    # Per-PID request budget. Must stay small: a hung adapter otherwise
    # stalls the whole sample loop for `timeout` seconds per PID.
    command_timeout: float = 1.0
    reconnect_seconds: int = 15
    # Consecutive all-empty read cycles before we tear the link down and
    # reconnect. A BLE link can stay "connected" while the adapter is wedged.
    max_read_failures: int = 5
    # ELM327 adaptive timing: 0=off, 1=adaptive, 2=aggressive.
    adaptive_timing: int = 1
    # ELM327 response timeout, in 4ms units, as a hex string (32 = 200ms).
    response_timeout: str = "32"
    # ELM327 protocol number (ATSP). "0" searches automatically on the first
    # request and the logger then pins whatever was found. A known vehicle
    # can skip the search: "6" is ISO 15765-4 CAN 11-bit 500k, which is
    # every modern car including the Bolt EUV and IONIQ 5.
    protocol: str = "0"
    # Ask the ECU which PIDs it supports and skip the rest.
    probe_supported: bool = True
    # Longest one sample-loop read may spend on the adapter. Commands still
    # due after this are picked up next cycle. Bounds how long GPS/IMU rows
    # are held up when many commands come due at once (107 on the Bolt).
    read_budget_seconds: float = 1.0
    # Polled every cycle.
    core_pids: List[str] = field(default_factory=lambda: [
        "SPEED", "HV_BATTERY_LIFE", "CONTROL_MODULE_VOLTAGE",
        "ACCEL_PEDAL_D", "ACCEL_PEDAL_E", "RELATIVE_ACCEL_POS",
        "ACTUAL_TORQUE", "RPM", "ENGINE_LOAD", "THROTTLE_POS",
    ])
    # Polled every `slow_every_n` cycles. For values that barely move.
    slow_pids: List[str] = field(default_factory=lambda: [
        "AMBIENT_AIR_TEMP", "COOLANT_TEMP", "RUN_TIME", "ODOMETER",
        "BAROMETRIC_PRESSURE", "FUEL_LEVEL",
    ])
    slow_every_n: int = 20


@dataclass
class VehicleConfig:
    """Vehicle-specific OBDb signalset. When `signalset` is set the logger
    polls that vehicle's manufacturer parameters instead of generic Mode 01.
    Fetch one at imaging time with `scripts/fetch_signalset.py`."""
    signalset: Optional[str] = None      # path to an OBDb default.json
    make_model: Optional[str] = None     # e.g. Hyundai-IONIQ-5, for the record
    year: Optional[int] = None           # selects year-filtered commands
    # Floor on request period, seconds. OBDb marks some commands 0.25s; the
    # BLE link cannot sustain that across many commands at once.
    min_period: float = 0.25
    default_period: float = 1.0          # for commands with no declared freq
    max_commands: int = 0                # 0 = every command in the signalset
    # Back a command off after this many consecutive unanswered requests.
    disable_after: int = 5
    # ...and retry it this often afterwards. Retirement is a backoff, not a
    # verdict: a car in accessory mode answers NO DATA on its HV commands
    # and starts answering once it is in READY.
    retry_disabled_after: float = 300.0
    include_paths: List[str] = field(default_factory=list)
    exclude_paths: List[str] = field(default_factory=list)


@dataclass
class GPSConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 2947


@dataclass
class IMUConfig:
    enabled: bool = True
    i2c_address: int = 0x68
    sample_hz: int = 100
    lowpass_alpha: float = 0.1


@dataclass
class LoggerConfig:
    output_dir: str = "./logs"
    # Flush on whichever comes first. The row count alone is not enough: at
    # sub-1Hz row rates ten rows can be a minute of driving, and on most cars
    # the ignition cuts power to the Pi with no warning.
    flush_every: int = 10
    flush_seconds: float = 2.0
    uploaded_subdir: str = "uploaded"
    # Upper bound on the row rate. The OBD read usually paces the loop well
    # below this; the cap only matters when OBD is disconnected or very fast.
    max_hz: float = 10.0
    # Row rate while the OBD adapter is disconnected (parked, car off). Keeps
    # GPS/IMU context without filling the card.
    idle_hz: float = 0.2
    # Close and rotate the trip file at least this often. Rotation is what
    # makes a file eligible for upload: upload.sh never touches the file the
    # logger is currently writing.
    rotate_minutes: float = 15.0
    # Rotation interval while parked (trip closed, idle_hz rows). Long, so a
    # car parked for a week does not become hundreds of files -- but not
    # unbounded, so parked data still gets uploaded and one file cannot
    # grow for weeks.
    idle_rotate_minutes: float = 360.0
    # OBD offline for this long ends the trip and closes the file.
    trip_gap_seconds: float = 120.0


@dataclass
class DeviceConfig:
    id: Optional[str] = None


@dataclass
class Config:
    obd: OBDConfig = field(default_factory=OBDConfig)
    vehicle: VehicleConfig = field(default_factory=VehicleConfig)
    gps: GPSConfig = field(default_factory=GPSConfig)
    imu: IMUConfig = field(default_factory=IMUConfig)
    logger: LoggerConfig = field(default_factory=LoggerConfig)
    device: DeviceConfig = field(default_factory=DeviceConfig)


def _build(cls, raw: Optional[dict]):
    """Construct a config dataclass, warning about unknown keys rather than
    crashing. A stale key in a deployed config.yaml must never take a kit
    offline in the field."""
    raw = raw or {}
    if not isinstance(raw, dict):
        log.warning("%s section is not a mapping (%r); using defaults",
                    cls.__name__, raw)
        raw = {}
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        log.warning("ignoring unknown %s keys: %s",
                    cls.__name__, ", ".join(sorted(unknown)))
    return cls(**{k: v for k, v in raw.items() if k in known})


def _sanitize(cfg: "Config") -> None:
    """Coerce the handful of values whose YAML type is easy to get wrong.
    `response_timeout: 32` parses as an int, `flush_every: 0` would divide
    by zero -- neither should take a kit offline."""
    obd, lg = cfg.obd, cfg.logger
    obd.response_timeout = str(obd.response_timeout).strip().upper() or "32"
    obd.protocol = str(obd.protocol).strip().upper() or "0"
    if obd.protocol not in "0123456789ABC" or len(obd.protocol) != 1:
        log.warning("obd.protocol=%r is not an ELM327 protocol number; "
                    "using automatic", obd.protocol)
        obd.protocol = "0"
    obd.command_timeout = max(0.1, float(obd.command_timeout))
    obd.read_budget_seconds = max(obd.command_timeout,
                                  float(obd.read_budget_seconds))
    lg.flush_every = max(1, int(lg.flush_every))
    lg.flush_seconds = max(0.1, float(lg.flush_seconds))
    lg.max_hz = max(0.01, float(lg.max_hz))
    lg.idle_hz = max(0.001, float(lg.idle_hz))
    if cfg.device.id is not None:
        cfg.device.id = str(cfg.device.id)


def load(path: str | Path = "config.yaml") -> Config:
    p = Path(path)
    raw = {}
    if p.exists():
        with p.open() as f:
            raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        log.error("%s is not a YAML mapping; ignoring it and using defaults", p)
        raw = {}

    cfg = Config(
        obd=_build(OBDConfig, raw.get("obd")),
        vehicle=_build(VehicleConfig, raw.get("vehicle")),
        gps=_build(GPSConfig, raw.get("gps")),
        imu=_build(IMUConfig, raw.get("imu")),
        logger=_build(LoggerConfig, raw.get("logger")),
        device=_build(DeviceConfig, raw.get("device")),
    )

    # Per-device override: env > config > hostname.
    if not cfg.device.id:
        cfg.device.id = os.environ.get("OBD_EV_DEVICE_ID") or socket.gethostname()
    # upload.sh reads the same variable; keep the two from drifting apart.
    log_dir = os.environ.get("OBD_EV_LOG_DIR")
    if log_dir:
        cfg.logger.output_dir = log_dir

    # The vehicle differs per participant while config.yaml is baked into the
    # master image, so /etc/default/obd-ev wins over it -- that is the file
    # personalised per kit.
    vehicle = os.environ.get("OBD_EV_VEHICLE")
    if vehicle:
        cfg.vehicle.make_model = vehicle
        cfg.vehicle.signalset = str(VEHICLE_DIR / f"{vehicle}.json")
    year = os.environ.get("OBD_EV_VEHICLE_YEAR")
    if year:
        try:
            cfg.vehicle.year = int(year)
        except ValueError:
            log.warning("OBD_EV_VEHICLE_YEAR=%r is not a year, ignoring", year)
    # A relative signalset path is relative to the repo, not to wherever
    # systemd happened to start us.
    if cfg.vehicle.signalset and not Path(cfg.vehicle.signalset).is_absolute():
        cfg.vehicle.signalset = str(REPO_ROOT / cfg.vehicle.signalset)
    _sanitize(cfg)
    return cfg
