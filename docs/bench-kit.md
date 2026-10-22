# The bench kit (P002)

A test kit with a screen, driven around rather than left with a participant.
It runs the **desktop** image, which a participant kit does not — see
[dashboard.md](dashboard.md) — so it is deliberately not byte-comparable with
one. Everything else about it is the same kit.

| | |
|---|---|
| Image | Raspberry Pi OS **with desktop**, Trixie, Pi 4B |
| Repo | `~/obd-ev`, tracking `deploy`, self-updating every 5 min |
| Vehicle | `Chevrolet-Bolt-EUV` 2023 → polls 15 of 292 commands |
| Upload | rclone `obd-ev` → Box |
| Dashboard | session autostart; screen and touch both working |
| Networks | home WiFi (what it joins in practice), phone hotspot for driving, lab WiFi if added |

## Adding a WiFi network

This kit keeps several. In practice it joins the **home** network it was
set up on; the phone hotspot is for driving, and the lab network is only
there if you add it. Add them from the terminal rather than through the
setup portal — the portal is for a kit with
no screen, it only speaks open and WPA2-PSK, and arming it on this kit would
take `wlan0` into AP mode and drop your SSH.

```bash
nmcli device wifi list                          # see what is in range
sudo nmcli device wifi connect "SSID" password "..." name "friendly-name"
```

**Hidden network** (a participant's, often):

```bash
sudo nmcli connection add type wifi con-name "home" ifname wlan0 \
  ssid "SSID" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "..." \
  802-11-wireless.hidden yes
sudo nmcli connection up home
```

**WPA2-Enterprise** (eduroam and most university networks). Neither the
simple form above nor the portal can do this one:

```bash
sudo nmcli connection add type wifi con-name lab ifname wlan0 ssid "LAB-SSID" \
  wifi-sec.key-mgmt wpa-eap 802-1x.eap peap 802-1x.phase2-auth mschapv2 \
  802-1x.identity "you@virginia.edu" 802-1x.password "..." \
  802-1x.ca-cert /etc/ssl/certs/ca-certificates.crt
sudo nmcli connection up lab
```

**Set priorities, or the choice between them is arbitrary** when more than
one is in range. Higher wins:

```bash
sudo nmcli connection modify "home"    connection.autoconnect-priority 20
sudo nmcli connection modify "lab"     connection.autoconnect-priority 10
sudo nmcli connection modify "iPhone"  connection.autoconnect-priority 1
```

Put the network the kit actually lives on at the top. If it keeps choosing
one you did not expect, the priorities are the thing to look at first —
without them NetworkManager picks among whatever is in range, and the kit
can sit on a hotspot for days while a home network it also knows is two
metres away.

Check what the kit knows and what it is on now:

```bash
nmcli -f NAME,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY connection show
nmcli -t -f NAME,DEVICE connection show --active
hostname -I
```

Every one of these is a network the kit will rejoin by itself, which is what
makes uploads and self-updates happen without anyone thinking about it. The
flip side is that a network it has ever joined is one it may prefer later;
`sudo nmcli connection delete "name"` when a participant's network should no
longer be on a kit.

## Starting the services that are not running yet

`setup_kit.sh` **enables** the services but leaves `obd-ev` stopped when
preflight ends in a FAIL — which it does while the GPS and IMU are unwired.
Enabling only arms a unit for the next boot, so after a build the kit is
armed and idle. Start the lot:

```bash
sudo systemctl start obd-ev                  # the logger itself
sudo systemctl start obd-ev-upload.timer     # uploads, every minute
sudo systemctl start obd-ev-update.timer     # self-update, every 5 minutes
sudo systemctl start obd-ev-timesync.timer   # clock from GPS, no RTC on a Pi
sudo systemctl start gpsd                    # only once the GPS is wired
```

The dashboard is not a service on this kit: it is a session autostart
(`~/.config/autostart/obd-ev-dashboard.desktop`), so it comes up at login.
Start it now without logging out, from a terminal **on the kit**:

```bash
~/obd-ev/scripts/dashboard.py
```

Over SSH it has no Wayland socket of its own and needs
`XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0` in front of it.

Then confirm all of it is actually running, rather than merely enabled:

```bash
systemctl is-active obd-ev gpsd
systemctl list-timers 'obd-ev-*' --no-pager   # three timers, next elapse set
journalctl -u obd-ev -f                       # trip start, rows, OBD state
```

What each one should say on a kit with no sensors wired yet: `obd-ev` active
and writing rows; the update timer next elapsing within five minutes; the
logger reporting `NO OBD ADAPTER` and no GPS fix. Those are correct, not
faults.

**Deliberately not started**: `obd-ev-provision`. It raises the setup access
point on `wlan0`, which on this kit means losing the lab WiFi, the hotspot
and your SSH session at once:

```bash
sudo systemctl disable obd-ev-provision
```

## Wiring the sensors

Pin numbers are physical positions on the 40-pin header; the full version
with cautions is in [wiring.md](wiring.md).

| GPS | Pi pin | | IMU | Pi pin |
|---|---|---|---|---|
| VCC | **1** (3.3 V) | | VCC | **1** (3.3 V) |
| GND | 6 | | GND | 9 |
| TX | 10 (GPIO15 / RXD) | | SDA | 3 (GPIO2) |
| RX | 8 (GPIO14 / TXD) | | SCL | 5 (GPIO3) |

GPS TX goes to the Pi's **RX**, and vice versa — the one that is easy to get
backwards, and it produces a device that exists and never speaks. Both
boards take 3.3 V from pin 1, so they share it; not 5 V, the GY-521's logic
is 3.3 V.

`setup_pi.sh` wrote `enable_uart=1`, which needs a reboot before
`/dev/ttyS0` exists at all. After wiring, reboot and take the sensors one at
a time rather than reading preflight as a single verdict:

```bash
sudo reboot

ls -l /dev/ttyS0             # the UART node: the reboot creates it
cgps -s                      # GPS: satellites, then a fix. Needs sky.
sudo i2cdetect -y 1          # IMU: expect 68 in the grid
./scripts/preflight.py       # everything, including a cloud round trip
```

A fix indoors can take many minutes or never arrive. `cgps` showing
satellites but no fix means the wiring is right and the sky is not.

## The OBD adapter, in the car

```bash
./scripts/obd_probe.py       # ignition off is fine for the adapter checks
./scripts/obd_probe.py       # again in READY, for the vehicle commands
```

The 15 Bolt signals have never been requested from the car. Anything the
vehicle does not answer backs off after five attempts and leaves its column
blank; `journalctl -u obd-ev | grep "backing off"` names them. That list is
the real result of the first drive.

## Driving it around

Uploads and self-updates run over whatever network is in range: the home
WiFi while parked, the phone hotspot on the road. Raw trip parts are small — about 164 KB
each on P001 — but `upload.sh` also re-cleans and re-ships the whole archive
whenever a new part lands, and that bundle only grows (18 MB on P001 after a
month). On a hotspot that is the expensive part:

```bash
# cellular-friendly: ship raw parts while driving, clean at the lab
sudo sed -i 's/^OBD_EV_CLEAN=.*//' /etc/default/obd-ev
echo 'OBD_EV_CLEAN=0' | sudo tee -a /etc/default/obd-ev
```

Clean on your laptop from the Box copy instead: `scripts/clean_trips.py
~/Downloads/P002`.

## If the touchscreen stops responding

It works now. If it stops, the usual cause is the **separate USB touch
cable** — HDMI carries video only, and that lead is easy to lose when the
panel is moved or repowered.

```bash
grep -i -A4 touch /proc/bus/input/devices    # is the panel there at all?
```

Nothing listed means the USB lead or the panel. Listed but unresponsive is a
compositor or calibration problem; `sudo libinput debug-events` shows
whether events are arriving.

## One thing still outstanding

The card is **91% full — 624 MB free of 7.2 GB** after the desktop image and
packages. A logging kit with that little headroom stops collecting within
days, and it reads as random gaps in the data rather than as a disk problem.
Move to a bigger card, or reclaim space (`sudo apt-get clean`, remove
desktop extras) before any multi-day run.

## Known-good state to compare against

```bash
git -C ~/obd-ev log --oneline -1               # matches origin/deploy
systemctl is-active obd-ev gpsd
systemctl list-timers 'obd-ev-*' --no-pager    # update timer every 5 min
nmcli -t -f NAME,DEVICE connection show --active
df -h /
./scripts/preflight.py
```
