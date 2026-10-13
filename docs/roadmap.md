# Roadmap

## 1. DDP spike — done (2026-10-12)

* Layout loaded from the xLights show folder matches the FPP port configs.
* Live chase and `.fseq` playback sent to both remotes (.45, .239) in remote
  mode; packets received with no errors after fixing per-packet sequence numbers.

## 2. Player MVP — in progress

Done:
* `serve`: schedule windows (incl. past midnight), shuffled playlist of `.fseq`
  files and live effects, 20 fps frame loop.
* Live effects: chase, fade, twinkle, solid; brightness scaling.
* HTTP API + tablet page (presets, colors, effect, speed, band, brightness,
  preview); tablet changes expire back to the show; optional admin token.
* Tested in dry-run (page at phone and tablet sizes; steady 20 fps on fseq playback).

Still to do:
* ~~Run `serve` against the real lights~~ — ran the show from 2026-10-12 17:00.
* ~~Start on boot~~ — logon scheduled task (`scripts/install-startup-task.ps1`).
* ~~Cut over from xSchedule~~ — xSchedule no longer started; its config is untouched.
* Consider a Pi 4/5 replacing the dead .64 so the show doesn't depend on this PC.
* Optionally send FPP MultiSync so remotes can play local `.fseq` copies.

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
