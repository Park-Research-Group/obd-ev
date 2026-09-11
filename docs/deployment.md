# Deployment guide (researcher)

## Build a kit

One script per SD card. There is no master image — cloning one would copy a
single cloud credential onto every kit, and Box invalidates a token when
another device refreshes it, so all but the first kit silently stop uploading.

1. Flash **Raspberry Pi OS Lite (64-bit)** with `rpi-imager`. Set a username,
   enable SSH, configure your own WiFi.
2. ```bash
   ssh <user>@raspberrypi.local
   git clone https://github.com/ethanmathias/obd-ev && cd obd-ev
   ./scripts/setup_kit.sh
   ```

10–15 minutes, mostly unattended package installation. Safe to re-run — every
step is idempotent, so a failure means fix-and-rerun, not start over.

| Step | |
|---|---|
| 1 Kit details | kit id + setup-WiFi password → `/etc/default/obd-ev` |
| 2 Vehicle | pick from a list, `s` searches all 205 known cars |
| 3 System | apt, I2C/UART, virtualenv, gpsd, Bluetooth, systemd units |
| 4 Cloud | `rclone config`, then a real test upload |
| 5 Verify | `preflight.py`; non-zero exit if not shippable |
| 6 Label | prints the setup network name and password |

Label the kit with the setup network name and password. You never handle
participant WiFi credentials — they enter them on the device.

## Vehicle selection

```
  1) Chevrolet-Bolt-EUV               107 signals   107 commands  2022-2023
  2) Hyundai-IONIQ-5                  389 signals    34 commands  2021-2027
  0) (no profile)                  generic Mode 01, works on any OBD-II vehicle
  s) search all 205 known vehicles
```

Pick from the list rather than typing a name. A wrong name fails **silently**:
the kit falls back to generic Mode 01 and collects a fraction of the data with
nothing visibly broken. Choose `0` deliberately when the car isn't supported.

Change it later without rebuilding:

```bash
./scripts/select_vehicle.py && sudo systemctl restart obd-ev
```

Searching spans `vehicles/index.json`, the catalogue of every OBDb vehicle that
actually has data — 205 of 733 repos; the rest are empty placeholders. Picking
one that is not on the card downloads it, so that step needs network. Refresh
the catalogue when OBDb adds models:

```bash
./scripts/build_index.py
```

## Cloud upload

Step 4 runs `rclone config` on that card, so each kit gets its own credential
automatically. Name the remote exactly **`obd-ev`** and pick `box`. The Pi has
no browser — answer **n** to "Use auto config?" and run the command it prints
on your laptop.

Two Box caveats: refresh tokens expire after ~60 days unused (fine in normal
operation, but a kit unplugged for two months needs re-authorizing), and
re-imaging means re-authorizing. If either becomes painful, a **Box JWT app**
has no user token and no expiry, but needs UVA Box admin approval.

Moving off Box is one line in `/etc/default/obd-ev`; rclone abstracts the
target. A Google **service account** needs a Shared Drive, not a My Drive
folder — service accounts have no storage quota of their own. An **API key**
(`AIzaSy...`) cannot do this at all; it carries no identity.

If you clone a finished card, run `./scripts/authorize_kit.sh` on the copy to
give it its own credential.

## The setup portal is held off while you build

`obd-ev-provision.service` puts `wlan0` into AP mode, which would disconnect
your SSH session. `setup_kit.sh` creates `/var/lib/obd-ev/setup-in-progress`
first, and the unit refuses to start while it exists — even across a reboot.
The lock is released only on clean completion; a failed run keeps it, so an
unfinished kit can't lock you out.

```bash
sudo touch /var/lib/obd-ev/setup-in-progress   # hold the AP off
sudo rm /var/lib/obd-ev/setup-in-progress      # re-arm before shipping
```

If a kit has already seized `wlan0` and you still have a shell:

```bash
sudo systemctl stop obd-ev-provision
sudo nmcli connection delete obd-ev-setup
sudo touch /var/lib/obd-ev/setup-in-progress
```

## How participant onboarding works

On first boot the Pi raises `OBD-EV-Setup-<id>` on 10.42.0.1 with wildcard DNS,
so phones auto-open the setup page. The participant picks their network and
types the password; **the browser derives the WPA2 PMK**
(`PBKDF2-HMAC-SHA1(passphrase, ssid, 4096)`) and posts only that. The Pi saves
it as the network credential, drops the AP, and joins to verify. On success it
writes `/var/lib/obd-ev/provisioned.json` and never runs again; on failure the
AP returns so the password can be corrected.

The stored PMK cannot be reversed into the passphrase — that is the point, since
participants reuse passwords — but it is still a credential for that network, so
treat returned SD cards as sensitive. **WPA3-only networks can't be joined
from a PMK** (nmcli lists them as `WPA3` alone, versus `WPA2 WPA3` for the
common transition mode); the page detects this, warns on screen, and only then
falls back to sending the passphrase, which the Pi saves with `sae` key
management. Open networks need no password at all — tick "No password" for a
network entered by hand. `provisioned.json` records which method was used.

Re-provision a kit:

```bash
sudo rm /var/lib/obd-ev/provisioned.json
sudo nmcli connection delete obd-ev-home
sudo reboot
```

## Files and uploads

```
obd-ev-uploads/P003/20260906_142201_a3f9c1d2/
    drive_P003_20260906_142201.csv
    drive_P003_20260906_143701.csv     # part, after a 15-minute rotation
    signals_P003.csv                   # dictionary for these files
```

Trip folders are `<timestamp>_<boot id>`. The Pi has no RTC, so two power
cycles can report the same wall time; the boot id keeps them distinct. Sort by
the `timestamp` column, not by folder name.

`signals.csv` is written per trip, not per device — the schema changes if a
profile is updated or a kit reassigned, and a shared dictionary would start
lying about older trips.

A **new trip** starts on logger start and when the vehicle returns after
`trip_gap_seconds`. A **new part** starts every `rotate_minutes` while driving,
every `idle_rotate_minutes` (6 h) while parked, and when a trip goes quiet. On
most cars power dies with the ignition, so one trip is one power cycle. The
logger records its open file in `logs/.current`; `upload.sh` re-checks it
before every file it touches, but only while the service is running — after an
unclean shutdown that file is complete and uploads normally. Folder and file
names are UTC, like the `timestamp` column.

Uploads fire on the NetworkManager dispatcher hook the moment the Pi joins a
network, with a one-minute timer as backstop. Shipped trips move to
`logs/uploaded/<trip>/`, pruned to the most recent 64.

## Remote updates

Kits can pull and apply updates themselves when they next have internet, which
is how you fix something on a kit sitting in a participant's driveway.

**Off by default.** `setup_kit.sh` writes `OBD_EV_UPDATE_BRANCH=deploy` and a
commented-out `OBD_EV_AUTO_UPDATE=1` into `/etc/default/obd-ev`; enable per
kit by uncommenting it:

```sh
OBD_EV_AUTO_UPDATE=1
OBD_EV_UPDATE_BRANCH=deploy        # default: whatever branch the kit is on
OBD_EV_UPDATE_MIN_INTERVAL=3600    # seconds between checks (counted from the
                                   # last check that actually reached GitHub)
```

A kit built before the update hooks existed needs them installed once by hand
(`sudo ./scripts/post_update.sh`); after that, updates install their own.

> **Understand the blast radius before enabling this.** Anyone who can push to
> that branch gets root on every kit running it, including ones in
> participants' vehicles. Point it at a branch you promote to deliberately —
> `deploy`, not `main` — so a work-in-progress commit cannot reach the fleet.

### Release workflow

`main` is where you work; `deploy` is what the kits run. Nothing reaches a kit
until you merge:

```bash
git checkout deploy
git merge main            # or: git merge main --ff-only
git push origin deploy
git checkout main
```

Kits pick that up the next time they have internet and are not mid-drive. A
kit built from `main` moves itself onto `deploy` on its first update and
fast-forwards from then on.

To see what the fleet is running versus what you have staged:

```bash
git log --oneline deploy..main      # merged into main, not yet released
```

A NetworkManager hook fires `obd-ev-update.service` on connect. `self_update.sh`
then:

1. exits immediately unless `OBD_EV_AUTO_UPDATE=1`
2. rate-limits to `OBD_EV_UPDATE_MIN_INTERVAL` (wifi flaps a lot)
3. **defers while a trip is running** — `obd_connected=1` in the live CSV means
   restarting the logger would lose the drive
4. refuses if the working tree is dirty, or if the update is not a fast-forward
5. pulls, runs `post_update.sh`, restarts the logger
6. runs preflight, and **rolls back to the previous commit** if it fails

`post_update.sh` does the parts a plain `git pull` cannot: reinstall systemd
units (they are copies in `/etc/systemd/system`, which is the usual reason an
update seems not to apply), refresh pip dependencies if `requirements.txt`
changed, reinstall the dispatcher hooks, and fix up Bluetooth. It deliberately
does not run apt — that is `setup_pi.sh`'s job.

### Updating an existing kit by hand (P001)

P001 was built from `main` before the update machinery existed, so it has
neither the dispatcher hook nor the `deploy` branch. Do this once, over SSH on
the kit's home WiFi (hostname `linklab01`):

```bash
ssh <user>@linklab01.local
cd ~/obd-ev

# 1. Move onto the release branch and take the update.
git fetch origin
git checkout deploy          # first time; afterwards `git pull` is enough
git pull --ff-only

# 2. Apply the parts a pull cannot: systemd units, pip deps, the update and
#    upload hooks, Bluetooth. Then restart the logger on the new code.
sudo ./scripts/post_update.sh
sudo systemctl restart obd-ev

# 3. Check it came up clean.
./scripts/preflight.py --quick
journalctl -u obd-ev -n 30 --no-pager

# 4. Optional: let it update itself from `deploy` from now on.
sudo sed -i 's/^#OBD_EV_AUTO_UPDATE=1/OBD_EV_AUTO_UPDATE=1/' /etc/default/obd-ev
grep -q '^OBD_EV_UPDATE_BRANCH=' /etc/default/obd-ev \
    || echo 'OBD_EV_UPDATE_BRANCH=deploy' | sudo tee -a /etc/default/obd-ev
```

If `git checkout deploy` refuses because of local edits, `git stash` first
(and `git stash drop` once you are sure nothing in it matters). `git pull`
will fail with "not a fast-forward" only if `deploy` was rewritten; that is a
sign to stop and look, not to force it.

Then, in the car, prove the adapter path — it has never been exercised on
this kit:

```bash
./scripts/obd_probe.py           # ignition off is fine for the adapter checks
./scripts/obd_probe.py           # again in READY, for the vehicle commands
```

Apply a later update by hand on any kit you can reach:

```bash
cd ~/obd-ev && git pull
sudo ./scripts/post_update.sh
sudo systemctl restart obd-ev
```

Or force a full self-update cycle regardless of the switches:

```bash
sudo ./scripts/self_update.sh --force
journalctl -u obd-ev-update --no-pager -n 30
```

## Field checks

```bash
./scripts/preflight.py            # everything, exits non-zero on failure
./scripts/sensors.py              # live GPS + IMU        --gps / --imu / --once
./scripts/obd_probe.py            # BLE adapter: scan, connect, handshake, first commands
./scripts/obd_watch.py            # live decoded vehicle signals    --all / --once
journalctl -u obd-ev -f           # connection and trip events
sudo systemctl start obd-ev-upload
```

### The OBD adapter, before the first drive

Run `obd_probe.py` in the parked car (the OBD port is powered with the ignition
off, so the adapter checks work; the vehicle commands need READY). It stops
`obd-ev` for the duration, scans, connects with the same code the logger uses,
prints `ATI` / `ATRV` / the protocol, then tries the profile's first commands.

```
  looking for name containing 'VEEPEAK'
   -62 dBm  AA:BB:CC:DD:EE:FF  VEEPEAK                  fff0   <-- matches obd.ble_name
  PASS  adapter advertising as 'VEEPEAK' at AA:BB:CC:DD:EE:FF
  NOTE  pin it in config.yaml:  obd.ble_address: AA:BB:CC:DD:EE:FF
  PASS  connected in 3.2s; write=0000fff2-... notify=0000fff1-...
  PASS  adapter identifies as: ELM327 v1.5
  PASS  battery voltage: 12.6V
  PASS  5 signals decoded from 5 commands
```

The adapter must be a **BLE** model: Veepeak **OBDCheck BLE** or **BLE+**
(both advertise as `VEEPEAK` on service `FFF0`). The OBDCheck BLE is dual-mode
— classic Bluetooth for Android, LE for iOS — which is why the kit forces the
controller to LE-only; a classic-only model (Veepeak Mini, VP11) never appears
in a BLE scan and cannot work with this logger. `--scan` alone lists what the
Pi can see.

`sensors.py` and `obd_watch.py` both use the same code the logger uses, so a
clean run means the logging path works, not merely that something is on the bus.

```
23:12:53  GPS  fix=2D   sats=3/13 snr=26.0  38.034426, -78.510212  alt 397.9m  speed 0.28 m/s  age 0.19s
23:12:53  IMU  accel   1.01   8.17   4.80 m/s2   gyro  -0.74   0.27  -0.74 deg/s   |a|max  9.57  n=58
```

- `sats=3/13` is **used/visible**. `0/13` means the antenna hears plenty but has
  locked nothing; `0/0` means it hears nothing at all.
- `snr` is the strongest signal. Roughly **30+** is needed to use a satellite.
- A fix with 0 used satellites is tagged `WEAK` and should not be trusted — you
  will see the position drift and a non-zero speed while stationary.
- `n=` is IMU samples since the previous line. If it stays 0 the sensor is not
  responding. Resting magnitude should be ~9.8 m/s² (gravity).

`obd_watch.py` reads the trip CSV rather than opening its own BLE connection,
because only one connection to the adapter is allowed — a tool that grabbed it
would force you to stop the logger and stop testing the real thing.

Lower level, if you need it: `cgps -s` or `gpspipe -w` for GPS, and
`i2cdetect -y 1` (in `/usr/sbin`) to confirm the IMU answers at `0x68`.

## When something does not work

Every entry here is a failure seen on real hardware.

| Symptom | Cause and fix |
|---|---|
| `br-connection-profile-unavailable` or `br-connection-create-socket` | BlueZ tried **classic** Bluetooth for a BLE adapter. `Device1.Connect()` is transport-agnostic and picks BR/EDR for an address it holds classic info about. Fix: `ControllerMode = le` in `/etc/bluetooth/main.conf` — `setup_pi.sh` sets it, and `bt_prepare.sh` re-applies it on every service start. The logger also self-heals by running `bluetoothctl remove` and retrying over LE. |
| `No powered Bluetooth adapters found` | Radio is rfkill soft-blocked. `sudo rfkill unblock bluetooth`. The `bluetooth` service looks perfectly healthy in this state, which is why preflight checks rfkill separately. |
| `BLE OBD adapter not found` | The dongle does not advertise a name containing `obd.ble_name` (default `VEEPEAK`). Scan for the real name, then pin `obd.ble_address`. |
| Connects, then `every command went unanswered; resetting` | The vehicle is not awake. An EV must be in **READY**, not accessory mode, or the HV systems will not answer diagnostics. |
| Repeated `backing off command …` | Those PIDs are not answering on this trim, or the car was not in READY yet. Backed-off commands are retried every `retry_disabled_after` (5 min) and come back the moment they answer; columns stay blank meanwhile. |
| Adapter connects but nothing decodes, `obd_probe.py` shows `NO DATA` in READY | Wrong profile for the car, or the adapter is in an unexpected mode. `obd_probe.py --no-vehicle` then `journalctl -u obd-ev` with `-v` shows the raw frames. |
| GPS device missing (`/dev/ttyS0` or `/dev/ttyAMA0`) | Wrong device for the board. Pi 4 uses `/dev/ttyS0` (mini UART; the PL011 is Bluetooth). Pi 5 uses `/dev/ttyAMA0` and needs `dtparam=uart0=on` plus a reboot. `setup_pi.sh` picks the right one — see [wiring.md](wiring.md). |
| gpsd running but never a fix | Needs sky view. Cold start is 30 s–2 min. Check `snr` in `sensors.py`. |
| `no I2C device at 0x68` | IMU wiring — SDA pin 3, SCL pin 5. Note many boards sold as MPU-6050 are actually MPU-6500 (`WHO_AM_I` returns `0x70`); the driver works either way. |
| Uploads stop weeks in | Two kits sharing one cloud token. Each card built by `setup_kit.sh` gets its own; a *cloned* card needs `authorize_kit.sh`. Compare `token_fingerprint` in `/var/lib/obd-ev/upload-authorized.json` across kits. |
| Participant gets no setup page | `/var/lib/obd-ev/provisioned.json` still exists from your testing. Remove it before shipping. |
| Kit stops self-updating | `not a fast-forward` — `deploy` diverged from `main`. Treat `deploy` as merge-only. |

## When a kit comes back

```bash
cd ~/obd-ev/logs/uploaded && rm -rf */       # already in the cloud
sudo nmcli connection delete obd-ev-home     # forget their network
sudo rm -f /var/lib/obd-ev/provisioned.json /var/lib/obd-ev/upload-authorized.json
sudo journalctl --vacuum-time=1d
```

Then re-flash and rebuild for the next participant.

