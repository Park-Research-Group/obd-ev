"""Poll a known vehicle's OBDb command set over the BLE ELM327 link.

Generic Mode 01 gives roughly ten values that every car happens to share.
A vehicle-specific OBDb signalset gives the manufacturer's own Mode 22
parameters -- pack voltage and current, per-cell temperatures, true state of
charge, per-wheel speed, steering angle -- which is what an EV study actually
needs. Use this reader when the make/model/year is known at imaging time.

Wire format is `ATH1` with the adapter's auto-formatting left on: the ELM327
adds the ISO-TP PCI byte to each request, and because headers are on it prints
every received frame raw (CAN id + PCI + data, no reassembly). This module
reassembles ISO-TP itself, which is the format OBDb's published test vectors
are recorded in (see tests/test_obdb.py).
"""

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .ble_obd import BleElm327, Elm327Timeout, OBDLinkDown
from .config import OBDConfig, VehicleConfig
from .naming import assign_columns, group_of
from .obdb import Command, SignalSet

log = logging.getLogger(__name__)


@dataclass
class _Scheduled:
    command: Command
    period: float
    due_at: float = 0.0
    failures: int = 0
    # "Disabled" is a backoff, not a verdict: the command is retried every
    # `retry_disabled_after` seconds and comes back the moment it answers.
    disabled: bool = False

    @property
    def group(self) -> Tuple[str, str, str, str, bool]:
        """Commands sharing a group need no header reprogramming between them."""
        c = self.command
        return (c.hdr or "", c.rax or "", c.eax or "", c.pri or "", c.fcm1)


class VehicleReader:
    """Schedules OBDb commands by their declared frequency and decodes replies."""

    def __init__(self, cfg: OBDConfig, vcfg: VehicleConfig, adapter=None):
        self.cfg = cfg
        self.vcfg = vcfg
        # `adapter` is an injection point for tests; production passes nothing.
        self.adapter = adapter or BleElm327(cfg, raw_frames=True)
        self.signalset = SignalSet.load(vcfg.signalset).for_year(vcfg.year)
        self._schedule = self._build_schedule()
        # Vendor signal id -> readable CSV column. Built once; signals.csv
        # carries the mapping back for anyone checking against OBDb.
        self.columns = assign_columns(
            (sig.id, sig.name, sig.fmt.unit)
            for item in self._schedule for sig in item.command.signals)
        self._group: Optional[Tuple[str, str, str, str, bool]] = None
        self._failures = 0
        self._pinned = False
        self._warned_headers: set = set()
        log.info("loaded %s: %d commands, %d signals",
                 vcfg.signalset, len(self._schedule), len(self.field_names()))

    def _build_schedule(self) -> List[_Scheduled]:
        out = []
        for command in self.signalset.commands:
            if not command.signals or command.dbg:
                continue
            if self.vcfg.exclude_paths and any(
                    s.path.startswith(p) for s in command.signals
                    for p in self.vcfg.exclude_paths):
                continue
            if self.vcfg.include_paths and not any(
                    s.path.startswith(p) for s in command.signals
                    for p in self.vcfg.include_paths):
                continue
            period = max(command.freq or self.vcfg.default_period,
                         self.vcfg.min_period)
            out.append(_Scheduled(command, period))
        # Fastest first, then grouped by header so a busy cycle reprograms the
        # adapter as few times as possible.
        out.sort(key=lambda s: (s.period, s.group))
        if self.vcfg.max_commands:
            out = out[:self.vcfg.max_commands]
        return out

    def connect(self) -> None:
        self.adapter.connect()
        self._group = None
        self._failures = 0
        self._pinned = False
        now = time.monotonic()
        for item in self._schedule:
            item.due_at = now
            item.failures = 0
            item.disabled = False

    def field_names(self) -> List[str]:
        """Stable CSV schema: every signal this vehicle could report, whether
        or not the ECU answers on a given cycle.

        De-duplicated -- a signal can legitimately appear in two commands (year
        variants, or two ECUs reporting the same quantity), and a repeated
        column name would make the CSV ambiguous to whatever reads it later.
        """
        seen = set()
        names = []
        for item in self._schedule:
            for signal in item.command.signals:
                column = self.columns[signal.id]
                if column not in seen:
                    seen.add(column)
                    names.append(column)
        return names

    def describe(self) -> List[dict]:
        """Rows for signals.csv: one per column, carrying the readable name,
        unit, category, and the OBDb id and command it was decoded from."""
        rows = []
        seen = set()
        for item in self._schedule:
            for signal in item.command.signals:
                column = self.columns[signal.id]
                if column in seen:
                    continue
                seen.add(column)
                fmt = signal.fmt
                rows.append({
                    "column": column,
                    "name": signal.name,
                    "unit": fmt.unit,
                    "group": group_of(signal.path),
                    "category": signal.path,
                    "source": "vehicle-obdb",
                    "source_id": signal.id,
                    "command": item.command.request(),
                    "period_s": item.period,
                    "min": fmt.min if fmt.max > fmt.min else "",
                    "max": fmt.max if fmt.max > fmt.min else "",
                })
        return rows

    def _at(self, command: str) -> None:
        self.adapter.command(command, timeout=2)

    def _select_group(self, item: _Scheduled) -> None:
        """Point the adapter at this command's ECU. Skipped when the previous
        command already left it configured the same way.

        Every setting a command can differ in is programmed or explicitly
        reset here -- a command with no receive filter after one that had
        one must not inherit the old filter, or its answers are dropped.
        """
        if self._group == item.group:
            return
        cmd = item.command

        # Transmit header. OBDb writes 11-bit ids as 3 hex chars; the ELM327
        # takes 3 (11-bit) or 6 (29-bit, with the priority byte from ATCP).
        if cmd.hdr:
            if len(cmd.hdr) == 6 and cmd.pri:
                self._at(f"ATCP{cmd.pri}")
            if len(cmd.hdr) in (3, 6):
                self._at(f"ATSH{cmd.hdr}")
            else:
                self._warn_once(cmd, f"header {cmd.hdr!r} is not 3 or 6 hex "
                                     f"chars; sending with the current header")

        # Receive filter: exactly this ECU, or back to the adapter's automatic
        # choice when the command does not specify one.
        if cmd.rax:
            if len(cmd.rax) in (3, 8):
                self._at(f"ATCRA{cmd.rax}")
            else:
                self._warn_once(cmd, f"receive address {cmd.rax!r} is not 3 "
                                     f"or 8 hex chars; leaving the filter open")
                self._at("ATAR")
        else:
            self._at("ATAR")

        # ISO 15765 extended addressing: one extra address byte in the data.
        if cmd.eax:
            self._at(f"ATCEA{cmd.eax}")
        else:
            self._at("ATCEA")

        if cmd.fcm1:
            # Manual flow control: answer the ECU's first frame ourselves so it
            # sends the remaining frames.
            self._at(f"ATFCSH{cmd.hdr or ''}")
            self._at("ATFCSD300000")
            self._at("ATFCSM1")
        else:
            self._at("ATFCSM0")
        self._group = item.group

    def _warn_once(self, cmd: Command, message: str) -> None:
        if cmd.key not in self._warned_headers:
            self._warned_headers.add(cmd.key)
            log.warning("%s: %s", cmd.key, message)

    def _note_failure(self, item: _Scheduled, now: float) -> None:
        """Back off a command the vehicle is not answering, so the sample
        loop stops spending a round trip on it every cycle. It is retried
        every `retry_disabled_after` seconds: a car that was asleep, or in
        accessory mode rather than READY, starts answering later."""
        item.failures += 1
        retry = self.vcfg.retry_disabled_after
        if not self.vcfg.disable_after or item.failures < self.vcfg.disable_after:
            item.due_at = now + item.period
            return
        if not item.disabled:
            item.disabled = True
            live = sum(1 for i in self._schedule if not i.disabled)
            log.info("backing off command %s after %d unanswered attempts "
                     "(%d of %d commands still active; retry in %.0fs)",
                     item.command.key, item.failures, live,
                     len(self._schedule), retry)
        item.due_at = now + max(retry, item.period)

    def read(self) -> Dict[str, object]:
        """Run every command that is due and return the decoded signals.

        Bounded by `read_budget_seconds`: whatever is still due afterwards
        waits for the next cycle, so a burst of commands coming due at once
        (all of them, right after connect) cannot hold up GPS/IMU rows.
        """
        now = time.monotonic()
        due = [i for i in self._schedule if i.due_at <= now]
        if not due:
            return {}
        due.sort(key=lambda i: i.due_at)     # longest-waiting first

        out: Dict[str, object] = {}
        timeouts = attempted = 0
        deadline = now + self.cfg.read_budget_seconds
        for item in due:
            if attempted and time.monotonic() >= deadline:
                break
            attempted += 1
            try:
                self._select_group(item)
                response = self.adapter.command(item.command.request())
            except Elm327Timeout:
                timeouts += 1
                self._note_failure(item, now)
                continue

            decoded = item.command.decode_response(response)
            if decoded:
                if item.disabled:
                    log.info("command %s is answering again", item.command.key)
                item.failures = 0
                item.disabled = False
                item.due_at = now + item.period
                out.update({self.columns[k]: v for k, v in decoded.items()})
            else:
                # The usual shape of an unsupported command is a prompt reply
                # of "NO DATA", not a timeout, so this path -- not the one
                # above -- is what backs off most dead commands.
                self._note_failure(item, now)

        if out:
            # The vehicle is demonstrably awake, so backoffs made while it
            # was answering are real and stand.
            self._failures = 0
            if not self._pinned:
                # The adapter has now searched and found the protocol.
                self._pinned = bool(self.adapter.pin_protocol())
        elif timeouts == attempted:
            self._failures += 1
            if self._failures >= self.cfg.max_read_failures:
                raise OBDLinkDown(
                    f"adapter silent for {self._failures} consecutive cycles")

        if all(i.disabled for i in self._schedule):
            # Nothing answered at all. Much the likeliest cause is that the
            # car was asleep when we connected -- every command returns NO
            # DATA -- rather than the signalset being wrong for this vehicle.
            # Reset and force a reconnect so the next attempt starts clean.
            log.warning("every command went unanswered; resetting the schedule "
                        "and reconnecting (was the vehicle asleep?)")
            for item in self._schedule:
                item.disabled = False
                item.failures = 0
            raise OBDLinkDown("no command answered on this connection")
        return out

    def close(self) -> None:
        self.adapter.close()
