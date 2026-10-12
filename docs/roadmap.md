# Roadmap

## 1. DDP spike — done (2026-10-12)

* Layout loaded from the xLights show folder matches the FPP port configs.
* Live chase and `.fseq` playback sent to both remotes (.45, .239) in remote
  mode; packets received with no errors after fixing per-packet sequence numbers.

## 2. Player MVP

* Long-running service: scheduler (replace xSchedule's nightly 16:50–23:15),
  playlist of `.fseq` files and live effects.
* Steady frame timing on Windows (or move to a Pi 4/5 that replaces the dead .64).
* Live parameter changes (palette, speed, effect) through an HTTP/WebSocket API.
* Optionally still send FPP MultiSync so remotes can play local `.fseq` copies.

## 3. Camera pixel mapping and image overlay

* Fixed camera photographs the house while the player flashes binary-coded
  on/off patterns (~12–15 patterns + all-on/all-off references).
* Decode each pixel's (x, y) in the photo; store a pixel map.
* Effects sample an image, gradient, video or shader at those coordinates.
* xLights' "Generate Custom Model" does similar from video but is an
  interactive wizard; we automate it ourselves.

## 4. Outdoor tablet

* Web UI served by the player, Android tablet in kiosk mode (e.g. Fully Kiosk).
* Big swatches, preset palettes, speed/effect buttons; live preview on the photo.
* Restricted to colors/effects only; separate Wi-Fi/VLAN since it's reachable from outside.
