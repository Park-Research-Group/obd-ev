# Test session with a participant (P002)

For a researcher-present session with the bench kit, where you set the
device up in their car and leave it collecting. P002 runs the desktop image
and has the setup portal **disabled**, so the participant never sees the
`OBD-EV-Setup-…` network described in [participant.md](participant.md) —
you put it on their WiFi yourself. Everything else behaves like a
participant kit.

## Before you leave

```bash
ssh linklab@P002.local
cd ~/obd-ev && git log --oneline -1          # matches origin/deploy
./scripts/preflight.py                       # full, includes a test upload
df -h /                                      # free space for the session
systemctl is-active obd-ev
```

Take: the OBD-II adapter, the car USB-C supply, the panel's own power
supply if the screen is coming, and the HDMI + USB-touch leads.

Expect preflight to fail on GPS and IMU until they are wired. That is fine
and no longer blocks updates, but it does mean `setup_kit.sh` would call the
kit "not ready" — ignore that wording for a test session.

## 1. Their WiFi

The kit needs a network it will rejoin by itself, or nothing uploads. Ask
for the network name and password, and add it over SSH (tether to your
phone, or use the desktop's own network menu on the kit's screen).

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

Make it the kit's first choice, above your hotspot, using the names exactly
as `nmcli connection show` prints them (on this kit they come from netplan
and look like `netplan-wlan0-iphone`, not the friendly names):

```bash
nmcli connection show
sudo nmcli connection modify "home" connection.autoconnect-priority 20
sudo nmcli connection modify "netplan-wlan0-iphone" connection.autoconnect-priority 1
```

Confirm it is actually on their network, and that uploads work from it:

```bash
nmcli -t -f NAME,DEVICE connection show --active
sudo systemctl start obd-ev-upload.service && journalctl -u obd-ev-upload -n 10 --no-pager
```

**Do not enable `obd-ev-provision` on this kit.** It puts the radio into
access-point mode at boot, which drops their WiFi and your SSH together.

## 2. In the car

1. OBD-II adapter into the port under the dash, driver's side.
2. Pi powered from the car USB-C supply.
3. Screen, if it is coming: HDMI to the socket **nearest the USB-C power
   connector**, its own power supply, and the USB touch lead to the Pi.
4. Ignition to READY, then check the adapter can be reached:

```bash
./scripts/obd_probe.py        # ignition off is fine for adapter checks
./scripts/obd_probe.py        # again in READY, for the vehicle commands
```

## 3. Start it

```bash
sudo systemctl start obd-ev
sudo systemctl start obd-ev-upload.timer obd-ev-update.timer obd-ev-timesync.timer
sudo systemctl start gpsd                     # only if the GPS is wired
```

Then watch one minute of real output before you walk away:

```bash
journalctl -u obd-ev -f
```

Good looks like: a trip starting, rows accumulating, and — in READY, with
the adapter connected — vehicle values rather than `NO OBD ADAPTER`. Any
command the car refuses backs off after five tries and is named in the log
(`grep "backing off"`); those columns stay blank and nothing else suffers.

The screen shows the same thing. It starts by itself at login, or:

```bash
~/obd-ev/scripts/dashboard.py                 # from a terminal on the kit
```

## 4. What to tell the participant

- The device records how the car is driven — speed, acceleration, battery
  and cabin heating/cooling — plus GPS position, continuously while the car
  is on.
- It needs their WiFi only to upload; it keeps recording without it and
  catches up later.
- **You never asked for their WiFi password to keep it**: it goes to the
  device, not to you, and is not stored in the data.
- Nothing is recorded inside the cabin — no audio, no video.
- If the device is unplugged, nothing breaks; plug it back in.
- They should contact you, not touch the device, if something looks wrong.

Leave them your contact details and the kit id (**P002**).

## 5. During and after

Check from anywhere the kit can reach you:

```bash
ssh linklab@P002.local 'systemctl is-active obd-ev; ls -t ~/obd-ev/logs/*/ | head -3'
```

Data lands in Box under `obd-ev-uploads/P002/<trip>/`. To look at it:

```bash
scripts/clean_trips.py ~/Downloads/P002
```

That produces one CSV per trip with the clock repaired from GPS, bad decodes
blanked, and empty columns dropped, plus a summary and a report of what it
changed.

At the end of the session: stop the logger, collect the hardware, and take
their network off the kit so it does not rejoin it later.

```bash
sudo systemctl stop obd-ev
sudo nmcli connection delete "home"
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
| Card filling up | `df -h /` — collection stops when it is full |

The kit updates itself from the `deploy` branch every five minutes whenever
it has internet, so a fix pushed from the lab reaches the car without
touching it.
