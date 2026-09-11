"""Decoder for OBDb signalsets (https://github.com/OBDb).

OBDb publishes one repo per vehicle containing `signalsets/v3/default.json`,
a community-maintained description of that model's diagnostic commands and how
to decode the bytes they return. For a known make/model this unlocks the
manufacturer Mode 22 PIDs -- pack current, cell temperatures, true state of
charge, per-wheel speed, steering angle -- instead of the ~10 generic Mode 01
values every car happens to share.

This module is pure: bytes in, decoded signals out. It carries no I/O so it can
be tested against OBDb's own published test vectors (see tests/test_obdb.py).

Wire format assumed: `ATH1` (headers on). With headers on the ELM327 prints
every received frame raw -- CAN id, PCI byte, data, no reassembly -- which is
exactly the form OBDb records in its test cases, e.g.

    738102262F010FFFF00
    7382100000000000000

which reassembles to the ISO-TP payload `62 F0 10 FF FF 00 00 ...`.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Bytes echoed back by the ECU before the payload proper: the response service
# id (request + 0x40) followed by the PID. Mode 22 PIDs are two bytes wide.
ECHO_LEN = {"01": 2, "21": 2, "22": 3}


@dataclass(frozen=True)
class Fmt:
    length: int
    bix: int = 0
    blsb: bool = False
    sign: bool = False
    min: float = 0.0
    max: float = 0.0
    add: float = 0.0
    mul: float = 1.0
    div: float = 1.0
    unit: str = "unknown"
    nullmin: Optional[float] = None
    nullmax: Optional[float] = None
    map: Optional[Dict[str, str]] = None

    @classmethod
    def from_json(cls, raw: dict) -> "Fmt":
        return cls(
            length=raw["len"],
            bix=raw.get("bix", 0),
            blsb=raw.get("blsb", False),
            sign=raw.get("sign", False),
            min=raw.get("min", 0.0),
            max=raw.get("max", 0.0),
            add=raw.get("add", 0.0),
            mul=raw.get("mul", 1.0),
            div=raw.get("div", 1.0),
            unit=raw.get("unit", "unknown"),
            nullmin=raw.get("nullmin"),
            nullmax=raw.get("nullmax"),
            map=raw.get("map"),
        )

    def decode(self, data: bytes):
        """Decode one signal out of an echo-stripped payload.

        Mirrors OBDb's reference implementation: extract big-endian bits at
        `bix`, byte-swapping the covered span when `blsb` is set, apply two's
        complement if signed, then scale as `raw * mul / div + add` and clamp
        to [min, max] when a real range is given.
        """
        raw = _extract_bits(data, self.bix, self.length, self.blsb)
        if raw is None:
            return None
        if self.map is not None:
            # Map entries are `{"value": "AC_2", "description": "A/C Level 2"}`
            # in every published signalset; tolerate a bare string too.
            entry = self.map.get(str(raw))
            if isinstance(entry, dict):
                return entry.get("value")
            return entry
        if self.sign:
            raw = _twos_complement(raw, self.length)

        value = (raw * self.mul / self.div) + self.add

        # Sentinel handling. The reference decoder leaves these in; we drop
        # them so "sensor not present" never reaches the dataset as a real
        # reading. Checked before clamping, which would hide the sentinel.
        if self.nullmin is not None and value <= self.nullmin:
            return None
        if self.nullmax is not None and value >= self.nullmax:
            return None

        if self.max > self.min:
            value = max(self.min, min(value, self.max))
        return value


@dataclass(frozen=True)
class Signal:
    id: str
    fmt: Fmt
    name: str = ""
    path: str = ""

    @classmethod
    def from_json(cls, raw: dict) -> "Signal":
        return cls(
            id=raw["id"],
            fmt=Fmt.from_json(raw["fmt"]),
            name=raw.get("name", ""),
            path=raw.get("path", ""),
        )


@dataclass(frozen=True)
class YearFilter:
    frm: Optional[int] = None
    to: Optional[int] = None
    years: Tuple[int, ...] = ()

    @classmethod
    def from_json(cls, raw: Optional[dict]) -> Optional["YearFilter"]:
        if not raw:
            return None
        return cls(raw.get("from"), raw.get("to"), tuple(raw.get("years", ())))

    def matches(self, year: int) -> bool:
        if self.years and year in self.years:
            return True
        if self.frm is not None and year < self.frm:
            return False
        if self.to is not None and year > self.to:
            return False
        return not self.years or year in self.years


@dataclass
class Command:
    service: str          # "01", "21" or "22"
    pid: str              # hex, 2 chars for 01/21, 4 for 22
    signals: Tuple[Signal, ...]
    hdr: Optional[str] = None
    rax: Optional[str] = None
    eax: Optional[str] = None
    pri: Optional[str] = None
    tst: Optional[str] = None
    tmo: Optional[str] = None
    fcm1: bool = False
    freq: Optional[float] = None
    filter: Optional[YearFilter] = None
    # OBDb marks commands under investigation with `dbg`; they are not part
    # of the production signal set and a logger should not poll them.
    dbg: bool = False

    @classmethod
    def from_json(cls, raw: dict) -> "Command":
        cmd = raw["cmd"]
        service, pid = next(iter(cmd.items()))
        return cls(
            service=service,
            pid=pid,
            signals=tuple(Signal.from_json(s) for s in raw.get("signals", [])),
            hdr=raw.get("hdr"),
            rax=raw.get("rax"),
            eax=raw.get("eax"),
            pri=raw.get("pri"),
            tst=raw.get("tst"),
            tmo=raw.get("tmo"),
            fcm1=raw.get("fcm1", False),
            freq=raw.get("freq"),
            filter=YearFilter.from_json(raw.get("filter")),
            dbg=bool(raw.get("dbg", False)),
        )

    @property
    def key(self) -> str:
        return f"{self.hdr or ''}.{self.rax or ''}.{self.service}{self.pid}"

    def request(self) -> str:
        """The bytes to send to the ELM327, e.g. `22F010`."""
        return f"{self.service}{self.pid}".upper()

    @property
    def echo_len(self) -> int:
        return ECHO_LEN.get(self.service, 1 + len(self.pid) // 2)

    def applies_to(self, year: Optional[int]) -> bool:
        if self.filter is None or year is None:
            return True
        return self.filter.matches(year)

    @property
    def echo_bytes(self) -> bytes:
        """What the ECU prefixes its answer with: request service + 0x40, then
        the PID. `22F010` is answered `62 F0 10 ...`."""
        return bytes.fromhex(f"{int(self.service, 16) + 0x40:02X}{self.pid}")

    def select_payload(self, packets: Dict[str, List[bytes]]) -> Optional[bytes]:
        """Pick this command's answer out of everything the adapter returned.

        Matching on the echo rather than on arrival order matters: a read can
        pick up the tail of a previous command, and on some vehicles more than
        one ECU answers the same request.

        When several messages match, the newest wins -- a stale message left
        in the adapter's buffer from an earlier command sorts first, and the
        answer to the request we just sent sorts last.
        """
        expected = self.echo_bytes
        if self.rax:
            matches = [m for m in packets.get(self.rax.upper(), [])
                       if m.startswith(expected)]
            if matches:
                return matches[-1]
        matches = [m for msgs in packets.values() for m in msgs
                   if m.startswith(expected)]
        return matches[-1] if matches else None

    @property
    def can_id_len(self) -> int:
        """OBDb writes 11-bit headers as 3 hex chars and 29-bit ones as 4.
        The format cannot be inferred from a response line -- frame lengths
        are ambiguous -- so it has to come from the signalset."""
        return 29 if self.hdr and len(self.hdr) == 4 else 11

    def decode_response(self, text: str) -> Dict[str, object]:
        """Reassemble raw adapter output and decode this command's signals."""
        packets = reassemble(text, can_id_len=self.can_id_len,
                             extended_addressing=bool(self.eax))
        payload = self.select_payload(packets)
        return self.decode(payload) if payload is not None else {}

    def decode(self, payload: bytes) -> Dict[str, object]:
        """Decode every signal from a reassembled, echo-inclusive payload."""
        expected = self.echo_bytes
        if not payload.startswith(expected):
            log.debug("%s: payload %s does not echo %s",
                      self.key, payload[:4].hex(), expected.hex())
            return {}
        data = payload[self.echo_len:]
        out: Dict[str, object] = {}
        for signal in self.signals:
            try:
                value = signal.fmt.decode(data)
            except Exception as exc:  # a malformed frame must not stop the rest
                log.debug("%s: could not decode %s: %s", self.key, signal.id, exc)
                continue
            if value is not None:
                out[signal.id] = value
        return out


@dataclass
class SignalSet:
    commands: List[Command] = field(default_factory=list)

    @classmethod
    def from_json(cls, raw: dict) -> "SignalSet":
        return cls(commands=[Command.from_json(c) for c in raw.get("commands", [])])

    @classmethod
    def load(cls, path: str | Path) -> "SignalSet":
        with Path(path).open() as fh:
            return cls.from_json(json.load(fh))

    def for_year(self, year: Optional[int]) -> "SignalSet":
        return SignalSet([c for c in self.commands if c.applies_to(year)])

    def signal_ids(self) -> List[str]:
        return [s.id for c in self.commands for s in c.signals]


# --------------------------------------------------------------------------
# ISO-TP reassembly over ELM327 raw-frame output
# --------------------------------------------------------------------------

SINGLE_FRAME = 0x0
FIRST_FRAME = 0x1
CONSECUTIVE_FRAME = 0x2
FLOW_CONTROL = 0x3


def _clean_lines(text: str) -> List[str]:
    out = []
    for line in re.split(r"[\r\n]+", text):
        line = re.sub(r"\s", "", line).upper()
        if not line or not re.fullmatch(r"[0-9A-F]+", line):
            continue
        out.append(line)
    return out


def reassemble(text: str, can_id_len: int = 11,
               extended_addressing: bool = False) -> Dict[str, List[bytes]]:
    """Turn raw ELM327 frame output into {can_id: [payload, ...]}.

    Handles single frames and multi-frame ISO-TP (first + consecutive), for
    both 11-bit (3 hex char) and 29-bit (8 hex char) CAN identifiers.

    A single read can carry more than one message per CAN ID -- a long
    multi-frame answer followed by a stray single-frame reply from an earlier
    command is routine -- so every completed message is kept, in arrival
    order, and the caller picks the one whose echo it asked for.
    """
    id_chars = 8 if can_id_len == 29 else 3
    done: Dict[str, List[bytes]] = {}
    open_msg: Dict[str, bytearray] = {}
    remaining: Dict[str, int] = {}

    def finish(can_id: str) -> None:
        if can_id in open_msg:
            done.setdefault(can_id, []).append(bytes(open_msg.pop(can_id)))
            remaining.pop(can_id, None)

    for line in _clean_lines(text):
        if len(line) < id_chars + 2:
            continue
        can_id, rest = line[:id_chars], line[id_chars:]
        if len(rest) < 2:
            continue
        body = bytes.fromhex(rest) if len(rest) % 2 == 0 else bytes.fromhex(rest[:-1])
        if extended_addressing:
            body = body[1:]
        if not body:
            continue

        pci_type = body[0] >> 4
        if pci_type == SINGLE_FRAME:
            finish(can_id)
            length = body[0] & 0x0F
            done.setdefault(can_id, []).append(bytes(body[1:1 + length]))
        elif pci_type == FIRST_FRAME:
            if len(body) < 2:
                continue
            finish(can_id)
            total = ((body[0] & 0x0F) << 8) | body[1]
            open_msg[can_id] = bytearray(body[2:total + 2])
            remaining[can_id] = total - len(open_msg[can_id])
        elif pci_type == CONSECUTIVE_FRAME:
            if can_id not in open_msg:
                continue
            take = remaining.get(can_id, 0)
            chunk = body[1:1 + take] if take > 0 else b""
            open_msg[can_id].extend(chunk)
            remaining[can_id] = take - len(chunk)
            if remaining[can_id] <= 0:
                finish(can_id)
        elif pci_type == FLOW_CONTROL:
            continue

    # Anything still open was truncated mid-transfer; keep what arrived.
    for can_id in list(open_msg):
        finish(can_id)
    return done


def _extract_bits(data: bytes, bix: int, length: int, blsb: bool) -> Optional[int]:
    end = bix + length
    if end > len(data) * 8:
        return None

    if blsb and length > 8:
        buf = bytearray(data)
        start_byte = bix // 8
        end_byte = min(start_byte + (length + 7) // 8, len(buf))
        buf[start_byte:end_byte] = bytes(reversed(buf[start_byte:end_byte]))
        data = bytes(buf)

    result = 0
    for i in range(bix, end):
        if data[i // 8] & (1 << (7 - (i % 8))):
            result |= 1 << (end - i - 1)
    return result


def _twos_complement(value: int, bits: int) -> int:
    if value & (1 << (bits - 1)):
        value -= 1 << bits
    return value
