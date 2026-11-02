# HouseLighting

A real-time player for the house light show. It reads the layout xLights
already maintains (controllers, channel ranges, models), sends frames straight
to the Kulp K8-Pi / FPP controllers over DDP, and serves a phone/tablet page for
running the show. It also drives projector Pis that play AtmosFX videos.

* Plays rendered xLights `.fseq` sequences and live effects (chase, fade,
  twinkle, strobe, solid) from a shuffled playlist, inside scheduled show times.
* Live changes from the page take over the lights, per string if you like, and
  revert to the show on their own.
* Kiosk mode lets visitors design a pattern that plays on the house for a minute.
* Projector Pis follow the same schedule and are found on the network automatically.

xLights stays the tool for layout and for designing sequences.

## Setup

Windows PC on the same network as the controllers, Python 3.11+.

```powershell
pip install -e .[dev]
copy config.example.toml config.toml          # then edit it (see Configuration)
python -m houselights serve --config config.toml
```

Open `http://<this PC>:8080/` on a phone or tablet. Windows asks to let Python
through the firewall the first time; allow it on private networks.

**Start automatically** — registers the "HouseLights Player" scheduled task
(starts 30 s after you log on, runs windowless, restarts after a failure, logs
to `logs\player.log`):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-startup-task.ps1
```

The PC has to be logged in and awake during show hours.

**Control window** — adds a "House Lights" shortcut (Desktop and Start menu)
with status, Start / Stop / Restart, the recent log and a link to the page.
Stop blacks out the lights before exiting.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1
```

Only one player may drive the lights at a time; a second `serve` refuses to start.

## The control page

| Tab | What it's for |
|---|---|
| **Controls** | Themes, colors (up to 16), effect, speed, band size, brightness and a live preview. **Which lights** picks all lights, an xLights group or a single string (each can also be turned off). Changes take over immediately and go back to the show after `override_minutes`, or right away with **Back to the show**. **Save to playlist** keeps the current look. |
| **Playlist** | What's playing, progress and what's next; **Play** any item now. **Edit playlist**: rename, reorder, remove, change length/speed/brightness, shuffle, add `.fseq` files from the show folder. |
| **Schedule** | Whether the show is on and when it next starts. Show times have days, on/off times (past midnight is fine), an optional date range and an on/off switch. |
| **Devices** | **Light controllers**: each one's online/offline state, FPP mode and whether the show's data is arriving (the tab shows ⚠ when one is offline). **Projectors**: mode, what's playing, Play / Stop, video playlist, **Sync now**, **Ready for outside** / **Switch to sync mode**, and **Scan for projectors**. See [projector/README.md](projector/README.md). |
| **Kiosk** | Locks a device (e.g. a tablet outside) to a simple pattern maker. Visitors tap **Show it on the house!** and it plays for `kiosk_seconds`; the next visitor can make a new one any time. Leaving kiosk mode needs the PIN. |

Outside show times the page refuses live changes (unless
`allow_live_outside_window = true`). Setting `api_token` locks editing and the
admin buttons behind a PIN that each device enters once.

## Configuration

Two files sit next to each other in the repo folder (both ignored by git):

* **`config.toml`** — settings you edit by hand. Start from
  [config.example.toml](config.example.toml).
* **`show.json`** — the schedule, playlist and projectors as edited from the
  page. It's created from `config.toml`'s `windows` / `playlist` / `shuffle` on
  first run and wins after that; delete it to start over from `config.toml`.

| Setting | Meaning |
|---|---|
| `show_dir` | xLights show folder: `xlights_networks.xml`, `xlights_rgbeffects.xml` and the `.fseq` files. Use the UNC path (`\\server\share\...`); mapped drives may not be up at logon. |
| `controller_ips` | Override controller addresses from xLights, by controller name, e.g. `{ "FPP-Housenew" = "172.16.17.10" }`. |
| `skip_controllers` | Controllers (name or IP) never sent to. |
| `fps`, `http_port` | Frame rate (20) and page port (8080). |
| `override_minutes` | How long a live change lasts before the show resumes. |
| `api_token` | Admin PIN for editing and admin buttons (off when empty). |
| `kiosk_pin`, `kiosk_seconds` | PIN to leave kiosk mode (falls back to `api_token`) and how long a visitor's pattern plays. |
| `media_dir` | Video library for the projectors (e.g. the AtmosFX folder). |
| `[[windows]]`, `[[playlist]]`, `shuffle` | Initial schedule and playlist (then edited on the page). |
| `[live]`, `[presets]` | Where the Controls tab starts, and its theme buttons. |
| `[[projectors]]` | Optional; projectors can be added from the page instead. |

## Controllers

| Controller | Address | Channels |
|---|---|---|
| FPP-Housenew (house) | 172.16.17.10 (`controller_ips`; xLights still says .64) | 1–4062 |
| FPP-front-right | 172.16.17.45 | 4063–7314 |
| FPP-front-left | 172.16.17.239 | 7315–9114 |

All three run FPP in remote mode, which accepts DDP while idle.

* Channels are numbered globally in `xlights_networks.xml` order, and the
  controllers use *Keep Channel Numbers*, so the DDP offset is the global channel − 1.
* DDP: UDP 4048, up to 1440 data bytes per packet, PUSH on a frame's last
  packet, sequence number advancing per packet (FPP counts repeats as errors).
* Brightness and color order are applied on the Pis.
* Each Pi's `/api/channel/input/stats` shows DDP packet and error counts.

## Command line

```powershell
python -m houselights info                                         # controllers and models from xLights
python -m houselights chase --colors 400080,FF3000 --seconds 30    # quick live test
python -m houselights --only FPP-front-left solid --color 400080 --seconds 5
python -m houselights play "\\THEHUB\Media\xlights\Halloween Chase.fseq"
python -m houselights --only FPP-front-left locate --model "Large bush center" --first 121
```

`--only` / `--skip` take a controller name or IP and can be repeated. These
send to the lights directly, so stop the player first. `locate` lights the end
of a string in color blocks to find a dead pixel. The one-off commands use the
addresses in xLights (they don't read `config.toml`).

## Development

```powershell
python -m pytest
python -m houselights serve --config config.toml --dry-run    # everything except sending to the lights
python projector\hl_projector.py --fake --config none --media-dir .\pi-media   # a projector agent without a Pi
```

Code: `houselights/` (player, effects, DDP output, web server and page,
projector manager), `projector/` (the agent and setup for projector Pis),
`scripts/` (Windows install helpers), `tests/`.

What's next: [docs/roadmap.md](docs/roadmap.md).
