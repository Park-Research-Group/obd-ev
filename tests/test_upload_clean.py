"""Cleaning on the kit, and shipping the result next to the raw data.

The participant never runs anything and the researcher should not have to
either, so `scripts/upload.sh` cleans what it has shipped and uploads that too.
These tests drive the real script with a stub `rclone` on PATH, and pin the
properties that matter on a kit which loses power without warning: the raw
upload is never put at risk by cleaning, a trip split across the pending folder
and the archive is not half-cleaned, and a run that changed nothing does not
re-clean the whole archive once a minute.
"""

import csv
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
UPLOAD_SH = REPO_ROOT / "scripts" / "upload.sh"

# A stub rclone that records every call and copies locally, so the test can
# assert on what would have been sent without touching a network.
FAKE_RCLONE = r"""#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "$FAKE_RCLONE_LOG"
if [ "${FAKE_RCLONE_FAIL:-0}" = "1" ]; then exit 1; fi

mode=""; src=""; dst=""
while [ $# -gt 0 ]; do
    case "$1" in
        copyto|copy) mode="$1"; src="$2"; dst="$3"; shift 3 ;;
        --config) shift 2 ;;
        --*) shift ;;
        *) shift ;;
    esac
done
[ -n "$mode" ] || exit 0
target="$FAKE_REMOTE_ROOT/${dst#*:}"
if [ "$mode" = "copyto" ]; then
    mkdir -p "$(dirname "$target")"
    cp "$src" "$target"
else
    mkdir -p "$target"
    cp -R "$src/." "$target/"
fi
"""

def read_csv(path: Path):
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


HEADER = ("timestamp,t_mono,device_id,obd_connected,"
          "vehicle_speed_kph,lat,lon\n")

DICTIONARY = (
    "column,name,unit,group,category,source,source_id,command,period_s,min,max\n"
    "timestamp,Row time,unknown,Meta,Meta,logger,timestamp,,,,\n"
    "t_mono,Monotonic,seconds,Meta,Meta,logger,t_mono,,,,\n"
    "device_id,Kit id,unknown,Meta,Meta,logger,device_id,,,,\n"
    "obd_connected,Link up,scalar,Meta,Meta,logger,obd_connected,,,,\n"
    "vehicle_speed_kph,Vehicle speed,kilometersPerHour,Movement,Movement,"
    "vehicle-obdb,VSS,22000D,0.25,0.0,255\n"
    "lat,Latitude,degrees,GPS,GPS,gps,lat,,,-90,90\n"
    "lon,Longitude,degrees,GPS,GPS,gps,lon,,,-180,180\n"
)


def part(start_mono: float, rows: int, speed: bool = True) -> str:
    out = [HEADER]
    for i in range(rows):
        mono = start_mono + i
        # lon is left empty throughout, so the empty-column drop has a
        # column to drop and --keep-empty-columns has one to keep.
        out.append(f"2026-10-01T00:00:{i % 60:02d}+00:00,{mono},P001,1,"
                   f"{'45' if speed else ''},38.0,\n")
    return "".join(out)


class UploadTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.log_dir = self.tmp / "logs"
        self.log_dir.mkdir()
        self.remote_root = self.tmp / "remote"
        self.remote_root.mkdir()
        self.rclone_log = self.tmp / "rclone.log"

        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "rclone"
        fake.write_text(FAKE_RCLONE)
        fake.chmod(0o755)
        self.bin_dir = bin_dir

    def tearDown(self):
        self._tmp.cleanup()

    # -- fixtures ----------------------------------------------------------

    def add_trip(self, name: str, parts=(("000000", 6),), archived=False,
                 speed=True) -> Path:
        base = self.log_dir / ("uploaded" if archived else "") / name
        base.mkdir(parents=True, exist_ok=True)
        for stamp, rows in parts:
            (base / f"drive_P001_{stamp}.csv").write_text(
                part(10.0, rows, speed=speed))
        (base / "signals_P001.csv").write_text(DICTIONARY)
        return base

    # -- running -----------------------------------------------------------

    def run_upload(self, fail_rclone=False, **env):
        environment = dict(os.environ)
        environment.update({
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "OBD_EV_LOG_DIR": str(self.log_dir),
            "OBD_EV_REMOTE": "fake:uploads",
            "OBD_EV_RCLONE_CONF": str(self.tmp / "rclone.conf"),
            "OBD_EV_DEVICE_ID": "P001",
            "FAKE_RCLONE_LOG": str(self.rclone_log),
            "FAKE_REMOTE_ROOT": str(self.remote_root),
            "FAKE_RCLONE_FAIL": "1" if fail_rclone else "0",
        })
        environment.update({k: str(v) for k, v in env.items()})
        result = subprocess.run(
            ["bash", str(UPLOAD_SH)], env=environment, capture_output=True,
            text=True, timeout=120)
        return result

    @property
    def cleaned_dir(self) -> Path:
        return self.log_dir / "P001_cleaned"

    @property
    def remote(self) -> Path:
        return self.remote_root / "uploads" / "P001"

    def rclone_calls(self):
        if not self.rclone_log.exists():
            return []
        return [line for line in self.rclone_log.read_text().splitlines()
                if line.strip()]


class TestCleanedUploadHappyPath(UploadTestCase):
    def test_ships_the_raw_trip_and_the_cleaned_data(self):
        self.add_trip("20261001_000000_aaaaaaaa")
        result = self.run_upload()
        self.assertEqual(result.returncode, 0, result.stderr)

        # Raw, exactly as before this feature.
        self.assertTrue(
            (self.remote / "20261001_000000_aaaaaaaa"
             / "drive_P001_000000.csv").is_file())
        # Cleaned, beside it under the kit's id.
        cleaned = self.remote / "P001_cleaned"
        self.assertTrue((cleaned / "trips_summary.csv").is_file())
        self.assertEqual(
            len(list((cleaned / "trips").glob("*.csv"))), 1)

    def test_the_cleaned_copy_is_actually_cleaned(self):
        self.add_trip("20261001_000000_aaaaaaaa")
        self.run_upload()

        summary = read_csv(self.remote / "P001_cleaned" / "trips_summary.csv")
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["device_id"], "P001")
        self.assertEqual(summary[0]["obd_signal_rows"], "6")

    def test_it_lands_in_the_log_dir_not_the_archive(self):
        """The archive is pruned a trip folder at a time; output there would
        eventually be deleted as though it were an old trip."""
        self.add_trip("20261001_000000_aaaaaaaa")
        self.run_upload()
        self.assertTrue(self.cleaned_dir.is_dir())
        self.assertFalse((self.log_dir / "uploaded" / "P001_cleaned").exists())

    def test_the_cleaned_folder_is_not_uploaded_as_a_trip(self):
        """The per-file loop would ship its top-level CSVs to the wrong place
        and then move them out of it, emptying the folder."""
        self.add_trip("20261001_000000_aaaaaaaa")
        self.run_upload()

        # Still complete locally: nothing was moved into the archive.
        self.assertTrue((self.cleaned_dir / "signals.csv").is_file())
        self.assertTrue((self.cleaned_dir / "trips").is_dir())
        self.assertFalse(
            (self.remote / "trips_summary.csv").exists())

    def test_passes_through_extra_cleaner_arguments(self):
        self.add_trip("20261001_000000_aaaaaaaa", parts=(("000000", 8),))
        self.run_upload(OBD_EV_CLEAN_ARGS="--keep-empty-columns")

        trip = next((self.cleaned_dir / "trips").glob("*.csv"))
        self.assertIn("lon", trip.read_text().splitlines()[0])


class TestNotAtTheExpenseOfTheRawUpload(UploadTestCase):
    """The raw upload is the job. Cleaning is a convenience on top of it."""

    def test_a_cleaner_failure_does_not_fail_the_run(self):
        self.add_trip("20261001_000000_aaaaaaaa")
        result = self.run_upload(OBD_EV_PYTHON="/nonexistent/python")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            (self.remote / "20261001_000000_aaaaaaaa"
             / "drive_P001_000000.csv").is_file())

    def test_the_raw_files_are_archived_even_when_cleaning_is_off(self):
        self.add_trip("20261001_000000_aaaaaaaa")
        result = self.run_upload(OBD_EV_CLEAN="0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.cleaned_dir.exists())
        self.assertTrue(
            (self.log_dir / "uploaded" / "20261001_000000_aaaaaaaa"
             / "drive_P001_000000.csv").is_file())
        self.assertFalse(
            any("P001_cleaned" in call for call in self.rclone_calls()))

    def test_cleaning_runs_after_the_raw_upload(self):
        """Before it, a trip mid-upload is split between the pending folder
        and the archive, and would be cleaned as two half trips."""
        self.add_trip("20261001_000000_aaaaaaaa")
        self.run_upload()

        calls = self.rclone_calls()
        raw = [i for i, c in enumerate(calls) if "drive_P001" in c]
        cleaned = [i for i, c in enumerate(calls) if "P001_cleaned" in c]
        self.assertTrue(raw and cleaned)
        self.assertLess(max(raw), min(cleaned))

    def test_a_trip_still_pending_is_not_cleaned_yet(self):
        """Only the archive is cleaned, so a part that has not shipped cannot
        produce a half-cleaned trip in the cloud."""
        self.add_trip("20261001_000000_aaaaaaaa", archived=True)
        self.add_trip("20261002_000000_bbbbbbbb")        # pending
        self.run_upload(FAKE_RCLONE_FAIL="0")

        # Both end up cleaned here, because the pending one ships first and is
        # in the archive by the time cleaning runs.
        summary = read_csv(self.cleaned_dir / "trips_summary.csv")
        self.assertEqual(
            {r["trip_id"] for r in summary},
            {"20261001_000000_aaaaaaaa", "20261002_000000_bbbbbbbb"})

    def test_an_upload_failure_leaves_the_raw_file_in_place(self):
        self.add_trip("20261001_000000_aaaaaaaa")
        result = self.run_upload(fail_rclone=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            (self.log_dir / "20261001_000000_aaaaaaaa"
             / "drive_P001_000000.csv").is_file())


class TestWhenItRuns(UploadTestCase):
    """The timer fires once a minute; the archive must not be re-cleaned
    every time for no change."""

    def test_skipped_when_nothing_new_was_uploaded(self):
        self.add_trip("20261001_000000_aaaaaaaa", archived=True)
        self.cleaned_dir.mkdir()
        (self.cleaned_dir / "trips_summary.csv").write_text("marker\n")

        result = self.run_upload()
        self.assertEqual(result.returncode, 0, result.stderr)
        # Untouched: no run, so no rewrite.
        self.assertEqual(
            (self.cleaned_dir / "trips_summary.csv").read_text(), "marker\n")

    def test_runs_when_the_output_is_missing_even_with_nothing_new(self):
        """First run after deploying this feature: the archive is full of
        trips that shipped before the cleaner existed."""
        self.add_trip("20261001_000000_aaaaaaaa", archived=True)
        self.assertFalse(self.cleaned_dir.exists())

        result = self.run_upload()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.cleaned_dir / "trips_summary.csv").is_file())
        self.assertEqual(
            len(list((self.cleaned_dir / "trips").glob("*.csv"))), 1)

    def test_runs_again_once_new_data_arrives(self):
        self.add_trip("20261001_000000_aaaaaaaa", archived=True)
        self.run_upload()
        first = sorted(p.name for p in (self.cleaned_dir / "trips").glob("*.csv"))

        self.add_trip("20261002_000000_bbbbbbbb")
        self.run_upload()
        second = sorted(p.name for p in (self.cleaned_dir / "trips").glob("*.csv"))

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 2)

    def test_uses_checksums_so_unchanged_trips_are_not_resent(self):
        """Every run rewrites every cleaned file, so modification times always
        differ; only content should decide what goes over the wire."""
        self.add_trip("20261001_000000_aaaaaaaa", archived=True)
        self.run_upload()

        call = next(c for c in self.rclone_calls() if "P001_cleaned" in c)
        self.assertIn("--checksum", call)
        self.assertNotIn("sync", call)


class TestTripsWithoutOBD(UploadTestCase):
    def test_a_bench_trip_still_uploads_raw_but_is_not_in_the_cleaned_set(self):
        self.add_trip("20261001_000000_aaaaaaaa", speed=False)
        self.run_upload()

        self.assertTrue(
            (self.remote / "20261001_000000_aaaaaaaa"
             / "drive_P001_000000.csv").is_file())
        self.assertFalse((self.cleaned_dir / "trips").exists())
        report = (self.cleaned_dir / "cleaning_report.txt").read_text()
        self.assertIn("no OBD readings", report)



class TestTheOpenPartIsProtectedWithoutSystemd(unittest.TestCase):
    """On a bench kit the logger is started by hand, so `systemctl is-active`
    says nothing. A part still being written must still be left alone."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.logs = self.tmp / "logs"
        (self.logs / "20260101_000000_abcd1234").mkdir(parents=True)
        self.part = self.logs / "20260101_000000_abcd1234" / "drive_P002_x.csv"
        self.part.write_text("timestamp,t_mono,device_id,obd_connected\n"
                             "2026-01-01T00:00:00Z,1.0,P002,0\n")
        (self.logs / ".current").write_text(str(self.part))

    def body(self):
        return (REPO_ROOT / "scripts" / "upload.sh").read_text()

    def test_a_freshly_written_part_counts_as_a_running_logger(self):
        self.assertIn("newermt", self.body(),
                      "nothing but systemd decides whether a part is open")

    def test_the_marker_alone_is_not_enough(self):
        """After a power cut the marker outlives the logger and its file is
        complete -- that one must still upload."""
        src = self.body()
        window = src[src.index("logger_running=0"):src.index("request_flush()")]
        self.assertIn("-newermt", window)
        self.assertIn("-f \"$current_part\"", window)


if __name__ == "__main__":
    unittest.main()
