#!/usr/bin/env python3
"""Full-screen status display for the Raspberry Pi 7" touchscreen.

For debugging a kit from the driver's seat: what the logger is recording right
now, and how long ago each signal last answered. Not part of a participant
build -- scripts/install_dashboard.sh puts it on one kit, by hand.

It cannot affect data collection, by construction:

* It never opens the BLE adapter, gpsd or the IMU. BLE allows one connection,
  so a display that opened its own would have to stop `obd-ev` first -- and
  then it would not be showing the real thing. Everything here comes from the
  trip CSV the logger is already writing, read incrementally from a byte
  offset, plus the `.current` marker the uploader also reads.
* It only ever reads from the log directory. No file in it is opened for
  writing, created or removed, so the logger's rotation and the uploader's
  scan see nothing new.
* It runs as a separate process under a separate service, niced and at idle
  I/O priority, so the scheduler prefers the logger whenever both are
  runnable. If it crashes, the logger does not notice.

Two renderers, picked automatically:

    pygame on KMS/DRM   the touchscreen, no desktop needed (the default)
    ANSI text           a terminal over SSH, or /dev/tty1

    scripts/dashboard.py                  # touchscreen if available, else text
    scripts/dashboard.py --backend text   # force text, e.g. over SSH
    scripts/dashboard.py --once           # one snapshot, for a quick check

Touch the top strip to change view, the left or right quarter to page. Over a
terminal: arrow keys to page, `v` for the view, `q` to quit.
"""

import argparse
import csv
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from obd_ev.venv import reexec_if_needed  # noqa: E402
reexec_if_needed()

from obd_ev import config as cfgmod  # noqa: E402
# Unit labels and the trip/dictionary lookups, shared with the terminal
# version of this tool so the two cannot drift apart.
from obd_watch import UNITS, fmt, load_dictionary, newest_trip  # noqa: E402

# Columns the logger contributes itself; never shown as vehicle signals.
META_COLUMNS = ("timestamp", "t_mono", "device_id", "obd_connected")

# No row for this long means the logger is not writing. Parked rows come at
# `logger.idle_hz` (0.2 Hz shipped -- one row every five seconds), so this has
# to clear that comfortably or a parked kit looks broken.
STALL_SECONDS = 20.0

# A signal older than this is stale enough to call out; older than the second,
# it has effectively stopped answering.
FRESH_SECONDS = 3.0
STALE_SECONDS = 15.0

OK, WARN, BAD = 0, 1, 2

VIEWS = ("vehicle", "vehicle-all", "gps", "imu")
VIEW_LABELS = {
    "vehicle": "VEHICLE · answering",
    "vehicle-all": "VEHICLE · all polled",
    "gps": "GPS",
    "imu": "IMU",
}


# -- model -----------------------------------------------------------------

@dataclass
class Signal:
    """One CSV column, and when it last carried a value."""
    column: str
    name: str
    unit: str
    group: str
    source: str
    value: str = ""
    seen_at: Optional[float] = None     # t_mono of the last non-empty cell

    def age(self, now: float) -> Optional[float]:
        return None if self.seen_at is None else max(0.0, now - self.seen_at)


@dataclass
class Row:
    label: str
    value: str
    unit: str
    age: Optional[float]


@dataclass
class Snapshot:
    """Everything a renderer draws. Backends read this and nothing else."""
    device: str = ""
    state: str = "waiting"
    severity: int = BAD
    rate: float = 0.0
    rows: int = 0
    trip: str = "-"
    part: str = "-"
    facts: List[Tuple[str, str, int]] = field(default_factory=list)
    view: str = "vehicle"
    page: int = 0
    pages: int = 1
    body: List[Row] = field(default_factory=list)
    note: str = ""


def severity_for_age(age: Optional[float]) -> int:
    if age is None:
        return BAD
    if age <= FRESH_SECONDS:
        return OK
    return WARN if age <= STALE_SECONDS else BAD


# -- reading the trip CSV ---------------------------------------------------

class TripTail:
    """Follows the CSV part the logger is writing, from a byte offset.

    Re-reading the whole file each tick, as the terminal tool does, is fine for
    a one-off look but not for a process left running a whole drive: a 15
    minute part is tens of megabytes of mostly-known rows. Reading only what
    was appended keeps the cost flat, which is what makes this safe to leave
    on the screen.

    A flush can land mid-line, so the trailing partial line is held back until
    the rest of it arrives.
    """

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.path: Optional[Path] = None
        self._fh = None
        self._header: List[str] = []
        self._partial = ""

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
        self._fh = None
        self._partial = ""

    def _open(self, path: Path) -> List[dict]:
        """Attach to `path` and read everything already in it."""
        self.close()
        try:
            self._fh = path.open("r", newline="")
        except OSError:
            self.path = None
            return []
        self.path = path
        self._header = []
        return self._drain()

    # -- reading -----------------------------------------------------------

    def _parse(self, line: str) -> Optional[dict]:
        try:
            cells = next(csv.reader([line]))
        except (csv.Error, StopIteration):
            return None
        if not self._header:
            self._header = cells
            return None
        return dict(zip(self._header, cells))

    def _drain(self) -> List[dict]:
        """Every complete line appended since the last call."""
        if self._fh is None:
            return []
        try:
            chunk = self._fh.read()
        except OSError:
            return []
        if not chunk:
            return []
        self._partial += chunk
        *lines, self._partial = self._partial.split("\n")
        rows = []
        for line in lines:
            if not line.strip():
                continue
            row = self._parse(line)
            if row is not None:
                rows.append(row)
        return rows

    def poll(self) -> Tuple[List[dict], bool]:
        """`(new rows, rotated)`. On a rotation the tail of the part being
        closed is read out before switching, so its last rows are not lost."""
        current = newest_trip(self.log_dir)
        if current is None:
            self.close()
            self.path = None
            return [], False
        if self.path is None or current != self.path:
            tail = self._drain()               # finish the old part
            rows = self._open(current)
            return tail + rows, True

        # Truncated or replaced under us -- not something the logger does, but
        # a partial read of a vanished file would otherwise loop forever.
        try:
            if self.path.stat().st_size < self._fh.tell():
                return self._open(current), True
        except OSError:
            return [], False
        return self._drain(), False


class Monitor:
    """Trip state assembled from the rows read so far."""

    def __init__(self, log_dir: Path, device_id: str):
        self.log_dir = log_dir
        self.device_id = device_id
        self.tail = TripTail(log_dir)
        self.meta: Dict[str, tuple] = {}
        self.signals: Dict[str, Signal] = {}
        self.rows = 0
        self.last_row: Dict[str, str] = {}
        self.last_row_at: Optional[float] = None
        self.last_obd_at: Optional[float] = None
        self._recent: List[float] = []          # t_mono of recent rows
        self._disk: Tuple[float, int] = (0.0, 0)
        self._queued: Tuple[float, int] = (0.0, 0)

    # -- ingest ------------------------------------------------------------

    def _signal(self, column: str) -> Signal:
        sig = self.signals.get(column)
        if sig is None:
            name, unit, group, source = self.meta.get(
                column, (column, "", "", ""))
            sig = Signal(column, name, unit, group, source)
            self.signals[column] = sig
        return sig

    @staticmethod
    def _row_time(row: dict, fallback: float) -> float:
        """When the logger wrote this row, on our own clock.

        `t_mono` is CLOCK_MONOTONIC, which both processes share -- so it stays
        comparable across the clock correction GPS applies at boot, where the
        wall-clock `timestamp` column would jump by hours.
        """
        try:
            return float(row["t_mono"])
        except (KeyError, TypeError, ValueError):
            return fallback

    def poll(self) -> None:
        new, rotated = self.tail.poll()
        if rotated and self.tail.path is not None:
            self.meta = load_dictionary(self.tail.path.parent)
            self.signals = {}
            self.rows = 0
            self._recent = []
        now = time.monotonic()
        for row in new:
            at = self._row_time(row, now)
            self.rows += 1
            self.last_row = row
            self.last_row_at = at
            self._recent.append(at)
            if (row.get("obd_connected") or "").strip() == "1":
                self.last_obd_at = at
            for column, value in row.items():
                if column in META_COLUMNS or not (value or "").strip():
                    continue
                sig = self._signal(column)
                sig.value = value.strip()
                sig.seen_at = at
        del self._recent[:-64]

    # -- derived -----------------------------------------------------------

    @property
    def rate(self) -> float:
        if len(self._recent) < 2:
            return 0.0
        span = self._recent[-1] - self._recent[0]
        return (len(self._recent) - 1) / span if span > 0.05 else 0.0

    def columns(self, source: str) -> List[Signal]:
        """Every column from one source, in the order the dictionary lists
        them, so the display order does not shuffle as signals come and go."""
        known = [c for c, m in self.meta.items() if m[3] == source]
        if not known and source == "vehicle-obdb":
            # No dictionary (an old trip, or a generic Mode 01 fallback):
            # everything that is not meta or a known sensor column.
            sensors = {c for c, m in self.meta.items()
                       if m[3] in ("gps", "imu")}
            known = [c for c in self.last_row
                     if c not in META_COLUMNS and c not in sensors]
        return [self._signal(c) for c in known]

    def free_gb(self) -> float:
        now = time.monotonic()
        if now - self._disk[0] > 10:
            try:
                self._disk = (now, shutil.disk_usage(self.log_dir).free)
            except OSError:
                self._disk = (now, 0)
        return self._disk[1] / 1e9

    def queued_trips(self) -> int:
        """Trip folders not yet moved under `uploaded/`. Cheap, but a glob
        over the card, so it is refreshed on its own slow timer."""
        now = time.monotonic()
        if now - self._queued[0] > 15:
            try:
                n = sum(1 for p in self.log_dir.glob("*/drive_*.csv")
                        if "uploaded" not in p.parts
                        and p != self.tail.path)
            except OSError:
                n = 0
            self._queued = (now, n)
        return self._queued[1]


# -- snapshot ---------------------------------------------------------------

def _state(mon: Monitor, now: float) -> Tuple[str, int]:
    if mon.last_row_at is None:
        return "WAITING FOR LOGGER", BAD
    if now - mon.last_row_at > STALL_SECONDS:
        return f"NO ROWS FOR {now - mon.last_row_at:.0f}s", BAD
    if (mon.last_row.get("obd_connected") or "").strip() != "1":
        return "NO OBD ADAPTER", WARN
    vehicle = [s for s in mon.columns("vehicle-obdb") if s.seen_at is not None]
    if not vehicle or now - max(s.seen_at for s in vehicle) > STALE_SECONDS:
        return "CAR NOT ANSWERING", WARN
    return "LOGGING", OK


def _facts(mon: Monitor, now: float) -> List[Tuple[str, str, int]]:
    out: List[Tuple[str, str, int]] = []

    if mon.last_obd_at is None:
        out.append(("OBD", "never", BAD))
    else:
        age = now - mon.last_obd_at
        out.append(("OBD", f"{age:.1f}s", severity_for_age(age)))

    fix = (mon.last_row.get("fix_mode") or "").strip()
    sats = (mon.last_row.get("gps_sats_used") or "0").strip()
    label = {"3": "3D", "2": "2D"}.get(fix, "no fix")
    out.append(("GPS", f"{label} {sats}sat",
                OK if fix == "3" else WARN if fix == "2" else BAD))

    try:
        imu_age = float(mon.last_row.get("imu_age_s") or "")
        out.append(("IMU", f"{imu_age:.1f}s", severity_for_age(imu_age)))
    except ValueError:
        out.append(("IMU", "-", BAD))

    free = mon.free_gb()
    out.append(("DISK", f"{free:.1f}G", OK if free > 2 else
                WARN if free > 0.5 else BAD))
    queued = mon.queued_trips()
    out.append(("PENDING", str(queued), OK if queued < 40 else WARN))
    return out


def _empty_note(mon: Monitor, polled: int) -> str:
    """Why there is nothing to show -- the three cases look identical on
    screen and have completely different fixes."""
    if not polled:
        return "no vehicle signals configured for this trip"
    if (mon.last_row.get("obd_connected") or "").strip() != "1":
        return ("no BLE link to the adapter -- check it is plugged in and "
                "that scripts/obd_probe.py can reach it")
    return ("adapter connected but the car has answered nothing -- "
            "an EV has to be in READY")


def _rows_for(mon: Monitor, view: str, now: float) -> Tuple[List[Row], str]:
    if view in ("gps", "imu"):
        signals = mon.columns(view)
        note = f"no {view.upper()} columns in this trip" if not signals else ""
    else:
        signals = mon.columns("vehicle-obdb")
        polled = len(signals)
        if view == "vehicle":
            signals = [s for s in signals if s.seen_at is not None]
        note = _empty_note(mon, polled) if not signals else ""

    body = [Row(s.name or s.column, fmt(s.value) if s.value else "-",
                UNITS.get(s.unit, s.unit), s.age(now)) for s in signals]
    return body, note


def snapshot(mon: Monitor, view: str, page: int, per_page: int) -> Snapshot:
    now = time.monotonic()
    body, note = _rows_for(mon, view, now)
    pages = max(1, -(-len(body) // max(1, per_page)))
    page = min(max(0, page), pages - 1)
    state, severity = _state(mon, now)
    part = mon.tail.path
    return Snapshot(
        device=mon.device_id,
        state=state,
        severity=severity,
        rate=mon.rate,
        rows=mon.rows,
        trip=part.parent.name if part else "-",
        part=part.name if part else "no open CSV",
        facts=_facts(mon, now),
        view=view,
        page=page,
        pages=pages,
        body=body[page * per_page:(page + 1) * per_page],
        note=note,
    )


# -- renderers --------------------------------------------------------------

class TextScreen:
    """ANSI text, for a terminal over SSH or a bare /dev/tty1."""

    name = "text"
    rows_per_page = 14

    COLOR = {OK: "\033[32m", WARN: "\033[33m", BAD: "\033[31m"}
    RESET = "\033[0m"

    def __init__(self, out=None, plain: bool = False):
        self._out = out or sys.stdout
        self._plain = plain or not self._out.isatty()
        self._width = shutil.get_terminal_size((80, 24)).columns

    def _tint(self, text: str, severity: int) -> str:
        if self._plain:
            return text
        return f"{self.COLOR.get(severity, '')}{text}{self.RESET}"

    def draw(self, snap: Snapshot) -> None:
        w = min(max(self._width, 60), 110)
        lines = []
        head = f"{snap.device}  {snap.state}"
        lines.append(self._tint(head, snap.severity)
                     + f"{snap.rate:>{max(1, w - len(head) - 12)}.2f} rows/s"
                     + f"  row {snap.rows}")
        lines.append(f"{snap.trip} / {snap.part}")
        lines.append("  ".join(self._tint(f"{k} {v}", s)
                               for k, v, s in snap.facts))
        lines.append("-" * w)
        lines.append(f"{VIEW_LABELS[snap.view]}"
                     f"{f'page {snap.page + 1}/{snap.pages}':>{w - len(VIEW_LABELS[snap.view])}}")
        for line in _wrap(snap.note, w - 4):
            lines.append(f"  {line}")
        name_w = max(20, w - 30)
        for row in snap.body:
            age = "-" if row.age is None else f"{row.age:.1f}s"
            lines.append(f"  {row.label[:name_w]:<{name_w}} "
                         f"{row.value:>10} {row.unit:<6} "
                         + self._tint(f"{age:>7}", severity_for_age(row.age)))
        if not self._plain:
            self._out.write("\033[2J\033[H")
        self._out.write("\n".join(lines) + "\n")
        self._out.flush()

    def events(self) -> List[str]:
        return []

    def close(self) -> None:
        if not self._plain:
            self._out.write("\033[0m\n")
            self._out.flush()


class PygameScreen:
    """The touchscreen. SDL on KMS/DRM, so no X or Wayland is needed."""

    name = "pygame"

    BG = (16, 18, 22)
    PANEL = (26, 29, 35)
    TEXT = (228, 232, 238)
    DIM = (128, 136, 148)
    TINT = {OK: (104, 204, 128), WARN: (232, 184, 84), BAD: (228, 96, 96)}

    def __init__(self, size: Optional[Tuple[int, int]] = None,
                 windowed: bool = False):
        os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
        # Without a desktop there is no audio device either, and SDL's audio
        # init failing would take the whole display down with it.
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
        import pygame                                   # noqa: PLC0415
        self.pygame = pygame
        pygame.display.init()
        pygame.font.init()
        flags = 0 if windowed else pygame.FULLSCREEN
        self.surface = pygame.display.set_mode(size or (0, 0), flags)
        pygame.display.set_caption("obd-ev")
        pygame.mouse.set_visible(False)
        self.w, self.h = self.surface.get_size()
        self._layout()

    # -- layout ------------------------------------------------------------

    def _font(self, fraction: float, bold: bool = False):
        size = max(10, int(self.h * fraction))
        try:
            return self.pygame.font.SysFont("dejavusansmono", size, bold=bold)
        except Exception:                                # noqa: BLE001
            return self.pygame.font.Font(None, size)

    def _layout(self) -> None:
        self.pad = max(6, int(self.w * 0.015))
        self.f_big = self._font(0.085, bold=True)
        self.f_mid = self._font(0.048)
        self.f_small = self._font(0.038)
        self.head_h = int(self.h * 0.235)
        self.foot_h = int(self.h * 0.075)
        self.row_h = int(self.f_mid.get_height() * 1.28)
        # The view bar sits between the header and the first row, so it comes
        # out of the body's budget -- leave it out and the last row of every
        # page is drawn underneath the footer.
        self.bar_h = int(self.f_small.get_height() * 1.3) + self.pad
        self.body_top = self.head_h + self.pad // 2 + self.bar_h
        body_h = self.h - self.body_top - self.foot_h - self.pad // 2
        self.rows_per_page = max(1, body_h // self.row_h)

    # -- drawing -----------------------------------------------------------

    def _text(self, text: str, font, x: int, y: int, color, right: int = 0):
        img = font.render(text, True, color)
        self.surface.blit(img, (x - img.get_width() if right else x, y))
        return img.get_width()

    @staticmethod
    def _fit(text: str, font, width: int, head: bool = False) -> str:
        """Trim `text` to `width` pixels. `head` drops characters from the
        front instead of the back, which is what a path wants -- the trip
        and part timestamps are at the end and are what you are reading."""
        if font.size(text)[0] <= width:
            return text
        while text and font.size("\u2026" + text)[0] > width:
            text = text[1:] if head else text[:-1]
        return "\u2026" + text if head else text + "\u2026"

    def _header(self, snap: Snapshot) -> None:
        pg = self.pygame
        tint = self.TINT[snap.severity]
        pg.draw.rect(self.surface, self.PANEL,
                     (0, 0, self.w, self.head_h))
        pg.draw.rect(self.surface, tint, (0, 0, self.w, max(3, self.pad // 3)))

        y = self.pad
        self._text(snap.device, self.f_big, self.pad, y, self.TEXT)
        self._text(snap.state, self.f_mid, self.w - self.pad,
                   y + int(self.f_big.get_height() * 0.25), tint, right=1)

        y += self.f_big.get_height()
        used = self._text(f"{snap.rate:.2f} rows/s   row {snap.rows}",
                          self.f_small, self.pad, y, self.DIM)
        room = self.w - self.pad * 4 - used
        self._text(self._fit(f"{snap.trip}/{snap.part}", self.f_small,
                             room, head=True),
                   self.f_small, self.w - self.pad, y, self.DIM, right=1)

        y += int(self.f_small.get_height() * 1.5)
        x = self.pad
        for label, value, severity in snap.facts:
            x += self._text(f"{label} ", self.f_small, x, y, self.DIM)
            x += self._text(value, self.f_small, x, y, self.TINT[severity])
            x += self.pad * 2

    def _body(self, snap: Snapshot) -> None:
        pg = self.pygame
        y = self.head_h + self.pad // 2
        self._text(VIEW_LABELS[snap.view], self.f_small, self.pad, y, self.DIM)
        self._text(f"{snap.page + 1}/{snap.pages}", self.f_small,
                   self.w - self.pad, y, self.DIM, right=1)
        pg.draw.line(self.surface, self.PANEL,
                     (0, self.body_top - self.pad // 2),
                     (self.w, self.body_top - self.pad // 2), 2)
        y = self.body_top

        if snap.note:
            for line in _wrap(snap.note, 46):
                self._text(line, self.f_small, self.pad, y, self.TINT[WARN])
                y += self.f_small.get_height()
            y += self.pad

        age_x = self.w - self.pad
        unit_x = age_x - int(self.w * 0.16)
        value_x = unit_x - int(self.w * 0.07)
        label_w = value_x - self.pad * 2
        for row in snap.body:
            self._text(self._fit(row.label, self.f_mid, label_w),
                       self.f_mid, self.pad, y, self.TEXT)
            self._text(row.value, self.f_mid, value_x, y, self.TEXT, right=1)
            self._text(row.unit, self.f_small, unit_x, y, self.DIM)
            age = "-" if row.age is None else f"{row.age:.1f}s"
            self._text(age, self.f_small, age_x, y,
                       self.TINT[severity_for_age(row.age)], right=1)
            y += self.row_h

    def _footer(self) -> None:
        y = self.h - self.foot_h
        self.pygame.draw.rect(self.surface, self.PANEL,
                              (0, y, self.w, self.foot_h))
        y += (self.foot_h - self.f_small.get_height()) // 2
        self._text("< page", self.f_small, self.pad, y, self.DIM)
        self._text("tap top: view", self.f_small, self.w // 2, y, self.DIM)
        self._text("page >", self.f_small, self.w - self.pad, y, self.DIM,
                   right=1)

    def draw(self, snap: Snapshot) -> None:
        self.surface.fill(self.BG)
        self._header(snap)
        self._body(snap)
        self._footer()
        self.pygame.display.flip()

    # -- input -------------------------------------------------------------

    def _at(self, x: float, y: float) -> Optional[str]:
        if y < self.head_h:
            return "view"
        if x < self.w * 0.25:
            return "prev"
        if x > self.w * 0.75:
            return "next"
        return None

    def events(self) -> List[str]:
        pg = self.pygame
        out = []
        for event in pg.event.get():
            if event.type == pg.QUIT:
                out.append("quit")
            elif event.type == pg.KEYDOWN:
                out.append({pg.K_LEFT: "prev", pg.K_RIGHT: "next",
                            pg.K_UP: "prev", pg.K_DOWN: "next",
                            pg.K_TAB: "view", pg.K_v: "view",
                            pg.K_q: "quit", pg.K_ESCAPE: "quit"}
                           .get(event.key, ""))
            elif event.type == pg.MOUSEBUTTONDOWN:
                out.append(self._at(*event.pos))
            elif event.type == pg.FINGERDOWN:
                # Normalised 0..1, unlike a mouse position.
                out.append(self._at(event.x * self.w, event.y * self.h))
        return [e for e in out if e]

    def close(self) -> None:
        try:
            self.pygame.display.quit()
        except Exception:                                # noqa: BLE001
            pass


def _wrap(text: str, width: int) -> List[str]:
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return lines


def open_screen(backend: str, windowed: bool):
    """The requested renderer, or text if the touchscreen is not reachable.

    `auto` tries pygame first: on a kit with no screen attached, with pygame
    not installed, or when something else holds DRM master, that raises -- and
    falling back to text is better than exiting, since the same script is how
    you check a kit over SSH.
    """
    if backend in ("auto", "pygame"):
        try:
            return PygameScreen(windowed=windowed)
        except Exception as exc:                         # noqa: BLE001
            if backend == "pygame":
                raise
            print(f"dashboard: no touchscreen ({exc}); using text",
                  file=sys.stderr)
    return TextScreen()


# -- main -------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(REPO_ROOT / "config.yaml"))
    parser.add_argument("--backend", choices=("auto", "pygame", "text"),
                        default="auto")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="seconds between redraws (default 0.5)")
    parser.add_argument("--view", choices=VIEWS, default="vehicle")
    parser.add_argument("--windowed", action="store_true",
                        help="pygame in a window, for testing on a desktop")
    parser.add_argument("--once", action="store_true",
                        help="draw one snapshot and exit")
    args = parser.parse_args()

    cfg = cfgmod.load(args.config)
    log_dir = Path(cfg.logger.output_dir)
    mon = Monitor(log_dir, cfg.device.id or "obd-ev")

    # `--once` exists for a quick look over SSH, so on its own it stays with
    # the text renderer rather than flashing a frame at an empty seat. Asking
    # for a backend by name overrides that: `--backend pygame --once` is how
    # you prove the screen works without leaving a process running, and it
    # has to actually reach the screen or raise saying why.
    if args.once and args.backend in ("auto", "text"):
        screen = TextScreen(plain=True)
    else:
        screen = open_screen(args.backend, args.windowed)
    view_at = VIEWS.index(args.view)
    page = 0

    try:
        while True:
            mon.poll()
            snap = snapshot(mon, VIEWS[view_at], page,
                            getattr(screen, "rows_per_page", 14))
            page = snap.page                     # clamped to what exists
            screen.draw(snap)
            if args.once:
                return 0

            # Act on input promptly without redrawing the world for it: the
            # redraw interval stays the budget, the touch response does not.
            deadline = time.monotonic() + args.interval
            while True:
                for event in screen.events():
                    if event == "quit":
                        return 0
                    if event == "next":
                        page = min(page + 1, snap.pages - 1)
                    elif event == "prev":
                        page = max(page - 1, 0)
                    elif event == "view":
                        view_at = (view_at + 1) % len(VIEWS)
                        page = 0
                    deadline = 0.0               # redraw now
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(0.05, left))
    except KeyboardInterrupt:
        return 0
    finally:
        screen.close()
        mon.tail.close()


if __name__ == "__main__":
    sys.exit(main())
