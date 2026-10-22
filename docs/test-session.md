# Test session with a participant (P002)

Short version: add their WiFi credentials, wire the sensors, start it,
watch a minute of output. P002 has the setup portal disabled, so the
participant never sees the `OBD-EV-Setup-…` network from
[participant.md](participant.md) — you add their network yourself.

## 1. Their WiFi

All the kit needs is a network it will rejoin by itself, or nothing
uploads. Ask for the name and password:

```bash
nmcli device wifi list
sudo nmcli device wifi connect "THEIR-SSID" password "..." name "home"
```

Hidden network:

```bash
sudo nmcli connection add type wifi con-name "home" ifname wlan0 \
  ssid "THEIR-SSID" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "..." \
  802-11-wireless.hidden yes
sudo nmcli connection up home
```

**Not in range of their network yet?** Add it anyway — use the `connection
add` form above, which saves the profile without needing the network to be
present. The kit joins it the first time it sees it.

Make it the kit's first choice over your hotspot, using the names exactly
as `nmcli connection show` prints them (this kit's come from netplan, so
they read like `netplan-wlan0-iphone`):

```bash
nmcli connection show
sudo nmcli connection modify "home" connection.autoconnect-priority 20
sudo nmcli connection modify "netplan-wlan0-iphone" connection.autoconnect-priority 1
```

Check what it is on now, and that uploads work from there:

```bash
nmcli -t -f NAME,DEVICE connection show --active
sudo systemctl start obd-ev-upload.service && journalctl -u obd-ev-upload -n 10 --no-pager
```

## 2. Wiring

Physical pin positions on the 40-pin header. The OBD adapter is BLE — no
wiring, just plug it into the port under the dash, driver's side.

| GPS (GY-NEO6MV2) | Pi pin | | IMU (GY-521) | Pi pin |
|---|---|---|---|---|
| VCC | **1** (3.3 V) | | VCC | **1** (3.3 V) |
| GND | 6 | | GND | 9 |
| TX | 10 (GPIO15 / RXD) | | SDA | 3 (GPIO2) |
| RX | 8 (GPIO14 / TXD) | | SCL | 5 (GPIO3) |

* **GPS TX goes to the Pi's RX**, and RX to TX. Straight through gives a
  device that exists and never speaks.
* Both boards take **3.3 V from pin 1** and share it — not 5 V.
* Screen, if it comes: HDMI in the socket nearest the USB-C power
  connector, its own power supply, and the USB lead for touch.

Reboot once after wiring — `/dev/ttyS0` for the GPS only appears at boot.

## 3. Start it

```bash
sudo systemctl start obd-ev
sudo systemctl start obd-ev-upload.timer obd-ev-update.timer obd-ev-timesync.timer
sudo systemctl start gpsd                 # only if the GPS is wired
journalctl -u obd-ev -f                   # watch a minute before walking away
```

Good looks like a trip starting, rows accumulating, and — ignition in READY
with the adapter connected — vehicle values instead of `NO OBD ADAPTER`.
Commands the car refuses back off after five tries and are named in the log
(`grep "backing off"`); those columns stay blank and nothing else suffers.

The screen shows the same thing and starts at login, or run
`~/obd-ev/scripts/dashboard.py` from a terminal on the kit.

## 4. What to tell them

- It records how the car is driven — speed, acceleration, battery, cabin
  heating and cooling — plus GPS position, while the car is on.
- WiFi is only for uploading; it keeps recording without it and catches up.
- Their WiFi password goes to the device, not to you, and is not in the data.
- Nothing is recorded inside the cabin — no audio, no video.
- Unplugging it breaks nothing; plug it back in.
- Contact you rather than touching it if something looks wrong.

Leave them your contact details and the kit id (**P002**).

## 5. Afterwards

Data lands in Box under `obd-ev-uploads/P002/<trip>/`. On the kit the CSVs
are in `~/obd-ev/logs/<trip>/`, with `signals_P002.csv` as the data
dictionary; uploaded trips move to `~/obd-ev/logs/uploaded/`.

```bash
scripts/clean_trips.py ~/Downloads/P002      # one tidy CSV per trip
sudo nmcli connection delete "home"          # take their network off the kit
```

## If something is wrong

| Symptom | First thing to check |
|---|---|
| No rows at all | `systemctl status obd-ev`, then `journalctl -u obd-ev -n 50` |
| `NO OBD ADAPTER` | adapter seated, ignition in READY, `./scripts/obd_probe.py` |
| Vehicle columns blank | `journalctl -u obd-ev \| grep "backing off"` names what the car refused |
| No GPS fix | `cgps -s` — satellites but no fix means wiring is right, sky is not |
| Nothing uploading | `nmcli -t -f NAME,DEVICE connection show --active`, then `journalctl -u obd-ev-upload -n 20` |
| Screen blank | the panel's own power, and HDMI in the socket nearest USB-C |
| Kit not taking updates | `git -C ~/obd-ev log --oneline -1` against `origin/deploy` |
| Card filling up | `df -h /` — collection stops when it is full |
