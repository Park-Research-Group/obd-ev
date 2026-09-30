#!/usr/bin/env python3
"""Set the system clock from the GPS receiver.

The Pi has no RTC. At boot it restores whatever fake-hwclock last saved, and
without reachable NTP it stays there -- kit P001 drifted to 1.05 days behind
while still reporting "System clock synchronized: yes". GPS carries exact UTC
and needs no network, which is the right source for a device that may spend
days in a driveway with no internet.

Run as root, from a timer. Always exits 0: a clock fix must never be the reason
a kit stops collecting.

    sudo scripts/gps_time_sync.py            # step if the offset is large
    sudo scripts/gps_time_sync.py --dry-run  # report what it would do
    sudo scripts/gps_time_sync.py --force    # step on any offset

Safety, because setting a clock from bad data is worse than a wrong clock:
gpsd emits a `time` field even with no fix -- observed reporting 2019-04-07 on
a receiver that had never locked. So a reading is only trusted when the fix is
2D or better, the year is plausible, and two readings agree with each other.
"""

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from obd_ev.venv import reexec_if_needed  # noqa: E402
reexec_if_needed()

log = logging.getLogger("gps_time_sync")

MIN_YEAR = 2025
MAX_YEAR = 2040
DEFAULT_THRESHOLD = 2.0      # seconds
AGREE_WITHIN = 5.0           # two readings must imply offsets this close


def plausible(epoch: float) -> bool:
    """A GPS time we are willing to believe."""
    year = time.gmtime(epoch).tm_year
    return MIN_YEAR <= year <= MAX_YEAR


def decide(offsets, threshold=DEFAULT_THRESHOLD, force=False):
    """Given trusted (gps_epoch, system_epoch) pairs, decide whether to step.

    Pure, so the policy is testable without a GPS receiver. Returns
    (step_to_epoch_or_None, reason).
    """
    if not offsets:
        return None, "no trusted GPS time (needs a 2D fix and a sane year)"
    if len(offsets) < 2:
        return None, "only one trusted reading; want two that agree"

    deltas = [g - s for g, s in offsets]
    spread = max(deltas) - min(deltas)
    if spread > AGREE_WITHIN:
        return None, f"readings disagree by {spread:.1f}s; not trusting them"

    # Use the newest reading, corrected for how long ago it was taken.
    gps_epoch, sys_epoch = offsets[-1]
    offset = gps_epoch - sys_epoch
    if not force and abs(offset) < threshold:
        return None, f"clock is within {abs(offset):.2f}s of GPS; leaving it"
    return gps_epoch, f"clock is {offset:+.2f}s from GPS"


def collect(timeout: float):
    """Gather trusted (gps_epoch, system_epoch) pairs from gpsd."""
    try:
        from gps import gps, WATCH_ENABLE, WATCH_NEWSTYLE
    except ImportError:
        log.error("python3-gps not available; cannot read GPS time")
        return []
    try:
        session = gps(mode=WATCH_ENABLE | WATCH_NEWSTYLE)
    except Exception as exc:
        log.error("cannot reach gpsd: %s", exc)
        return []

    trusted, deadline = [], time.time() + timeout
    while time.time() < deadline and len(trusted) < 2:
        try:
            report = session.next()
        except StopIteration:
            break
        except Exception:
            continue
        if getattr(report, "class", None) != "TPV":
            continue
        mode = getattr(report, "mode", 0) or 0
        stamp = getattr(report, "time", None)
        if mode < 2 or not stamp:
            continue
        try:
            # gpsd emits RFC3339 with a Z suffix.
            gps_epoch = time.mktime(time.strptime(
                stamp.replace("Z", "").split(".")[0], "%Y-%m-%dT%H:%M:%S"))
            gps_epoch -= time.timezone if not time.daylight else time.altzone
        except (ValueError, TypeError):
            continue
        if not plausible(gps_epoch):
            log.warning("ignoring implausible GPS time %s", stamp)
            continue
        trusted.append((gps_epoch, time.time()))
    return trusted


def set_clock(epoch: float) -> bool:
    try:
        time.clock_settime(time.CLOCK_REALTIME, epoch)
        return True
    except (OSError, PermissionError, AttributeError) as exc:
        log.debug("clock_settime failed (%s); falling back to date", exc)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))
    return subprocess.run(["date", "-u", "-s", stamp],
                          capture_output=True).returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--timeout", type=float, default=45.0,
                    help="seconds to wait for a usable fix")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    trusted = collect(args.timeout)
    target, reason = decide(trusted, args.threshold, args.force)

    if target is None:
        log.info("%s", reason)
        return 0

    before = time.time()
    if args.dry_run:
        log.info("would step clock: %s -> %s (%s)",
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(before)),
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(target)),
                 reason)
        return 0

    if set_clock(target):
        log.info("stepped clock from %s to %s (%s)",
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(before)),
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(target)),
                 reason)
        # Persist it so the next boot starts close, before any fix exists.
        subprocess.run(["fake-hwclock", "save"], capture_output=True)
    else:
        log.error("could not set the clock (need root?)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
