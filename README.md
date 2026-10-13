# HouseLighting

A small real-time player for the house light show. It reads the layout that
xLights already maintains (controllers, channel ranges, models) and sends
frames straight to the Kulp K8-Pi / FPP controllers over DDP, so effects and
colors can change live instead of being baked into `.fseq` files.

xLights stays the tool for layout and for designed/musical sequences; this
player can play those `.fseq` files too.

## Setup

```bash
pip install -e .[dev]
```

The xLights show folder defaults to `Z:\xlights`; override with `--show-dir`
or the `HOUSELIGHTS_SHOW_DIR` environment variable.

## Running the show (phase 2)

```bash
cp config.example.toml config.toml   # then edit schedule, playlist, presets
python -m houselights serve --config config.toml
```

* Plays the playlist (rendered `.fseq` files and live effects) inside the
  schedule windows; outside them it sends black for a second, then goes quiet.
* Control page for the tablet/phone: `http://<this PC>:8080/`. Theme presets,
  colors, effect (chase / fade / twinkle / solid), speed, band size, brightness,
  and a live preview. Changes take over immediately and revert to the show after
  `override_minutes`; "Back to the show" reverts now.
* Outside the show window the page refuses changes (unless
  `allow_live_outside_window = true`).
* Set `api_token` to require a token for the admin buttons (Off, Next).
* `--dry-run` runs everything, including the page, without sending to the lights.
* Windows will ask to allow Python through the firewall the first time; allow it
  on private networks so the tablet can reach the page.

**Only one player at a time:** stop xSchedule (or remove its schedule) before
running `serve` against the real lights. A second `serve` on the same port
refuses to start.

### Start automatically

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-startup-task.ps1
```

Registers the "HouseLights Player" scheduled task: starts 30 s after this user
logs on, runs windowless, restarts after a failure, never times out. Output and
errors go to `logs\player.log`. The PC must be logged in (set up auto-logon if
it reboots unattended) and must not go to sleep during show hours.

```powershell
Stop-ScheduledTask  -TaskName "HouseLights Player"   # stop the show player
Start-ScheduledTask -TaskName "HouseLights Player"   # start it (e.g. after editing config.toml)
powershell -ExecutionPolicy Bypass -File scripts\install-startup-task.ps1 -Uninstall
```

Playlist items take an optional `name` and `brightness` (percent). The tablet's
brightness slider only applies while its override is active.

## One-off commands

```bash
python -m houselights info
python -m houselights --skip FPP-Housenew chase --colors 400080,FF3000 --seconds 30
python -m houselights --only FPP-front-left solid --color 400080 --seconds 5
python -m houselights --skip FPP-Housenew play "Z:/xlights/Halloween Chase.fseq"
```

`--only` / `--skip` take a controller name or IP and can be repeated.
The house controller (`FPP-Housenew`, 172.16.17.64) is currently broken, so skip it.

Don't run these while xSchedule is playing the scheduled show (16:50–23:15) –
the remotes would get both their synced local sequence and our DDP frames.

## How it talks to the controllers

* Channel numbers are global, in `xlights_networks.xml` order (house 1–4062,
  front-right 4063–7314, front-left 7315–9114). The controllers use
  *Keep Channel Numbers*, so the DDP offset is the global channel − 1.
* DDP: UDP 4048, ≤1440 data bytes per packet, PUSH flag on the last packet of
  each frame, sequence number advancing per packet (FPP counts repeats as errors).
* Brightness (70%) and color order are applied on the Pi, not here.
* The remotes accept DDP in FPP *remote* mode while idle (verified 2026-10-12;
  check `/api/channel/input/stats` on each Pi for packet/error counters).

## Tests

```bash
python -m pytest
```

See [docs/roadmap.md](docs/roadmap.md) for what's next.
