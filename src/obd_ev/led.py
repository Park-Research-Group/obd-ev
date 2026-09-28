"""Status LED, so you can tell what the kit is doing without a terminal.

Sitting in a car you cannot read a journal. The onboard ACT LED is enough to
answer the only question that matters at that moment -- did it reach the OBD
adapter -- provided the states are distinguishable at a glance:

    solid on            connected, and vehicle data is coming back
    fast blink  (0.15s) connected, but every command returns nothing
                        (usually the car is asleep -- an EV must be in READY)
    slow blink  (1.0s)  running, no OBD adapter
    off                 logger not running

The LED is driven through sysfs, which needs the trigger detached and the
brightness file made writable first -- scripts/led_prepare.sh does that as root
from the service's ExecStartPre. If any of that is missing this degrades to a
no-op rather than taking the logger down with it.
"""

import logging
import threading
from pathlib import Path
from typing import Optional

from .config import LedConfig

log = logging.getLogger(__name__)

OFF = "off"
SEARCHING = "searching"
CONNECTED_IDLE = "connected_idle"
CONNECTED = "connected"

# state -> (on_seconds, off_seconds); None means hold steady
_PATTERN = {
    OFF: (0.0, 1.0),
    SEARCHING: (1.0, 1.0),
    CONNECTED_IDLE: (0.15, 0.15),
    CONNECTED: (1.0, 0.0),
}


class LedIndicator:
    def __init__(self, cfg: LedConfig):
        self.cfg = cfg
        self.path = Path(cfg.path) / "brightness"
        self._state = OFF
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._warned = False
        self.available = False

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if not self.cfg.enabled:
            return
        if not self._write(0):
            log.info("status LED at %s is not writable; no LED output. "
                     "scripts/led_prepare.sh sets this up as root.", self.path)
            return
        self.available = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="led")
        self._thread.start()
        log.info("status LED on %s", self.cfg.path)

    def set_state(self, state: str) -> None:
        with self._lock:
            self._state = state

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self.available:
            self._write(0)

    # -- helpers ------------------------------------------------------------

    def _level(self, on: bool) -> int:
        lit = self.cfg.max_brightness if on else 0
        if self.cfg.invert:
            lit = 0 if on else self.cfg.max_brightness
        return lit

    def _write(self, value: int) -> bool:
        try:
            self.path.write_text(f"{value}\n")
            return True
        except OSError as exc:
            if not self._warned:
                log.debug("cannot write %s: %s", self.path, exc)
                self._warned = True
            return False

    def _run(self) -> None:
        phase_on = True
        while not self._stop.is_set():
            with self._lock:
                state = self._state
            on_s, off_s = _PATTERN.get(state, _PATTERN[SEARCHING])

            # A zero-length half-cycle means hold the other level steady, so
            # "solid on" and "off" need no special-casing in the loop.
            if off_s == 0.0:
                self._write(self._level(True))
                self._stop.wait(0.25)
                continue
            if on_s == 0.0:
                self._write(self._level(False))
                self._stop.wait(0.25)
                continue

            self._write(self._level(phase_on))
            self._stop.wait(on_s if phase_on else off_s)
            phase_on = not phase_on
