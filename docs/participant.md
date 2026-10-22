# Participant quick-start

## In the box

A small computer (Raspberry Pi), a Bluetooth OBD-II scanner, a USB-C car power
adapter, and GPS/motion sensors already wired in. The device has a label with
its **setup network name** — you'll need it once.

## Setup — about a minute, once

Do this at home, where the device can reach your WiFi.

1. **Plug the device into your car's USB-C power** and wait about a minute.
2. On your phone, **join the WiFi network `OBD-EV-Setup-…`** named on the
   label. It has no password.
3. A setup page should open by itself. If not, go to **http://10.42.0.1**
4. **Choose your home WiFi**, type your WiFi password, tap **Connect**.
5. The setup network disappears — that's what should happen. If nothing else
   happens, you're done.

Mistyped your home WiFi password? The `OBD-EV-Setup-…` network comes back
after about a minute. Rejoin it and try again.

### Your WiFi password is not stored

It is scrambled on your phone into a code that works only for your one network
and cannot be turned back into your password. Only that code reaches the
device, so it can't be read off the device even if it were lost. That matters
most if you use the same password elsewhere.

The exception: a few newer "WPA3-only" networks can't be joined this way. The
setup page tells you on screen before you continue, so the choice is yours.

## Using it

1. Plug the OBD-II scanner into the port under your dashboard, usually below
   the steering wheel.
2. Plug the device into the car's power outlet.
3. Drive normally. Nothing to press.
4. When you park at home it uploads by itself. Leaving it powered a few minutes
   after parking gives it time to finish.

Leave everything plugged in between drives.

## Troubleshooting

| | |
|---|---|
| No `OBD-EV-Setup-…` network | It only appears until setup is done. If you haven't set up yet, unplug for ten seconds, replug, check again after a minute. |
| Setup page won't open | Confirm you're joined to `OBD-EV-Setup-…`, then go to http://10.42.0.1 directly. Turning mobile data off briefly can help. |
| Is it working? | There are no lights or beeps. Just drive normally. |
| Changed WiFi password, or moved | Contact the researcher — the device needs putting back into setup mode. |

## Privacy

The device records vehicle data (speed, battery state, pedal and steering) and
**GPS location**, so your routes and where you park — including home — are part
of the recorded data. It does not record audio, video, or anything from your
phone. Your WiFi password is never recorded in any readable form.

Ask the researcher for the study's consent documentation for how location data
is stored, retained and shared.
