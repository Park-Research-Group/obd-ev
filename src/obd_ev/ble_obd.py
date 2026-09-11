import asyncio
import concurrent.futures
import logging
import re
import subprocess
import threading
from concurrent.futures import Future
from typing import (TYPE_CHECKING, Callable, Dict, List, NamedTuple, Optional,
                    Sequence, Set, Tuple)

from .naming import assign_columns

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover - exercised on devices before setup reruns
    BleakClient = None
    BleakScanner = None

if TYPE_CHECKING:
    from .config import OBDConfig

log = logging.getLogger(__name__)


def _u16(b):
    return (b[0] * 256) + b[1]


class PidDef(NamedTuple):
    pid: int
    decode: Callable[[Sequence[int]], float]
    nbytes: int
    label: str          # human-readable, for signals.csv
    unit: str           # OBDb unit enum, so column suffixes match


# SAE J1979 Mode 01 PIDs. Vehicles answer only a subset; the reader probes
# support at connect and skips the rest, so listing extras costs nothing.
#
# The EV-relevant ones are 0x5B (pack state of charge), 0x42 (module
# voltage), 0x49/0x4A/0x5A (pedal demand) and 0x62 (actual torque). The
# classic ICE PIDs are kept for hybrids and for combustion control vehicles.
PID_DEFS = {
    "ENGINE_LOAD": PidDef(
        0x04, lambda b: b[0] * 100.0 / 255.0, 1, "Calculated engine load", "percent"),
    "COOLANT_TEMP": PidDef(
        0x05, lambda b: b[0] - 40.0, 1, "Coolant temperature", "celsius"),
    "RPM": PidDef(
        0x0C, lambda b: _u16(b) / 4.0, 2, "Engine speed", "rpm"),
    "SPEED": PidDef(
        0x0D, lambda b: float(b[0]), 1, "Vehicle speed", "kilometersPerHour"),
    "INTAKE_TEMP": PidDef(
        0x0F, lambda b: b[0] - 40.0, 1, "Intake air temperature", "celsius"),
    "MAF": PidDef(
        0x10, lambda b: _u16(b) / 100.0, 2, "Mass air flow", "gramsPerSecond"),
    "THROTTLE_POS": PidDef(
        0x11, lambda b: b[0] * 100.0 / 255.0, 1, "Throttle position", "percent"),
    "RUN_TIME": PidDef(
        0x1F, lambda b: float(_u16(b)), 2, "Run time since engine start", "seconds"),
    "FUEL_LEVEL": PidDef(
        0x2F, lambda b: b[0] * 100.0 / 255.0, 1, "Fuel tank level", "percent"),
    "BAROMETRIC_PRESSURE": PidDef(
        0x33, lambda b: float(b[0]), 1, "Barometric pressure", "kilopascal"),
    "CONTROL_MODULE_VOLTAGE": PidDef(
        0x42, lambda b: _u16(b) / 1000.0, 2, "Control module voltage", "volts"),
    "AMBIENT_AIR_TEMP": PidDef(
        0x46, lambda b: b[0] - 40.0, 1, "Ambient air temperature", "celsius"),
    "ACCEL_PEDAL_D": PidDef(
        0x49, lambda b: b[0] * 100.0 / 255.0, 1, "Accelerator pedal position D", "percent"),
    "ACCEL_PEDAL_E": PidDef(
        0x4A, lambda b: b[0] * 100.0 / 255.0, 1, "Accelerator pedal position E", "percent"),
    "RELATIVE_ACCEL_POS": PidDef(
        0x5A, lambda b: b[0] * 100.0 / 255.0, 1, "Relative accelerator position", "percent"),
    "HV_BATTERY_LIFE": PidDef(
        0x5B, lambda b: b[0] * 100.0 / 255.0, 1, "Hybrid/EV battery remaining life", "percent"),
    "ENGINE_OIL_TEMP": PidDef(
        0x5C, lambda b: b[0] - 40.0, 1, "Engine oil temperature", "celsius"),
    "ACTUAL_TORQUE": PidDef(
        0x62, lambda b: b[0] - 125.0, 1, "Actual engine torque", "percent"),
    "REFERENCE_TORQUE": PidDef(
        0x63, lambda b: float(_u16(b)), 2, "Reference engine torque", "newtonMeters"),
    "ODOMETER": PidDef(
        0xA6, lambda b: ((b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]) / 10.0,
        4, "Odometer", "kilometers"),
}

PROMPT = ">"

# Banks of the "PIDs supported" bitmask (Mode 01). Each answers 4 bytes
# covering the next 32 PIDs; the lowest bit says whether the next bank exists.
SUPPORT_BANKS = [0x00, 0x20, 0x40, 0x60, 0x80, 0xA0]


class Elm327Timeout(RuntimeError):
    """The adapter did not return a prompt within the command budget."""


class OBDLinkDown(RuntimeError):
    """The link is up but the adapter has stopped answering; reconnect."""


# ELM327 protocol numbers (AT SP h). "0" means search automatically.
ELM_PROTOCOLS = {
    "0": "automatic", "1": "SAE J1850 PWM", "2": "SAE J1850 VPW",
    "3": "ISO 9141-2", "4": "ISO 14230-4 KWP (5 baud)",
    "5": "ISO 14230-4 KWP (fast)", "6": "ISO 15765-4 CAN 11-bit 500k",
    "7": "ISO 15765-4 CAN 29-bit 500k", "8": "ISO 15765-4 CAN 11-bit 250k",
    "9": "ISO 15765-4 CAN 29-bit 250k", "A": "SAE J1939",
    "B": "USER1 CAN", "C": "USER2 CAN",
}


def parse_dpn(response: str) -> Optional[str]:
    """Protocol number out of an `ATDPN` reply, or None if the adapter has
    not settled on one yet.

    The reply is a single hex digit, prefixed with 'A' while automatic
    search is enabled. Straight after `ATSP0`, before any OBD request has
    gone out, the adapter has searched nothing and reports protocol 0 --
    pinning that would be a no-op, so it is reported as "not yet known".
    """
    for line in reversed(response.strip().upper().splitlines()):
        m = re.fullmatch(r"\s*A?([0-9A-C])\s*", line)
        if m:
            return None if m.group(1) == "0" else m.group(1)
    return None


def _forget_cached_device(address: str) -> None:
    """Drop BlueZ's cached record for an address.

    BlueZ's Device1.Connect() is transport-agnostic: for an address it holds
    BR/EDR information about it will try classic profiles and fail with
    "br-connection-profile-unavailable", even when the LE scan that found the
    device worked perfectly. Removing the cached device makes BlueZ re-learn it
    from the next LE advertisement. `ControllerMode = le` in
    /etc/bluetooth/main.conf prevents this properly; this is the in-field
    recovery for kits that predate that setting.
    """
    for args in (["disconnect", address], ["remove", address]):
        try:
            subprocess.run(["bluetoothctl", *args], capture_output=True,
                           text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("bluetoothctl %s failed: %s", args[0], exc)


def _is_bredr_failure(exc: Exception) -> bool:
    return "br-connection" in str(exc)


class BleElm327:
    def __init__(self, cfg: "OBDConfig", raw_frames: bool = False):
        self.cfg = cfg
        # raw_frames: print CAN headers and skip the adapter's own ISO-TP
        # assembly, so multi-frame Mode 22 answers can be reassembled here.
        # Required by the OBDb reader; the generic Mode 01 reader wants the
        # adapter's tidy formatting instead.
        self.raw_frames = raw_frames
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.client: Optional[BleakClient] = None
        self.write_uuid: Optional[str] = None
        self.notify_uuid: Optional[str] = None
        self.buffer = ""
        self.notify_event: Optional[asyncio.Event] = None
        self._closed = False
        self.thread.start()

    def connect(self) -> None:
        # Scan and connect each get `cfg.timeout`, plus the ELM327 handshake.
        self._run(self._connect(), timeout=2 * self.cfg.timeout + 30)

    def close(self) -> None:
        try:
            self._run(self._close(), timeout=10)
        except Exception as exc:
            log.debug("error closing BLE link: %s", exc)
        finally:
            self._closed = True
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=2)

    def command(self, command: str, timeout: Optional[float] = None) -> str:
        budget = timeout or self.cfg.command_timeout
        return self._run(self._command(command, budget), timeout=budget + 5)

    def pin_protocol(self) -> bool:
        """Freeze the protocol the adapter settled on. Returns True once it
        is pinned (or was fixed by config), False if it is not yet known."""
        return self._run(self._pin_protocol(), timeout=15)

    def _run(self, coro, timeout: float):
        """Run a coroutine on the BLE thread and wait for it, bounded.

        Every BLE operation ends up here from the sample loop's thread. An
        unbounded wait would let a wedged BlueZ/D-Bus call stall the whole
        logger -- GPS and IMU rows included -- so a timeout here is treated
        as the link being gone, and the supervisor rebuilds it.
        """
        if self._closed or not self.thread.is_alive():
            coro.close()
            raise OBDLinkDown("BLE event loop is not running")
        future: Future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise OBDLinkDown(f"BLE operation did not complete in {timeout:.0f}s")

    async def _connect(self) -> None:
        if BleakClient is None or BleakScanner is None:
            raise RuntimeError("BLE transport requires the 'bleak' Python package")

        if self.cfg.ble_address:
            log.info("connecting to BLE OBD adapter at %s", self.cfg.ble_address)
            subprocess.run(
                ["bluetoothctl", "disconnect", self.cfg.ble_address],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.client = BleakClient(self.cfg.ble_address, timeout=self.cfg.timeout)
            try:
                await self.client.connect()
            except Exception as exc:
                if _is_bredr_failure(exc):
                    # Same BlueZ transport mix-up as on the scan path below.
                    log.warning("BlueZ attempted a BR/EDR connection to %s (%s); "
                                "forgetting the cached device and retrying over LE",
                                self.cfg.ble_address, exc)
                    _forget_cached_device(self.cfg.ble_address)
                    await asyncio.sleep(2)
                    self.client = None
                else:
                    log.warning("direct BLE connect failed (%s), falling back to scan", exc)
                    self.client = None

        if self.client is None:
            device = await BleakScanner.find_device_by_filter(
                lambda d, _: bool(d.name and self.cfg.ble_name.lower() in d.name.lower()),
                timeout=self.cfg.timeout,
            )
            if device is None:
                raise RuntimeError("BLE OBD adapter not found")
            log.info("connecting to BLE OBD adapter %s (%s)", device.name, device.address)
            self.client = BleakClient(device, timeout=self.cfg.timeout)
            try:
                await self.client.connect()
            except Exception as exc:
                if not _is_bredr_failure(exc):
                    raise
                # BlueZ tried classic Bluetooth for a BLE-only adapter. Forget
                # the cached record and try once more over LE.
                log.warning("BlueZ attempted a BR/EDR connection (%s); "
                            "forgetting the cached device and retrying over LE",
                            exc)
                _forget_cached_device(device.address)
                await asyncio.sleep(2)
                self.client = BleakClient(device, timeout=self.cfg.timeout)
                await self.client.connect()

        self.write_uuid, self.notify_uuid = self._select_characteristics()
        self.notify_event = asyncio.Event()
        await self.client.start_notify(self.notify_uuid, self._on_notify)

        # ATZ resets the adapter and emits a version banner; give it room.
        await self._command("ATZ", timeout=8)
        headers = "ATH1" if self.raw_frames else "ATH0"
        # Auto-formatting stays ON in both modes. With headers on, the ELM327
        # prints received frames raw (CAN id + PCI byte, no reassembly),
        # which is the format obdb.reassemble() expects -- while on the send
        # side it still adds the ISO-TP PCI byte and pads to 8 bytes. With
        # CAF0 the request bytes go out exactly as typed, so `22F010` would
        # hit the bus as a consecutive frame and no ECU would ever answer.
        for cmd in ["ATE0", "ATL0", "ATS0", headers, "ATCAF1", "ATCFC1"]:
            await self._command(cmd, timeout=3)
        # Adaptive timing lets the adapter return as soon as the ECU is done
        # instead of always waiting out the full response window.
        await self._command(f"ATAT{self.cfg.adaptive_timing}", timeout=3)
        await self._command(f"ATST{self.cfg.response_timeout}", timeout=3)
        await self._command(f"ATSP{self._protocol()}", timeout=3)

    def _protocol(self) -> str:
        return str(self.cfg.protocol or "0").strip().upper()

    async def _pin_protocol(self) -> bool:
        """Freeze the auto-detected protocol so later failures don't trigger a
        fresh (multi-second) protocol search on every command.

        Only meaningful after the adapter has actually talked to the vehicle:
        the search happens on the first OBD request, not on `ATSP0`, so the
        caller invokes this once data has come back. Returns True when
        pinned, False when the protocol is not known yet.
        """
        if self._protocol() != "0":
            return True         # fixed by config; nothing to discover
        try:
            found = await self._command("ATDPN", timeout=3)
        except Elm327Timeout:
            return False
        number = parse_dpn(found)
        if number is None:
            log.debug("protocol not determined yet (ATDPN -> %r)", found)
            return False
        try:
            await self._command(f"ATSP{number}", timeout=3)
        except Elm327Timeout:
            log.debug("could not pin protocol, leaving auto-detect on")
            return False
        log.info("pinned OBD protocol to %s (%s)", number,
                 ELM_PROTOCOLS.get(number, "?"))
        return True

    async def _close(self) -> None:
        if self.client and self.client.is_connected:
            if self.notify_uuid:
                try:
                    await self.client.stop_notify(self.notify_uuid)
                except Exception:
                    pass
            await self.client.disconnect()

    def is_connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    def _select_characteristics(self) -> Tuple[str, str]:
        if not self.client:
            raise RuntimeError("BLE client is not connected")

        chars = [c for service in self.client.services for c in service.characteristics]
        by_uuid = {c.uuid.lower(): c for c in chars}

        write_uuid = self.cfg.ble_write_uuid
        notify_uuid = self.cfg.ble_notify_uuid
        if write_uuid and notify_uuid:
            return write_uuid, notify_uuid

        notify_chars = [c for c in chars if "notify" in c.properties]
        write_chars = [
            c for c in chars
            if "write" in c.properties or "write-without-response" in c.properties
        ]

        # Common VEEPEAK / BLE ELM327 UART-style characteristic pairs.
        preferred_pairs = [
            ("0000fff2-0000-1000-8000-00805f9b34fb", "0000fff1-0000-1000-8000-00805f9b34fb"),
            ("0000fff1-0000-1000-8000-00805f9b34fb", "0000fff2-0000-1000-8000-00805f9b34fb"),
            ("000069fe-0000-1000-8000-00805f9b34fb", "00000318-0000-1000-8000-00805f9b34fb"),
            ("00000318-0000-1000-8000-00805f9b34fb", "000069fe-0000-1000-8000-00805f9b34fb"),
        ]
        for write, notify in preferred_pairs:
            if not write_uuid and write in by_uuid and by_uuid[write] in write_chars:
                write_uuid = write
            if not notify_uuid and notify in by_uuid and by_uuid[notify] in notify_chars:
                notify_uuid = notify
            if write_uuid and notify_uuid:
                return write_uuid, notify_uuid

        if not write_uuid and write_chars:
            write_uuid = write_chars[0].uuid
        if not notify_uuid and notify_chars:
            notify_uuid = notify_chars[0].uuid
        if not write_uuid or not notify_uuid:
            raise RuntimeError("could not find BLE write/notify characteristics")
        log.info("BLE OBD write=%s notify=%s", write_uuid, notify_uuid)
        return write_uuid, notify_uuid

    def _on_notify(self, _sender, data: bytearray) -> None:
        text = bytes(data).decode(errors="ignore")
        self.buffer += text
        if self.notify_event and PROMPT in self.buffer:
            self.notify_event.set()

    async def _command(self, command: str, timeout: Optional[float] = None) -> str:
        if not self.client or not self.write_uuid or not self.notify_event:
            raise RuntimeError("BLE OBD adapter is not connected")
        budget = timeout or self.cfg.command_timeout
        self.buffer = ""
        self.notify_event.clear()
        try:
            await asyncio.wait_for(
                self.client.write_gatt_char(
                    self.write_uuid, (command.strip() + "\r").encode(),
                    response=False),
                timeout=budget)
        except asyncio.TimeoutError:
            raise OBDLinkDown(f"BLE write of {command} did not complete")
        try:
            await asyncio.wait_for(self.notify_event.wait(), timeout=budget)
        except asyncio.TimeoutError:
            # Let any straggling notification land, then drop it, so the next
            # command doesn't read this one's tail as its own response.
            await asyncio.sleep(0.05)
            self.buffer = ""
            self.notify_event.clear()
            raise Elm327Timeout(f"no response to {command} within {budget:.1f}s")
        return _clean_response(self.buffer, command)


def _clean_response(raw: str, command: str) -> str:
    text = raw.replace("\r", "\n").replace(PROMPT, "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(line for line in lines if line.upper() != command.upper())


def parse_pid_response(response: str, pid: int, needed: int) -> Optional[List[int]]:
    """Pull the data bytes out of a Mode 01 response. `41 0C 1A F8` -> [26, 248]."""
    if "NODATA" in re.sub(r"\s", "", response).upper():
        return None
    payload = re.sub(r"[^0-9A-Fa-f]", "", response).upper()
    marker = f"41{pid:02X}"
    idx = payload.find(marker)
    if idx < 0:
        return None
    data = payload[idx + len(marker):]
    if len(data) < needed * 2:
        return None
    return [int(data[i:i + 2], 16) for i in range(0, needed * 2, 2)]


def decode_support_mask(data: Sequence[int], base: int) -> Set[int]:
    """A support bank answers 4 bytes; the high bit of byte 0 is PID base+1."""
    supported = set()
    for i, byte in enumerate(data[:4]):
        for bit in range(8):
            if byte & (0x80 >> bit):
                supported.add(base + i * 8 + bit + 1)
    return supported


class BleOBDReader:
    def __init__(self, cfg: "OBDConfig"):
        self.cfg = cfg
        self.adapter = BleElm327(cfg)
        self.core = [n for n in cfg.core_pids if n in PID_DEFS]
        self.slow = [n for n in cfg.slow_pids if n in PID_DEFS]
        for name in set(cfg.core_pids + cfg.slow_pids) - set(PID_DEFS):
            log.warning("unknown PID %r in config, ignoring", name)
        self.columns = assign_columns(
            (n, PID_DEFS[n].label, PID_DEFS[n].unit) for n in self.core + self.slow)
        self.supported: Optional[Set[int]] = None
        self._cycle = 0
        self._failures = 0
        self._pinned = False

    def connect(self) -> None:
        self.adapter.connect()
        self._cycle = 0
        self._failures = 0
        self._pinned = False
        self.supported = self._probe_supported() if self.cfg.probe_supported else None
        if self.supported:
            # The probe made the adapter search for a protocol; freeze it now.
            self._pinned = bool(self.adapter.pin_protocol())
        if self.supported is not None:
            live = [n for n in self.core + self.slow
                    if PID_DEFS[n].pid in self.supported]
            missing = [n for n in self.core + self.slow if n not in live]
            log.info("OBD ready: %d/%d configured PIDs supported by this vehicle",
                     len(live), len(self.core) + len(self.slow))
            if missing:
                log.info("vehicle does not report: %s", ", ".join(missing))
        else:
            log.info("OBD ready, polling all %d configured PIDs unprobed",
                     len(self.core) + len(self.slow))

    def _probe_supported(self) -> Optional[Set[int]]:
        supported: Set[int] = set()
        for base in SUPPORT_BANKS:
            try:
                response = self.adapter.command(f"01{base:02X}", timeout=3)
            except Elm327Timeout:
                log.warning("PID support probe timed out at bank %02X", base)
                break
            data = parse_pid_response(response, base, 4)
            if data is None:
                break
            supported |= decode_support_mask(data, base)
            # The last bit of a bank flags whether the next bank exists.
            if (base + 0x20) not in supported:
                break
        if not supported:
            log.warning("vehicle returned no PID support mask; polling everything")
            return None
        return supported

    def _due(self) -> List[str]:
        names = list(self.core)
        if self.cfg.slow_every_n > 0 and self._cycle % self.cfg.slow_every_n == 0:
            names += self.slow
        if self.supported is None:
            return names
        return [n for n in names if PID_DEFS[n].pid in self.supported]

    def read(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        timeouts = 0
        due = self._due()
        for name in due:
            spec = PID_DEFS[name]
            try:
                response = self.adapter.command(f"01{spec.pid:02X}")
            except Elm327Timeout:
                timeouts += 1
                continue
            data = parse_pid_response(response, spec.pid, spec.nbytes)
            if data is None:
                continue
            try:
                out[self.columns[name]] = spec.decode(data)
            except (IndexError, ValueError) as exc:
                log.debug("could not decode %s from %r: %s", name, response, exc)
        self._cycle += 1

        if out and not self._pinned:
            self._pinned = bool(self.adapter.pin_protocol())

        # A wedged adapter answers nothing at all; a merely unsupported PID
        # answers "NO DATA" quickly. Only the former should force a reconnect.
        if due and timeouts == len(due):
            self._failures += 1
            if self._failures >= self.cfg.max_read_failures:
                raise OBDLinkDown(
                    f"adapter silent for {self._failures} consecutive cycles")
        else:
            self._failures = 0
        return out

    def field_names(self) -> List[str]:
        """Every configured PID, supported or not, so the CSV schema is stable
        across vehicles and across reconnects mid-drive."""
        return [self.columns[n] for n in self.core + self.slow]

    def describe(self) -> List[dict]:
        """Rows for signals.csv, so every column in the CSV is documented."""
        rows = []
        for name in self.core + self.slow:
            spec = PID_DEFS[name]
            rows.append({
                "column": self.columns[name],
                "name": spec.label,
                "unit": spec.unit,
                "group": "OBD",
                "category": "OBD",
                "source": "obd-mode01",
                "source_id": name,
                "command": f"01{spec.pid:02X}",
                "period_s": "" if name in self.core else self.cfg.slow_every_n,
            })
        return rows

    def close(self) -> None:
        self.adapter.close()
