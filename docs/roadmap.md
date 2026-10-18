# Roadmap

## Done

* **Player** — xLights layout, DDP to the FPP controllers, `.fseq` playback and
  live effects, scheduled show times, start on logon, control window.
* **Control page** — Controls (themes, per-string looks, preview), Playlist and
  Schedule editing (kept in `show.json`), admin PIN.
* **Kiosk** — visitors make a pattern that plays for a minute; PIN to leave.
* **Projectors** — Pis play AtmosFX videos: sync mode inside, read-only show
  mode outside, follow the show schedule, found by network discovery.
* **House controller** back on line at 172.16.17.10.

## Next

* **Multiple playlists** (e.g. Halloween / Christmas), each picked by its show times.
* **Projector boot to black** — a fresh projector card shows boot messages for a
  few seconds at power-up. Hiding them (`quiet`, no `console=tty1`) made the Pi 3
  fail to come back in show mode; find out why before hiding them again.
* **Camera pixel mapping and image overlay** — a fixed camera photographs the
  house while the player flashes binary-coded patterns (~12–15 plus all-on/off),
  giving each pixel's (x, y) in the photo; effects then sample an image, video or
  shader at those points. (xLights' "Generate Custom Model" does similar from
  video, but only as an interactive wizard.)
* **Update xLights** with the house controller's new address (172.16.17.10) so
  `controller_ips` isn't needed.
