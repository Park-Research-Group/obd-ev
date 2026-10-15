#!/usr/bin/env python3
"""Turn a folder of raw obd-ev uploads into analysis-ready trip CSVs.

Self-contained and dependency-free: Python 3.9+ and nothing else. Copy this
one file anywhere -- it does not import from this repository, and it never
writes to or deletes anything under the input folder.

    python3 clean_trips.py ~/Downloads/P001
    python3 clean_trips.py ~/Downloads/obd-ev-uploads --merge

Output goes inside the input folder, named after it with `_cleaned` appended,
so the cleaned data uploads alongside the raw data it came from. `-o` puts it
anywhere else.

Input is whatever the kits uploaded: a device folder of trip folders, a tree
of several device folders, or a single trip folder.

    P001/20260928_195204_0b1dadf0/drive_P001_20260928_195204.csv
                                 /drive_P001_20260928_200704.csv
                                 /signals_P001.csv

Output is one CSV per trip, named by the *corrected* start time so the files
sort in the order the driving happened, plus a merged data dictionary, a
per-trip summary and a report of everything that was changed.

    P001/P001_cleaned/trips/P001_20260929T211008Z_0b1dadf0.csv
                     /signals.csv
                     /trips_summary.csv
                     /obd_timing.csv       measured polling interval
                     /cleaning_report.txt

What it fixes, in order:

1. Parts are concatenated per trip. A trip is split into 15-minute parts so an
   unclean shutdown costs less, which is a logging concern, not an analysis
   one.
2. Rows are ordered by `t_mono` and duplicates are dropped. `t_mono` is
   monotonic seconds since boot and is the only time on the kit that cannot
   jump; a trip never spans a reboot, so it orders the trip correctly even
   when the wall clock does not.
3. The clock is repaired. The Pi has no RTC, so until GPS or NTP corrects it
   the wall clock is whatever it was at shutdown, and the correction lands
   *inside* the recording as a step. In the P001 data whole trips sit up to 25
   hours off, one clock steps 434 hours a minute in, and the trip folder names
   inherit the error. Every row's `timestamp` is rebuilt from the best clock
   the trip has: `gps_time`, which is the receiver's own UTC and owes nothing
   to the Pi; failing that, the Pi clock as it stood after its last step,
   carried back over the rows recorded before it. The reading as logged stays
   in `timestamp_pi`, and `clock_source` says which of the three you have.
   Durations are taken from `t_mono`, which no correction can distort.
4. Values outside the min/max that `signals.csv` declares for their signal are
   blanked and counted. A decode that lands outside the signal's own range is
   a decode that went wrong.
5. Columns that are empty for the whole trip are dropped, so a 132-column file
   with 30 live signals reads as 30 columns. `--keep-empty-columns` keeps them.
6. Trips in which the car never answered are skipped and listed in the report
   with the reason, as are trips with no usable rows. Most trips in the field
   are bench and GPS-only runs -- 16 of the 17 in the P001 data -- and the
   point of the dataset is the vehicle. What counts is a value from a vehicle
   source in `signals.csv`, not `obd_connected`, which only says the ELM327
   answered: a kit can hold that link for a whole trip and decode nothing.
   `--keep-without-obd` keeps them.

Optionally (`--fill`): each signal is carried forward over the rows that did
not sample it. Rows are sparse by nature -- the scheduler polls commands round
robin, so one row holds whatever answered in that cycle, which on the P001
drive is 22 of 128 columns. Filling gives every row a complete state vector
(about 120 of those 128), at the cost of repeating a value the car did not
report at that instant. It is off by default because that is a choice about
your analysis, not a cleanup.

A value is carried no further than `--max-stale` times the signal's sampling
period, capped by `--max-hold` seconds, so a signal that stops answering goes
empty rather than flatlining. The period is the slower of the one the
signalset declares and the one the signal achieved in that trip: the declared
figure is the scheduler's target, and over BLE, with a hundred commands to get
through, it is not met by an order of magnitude.
"""

import argparse
import csv
import datetime as dt
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# Columns the logger writes for every row, whatever the vehicle.
META_COLUMNS = ["timestamp", "timestamp_pi", "clock_source", "t_mono",
                "device_id", "trip_id", "obd_connected"]

# Everything the kit itself contributes. A column from any other source came
# off the vehicle's bus.
NON_VEHICLE_SOURCES = {"logger", "gps", "imu"}

# The same split by column name, for trips whose signals.csv did not arrive.
NON_VEHICLE_COLUMNS = {
    "timestamp", "timestamp_pi", "clock_source", "t_mono", "device_id",
    "trip_id", "obd_connected",
    "lat", "lon", "alt", "speed_gps", "heading", "fix_mode", "gps_sats",
    "gps_sats_used", "gps_sats_visible", "gps_snr_max", "gps_time",
    "gps_age_s",
    "accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z",
    "accel_mag_max", "accel_mag_min", "imu_samples", "imu_age_s",
}

# Never carried forward by --fill: identifiers, and the freshness counters
# whose whole meaning is that they describe the row they were written on.
NEVER_FILL = {"timestamp", "timestamp_pi", "clock_source", "t_mono",
              "device_id", "trip_id", "gps_time", "gps_age_s", "imu_age_s",
              "imu_samples", "accel_mag_max", "accel_mag_min"}

# Fallback staleness bound, seconds, for a signal whose dictionary row carries
# no period. Generous: the point is to stop a flatline, not to trim.
DEFAULT_PERIOD_S = 10.0

STAMP_RE = re.compile(r"(\d{8}_\d{6})")


# -- small helpers ---------------------------------------------------------

def parse_time(text: Optional[str]) -> Optional[dt.datetime]:
    """ISO 8601 to an aware datetime. `gps_time` ends in Z, `timestamp` in +00:00."""
    if not text:
        return None
    s = text.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        stamp = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp


def to_float(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    s = text.strip()
    if not s:
        return None
    try:
        value = float(s)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def iso(stamp: dt.datetime) -> str:
    return stamp.astimezone(dt.timezone.utc).isoformat()


def compact(stamp: dt.datetime) -> str:
    """A filename-safe UTC stamp: 20260929T211008Z."""
    return stamp.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# -- the data dictionary ---------------------------------------------------

class Dictionary:
    """`signals.csv`: one row per column, saying what that column is.

    Each trip carries its own, because the schema legitimately changes between
    trips -- a vehicle profile is added, a kit is reassigned. Merging them
    across a dataset is therefore a union, and a column may appear with
    different descriptions in different trips; the first one wins and the
    clash is reported.
    """

    def __init__(self):
        self.rows: Dict[str, Dict[str, str]] = {}
        self.conflicts: List[str] = []

    def load(self, path: Path) -> Dict[str, Dict[str, str]]:
        local: Dict[str, Dict[str, str]] = {}
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                column = (row.get("column") or "").strip()
                if not column:
                    continue
                local[column] = row
                known = self.rows.get(column)
                if known is None:
                    self.rows[column] = row
                elif (known.get("source_id") != row.get("source_id")
                      and column not in self.conflicts):
                    self.conflicts.append(column)
        return local

    def limits(self, local: Dict[str, Dict[str, str]], column: str
               ) -> Tuple[Optional[float], Optional[float]]:
        row = local.get(column) or self.rows.get(column) or {}
        return to_float(row.get("min")), to_float(row.get("max"))

    def period(self, local: Dict[str, Dict[str, str]], column: str) -> float:
        row = local.get(column) or self.rows.get(column) or {}
        value = to_float(row.get("period_s"))
        return value if value and value > 0 else DEFAULT_PERIOD_S

    def write(self, path: Path, columns: Sequence[str]) -> None:
        fields = ["column", "name", "unit", "group", "category", "source",
                  "source_id", "command", "period_s", "min", "max"]
        with path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for column in columns:
                row = self.rows.get(column)
                if row is None:
                    # A column present in the data but not in any dictionary.
                    row = {"column": column, "name": column,
                           "group": "Unknown", "category": "Unknown",
                           "source": "unknown", "source_id": column}
                writer.writerow({f: row.get(f, "") for f in fields})


# -- reading a trip --------------------------------------------------------

def part_sort_key(path: Path) -> Tuple[str, str]:
    """Order parts by the stamp in the filename, then the name itself.

    The stamp comes from the same unreliable clock as everything else, so it
    only gets the parts roughly in order; `t_mono` fixes the rest once the
    rows are read.
    """
    match = STAMP_RE.search(path.name)
    return (match.group(1) if match else "", path.name)


def find_trip_dirs(root: Path, exclude: Optional[Path] = None) -> List[Path]:
    """Any directory holding at least one `drive*.csv`, including root itself.

    `exclude` is the output folder, which normally sits inside `root` so the
    cleaned data uploads with the raw data. Skipping it keeps a second run
    over the same folder from treating the first run's output as input.
    """
    found = set()
    if any(root.glob("drive*.csv")):
        found.add(root)
    for path in root.rglob("drive*.csv"):
        found.add(path.parent)
    if exclude is not None:
        found = {d for d in found
                 if d != exclude and exclude not in d.parents}
    return sorted(found)


def read_parts(trip_dir: Path) -> Tuple[List[Dict[str, str]], List[str], List[Path]]:
    """Concatenate the trip's parts, returning rows and the union of columns."""
    parts = sorted(trip_dir.glob("drive*.csv"), key=part_sort_key)
    rows: List[Dict[str, str]] = []
    columns: List[str] = []
    seen = set()
    for part in parts:
        with part.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for name in reader.fieldnames or []:
                if name and name not in seen:
                    seen.add(name)
                    columns.append(name)
            for row in reader:
                # A power cut can truncate the last line mid-row; DictReader
                # pads the rest with None, which reads the same as empty here.
                rows.append({k: ("" if v is None else v)
                             for k, v in row.items() if k})
    return rows, columns, parts


# -- the cleaning steps ----------------------------------------------------

class TripReport:
    def __init__(self, trip_dir: Path):
        self.trip_dir = trip_dir
        self.notes: List[str] = []
        self.stats: Dict[str, object] = {}

    def note(self, text: str) -> None:
        self.notes.append(text)


def order_rows(rows: List[Dict[str, str]], report: TripReport
               ) -> List[Tuple[float, Dict[str, str]]]:
    """Sort by `t_mono` and drop rows that repeat one or lack it entirely."""
    ordered: List[Tuple[float, Dict[str, str]]] = []
    no_mono = 0
    for row in rows:
        mono = to_float(row.get("t_mono"))
        if mono is None:
            no_mono += 1
            continue
        ordered.append((mono, row))
    ordered.sort(key=lambda item: item[0])

    deduped: List[Tuple[float, Dict[str, str]]] = []
    duplicates = 0
    last: Optional[float] = None
    for mono, row in ordered:
        if last is not None and abs(mono - last) < 1e-9:
            duplicates += 1
            continue
        deduped.append((mono, row))
        last = mono

    if no_mono:
        report.note(f"dropped {no_mono} row(s) with no usable t_mono")
    if duplicates:
        report.note(f"dropped {duplicates} duplicate row(s) by t_mono")
    report.stats["rows_dropped_no_mono"] = no_mono
    report.stats["rows_dropped_duplicate"] = duplicates
    return deduped


def largest_gap(ordered: Sequence[Tuple[float, Dict[str, str]]],
                report: TripReport, warn_after: float) -> float:
    """Longest silence between two consecutive rows, in seconds.

    A trip ends when the vehicle goes away and comes back, so a trip folder
    can legitimately hold hours of parked rows either side of a drive -- in
    this dataset, several span days. The duration of such a trip is not the
    duration of a drive, so say so rather than letting the summary imply it.
    """
    gap = 0.0
    for (a, _), (b, _) in zip(ordered, ordered[1:]):
        gap = max(gap, b - a)
    if gap >= warn_after:
        report.note(f"longest gap between rows is {gap / 3600:.1f} h; this "
                    f"folder covers parked time, not one continuous drive")
    report.stats["max_gap_s"] = round(gap, 1)
    return gap


def clock_offset(ordered: Sequence[Tuple[float, Dict[str, str]]]
                 ) -> Tuple[Optional[float], int, float]:
    """Median `gps_time - t_mono` over the trip, plus how it scattered.

    The kit's boot id is part of the trip name, so a trip never spans a
    reboot and this offset is a constant for the trip. The scatter is
    therefore a check, not a fit: GPS reports whole seconds, so anything much
    above a second means the assumption does not hold and the repair is left
    alone.
    """
    offsets = []
    for mono, row in ordered:
        stamp = parse_time(row.get("gps_time"))
        if stamp is not None:
            offsets.append(stamp.timestamp() - mono)
    if not offsets:
        return None, 0, 0.0
    return statistics.median(offsets), len(offsets), max(offsets) - min(offsets)


def pi_clock_jumps(ordered: Sequence[Tuple[float, Dict[str, str]]],
                   threshold: float) -> List[Tuple[float, float]]:
    """Where the Pi's own clock stepped, as `(t_mono, step_seconds)`.

    The kit boots with the clock it shut down with and corrects it once GPS or
    NTP arrives, which is typically a few tens of seconds into a trip. That
    correction lands inside the recording as a discontinuity -- in this dataset
    up to +434 hours -- so `timestamp` is not monotonic and any duration taken
    from it is fiction. `t_mono` does not move, so the step shows up cleanly as
    a change in `pi_clock - t_mono`.
    """
    jumps = []
    previous: Optional[float] = None
    for mono, row in ordered:
        stamp = parse_time(row.get("timestamp"))
        if stamp is None:
            continue
        offset = stamp.timestamp() - mono
        if previous is not None and abs(offset - previous) > threshold:
            jumps.append((mono, offset - previous))
        previous = offset
    return jumps


def trailing_pi_offset(ordered: Sequence[Tuple[float, Dict[str, str]]],
                       threshold: float) -> Optional[float]:
    """Median `pi_clock - t_mono` over the rows after the last clock step.

    After a step, the clock has just been set from an authority the kit
    trusts; before it, it is whatever it shut down with. So the last segment
    is the one worth keeping, and extending its offset back over the trip
    puts the earlier rows on the corrected clock too.
    """
    jumps = pi_clock_jumps(ordered, threshold)
    after = jumps[-1][0] if jumps else None
    offsets = []
    for mono, row in ordered:
        if after is not None and mono < after:
            continue
        stamp = parse_time(row.get("timestamp"))
        if stamp is not None:
            offsets.append(stamp.timestamp() - mono)
    return statistics.median(offsets) if offsets else None


def repair_clock(ordered: Sequence[Tuple[float, Dict[str, str]]],
                 report: TripReport, max_scatter: float, jump_threshold: float
                 ) -> Tuple[str, Optional[float]]:
    """Put every row on the best clock the trip can offer, and say which.

    In order of preference: the GPS receiver's own UTC, which is independent
    of the Pi entirely; then the Pi clock as it stood after its last
    correction; then the Pi clock as recorded, which is the case with nothing
    to go on.
    """
    offset, n, scatter = clock_offset(ordered)
    correction: Optional[float] = None

    if offset is not None and scatter > max_scatter:
        report.note(f"gps_time scattered by {scatter:.1f}s against t_mono "
                    f"(limit {max_scatter:.1f}s); did not trust it")
        offset = None

    for _, row in ordered:
        row["timestamp_pi"] = row.get("timestamp", "")

    jumps = pi_clock_jumps(ordered, jump_threshold)
    if jumps:
        worst = max(jumps, key=lambda j: abs(j[1]))
        report.note(f"the Pi clock stepped {len(jumps)} time(s) mid-trip, "
                    f"the largest by {worst[1] / 3600:+.2f} h at "
                    f"t_mono={worst[0]:.0f}s")
    report.stats["clock_jumps"] = len(jumps)

    if offset is not None:
        source = "gps"
    else:
        offset = trailing_pi_offset(ordered, jump_threshold) if jumps else None
        source = "pi_corrected" if offset is not None else "pi"

    if offset is None:
        for _, row in ordered:
            row["clock_source"] = "pi"
        if n == 0:
            report.note("no gps_time in this trip; timestamps are the Pi "
                        "clock as recorded and may be badly wrong")
    else:
        deltas = []
        for mono, row in ordered:
            repaired = dt.datetime.fromtimestamp(offset + mono, dt.timezone.utc)
            original = parse_time(row.get("timestamp_pi"))
            if original is not None:
                deltas.append((repaired - original).total_seconds())
            row["timestamp"] = iso(repaired)
            row["clock_source"] = source
        correction = statistics.median(deltas) if deltas else 0.0
        moved = [d for d in deltas if abs(d) >= 1.0]
        if moved or jumps:
            basis = (f"gps_time ({n} fix(es))" if source == "gps"
                     else "the Pi clock after its last correction")
            # The median is the typical row, which after a mid-trip step is a
            # row that barely moved; the largest move is what the step cost.
            worst_move = max(moved, key=abs) if moved else 0.0
            report.note(f"rebuilt {len(ordered)} timestamp(s) from {basis}; "
                        f"{len(moved)} row(s) moved, the largest by "
                        f"{worst_move / 3600:+.2f} h")

    report.stats["gps_time_rows"] = n
    report.stats["clock_scatter_s"] = round(scatter, 3)
    return source, correction


def range_check(ordered: Sequence[Tuple[float, Dict[str, str]]],
                columns: Sequence[str], dictionary: Dictionary,
                local: Dict[str, Dict[str, str]], report: TripReport) -> int:
    """Blank values a signal's own declared min/max say cannot be real."""
    limits = {}
    for column in columns:
        low, high = dictionary.limits(local, column)
        if low is not None or high is not None:
            limits[column] = (low, high)

    blanked = 0
    per_column: Dict[str, int] = {}
    for _, row in ordered:
        for column, (low, high) in limits.items():
            value = to_float(row.get(column))
            if value is None:
                continue
            if (low is not None and value < low) or (high is not None and value > high):
                row[column] = ""
                blanked += 1
                per_column[column] = per_column.get(column, 0) + 1

    if blanked:
        worst = sorted(per_column.items(), key=lambda kv: -kv[1])[:5]
        detail = ", ".join(f"{c} x{n}" for c, n in worst)
        report.note(f"blanked {blanked} out-of-range value(s): {detail}")
    report.stats["values_out_of_range"] = blanked
    return blanked


def live_columns(ordered: Sequence[Tuple[float, Dict[str, str]]],
                 columns: Sequence[str]) -> List[str]:
    """Columns that hold at least one value somewhere in the trip."""
    live = []
    for column in columns:
        for _, row in ordered:
            if (row.get(column) or "").strip():
                live.append(column)
                break
    return live


def observed_period(ordered: Sequence[Tuple[float, Dict[str, str]]],
                    column: str) -> Optional[float]:
    """Median gap between the rows where this signal actually has a value."""
    stamps = [mono for mono, row in ordered if (row.get(column) or "").strip()]
    if len(stamps) < 3:
        return None
    gaps = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
    return statistics.median(gaps) if gaps else None


def forward_fill(ordered: Sequence[Tuple[float, Dict[str, str]]],
                 columns: Sequence[str], dictionary: Dictionary,
                 local: Dict[str, Dict[str, str]], max_stale: float,
                 max_hold: float, report: TripReport) -> int:
    """Carry each signal forward, but no further than it stays plausible.

    The window is `max_stale` times the slower of two periods: the one the
    signalset declares, and the one the signal actually achieved in this trip.
    The declared period is the scheduler's target, and over BLE the scheduler
    does not get near it -- a signal declared at 0.25 s may in practice land
    every 20 s, and a window built on the declared figure would refuse to fill
    a single cell. The observed period is what the data can support; the
    declared one keeps a signal sampled only two or three times from setting
    an absurdly generous window. `max_hold` caps both.
    """
    last: Dict[str, Tuple[float, str]] = {}
    filled = 0
    windows = {}
    for column in columns:
        declared = dictionary.period(local, column)
        seen = observed_period(ordered, column)
        base = max(declared, seen) if seen is not None else declared
        windows[column] = min(base * max_stale, max_hold)

    for mono, row in ordered:
        for column in columns:
            if column in NEVER_FILL:
                continue
            value = (row.get(column) or "").strip()
            if value:
                last[column] = (mono, value)
                continue
            held = last.get(column)
            if held is None:
                continue
            age = mono - held[0]
            if age <= windows[column]:
                row[column] = held[1]
                filled += 1

    report.note(f"forward-filled {filled} cell(s), holding each signal at "
                f"most {max_stale:g}x its sampling period and never more "
                f"than {max_hold:g}s")
    report.stats["cells_filled"] = filled
    return filled


# -- per-trip metrics ------------------------------------------------------

def trip_metrics(ordered: Sequence[Tuple[float, Dict[str, str]]]
                 ) -> Dict[str, object]:
    """Enough to triage a trip without opening it: is there OBD, is there a
    fix, how far did it go."""
    obd_rows = sum(1 for _, r in ordered if (r.get("obd_connected") or "").strip() == "1")
    fixes: List[Tuple[float, float]] = []
    speeds: List[float] = []
    for _, row in ordered:
        lat, lon = to_float(row.get("lat")), to_float(row.get("lon"))
        if lat is not None and lon is not None and (lat, lon) != (0.0, 0.0):
            fixes.append((lat, lon))
        speed = to_float(row.get("vehicle_speed_kph"))
        if speed is None:
            gps_speed = to_float(row.get("speed_gps"))   # m/s
            speed = gps_speed * 3.6 if gps_speed is not None else None
        if speed is not None:
            speeds.append(speed)

    distance = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(fixes, fixes[1:]):
        step = haversine_km(lat1, lon1, lat2, lon2)
        # A fix that jumps a kilometre between two rows seconds apart is the
        # receiver reacquiring, not the car moving.
        if step < 1.0:
            distance += step

    return {
        "obd_rows": obd_rows,
        "gps_fix_rows": len(fixes),
        "distance_km": round(distance, 3),
        "max_speed_kph": round(max(speeds), 1) if speeds else "",
    }


def vehicle_columns(columns: Sequence[str], dictionary: Dictionary,
                    local: Dict[str, Dict[str, str]]) -> List[str]:
    """The columns that came from the car, as opposed to the kit.

    Decided by exclusion -- anything whose dictionary `source` is not the
    logger, the GPS or the IMU came off the bus. The vehicle sources are named
    per profile (`obd-mode01` for the generic PID fallback, `vehicle-obdb` for
    a signalset), so excluding the three fixed ones survives a source name this
    script has never seen.
    """
    out = []
    for column in columns:
        row = local.get(column) or dictionary.rows.get(column)
        if row is not None:
            if (row.get("source") or "").strip() not in NON_VEHICLE_SOURCES:
                out.append(column)
        elif column not in NON_VEHICLE_COLUMNS:
            # No dictionary to go on: fall back to the fixed column names the
            # logger, GPS and IMU contribute.
            out.append(column)
    return out


def device_of(ordered: Sequence[Tuple[float, Dict[str, str]]]) -> str:
    for _, row in ordered:
        device = (row.get("device_id") or "").strip()
        if device:
            return device
    return ""


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile. Enough for timing, and no numpy."""
    if not values:
        return 0.0
    ranked = sorted(values)
    index = min(len(ranked) - 1, max(0, int(round(q * (len(ranked) - 1)))))
    return ranked[index]


def row_gap_stats(ordered: Sequence[Tuple[float, Dict[str, str]]],
                  columns: Sequence[str]) -> Dict[str, object]:
    """How long between one reading off the bus and the next, any signal.

    This is the rate at which the kit hears *something* from the car, which is
    not the rate at which it hears any particular thing -- see
    `signal_timing`. Measured on `t_mono`, so a clock correction cannot
    distort it.
    """
    stamps = [mono for mono, row in ordered
              if any((row.get(c) or "").strip() for c in columns)]
    gaps = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
    if not gaps:
        return {"obd_row_gap_median_s": "", "obd_row_gap_p95_s": "",
                "obd_row_gap_max_s": ""}
    return {
        "obd_row_gap_median_s": round(statistics.median(gaps), 2),
        "obd_row_gap_p95_s": round(percentile(gaps, 0.95), 2),
        "obd_row_gap_max_s": round(max(gaps), 2),
    }


def signal_timing(ordered: Sequence[Tuple[float, Dict[str, str]]],
                  columns: Sequence[str], dictionary: Dictionary,
                  local: Dict[str, Dict[str, str]], trip_id: str,
                  device: str) -> List[Dict[str, object]]:
    """Per-signal refresh interval, against the period the profile asked for.

    The gap that matters to an analysis is this one, not the row gap: with a
    hundred commands sharing one BLE link round robin, the kit can hear from
    the car constantly and still refresh any single signal rarely. Comparing
    it against `period_s` shows how far the scheduler is from its target --
    the figure to quote when the polling interval is the question.

    Always measured before `--fill`, which would otherwise report the interval
    of the filling rather than of the car.
    """
    out = []
    for column in columns:
        stamps = [mono for mono, row in ordered
                  if (row.get(column) or "").strip()]
        if len(stamps) < 2:
            continue
        gaps = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
        if not gaps:
            continue
        median = statistics.median(gaps)
        row = local.get(column) or dictionary.rows.get(column) or {}
        declared = to_float(row.get("period_s"))
        out.append({
            "device_id": device,
            "trip_id": trip_id,
            "column": column,
            "group": (row.get("group") or "").strip(),
            "samples": len(stamps),
            "declared_period_s": declared if declared is not None else "",
            "median_s": round(median, 2),
            "p95_s": round(percentile(gaps, 0.95), 2),
            "max_s": round(max(gaps), 2),
            # How far off the profile's target the link actually runs.
            "times_slower_than_declared":
                round(median / declared, 1)
                if declared and declared > 0 else "",
        })
    return out


TIMING_FIELDS = ["device_id", "trip_id", "column", "group", "samples",
                 "declared_period_s", "median_s", "p95_s", "max_s",
                 "times_slower_than_declared"]


def obd_signal_rows(ordered: Sequence[Tuple[float, Dict[str, str]]],
                    columns: Sequence[str]) -> int:
    """Rows carrying at least one value that actually came from the car.

    Not `obd_connected`, which says the link was up: a kit can hold an ELM327
    connection for a whole trip and decode nothing from it, and on this
    dataset most trips are exactly that.
    """
    return sum(1 for _, row in ordered
               if any((row.get(c) or "").strip() for c in columns))


# -- driving it ------------------------------------------------------------

def order_output_columns(columns: Sequence[str], dictionary: Dictionary
                         ) -> List[str]:
    """Meta first, then by group, so the human-readable columns lead."""
    rest = [c for c in columns if c not in META_COLUMNS]
    order = {"Meta": 0, "Movement": 1, "GPS": 2, "IMU": 3}

    def key(column: str) -> Tuple[int, int]:
        row = dictionary.rows.get(column) or {}
        group = (row.get("group") or "").strip()
        return (order.get(group, 4), rest.index(column))

    return [c for c in META_COLUMNS if c in columns] + sorted(rest, key=key)


def clean_trip(trip_dir: Path, dictionary: Dictionary, args
               ) -> Optional[Dict[str, object]]:
    report = TripReport(trip_dir)
    rows, columns, parts = read_parts(trip_dir)

    local: Dict[str, Dict[str, str]] = {}
    for signals in sorted(trip_dir.glob("signals*.csv")):
        local.update(dictionary.load(signals))
    if not local:
        report.note("no signals*.csv in this folder; "
                    "no range checks and no fill windows")

    ordered = order_rows(rows, report)
    if len(ordered) < args.min_rows:
        return {"skipped": f"{len(ordered)} row(s), below --min-rows "
                           f"{args.min_rows}", "report": report,
                "trip_dir": trip_dir, "parts": len(parts),
                "rows_in": len(rows)}

    gap = largest_gap(ordered, report, args.gap_warn * 60)
    source, correction = repair_clock(ordered, report, args.max_scatter,
                                      args.clock_jump)
    range_check(ordered, columns, dictionary, local, report)

    # After the range check, so a trip whose only vehicle values were junk the
    # decoder got wrong counts as having none.
    from_vehicle = vehicle_columns(columns, dictionary, local)
    with_obd = obd_signal_rows(ordered, from_vehicle)
    if with_obd == 0 and not args.keep_without_obd:
        return {"skipped": "no OBD readings", "report": report,
                "trip_dir": trip_dir, "parts": len(parts),
                "rows_in": len(rows)}

    keep = columns if args.keep_empty_columns else live_columns(ordered, columns)
    dropped = len(columns) - len(keep)
    if dropped:
        report.note(f"dropped {dropped} column(s) empty for the whole trip")
    report.stats["columns_dropped"] = dropped

    # Measured before any fill: a filled lat/lon would count as a fix the
    # receiver never had, and a filled signal would report the cadence of the
    # filling rather than of the car.
    metrics = trip_metrics(ordered)
    gaps = row_gap_stats(ordered, from_vehicle)
    timing = signal_timing(ordered, from_vehicle, dictionary, local,
                           trip_dir.name, device_of(ordered))

    if args.fill:
        forward_fill(ordered, keep, dictionary, local, args.max_stale,
                     args.max_hold, report)

    device = device_of(ordered)
    trip_id = trip_dir.name
    for _, row in ordered:
        row["trip_id"] = trip_id

    start = parse_time(ordered[0][1].get("timestamp"))
    end = parse_time(ordered[-1][1].get("timestamp"))
    out_columns = order_output_columns(
        [c for c in keep if c not in ("timestamp_pi", "clock_source", "trip_id")]
        + ["timestamp_pi", "clock_source", "trip_id"], dictionary)

    # `<stamp>_<boot id>`: keep the boot id, since the corrected start time
    # already carries the date and the two together stay traceable to the
    # source folder. A folder named anything else keeps its whole name.
    suffix = (trip_id.split("_")[-1] if STAMP_RE.match(trip_id) else trip_id)
    name_parts = [p for p in (device, compact(start) if start else "", suffix) if p]
    out_path = args.out / "trips" / ("_".join(name_parts) + ".csv")
    if not args.dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=out_columns,
                                    extrasaction="ignore")
            writer.writeheader()
            for _, row in ordered:
                writer.writerow({c: row.get(c, "") for c in out_columns})

    return {
        "report": report,
        "trip_dir": trip_dir,
        "timing": timing,
        "out_path": out_path,
        "columns": out_columns,
        "rows": ordered,
        "summary": {
            "device_id": device,
            "trip_id": trip_id,
            "source_folder": str(trip_dir),
            "output_file": out_path.name,
            "start_utc": iso(start) if start else "",
            "end_utc": iso(end) if end else "",
            # From t_mono, not the timestamps: the wall clock steps
            # mid-trip when it is corrected, and a duration taken across
            # that step is off by the size of the step.
            "duration_min": round((ordered[-1][0] - ordered[0][0]) / 60, 2),
            "max_gap_min": round(gap / 60, 2),
            "rows": len(ordered),
            "parts": len(parts),
            "columns": len(out_columns),
            "clock_source": source,
            "clock_correction_h": round(correction / 3600, 3)
                                  if correction is not None else "",
            "obd_signal_rows": with_obd,
            **gaps,
            **metrics,
            "notes": "; ".join(report.notes),
        },
    }


SUMMARY_FIELDS = ["device_id", "trip_id", "start_utc", "end_utc",
                  "duration_min", "max_gap_min", "rows", "parts", "columns",
                  "obd_signal_rows", "obd_rows", "obd_row_gap_median_s",
                  "obd_row_gap_p95_s", "obd_row_gap_max_s",
                  "gps_fix_rows", "distance_km", "max_speed_kph",
                  "clock_source", "clock_correction_h", "output_file",
                  "source_folder", "notes"]


def write_merged(path: Path, results: List[Dict[str, object]],
                 dictionary: Dictionary) -> None:
    """One table for the whole dataset, over the union of every schema."""
    columns: List[str] = []
    seen = set()
    for result in results:
        for column in result["columns"]:
            if column not in seen:
                seen.add(column)
                columns.append(column)
    columns = order_output_columns(columns, dictionary)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for result in results:
            for _, row in result["rows"]:
                writer.writerow({c: row.get(c, "") for c in columns})


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Clean raw obd-ev trip folders into analysis-ready CSVs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="The input folder is only ever read, never modified.")
    parser.add_argument("root", type=Path,
                        help="a device folder, a tree of them, or one trip folder")
    parser.add_argument("-o", "--out", type=Path, default=None,
                        help="output directory (default: a folder inside the "
                             "input named after it with _cleaned appended)")
    parser.add_argument("--fill", action="store_true",
                        help="carry each signal forward over rows that did not "
                             "sample it (see --max-stale)")
    parser.add_argument("--max-stale", type=float, default=3.0, metavar="N",
                        help="with --fill, carry a value at most N times its "
                             "own sampling period (default: 3)")
    parser.add_argument("--max-hold", type=float, default=60.0, metavar="S",
                        help="with --fill, never carry a value more than S "
                             "seconds whatever its period (default: 60)")
    parser.add_argument("--keep-without-obd", action="store_true",
                        help="keep trips in which the car reported nothing; "
                             "they are skipped by default")
    parser.add_argument("--keep-empty-columns", action="store_true",
                        help="keep columns that are empty for the whole trip")
    parser.add_argument("--min-rows", type=int, default=2, metavar="N",
                        help="skip trips with fewer than N rows (default: 2)")
    parser.add_argument("--gap-warn", type=float, default=30.0, metavar="M",
                        help="note a trip whose rows fall silent for more "
                             "than M minutes (default: 30)")
    parser.add_argument("--max-scatter", type=float, default=3.0, metavar="S",
                        help="leave the clock alone if gps_time scatters more "
                             "than S seconds against t_mono (default: 3)")
    parser.add_argument("--clock-jump", type=float, default=2.0, metavar="S",
                        help="treat a step of more than S seconds in the Pi "
                             "clock as a correction, not drift (default: 2)")
    parser.add_argument("--merge", action="store_true",
                        help="also write all_trips.csv over every trip")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="write the report file but do not print it")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    args = parser.parse_args(argv)

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        parser.error(f"{root} is not a directory")

    # Inside the input folder, so the cleaned data uploads alongside the raw
    # data it came from and stays with it. Safe to nest: a run discovers trips
    # by `drive*.csv`, and nothing this script writes is named that way. The
    # output folder is skipped explicitly as well, so that stays true even if
    # the output naming changes later.
    if args.out is None:
        args.out = root / f"{root.name}_cleaned"
    args.out = args.out.expanduser().resolve()

    trip_dirs = find_trip_dirs(root, exclude=args.out)
    if not trip_dirs:
        parser.error(f"no drive*.csv found anywhere under {root}")

    dictionary = Dictionary()
    results: List[Dict[str, object]] = []
    skipped: List[Dict[str, object]] = []

    for trip_dir in trip_dirs:
        outcome = clean_trip(trip_dir, dictionary, args)
        if outcome is None:
            continue
        if outcome.get("skipped"):
            skipped.append(outcome)
        else:
            results.append(outcome)

    # Chronological by corrected start, which is not the folder order whenever
    # the clock was wrong.
    results.sort(key=lambda r: r["summary"]["start_utc"] or "")

    if not args.dry_run:
        args.out.mkdir(parents=True, exist_ok=True)
        with (args.out / "trips_summary.csv").open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS,
                                    extrasaction="ignore")
            writer.writeheader()
            for result in results:
                writer.writerow(result["summary"])

        every_column: List[str] = []
        seen = set()
        for result in results:
            for column in result["columns"]:
                if column not in seen:
                    seen.add(column)
                    every_column.append(column)
        dictionary.write(args.out / "signals.csv",
                         order_output_columns(every_column, dictionary))

        timing = [row for result in results for row in result["timing"]]
        if timing:
            # Sorted slowest first: the signals that lag are the question.
            timing.sort(key=lambda r: -float(r["median_s"]))
            with (args.out / "obd_timing.csv").open("w", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=TIMING_FIELDS,
                                        extrasaction="ignore")
                writer.writeheader()
                writer.writerows(timing)

        if args.merge:
            write_merged(args.out / "all_trips.csv", results, dictionary)

    lines = report_lines(root, args, results, skipped, dictionary)
    text = "\n".join(lines)
    if not args.dry_run:
        (args.out / "cleaning_report.txt").write_text(text + "\n")
    if not args.quiet:
        print(text)
    return 0


def report_lines(root: Path, args, results: List[Dict[str, object]],
                 skipped: List[Dict[str, object]], dictionary: Dictionary
                 ) -> List[str]:
    lines = [f"obd-ev cleaning report",
             f"  input   {root}",
             f"  output  {args.out if not args.dry_run else '(dry run, nothing written)'}",
             f"  ran     {iso(dt.datetime.now(dt.timezone.utc))}",
             ""]

    total_rows = sum(len(r["rows"]) for r in results)
    repaired = [r for r in results if r["summary"]["clock_source"] == "gps"
                and r["summary"]["clock_correction_h"] not in ("", 0.0)]
    verb = "would be written" if args.dry_run else "written"
    lines += [f"{len(results)} trip(s) {verb}, {total_rows} row(s) in total; "
              f"{len(skipped)} skipped",
              f"{len(repaired)} trip(s) had the clock rebuilt from GPS", ""]

    for result in results:
        s = result["summary"]
        lines.append(f"{s['output_file']}  <- {Path(s['source_folder']).name}")
        lines.append(f"    {s['rows']} rows, {s['columns']} columns, "
                     f"{s['duration_min']} min, {s['parts']} part(s)")
        lines.append(f"    obd rows {s['obd_rows']}, gps fixes "
                     f"{s['gps_fix_rows']}, {s['distance_km']} km")
        if s["obd_row_gap_median_s"] != "":
            lines.append(f"    a reading off the bus every "
                         f"{s['obd_row_gap_median_s']}s median, "
                         f"{s['obd_row_gap_max_s']}s at worst")
        trip_timing = result["timing"]
        if trip_timing:
            medians = [float(r["median_s"]) for r in trip_timing]
            slowest = max(trip_timing, key=lambda r: float(r["median_s"]))
            lines.append(f"    any one signal refreshes every "
                         f"{statistics.median(medians):.1f}s median across "
                         f"{len(trip_timing)} signal(s); slowest is "
                         f"{slowest['column']} at {slowest['median_s']}s")
        for note in result["report"].notes:
            lines.append(f"    - {note}")
        lines.append("")

    if skipped:
        lines.append("Skipped:")
        for item in skipped:
            lines.append(f"    {item['trip_dir'].name}: {item['skipped']}")
        lines.append("")

    # Only the columns that reached the output: a dictionary is read from
    # every trip, including the ones skipped, and a clash confined to those
    # says nothing about the data written here.
    written = {c for result in results for c in result["columns"]}
    conflicts = [c for c in dictionary.conflicts if c in written]
    if conflicts:
        lines.append("Columns whose definition changed between trips "
                     "(first seen definition kept in signals.csv):")
        for column in conflicts:
            lines.append(f"    {column}")
        lines.append("")

    if any(result["timing"] for result in results):
        lines.append("obd_timing.csv gives the refresh interval of every "
                     "signal against the period its profile asks for. The two "
                     "rates differ: the kit hears from the car often, but a "
                     "hundred commands share one BLE link round robin, so any "
                     "single signal comes round rarely.")
        lines.append("")

    if not args.fill:
        lines.append("Rows are sparse: each one holds whatever the scheduler "
                     "polled in that cycle. Pass --fill to carry values "
                     "forward.")
    return lines


if __name__ == "__main__":
    sys.exit(main())
