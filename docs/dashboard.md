# Touchscreen dashboard

A full-screen status display for the official Raspberry Pi 7" touchscreen, so
a kit can be debugged from the driver's seat instead of over SSH with a laptop
on the passenger seat. **Test kits only** — it is installed by hand on the
bench kit (P002) and reaches no participant build.

```
P002                                             LOGGING
10.00 rows/s   row 1482   …_abc12345/drive_P002_20261015_142201.csv
OBD 0.1s   GPS 3D 9sat   IMU 0.0s   DISK 21.4G   PENDING 0
───────────────────────────────────────────────────────────────────
VEHICLE · answering                                           1/2
  State of charge                        61.26   %        0.1s
  HV pack current                        -28.3   A        0.1s
  HV pack voltage                        352.4   V        0.1s
  Front left wheel speed                 48.25   km/h     0.1s
  Cabin temperature                       22.5   C        1.0s
```

## No driver to write

The 7" DSI panel and its touch controller are supported by the kernel already;
plugging it into the DSI port and powering it is the whole hardware step. What
this adds is the program that draws on it, which renders through SDL on
KMS/DRM — no desktop, no X, no Wayland, nothing else installed.

## It cannot affect data collection

That is the point of the design, not a hope:

* **It never opens the hardware.** BLE allows one connection at a time, so a
  display that opened its own would have to stop `obd-ev` first — and would
  then be showing something other than what the kit records. Everything on
  screen is read out of the trip CSV the logger is already writing, plus the
  `.current` marker the uploader also reads.
* **It only reads.** No file under the log directory is opened for writing,
  created or removed, so neither the logger's rotation nor the uploader's scan
  sees anything new. The service enforces this with `ProtectSystem=strict`: the
  filesystem is mounted read-only for that process, so a bug in it cannot
  truncate a trip, clear `.current` or leave a stray `.flush-request`.
* **It reads incrementally.** Only the bytes appended since the last redraw are
  parsed, from a saved offset, so the cost is flat whether the part is one
  minute or fifteen minutes old.
* **It loses every contest.** Separate process, separate service, `Nice=10`,
  `CPUWeight=20`, idle I/O priority. Twice a second it reads a few kilobytes
  and draws a screen. If it crashes, the logger does not notice.

Ages come from the `t_mono` column, not the wall clock: both processes share
CLOCK_MONOTONIC, so the display stays correct across the clock correction GPS
applies at boot, where `timestamp` jumps by hours.

## Install

On the test kit, with the repo already set up:

```bash
./scripts/install_dashboard.sh
```

It installs `python3-pygame`, adds the service user to `video`, `render` and
`input`, and enables `obd-ev-dashboard.service`. To take it off again —
leaving the kit byte-for-byte as a participant kit:

```bash
./scripts/install_dashboard.sh --remove
```

Nothing else installs it. The unit template lives in `systemd/optional/`, which
neither `setup_pi.sh` nor `post_update.sh` globs, so a participant kit never
receives it. `post_update.sh` does refresh the unit on a kit that *already* has
it, so the test kit does not drift from the repo after an update.

## Reading it

**Header.** The kit id and one of five states:

| State | Meaning |
|-------|---------|
| `LOGGING` | rows are being written and the car is answering |
| `CAR NOT ANSWERING` | adapter connected, no vehicle signal for 15s — an EV has to be in READY |
| `NO OBD ADAPTER` | no BLE link; try `scripts/obd_probe.py` |
| `NO ROWS FOR Ns` | the logger is not writing — `systemctl status obd-ev` |
| `WAITING FOR LOGGER` | no trip CSV at all yet |

Then the row rate and row count, the trip folder and open part, and a fact
line: age of the last OBD-connected row, GPS fix and satellites, IMU sample
age, free space, and parts waiting to upload. Each is green, amber or red.

**Body.** One line per signal: name, value, unit, and **how long ago that
column last carried a value**. Green under 3s, amber under 15s, red beyond —
which is how you spot the one signal that quietly stopped answering while the
rest of the trip looks healthy. Four views, cycled by tapping the top strip:

| View | Shows |
|------|-------|
| `VEHICLE · answering` | vehicle signals that have returned a value |
| `VEHICLE · all polled` | every signal in the profile, including silent ones |
| `GPS` | fix, position, speed, satellites |
| `IMU` | acceleration, rotation, sample age |

`all polled` is the one to look at when a signal is missing: a row with a `-`
age is being requested and never answered, which is a different problem from a
signal that is not in the profile at all.

**Touch.** Top strip changes view; left and right quarters page.

## The same thing over SSH

The script falls back to text when there is no screen, pygame is missing, or
something else holds DRM master — so it is also the quickest remote check:

```bash
scripts/dashboard.py --backend text    # refreshing, arrows to page, q to quit
scripts/dashboard.py --once            # one snapshot, scriptable
```

`scripts/obd_watch.py` remains the plain list of vehicle signals;
`scripts/dashboard.py` shows the whole kit, with ages. The two share their
trip lookup and unit labels, so they cannot disagree.

## When it does not come up

The logger is unaffected either way — debug it at leisure.

```bash
sudo journalctl -u obd-ev-dashboard -n 30     # sudo: the kit user is not in adm
```

**First, decide whether the fault is ours at all.** This drives
DRM → GBM → EGL → GLES with no SDL in the picture:

```bash
sudo apt-get install -y kmscube && sudo kmscube
```

A spinning cube means the display stack is fine and the problem is in SDL,
pygame or the unit. No cube means it is below us, and nothing in this repo
will fix it.

* **`pygame.error: EGL not initialized`** — Pi OS Lite ships the kernel side
  of KMS and none of the Mesa userspace behind it, so `/dev/dri` looks
  healthy while EGL has nothing to initialize. `sudo apt-get install -y
  libgl1-mesa-dri libegl1 libgles2 libgbm1`. The installer does this now;
  kits built before it did, do not have it.
* `No module named 'pygame'` — the venv is built with `--system-site-packages`,
  so `sudo apt install python3-pygame` is usually the fix; the installer then
  falls back to pip.
* **`undefined symbol: _udev_device_get_action`** in `libSDL2` — the distro
  SDL2 is mismatched against the system libudev. Go round it with the pip
  wheel, which bundles its own SDL2:
  `./.venv/bin/python -m pip install --force-reinstall pygame`.
* No display, no error — the service is running the text renderer into the
  journal. It passes `--backend pygame` precisely so that cannot happen
  silently; if you see it, something is launching it with `--backend auto`.
* Starts, then exits — if `ProtectSystem=strict` is the cause it shows as a
  read-only filesystem error. Comment it out of the unit and report what wrote.
* Console text showing through — `sudo systemctl disable --now getty@tty1`.
