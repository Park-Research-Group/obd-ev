# obd-ev

Raspberry Pi data logger for EVs, built for handing out to study participants.
Reads the vehicle over a BLE ELM327 adapter, merges it with GPS and IMU, writes
one CSV per trip, and uploads whenever the participant parks at home.

## Hardware

| Component | Part | Interface |
|-----------|------|-----------|
| OBD scanner | BLE ELM327 (e.g. VEEPEAK) | Bluetooth LE |
| GPS | u-blox NEO-6M | UART, GPIO14/15 |
| IMU | MPU-6050 | I2C, GPIO2/3 |
| Host | Raspberry Pi 5 | 5V 5A supply |

Wiring: [docs/wiring.md](docs/wiring.md).

## Build a kit

One script per SD card. No master image.

```bash
# flash Raspberry Pi OS Lite (64-bit), boot, then:
git clone https://github.com/ethanmathias/obd-ev && cd obd-ev
./scripts/setup_kit.sh
```

10–15 minutes, mostly unattended. Details: [docs/deployment.md](docs/deployment.md).

## Vehicle profiles

With the make and model known, the kit reads that vehicle's manufacturer
parameters from an [OBDb](https://github.com/OBDb) signalset — pack current,
cell temperatures, true state of charge, per-wheel speed — instead of the
handful of generic Mode 01 values every car shares.

```bash
scripts/select_vehicle.py            # pick from a list, 's' to search 205 cars
scripts/build_index.py               # refresh the catalogue
```

Without a profile it falls back to 16 Mode 01 PIDs chosen for EV relevance,
probed against what the vehicle actually answers
([docs/pid_reference.md](docs/pid_reference.md)).

The decoder is checked against OBDb's own published test vectors: **191,994
signal values across 8 EV models, all exact**.

```bash
python -m unittest discover -s tests   # fast, offline
scripts/validate_obdb.py --all-evs     # full sweep
```

## The data

One folder per trip, uploaded under the kit's id:

```
obd-ev-uploads/P003/20260906_142201_a3f9c1d2/
    drive_P003_20260906_142201.csv     # parts, rotated every 15 min
    drive_P003_20260906_143701.csv
    signals_P003.csv                   # data dictionary for these files
```

Columns are named from the signalset (`lateral_acceleration_mps2`,
`state_of_charge_pct`). `signals.csv` maps each back to its OBDb id, unit and
category, so a 400-column file stays reviewable:

```python
sig  = pd.read_csv("signals_P003.csv")
trip = pd.read_csv("drive_P003_20260906_142201.csv")
core = sig.loc[sig.group.isin(["Meta","Movement","Orientation","GPS"]), "column"]
trip[core]                             # ~30 columns instead of 400
```

### Cleaning

The kit does this itself: after each upload run it cleans the trips it has
shipped and uploads the result to `<remote>/P003/P003_cleaned/`, so the cloud
folder already holds analysis-ready CSVs
([docs/deployment.md](docs/deployment.md)).

To run it by hand over a downloaded folder — the same script, no setup:

`scripts/clean_trips.py` turns a folder of raw uploads into one CSV per trip.
It is standalone — Python 3.9+ and no dependencies, no imports from this repo —
so it can be handed to a collaborator with a copy of the data and nothing else.
It only reads the input folder.

```bash
python3 scripts/clean_trips.py ~/Downloads/P001 --fill --merge
```

Output lands inside the input as `~/Downloads/P001/P001_cleaned`, so it
uploads alongside the raw data it came from; `-o` puts it anywhere else.

Trips in which the car never answered — bench and GPS-only runs, which in the
P001 data is 16 of 17 trips — are skipped, and listed in the report with the
reason. `--keep-without-obd` keeps them. A trip counts as having data when at
least one value came from a vehicle source in `signals.csv`, not when
`obd_connected` was set: the kit can hold an ELM327 link for a whole trip and
decode nothing from it.

It joins each trip's parts, orders rows by `t_mono`, drops duplicates, blanks
values outside the range `signals.csv` declares, and drops columns the trip
never filled. Two repairs matter most:

- **The clock.** The Pi has no RTC, so until GPS or NTP corrects it the wall
  clock is whatever it was at shutdown, and the correction lands *inside* the
  recording as a step — in the P001 data, trips are off by up to 25 hours and
  one clock steps 434 hours mid-trip. Trip folder names inherit the error.
  Timestamps are rebuilt from `gps_time` where the trip saw it, else from the
  Pi clock after its last correction; `timestamp_pi` and `clock_source` record
  what happened, and output files are named for the corrected start.
- **Sparseness** (`--fill`, opt-in). The scheduler polls commands round robin,
  so a row holds only what answered in that cycle — about 22 of 128 columns.
  Filling carries each signal forward, bounded by the rate it actually
  achieved, giving ~120 of 128. It is off by default because it repeats a
  value the car did not report.

`cleaning_report.txt` lists every change, and `trips_summary.csv` gives one row
per trip (duration, distance, OBD and GPS coverage, clock source) for deciding
which trips are worth opening.

### Polling interval

`obd_timing.csv` gives each signal's measured refresh interval per trip —
samples, median, p95, max, and how many times slower than the `period_s` its
profile asks for. Two different rates matter, and the summary carries both:

- **Row gap** (`obd_row_gap_*` in `trips_summary.csv`) — how often the kit
  hears *anything* from the car. About 1.07 s median on the P001 drives.
- **Per-signal interval** (`obd_timing.csv`) — how often any *one* signal
  refreshes. About 16.6 s median, because ~106 commands share one BLE link
  round robin.

Both are measured on `t_mono` and before any `--fill`, so neither a clock
correction nor a carried-forward value can distort them.

## Repo layout

```
src/obd_ev/
  main.py         sample loop, trip rotation      obdb.py     OBDb decoding
  obd_reader.py   connect/reconnect supervisor    vehicle.py  command scheduler
  ble_obd.py      BLE ELM327, Mode 01 PIDs        naming.py   column names
  gps_reader.py   gpsd                            logger.py   CSV writer
  imu_reader.py   MPU-6050                        config.py   YAML + env
  led.py          status LED (OBD link state)
  provision/      participant WiFi portal (AP, captive page, in-browser PBKDF2)
scripts/
  setup_kit.sh        build one SD card, start to finish
  select_vehicle.py   pick this kit's car        preflight.py   pre-ship checks
  sensors.py          live GPS + IMU readings
  fetch_signalset.py  vendor a profile           build_index.py catalogue
  upload.sh           rclone upload + clean      authorize_kit.sh  cloned cards
  self_update.sh      pull + apply, on connect   post_update.sh    apply an update
  obd_watch.py        live decoded OBD signals   bt_prepare.sh     BT state repair
  obd_probe.py        BLE adapter bench check    led_prepare.sh    LED handover
  clean_trips.py      offline data cleanup (standalone, no dependencies)
  gps_time_sync.py    set the clock from GPS
tests/            decoder, portal, logger, naming, cleaner, upload
vehicles/         vendored OBDb profiles + catalogue
```

## Docs

- Researchers building kits: [docs/deployment.md](docs/deployment.md)
- Participants receiving one: [docs/participant.md](docs/participant.md)

## Notes

- **Participant WiFi passwords are never stored.** The setup page derives the
  WPA2 PMK in the participant's browser and sends only that.
- **Acceleration.** The vehicle's own bus gives better lateral/longitudinal
  acceleration than the IMU where the profile exposes it — no gravity vector or
  mounting angle to correct.
- **Row rate** is set by BLE latency, not config. Expect 0.2–2 Hz.
- **GPS cold start** takes 30 s–2 min. Mount with sky view.

## License

MIT — see [LICENSE](LICENSE).
