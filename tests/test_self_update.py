"""When a kit takes an update, and when it refuses to.

A kit powered from the OBD port is online only while the car is on, which is
exactly when the vehicle is connected -- so "defer until the trip ends" is a
refusal unless something retries and the wait is bounded. These tests drive
the real script with stub `git`, `systemctl` and `sudo` on PATH, in a throwaway
repo, and pin the cases that decide whether a kit in a participant's car can
still be reached:

  * a drive is deferred, but only for as long as MAX_DEFER;
  * a logger that is not running can never block its own repair;
  * nothing is written to the repo before the decision to update is made.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SELF_UPDATE = REPO_ROOT / "scripts" / "self_update.sh"

# `git`, answering from files the test writes. Every invocation is recorded,
# so a test can assert that the repo was never touched.
FAKE_GIT = r"""#!/usr/bin/env bash
args="$*"
echo "$args" >> "$FAKE_LOG/git"
# Strip the `-C <dir>` the script always passes.
while [ $# -gt 0 ]; do
    case "$1" in
        -C) shift 2 ;;
        *) break ;;
    esac
done
case "$1 ${2:-}" in
    "rev-parse --abbrev-ref") cat "$FAKE_LOG/branch" ;;
    "rev-parse HEAD")         cat "$FAKE_LOG/head" ;;
    "rev-parse origin/deploy") cat "$FAKE_LOG/remote" ;;
    "fetch --quiet")          [ -f "$FAKE_LOG/fetch-fails" ] && exit 1 ;;
    "status --porcelain")     cat "$FAKE_LOG/dirty" 2>/dev/null ;;
    "merge --ff-only")        cat "$FAKE_LOG/remote" > "$FAKE_LOG/head" ;;
    "checkout -B")            cat "$FAKE_LOG/remote" > "$FAKE_LOG/head" ;;
esac
exit 0
"""

# `systemctl`, whose only interesting answer is whether obd-ev is running.
FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG/systemctl"
if [ "$1" = "is-active" ]; then
    [ -f "$FAKE_LOG/logger-active" ] && exit 0
    exit 3
fi
exit 0
"""

# `sudo -u <user> <cmd>...` -> `<cmd>...`
FAKE_SUDO = r"""#!/usr/bin/env bash
[ "$1" = "-u" ] && shift 2
exec "$@"
"""

# The repo's own stat(1) usage is GNU-flavoured; macOS stat is not.
FAKE_STAT = r"""#!/usr/bin/env bash
fmt=""
[ "$1" = "-c" ] && { fmt="$2"; shift 2; }
case "$fmt" in
    %U) echo "$(id -un)" ;;
    %Y) perl -e 'print((stat($ARGV[0]))[9], "\n")' "$1" ;;
    *)  echo "" ;;
esac
"""

# Nothing in these tests should wait out the post-restart settle.
FAKE_SLEEP = "#!/usr/bin/env bash\nexit 0\n"

STUB_POST_UPDATE = "#!/usr/bin/env bash\necho post_update >> \"$FAKE_LOG/ran\"\n"
STUB_PREFLIGHT = ("#!/usr/bin/env bash\necho preflight >> \"$FAKE_LOG/ran\"\n"
                  "[ -f \"$FAKE_LOG/preflight-fails\" ] && exit 1\nexit 0\n")

CSV_HEADER = "timestamp,t_mono,device_id,obd_connected,speed_kph\n"


def write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class SelfUpdateHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.repo = self.tmp / "obd-ev"
        (self.repo / "scripts").mkdir(parents=True)
        self.logs = self.repo / "logs"
        self.logs.mkdir()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.fake = self.tmp / "fake"
        self.fake.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()

        shutil.copy(SELF_UPDATE, self.repo / "scripts" / "self_update.sh")
        write_exec(self.repo / "scripts" / "post_update.sh", STUB_POST_UPDATE)
        write_exec(self.repo / "scripts" / "preflight.py", STUB_PREFLIGHT)
        for name, body in (("git", FAKE_GIT), ("systemctl", FAKE_SYSTEMCTL),
                           ("sudo", FAKE_SUDO), ("stat", FAKE_STAT),
                           ("sleep", FAKE_SLEEP)):
            write_exec(self.bin / name, body)

        # A kit on `deploy` with an update waiting for it.
        (self.fake / "branch").write_text("deploy\n")
        (self.fake / "head").write_text("aaaaaaaa\n")
        (self.fake / "remote").write_text("bbbbbbbb\n")
        (self.fake / "dirty").write_text("")

    # -- state the tests set up ---------------------------------------------

    def up_to_date(self):
        (self.fake / "remote").write_text("aaaaaaaa\n")

    def driving(self, connected=True):
        """A logger that is running, with a part open and the vehicle on it."""
        (self.fake / "logger-active").touch()
        part = self.logs / "drive_P001_20261015_224227.csv"
        part.write_text(CSV_HEADER + "2026-10-15T22:42:27,30.9,P001,%s,41.0\n"
                        % ("1" if connected else "0"))
        (self.logs / ".current").write_text(str(part))
        return part

    def deferred_since(self, seconds_ago):
        import time
        (self.state / "update-deferred-since").write_text(
            str(int(time.time()) - seconds_ago))

    def run_update(self, *args, **env):
        environ = dict(os.environ)
        environ.update({
            "PATH": f"{self.bin}:{environ['PATH']}",
            "FAKE_LOG": str(self.fake),
            "OBD_EV_STATE_DIR": str(self.state),
            "OBD_EV_LOG_DIR": str(self.logs),
            "OBD_EV_AUTO_UPDATE": "1",
        })
        environ.update(env)
        return subprocess.run(
            ["bash", str(self.repo / "scripts" / "self_update.sh"), *args],
            capture_output=True, text=True, env=environ, timeout=60)

    # -- helpers ------------------------------------------------------------

    def ran(self):
        path = self.fake / "ran"
        return path.read_text().split() if path.exists() else []

    def git_calls(self):
        path = self.fake / "git"
        return path.read_text().splitlines() if path.exists() else []

    def head(self):
        return (self.fake / "head").read_text().strip()


class TestDeferringADrive(SelfUpdateHarness):
    def test_update_is_deferred_while_the_vehicle_is_connected(self):
        self.driving()
        result = self.run_update()
        self.assertIn("deferring", result.stdout)
        self.assertEqual(self.head(), "aaaaaaaa", "repo must not have moved")
        self.assertEqual(self.ran(), [], "nothing applied mid-drive")

    def test_the_repo_is_not_touched_before_the_decision(self):
        """A deferral that had already merged would leave the running logger
        on code that no longer matches the working tree."""
        self.driving()
        self.run_update()
        for call in self.git_calls():
            self.assertFalse(call.startswith(("merge", "checkout", "reset")),
                             f"repo was modified before deferring: {call}")

    def test_deferral_is_bounded_and_the_part_is_closed_first(self):
        self.driving()
        self.deferred_since(7 * 3600)        # longer than the 6h default
        result = self.run_update()
        self.assertIn("updating mid-drive", result.stdout)
        self.assertEqual(self.head(), "bbbbbbbb")
        self.assertEqual(self.ran(), ["post_update", "preflight"])

    def test_a_bounded_deferral_asks_the_logger_to_close_the_part(self):
        """The drive survives the restart as a complete file, rather than as
        whatever had been flushed when the process was killed."""
        self.driving()
        self.deferred_since(7 * 3600)
        self.run_update()
        # The stub logger never acknowledges, so the script clears the request
        # itself rather than leaving one for the next logger to act on.
        self.assertFalse((self.logs / ".flush-request").exists())

    def test_the_wait_is_measured_from_the_first_deferral(self):
        self.driving()
        self.run_update()
        stamp = self.state / "update-deferred-since"
        self.assertTrue(stamp.exists(), "the wait has to start somewhere")
        first = stamp.read_text()
        self.run_update(**{"OBD_EV_UPDATE_MIN_INTERVAL": "0"})
        self.assertEqual(stamp.read_text(), first,
                         "a second attempt must not restart the clock")

    def test_the_clock_is_cleared_once_the_update_lands(self):
        self.deferred_since(100)
        self.run_update()
        self.assertEqual(self.head(), "bbbbbbbb")
        self.assertFalse((self.state / "update-deferred-since").exists())

    def test_the_clock_is_cleared_when_there_is_nothing_to_take(self):
        """Otherwise the next real update inherits a stale wait and takes
        itself mid-drive immediately."""
        self.deferred_since(100)
        self.up_to_date()
        self.run_update()
        self.assertFalse((self.state / "update-deferred-since").exists())


class TestTheLoggerCannotBlockItsOwnRepair(SelfUpdateHarness):
    def test_a_stale_marker_from_a_power_cut_does_not_defer(self):
        """.current is removed on a clean close, and these kits die with the
        ignition instead -- so the marker usually outlives the drive."""
        self.driving()                       # leaves .current behind...
        (self.fake / "logger-active").unlink()   # ...with the logger stopped
        result = self.run_update()
        self.assertNotIn("deferring", result.stdout)
        self.assertEqual(self.head(), "bbbbbbbb")

    def test_a_crash_looping_logger_still_takes_the_update(self):
        """The one case where an update matters most is the one where the
        old gate was guaranteed to block it."""
        self.driving()
        (self.fake / "logger-active").unlink()
        self.run_update()
        self.assertEqual(self.ran(), ["post_update", "preflight"])

    def test_no_marker_at_all_is_not_a_drive(self):
        (self.fake / "logger-active").touch()
        self.run_update()
        self.assertEqual(self.head(), "bbbbbbbb")

    def test_a_parked_kit_with_the_logger_up_is_not_a_drive(self):
        self.driving(connected=False)
        self.run_update()
        self.assertEqual(self.head(), "bbbbbbbb")


class TestForceAndRateLimit(SelfUpdateHarness):
    def test_force_ignores_a_drive_in_progress(self):
        self.driving()
        self.run_update("--force")
        self.assertEqual(self.head(), "bbbbbbbb")

    def test_opted_out_kit_does_nothing(self):
        self.run_update(**{"OBD_EV_AUTO_UPDATE": "0"})
        self.assertEqual(self.git_calls(), [])

    def test_a_failed_fetch_does_not_use_up_the_interval(self):
        """A connect with no internet behind it -- the setup AP, a captive
        portal -- must not skip the real home connection minutes later."""
        (self.fake / "fetch-fails").touch()
        self.run_update()
        self.assertFalse((self.state / "last-update-check").exists())

    def test_nothing_to_take_is_a_no_op(self):
        self.up_to_date()
        self.run_update()
        self.assertEqual(self.ran(), [])


class TestTheRetryExists(unittest.TestCase):
    """The deferral is only a deferral if something comes back for it."""

    def test_an_update_timer_ships(self):
        timer = REPO_ROOT / "systemd" / "obd-ev-update.timer"
        self.assertTrue(timer.exists(),
                        "without a timer the only trigger is a connect, which "
                        "on an OBD-powered kit only happens mid-drive")
        body = timer.read_text()
        self.assertIn("Unit=obd-ev-update.service", body)
        self.assertIn("OnUnitActiveSec=", body)

    def test_post_update_enables_it(self):
        """Copying a unit file is not enough: a timer nothing enables ships
        to every kit and never fires."""
        body = (REPO_ROOT / "scripts" / "post_update.sh").read_text()
        enable_block = body[body.index("for unit in"):]
        self.assertIn("obd-ev-update.timer",
                      enable_block[:enable_block.index("done")])


if __name__ == "__main__":
    unittest.main()
