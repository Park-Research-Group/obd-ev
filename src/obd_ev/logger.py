import csv
import datetime as dt
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from .config import LoggerConfig
from .naming import DICTIONARY_FIELDS

log = logging.getLogger(__name__)


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _boot_id() -> str:
    """Short identifier that changes on every boot and never repeats.

    The Pi has no RTC, so at boot the clock is whatever it was at shutdown
    until GPS or NTP corrects it. Two power cycles can therefore produce the
    same wall-clock timestamp. Mixing the kernel's boot id into trip names
    makes them unique regardless of what the clock believes.
    """
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()[:8]
    except OSError:
        return uuid.uuid4().hex[:8]


BOOT_ID = _boot_id()


def write_signal_dictionary(output_dir, rows: List[Dict], device_id: str = "") -> Path:
    """Write signals.csv: one row per CSV column, saying what it is.

    Written into each trip folder rather than once per device. The schema can
    change between trips -- a vehicle profile is updated, a kit is reassigned --
    and a dictionary that describes the data next to it stays correct where a
    single shared one would silently start lying about older trips.
    """
    tag = f"_{device_id}" if device_id else ""
    path = Path(output_dir) / f"signals{tag}.csv"
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=DICTIONARY_FIELDS,
                                extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({f: row.get(f, "") for f in DICTIONARY_FIELDS})
    return path


class CsvLogger:
    """One folder per trip, one or more CSV parts inside it.

    Layout under `output_dir`:

        20260906_142201_a3f9c1d2/       <- a trip
            drive_P003_20260906_142201.csv
            drive_P003_20260906_143701.csv   <- part, after a 15-minute rotation
            signals_P003.csv
        .current                        <- path of the file being written now

    A trip ends when the vehicle goes away and comes back, or when the kit
    loses power -- which on most cars is the moment the ignition goes off, so
    in practice one trip is one power cycle.

    Parts exist because power is cut without warning: rotating mid-trip bounds
    how much a single unclean shutdown can cost, and closes files so the
    uploader can ship them.
    """

    def __init__(self, cfg: LoggerConfig, fieldnames: List[str],
                 device_id: str = "", dictionary: Optional[List[Dict]] = None):
        self.cfg = cfg
        self.device_id = device_id
        self.dictionary = dictionary or []
        self.root = Path(cfg.output_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.fieldnames = ["timestamp", "t_mono"] + fieldnames
        self._fh = None
        self._writer = None
        self.path: Optional[Path] = None
        self.trip_dir: Optional[Path] = None
        self.trip_id: Optional[str] = None
        self._n = 0
        self._opened_at = 0.0
        self._last_flush = 0.0
        self.new_trip()

    # -- trip and part management -------------------------------------------

    @staticmethod
    def _stamp() -> str:
        # UTC, like the `timestamp` column, so folder names and contents
        # agree whatever timezone the kit happens to be set to.
        return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")

    def _new_trip_id(self) -> str:
        stamp = self._stamp()
        base = f"{stamp}_{BOOT_ID}"
        candidate, seq = base, 2
        while (self.root / candidate).exists():
            candidate = f"{base}_{seq}"
            seq += 1
        return candidate

    def new_trip(self) -> None:
        """Close whatever is open and begin a new trip folder."""
        self.close()
        self.trip_id = self._new_trip_id()
        self.trip_dir = self.root / self.trip_id
        self.trip_dir.mkdir(parents=True, exist_ok=True)
        if self.dictionary:
            write_signal_dictionary(self.trip_dir, self.dictionary,
                                    self.device_id)
        self._open_part()
        log.info("trip %s started", self.trip_id)

    def _next_part_path(self) -> Path:
        tag = f"_{self.device_id}" if self.device_id else ""
        stamp = self._stamp()
        path = self.trip_dir / f"drive{tag}_{stamp}.csv"
        seq = 2
        while path.exists():
            path = self.trip_dir / f"drive{tag}_{stamp}_{seq}.csv"
            seq += 1
        return path

    def _open_part(self) -> None:
        self.path = self._next_part_path()
        # Marker first, then the file. upload.sh checks the marker before
        # every file it touches; if the file existed before the marker named
        # it, an upload run could ship and move it out from under us.
        self._mark_current(self.path)
        self._fh = self.path.open("w", newline="")
        self._writer = csv.DictWriter(
            self._fh, fieldnames=self.fieldnames, extrasaction="ignore"
        )
        self._writer.writeheader()
        self._fh.flush()
        self._n = 0
        self._opened_at = time.monotonic()
        self._last_flush = self._opened_at
        log.info("logging to %s", self.path)

    def rotate(self) -> None:
        """Start a new part within the same trip. The closed part becomes
        eligible for upload; the uploader never touches the open one."""
        empty = self._n == 0
        closing = self.path
        self.close()
        if empty and closing is not None:
            closing.unlink(missing_ok=True)   # don't ship a header-only file
        self._open_part()

    # -- pointer file the uploader reads ------------------------------------

    def _mark_current(self, path: Optional[Path]) -> None:
        marker = self.root / ".current"
        try:
            if path is None:
                marker.unlink(missing_ok=True)
            else:
                marker.write_text(str(path) + "\n")
        except OSError as exc:
            log.debug("could not update %s: %s", marker, exc)

    # -- writing ------------------------------------------------------------

    def write(self, row: Dict) -> None:
        row.setdefault("timestamp", utc_now_iso())
        row.setdefault("t_mono", round(time.monotonic(), 3))
        self._writer.writerow(row)
        self._n += 1

        # Flush on whichever comes first: a row count, or a wall-clock
        # interval. The count alone is not enough -- at the sub-1Hz row rates
        # a slow BLE link produces, ten rows can be a minute of driving, and
        # the ignition cutting power takes all of it.
        now = time.monotonic()
        if (self._n % max(1, self.cfg.flush_every) == 0
                or now - self._last_flush >= self.cfg.flush_seconds):
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._last_flush = now

    @property
    def rows(self) -> int:
        return self._n

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self._opened_at

    def close(self) -> None:
        if self._fh is None:
            return
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self._fh = None
        self._writer = None
        self._mark_current(None)
