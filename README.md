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

## Repo layout

```
src/obd_ev/
  main.py         sample loop, trip rotation      obdb.py     OBDb decoding
  obd_reader.py   connect/reconnect supervisor    vehicle.py  command scheduler
  ble_obd.py      BLE ELM327, Mode 01 PIDs        naming.py   column names
  gps_reader.py   gpsd                            logger.py   CSV writer
  imu_reader.py   MPU-6050                        config.py   YAML + env
  provision/      participant WiFi portal (AP, captive page, in-browser PBKDF2)
scripts/
  setup_kit.sh        build one SD card, start to finish
  select_vehicle.py   pick this kit's car        preflight.py   pre-ship checks
  sensors.py          live GPS + IMU readings
  fetch_signalset.py  vendor a profile           build_index.py catalogue
  upload.sh           rclone upload              authorize_kit.sh  cloned cards
  self_update.sh      pull + apply, on connect   post_update.sh    apply an update
  obd_watch.py        live decoded OBD signals   bt_prepare.sh     BT state repair
  obd_probe.py        BLE adapter bench check
tests/            decoder, portal, logger, naming
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
