# The bench kit (P002)

A test kit with a screen, on the lab bench rather than in a participant's
car. It runs the **desktop** image, which a participant kit does not — see
[dashboard.md](dashboard.md) for why — so it is deliberately not
byte-comparable with one. Everything else about it is the same kit.

## Where P002 stands

| | |
|---|---|
| Image | Raspberry Pi OS **with desktop**, Trixie, Pi 4B |
| Repo | `~/obd-ev`, tracking `deploy`, self-updating every 5 min |
| Vehicle | `Chevrolet-Bolt-EUV` 2023 → polls 15 of 292 commands |
| Upload | rclone `obd-ev` → Box, test upload passed |
| Dashboard | installed as a session autostart; verified on screen |
| GPS / IMU | **not wired yet** — preflight fails on both, as it should |
| Touchscreen | not working yet, see below |

## 1. Put it on the lab WiFi

The participant setup portal is the wrong tool here: it is for a kit with no
screen, and it only speaks open/WPA2-PSK. Use the desktop's own network menu,
or, over SSH:

```bash
nmcli device wifi list
sudo nmcli device wifi connect "LAB-SSID" password "..."
```

**If the lab network is WPA2-Enterprise** (eduroam and most university
networks are), neither the menu's simple form nor the command above is
enough, and the portal cannot do it at all:

```bash
sudo nmcli connection add type wifi con-name lab ifname wlan0 ssid "LAB-SSID" \
  wifi-sec.key-mgmt wpa-eap 802-1x.eap peap 802-1x.phase2-auth mschapv2 \
  802-1x.identity "you@virginia.edu" 802-1x.password "..." \
  802-1x.ca-cert /etc/ssl/certs/ca-certificates.crt
sudo nmcli connection up lab
```

Check it stuck, and note the address so you can reach it:

```bash
nmcli -t -f NAME,DEVICE connection show --active
hostname -I
```

Keep the kit's own WiFi as the only radio in use: do not enable
`obd-ev-provision` on this kit (below), or it will take `wlan0` into AP mode
at boot and drop you.

## 2. Wire the sensors

Pin numbers are physical positions on the 40-pin header; the full version
with cautions is in [wiring.md](wiring.md).

| GPS | Pi pin | | IMU | Pi pin |
|---|---|---|---|---|
| GND | 6 | | GND | 9 |
| TX | 10 (GPIO15 / RXD) | | SDA | 3 (GPIO2) |
| RX | 8 (GPIO14 / TXD) | | SCL | 5 (GPIO3) |

GPS TX goes to the Pi's **RX**, and vice versa — the one that is easy to get
backwards and produces a device that exists and never speaks.

## 3. Reboot, then check each sensor on its own

`setup_pi.sh` wrote `enable_uart=1`, which needs a reboot before
`/dev/ttyS0` exists at all. Do that first, then take the sensors one at a
time rather than reading preflight as a single verdict:

```bash
sudo reboot

ls -l /dev/ttyS0                 # the UART node: reboot creates it
cgps -s                          # GPS: satellites, then a fix. Needs sky.
sudo i2cdetect -y 1              # IMU: expect 68 in the grid
./scripts/preflight.py           # everything, including a cloud round trip
```

A GPS fix indoors can take many minutes or never arrive; `cgps` showing
satellites with no fix means the wiring is right and the sky is not.

## 4. The OBD adapter, in the car

```bash
./scripts/obd_probe.py           # ignition off is fine for adapter checks
./scripts/obd_probe.py           # again in READY, for the vehicle commands
```

The 15 Bolt signals have never been requested from the car. Anything the
vehicle does not answer backs off after five attempts and leaves its column
blank — `journalctl -u obd-ev | grep "backing off"` names them. That list is
the real result of the first drive.

## 5. Start collecting

```bash
sudo systemctl start obd-ev
journalctl -u obd-ev -f                      # rows, trip start, OBD state
~/obd-ev/scripts/dashboard.py                # from a terminal ON the kit
```

The dashboard also starts by itself at login. Run it on the kit rather than
over SSH, or it has no Wayland socket to draw on; over SSH it needs
`XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0` in front.

## The touchscreen

The dashboard handles both mouse and finger events, so if touch does nothing
there, check whether it does anything **anywhere** — try dragging a window on
the desktop with your finger. If the desktop ignores touch too, it is not us.

Most HDMI touch panels carry video on HDMI and touch on a **separate USB
cable**, which is easy to leave out, and easy to lose when the panel moves to
its own power supply. That cable goes panel → Pi USB port; it carries data,
not the panel's power.

```bash
grep -i -A4 touch /proc/bus/input/devices    # is the panel there at all?
ls /dev/input/event*                         # and as an event device?
sudo apt-get install -y libinput-tools && sudo libinput list-devices | grep -i -A3 touch
```

No touch device listed means the USB lead or the panel; a device listed but
no response in the desktop is a compositor/calibration problem, and worth
trying `sudo libinput debug-events` to see whether events arrive at all.

## Two things to fix on this kit

**The card is nearly full** — 624 MB free of 7.2 GB, 91% used, after the
desktop image and packages. A logging kit with that little headroom stops
collecting within days. Either move to a bigger card or reclaim space
(`sudo apt-get clean`, remove desktop extras). Treat it as a blocker for
anything longer than a short test.

**The setup AP is still WPA2 and the portal is still armed.** Neither is
wanted on a bench kit:

```bash
sudo sed -i 's/^OBD_EV_AP_PASSWORD=.*/OBD_EV_AP_PASSWORD=/' /etc/default/obd-ev
sudo systemctl disable obd-ev-provision
```

## Known-good state to compare against

```bash
git -C ~/obd-ev log --oneline -1            # should match origin/deploy
systemctl is-active obd-ev obd-ev-upload.timer obd-ev-update.timer
systemctl list-timers 'obd-ev-*'            # update timer every 5 min
vcgencmd get_throttled                      # 0x0 on good power
df -h /
```

`0x50000` from `get_throttled` means undervoltage has occurred since boot —
on this kit that was the panel drawing from the Pi's USB before it had its
own supply. If it reappears with the panel independently powered, suspect
the Pi's own adapter.
