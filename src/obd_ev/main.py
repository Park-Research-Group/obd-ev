import argparse
import logging
import signal
import sys
import time

from . import config as cfgmod
from .obd_reader import OBDLink, build_reader
from .gps_reader import (GPSReader, FIELDS as GPS_FIELDS,
                         DESCRIPTIONS as GPS_DESCRIPTIONS)
from .imu_reader import (IMUReader, FIELDS as IMU_FIELDS,
                         DESCRIPTIONS as IMU_DESCRIPTIONS)
from .logger import CsvLogger


# Columns the logger itself contributes, before any sensor.
META_DESCRIPTIONS = [
    ("timestamp", "Row time, UTC, from the Pi clock (see gps_time)", "unknown"),
    ("t_mono", "Monotonic seconds since boot; survives a clock correction", "seconds"),
    ("device_id", "Kit identifier", "unknown"),
    ("obd_connected", "1 while the vehicle link was up", "scalar"),
]


def _dictionary(link) -> list:
    """Assemble signals.csv: every column in the trip CSV, described once."""
    rows = [{"column": c, "name": n, "unit": u, "group": "Meta",
             "category": "Meta", "source": "logger", "source_id": c}
            for c, n, u in META_DESCRIPTIONS]
    rows += link.describe()
    for source, descriptions in (("gps", GPS_DESCRIPTIONS),
                                 ("imu", IMU_DESCRIPTIONS)):
        rows += [{"column": c, "name": n, "unit": u, "group": source.upper(),
                  "category": source.upper(), "source": source, "source_id": c}
                 for c, n, u in descriptions]
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(prog="obd-ev")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("obd_ev")

    cfg = cfgmod.load(args.config)

    # Sensors first. They must record even if the OBD adapter never shows up,
    # which is the normal case for a car that is parked or an EV that refuses
    # the handshake.
    gps_reader = GPSReader(cfg.gps)
    gps_reader.start()
    imu_reader = IMUReader(cfg.imu)
    imu_reader.start()

    link = OBDLink(cfg.obd, lambda: build_reader(cfg))
    fieldnames = (["device_id", "obd_connected"]
                  + link.field_names() + GPS_FIELDS + IMU_FIELDS)
    csv_log = CsvLogger(cfg.logger, fieldnames, device_id=cfg.device.id,
                        dictionary=_dictionary(link))
    log.info("device_id=%s, %d columns (%d vehicle signals)",
             cfg.device.id, len(fieldnames), len(link.field_names()))
    if cfg.vehicle.signalset:
        log.info("vehicle profile: %s %s",
                 cfg.vehicle.make_model or cfg.vehicle.signalset,
                 cfg.vehicle.year or "")
    link.start()

    stop = False

    def _sig(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    active_period = 1.0 / max(cfg.logger.max_hz, 0.01)
    idle_period = 1.0 / max(cfg.logger.idle_hz, 0.001)
    rotate_after = cfg.logger.rotate_minutes * 60.0
    idle_rotate_after = cfg.logger.idle_rotate_minutes * 60.0
    last_obd_at = time.monotonic()
    trip_closed = False

    try:
        while not stop:
            started = time.monotonic()
            obd_values = link.read()
            row = {
                "device_id": cfg.device.id,
                # Sampled after the read, so a row on which the link was
                # found dead does not claim it was up.
                "obd_connected": int(link.connected),
            }

            # A trip starting: ship the parked rows accumulated since the last
            # trip ended and give the drive its own file.
            if obd_values and trip_closed:
                log.info("vehicle responding again")
                csv_log.new_trip()
                trip_closed = False

            row.update(obd_values)
            row.update(gps_reader.latest())
            row.update(imu_reader.latest())
            csv_log.write(row)

            now = time.monotonic()
            if obd_values:
                last_obd_at = now

            # End the trip once the vehicle has been unreachable for a while,
            # so the file closes and the upload timer can ship it.
            gap = now - last_obd_at
            if not trip_closed and gap > cfg.logger.trip_gap_seconds:
                log.info("no vehicle data for %.0fs, closing trip (%d rows)",
                         gap, csv_log.rows)
                csv_log.rotate()
                trip_closed = True
            elif csv_log.age_seconds > (idle_rotate_after if trip_closed
                                        else rotate_after):
                # Bound how much a single power cut can cost, and close the
                # part so it becomes eligible for upload. Parked rows rotate
                # on a much longer interval -- a car parked for a week must
                # not turn into hundreds of near-empty files -- but they do
                # rotate, so they still get shipped and one file cannot
                # grow for weeks.
                log.info("rotating after %.0f min (%d rows)",
                         csv_log.age_seconds / 60, csv_log.rows)
                csv_log.rotate()

            period = active_period if link.connected else idle_period
            remaining = period - (time.monotonic() - started)
            if remaining > 0:
                # Sleep in slices so shutdown stays responsive at idle_hz,
                # where a period can be several seconds.
                deadline = time.monotonic() + remaining
                while not stop:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    time.sleep(min(0.2, left))
    finally:
        gps_reader.stop()
        imu_reader.stop()
        link.close()
        csv_log.close()
        log.info("shutdown clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
