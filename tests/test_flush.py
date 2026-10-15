"""Closing the open part early, when the kit reaches WiFi.

A kit powered from the OBD port loses power with the ignition. The part still
open at that moment is never closed, and the uploader never touches an open
part -- which is how the tail of a drive goes missing. scripts/upload.sh asks
for that part as soon as the Pi is online, via a request file in the log
directory.

These tests pin the handshake, the cases where the request must be refused, and
the shutdown paths that must stay safe whether or not WiFi was ever reached.
"""

import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from obd_ev.config import LoggerConfig                      # noqa: E402
from obd_ev.logger import (CURRENT_MARKER, FLUSH_REQUEST,   # noqa: E402
                           CsvLogger)
from obd_ev.main import FLUSH_MIN_GAP, should_flush         # noqa: E402

FIELDS = ["speed_kph"]


def make_logger(tmp, **kw):
    cfg = LoggerConfig(output_dir=str(tmp), **kw)
    return CsvLogger(cfg, FIELDS, device_id="P001")


class TestShouldFlush(unittest.TestCase):
    """The decision, away from any I/O."""

    def test_mid_drive_with_rows_is_flushed(self):
        # The case the feature exists for: home WiFi, engine still running.
        self.assertTrue(should_flush(42, False, 1000.0, float("-inf")))

    def test_empty_part_is_not_worth_rotating(self):
        # Nothing to save; rotating would only churn the filename.
        self.assertFalse(should_flush(0, False, 1000.0, float("-inf")))

    def test_parked_is_declined(self):
        # Parked rows rotate on idle_rotate_minutes. Flushing them would give a
        # car sitting at home a new file on every upload run, once a minute.
        self.assertFalse(should_flush(500, True, 1000.0, float("-inf")))

    def test_repeated_requests_are_rate_limited(self):
        now = 1000.0
        self.assertTrue(should_flush(10, False, now, float("-inf")))
        # NetworkManager reporting the same connection up a second time.
        self.assertFalse(should_flush(10, False, now + 1.0, now))
        self.assertFalse(should_flush(10, False, now + FLUSH_MIN_GAP - 0.1, now))
        self.assertTrue(should_flush(10, False, now + FLUSH_MIN_GAP, now))


class TestRequestHandshake(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_request_is_seen_and_acknowledged(self):
        lg = make_logger(self.tmp)
        self.assertFalse(lg.flush_requested())
        (self.tmp / FLUSH_REQUEST).write_text("")
        self.assertTrue(lg.flush_requested())
        # Deleting the file is the ack upload.sh blocks on.
        lg.clear_flush_request()
        self.assertFalse(lg.flush_requested())
        # It is cleared whether or not we rotated, so it must tolerate being
        # cleared when already gone -- including after upload.sh times out and
        # takes its own request back.
        lg.clear_flush_request()
        lg.close()

    def test_stale_request_from_a_power_cut_is_dropped_at_startup(self):
        # The request belonged to a part that died with the last boot.
        (self.tmp / FLUSH_REQUEST).write_text("")
        lg = make_logger(self.tmp)
        self.assertFalse(lg.flush_requested())
        lg.close()

    def test_flushed_part_is_complete_and_no_longer_current(self):
        lg = make_logger(self.tmp)
        for i in range(5):
            lg.write({"speed_kph": i})
        closed = lg.path
        lg.rotate()          # what the loop does when should_flush says yes

        self.assertTrue(closed.exists())
        # upload.sh skips whatever .current names, so this must not be it.
        self.assertNotEqual((self.tmp / CURRENT_MARKER).read_text().strip(),
                            str(closed))
        with closed.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([r["speed_kph"] for r in rows],
                         ["0", "1", "2", "3", "4"])
        lg.close()

    def test_shutdown_before_wifi_leaves_the_part_shippable(self):
        # The kit is stopped having never reached WiFi, so no flush ever came.
        # close() must drop the marker, or the next upload run would mistake a
        # finished part for one still being written and skip it forever.
        lg = make_logger(self.tmp)
        lg.write({"speed_kph": 1})
        path = lg.path
        lg.close()
        self.assertFalse((self.tmp / CURRENT_MARKER).exists())
        self.assertTrue(path.exists())

    def test_shutdown_with_a_request_still_pending(self):
        # upload.sh wrote the request, then the kit lost power. Nothing must be
        # corrupted, and the next boot starts clean.
        lg = make_logger(self.tmp)
        lg.write({"speed_kph": 1})
        (self.tmp / FLUSH_REQUEST).write_text("")
        path = lg.path
        lg.close()
        self.assertTrue(path.exists())

        lg2 = make_logger(self.tmp)
        self.assertFalse(lg2.flush_requested())
        self.assertNotEqual(lg2.path, path)
        lg2.close()


if __name__ == "__main__":
    unittest.main()
