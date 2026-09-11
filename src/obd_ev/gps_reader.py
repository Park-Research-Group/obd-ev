import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from .config import GPSConfig

log = logging.getLogger(__name__)

FIELDS = ["lat", "lon", "alt", "speed_gps", "heading", "fix_mode",
          "gps_sats_used", "gps_sats_visible", "gps_snr_max",
          "gps_time", "gps_age_s"]

# Column documentation for signals.csv.
DESCRIPTIONS = [
    ("lat", "Latitude", "degrees"),
    ("lon", "Longitude", "degrees"),
    ("alt", "Altitude above mean sea level", "meters"),
    ("speed_gps", "Ground speed from GPS", "metersPerSecond"),
    ("heading", "Course over ground", "degrees"),
    ("fix_mode", "Fix quality: 0=none, 1=no fix, 2=2D, 3=3D", "scalar"),
    ("gps_sats_used", "Satellites contributing to the fix", "scalar"),
    ("gps_sats_visible", "Satellites the receiver can hear at all", "scalar"),
    ("gps_snr_max", "Strongest satellite signal; ~30+ is needed to lock",
     "scalar"),
    ("gps_time", "Receiver UTC, independent of the Pi clock", "unknown"),
    ("gps_age_s", "Age of this fix when the row was written", "seconds"),
]


@dataclass
class GPSSample:
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt: Optional[float] = None
    speed_gps: Optional[float] = None  # m/s
    heading: Optional[float] = None    # degrees
    fix_mode: int = 0                  # 0=no fix, 2=2D, 3=3D
    gps_time: Optional[str] = None     # receiver UTC, independent of the Pi clock
    t: float = field(default_factory=time.monotonic)


class GPSReader:
    """Background gpsd client. Latest sample is read via .latest()."""

    def __init__(self, cfg: GPSConfig):
        self.cfg = cfg
        self._sample = GPSSample()
        # Used vs visible are different questions: used tells you the fix
        # quality, visible tells you whether the antenna works at all. A fix
        # with 0 used but many visible is a weak fix worth distrusting.
        self._sats_used = 0
        self._sats_visible = 0
        self._snr_max = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not self.cfg.enabled:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            from gps import gps, WATCH_ENABLE, WATCH_NEWSTYLE
        except ImportError:
            log.error("python-gps not installed; GPS disabled")
            return

        # Outer loop reconnects: gpsd restarting, or not being up yet when
        # the logger starts, must not cost the rest of the drive.
        backoff = 2.0
        while not self._stop.is_set():
            try:
                session = gps(host=self.cfg.host, port=str(self.cfg.port),
                              mode=WATCH_ENABLE | WATCH_NEWSTYLE)
            except Exception as e:
                log.warning("gpsd connect failed (%s); retrying in %.0fs",
                            e, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            backoff = 2.0
            log.info("connected to gpsd at %s:%s", self.cfg.host, self.cfg.port)
            self._read_session(session)
            if not self._stop.is_set():
                log.warning("gpsd connection lost; reconnecting")
                self._stop.wait(backoff)

    def _read_session(self, session) -> None:
        errors = 0
        while not self._stop.is_set():
            try:
                report = session.next()
            except StopIteration:
                return
            except Exception as e:
                errors += 1
                if errors == 1 or errors % 30 == 0:
                    log.warning("gpsd read error (%d in a row): %s", errors, e)
                if errors >= 30:
                    return            # give up on this session; reconnect
                self._stop.wait(1.0)
                continue
            errors = 0

            kind = getattr(report, "class", None)
            if kind == "SKY":
                # Satellites used is the honest signal-quality number; a fix
                # can persist on stale ephemeris long after the sky is lost.
                # gpsd emits SKY reports with no satellite list at all
                # (observed on a NEO-6M). Treating those as "0 satellites"
                # wipes the counts from the previous good report, which is
                # what made a receiver hearing 16 satellites read as 0/0.
                sats = getattr(report, "satellites", None)
                if not sats:
                    continue
                snrs = [getattr(s, "ss", 0) or 0 for s in sats]
                with self._lock:
                    self._sats_used = sum(
                        1 for s in sats if getattr(s, "used", False))
                    self._sats_visible = len(sats)
                    self._snr_max = max(snrs) if snrs else 0.0
                continue
            if kind != "TPV":
                continue

            s = GPSSample(
                lat=getattr(report, "lat", None),
                lon=getattr(report, "lon", None),
                alt=getattr(report, "alt", None),
                speed_gps=getattr(report, "speed", None),
                heading=getattr(report, "track", None),
                fix_mode=getattr(report, "mode", 0),
                gps_time=getattr(report, "time", None),
            )
            with self._lock:
                self._sample = s

    def latest(self) -> Dict[str, Optional[float]]:
        with self._lock:
            s = self._sample
            used, visible, snr = (self._sats_used, self._sats_visible,
                                  self._snr_max)
        return {
            "lat": s.lat,
            "lon": s.lon,
            "alt": s.alt,
            "speed_gps": s.speed_gps,
            "heading": s.heading,
            "fix_mode": s.fix_mode,
            "gps_sats_used": used,
            "gps_sats_visible": visible,
            "gps_snr_max": round(snr, 1),
            "gps_time": s.gps_time,
            # How old this fix is. Without it a row silently repeats the last
            # good position for as long as the receiver is blocked.
            "gps_age_s": round(time.monotonic() - s.t, 2),
        }

    def has_fix(self) -> bool:
        with self._lock:
            return self._sample.fix_mode >= 2

    def stop(self) -> None:
        self._stop.set()
