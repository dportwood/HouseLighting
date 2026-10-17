# Projector Pi

A Raspberry Pi (3B or newer) on a projector's HDMI that plays videos from the
HouseLighting player. It boots straight to a black screen and waits for
commands; nothing else is ever drawn on the projector.

It has two modes, switched from the app's **Projectors** tab (each switch reboots, ~1 minute):

| Mode | Where | What it does |
|---|---|---|
| **Sync** | inside | Writable. The app copies the playlist's videos over and removes old ones. |
| **Show** | outside | Read-only (overlay file system): safe to lose power every day. Plays and stops on command; refuses to change videos. |

Outside, the app plays the projector's playlist during the light show's hours
and blacks it out after. When the Pi powers up it's picked up again within a few
seconds. Reordering or trimming the playlist works outside; **new** videos need a
trip inside to sync.

## Build the SD card (once per projector)

1. **Raspberry Pi Imager** → Device *Raspberry Pi 3* → OS *Raspberry Pi OS (other) →
   Raspberry Pi OS Lite (64-bit)* → your SD card (32 GB+ holds the whole AtmosFX library).
2. **Edit settings** before writing:
   - hostname `projector1` (then `projector2`, … for more)
   - username `pi` and a password
   - Wi-Fi network, password and country; time zone America/Chicago
   - Services: **enable SSH** (password authentication)
3. Put the card in the Pi, connect the projector (or any HDMI screen) and power. First boot takes a few minutes.
4. From this PC (in the repo folder), run — it asks for the Pi's password:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\setup-projector.ps1 -PiHost projector1.local -Name "Projector 1"
   ```

   It installs mpv and the agent, makes the Pi boot to black with no login prompt
   on HDMI, cuts down SD card writes, and starts the agent on port 8090 in sync mode.
   (No PC? Copy `hl_projector.py` and `setup.sh` to the Pi and run `sudo bash setup.sh "Projector 1"`.)

5. **Check video playback on the projector** — the Pi 3 decodes H.264 in hardware
   and there are a few ways to drive the screen, so test once:

   ```bash
   ssh pi@projector1.local
   sudo systemctl stop hl-projector
   sudo python3 /opt/hl-projector/hl_projector.py --probe "/var/lib/hl-projector/media/<some 1080p video>.mp4"
   sudo systemctl start hl-projector
   ```

   (Sync a video from the app first, or `scp` one over.) It plays the video 20 s
   per setting and prints dropped frames; put the best one in `/etc/hl-projector.json`
   as `"mpv_profile"` if it isn't `gpu-drm-hw`, then `sudo systemctl restart hl-projector`.

## Hook it up to the app

In `config.toml` on the PC (top level, above the first `[section]`):

```toml
media_dir = 'C:\Users\david\Videos\AtmosFX'

[[projectors]]
name = "Projector 1"
host = "projector1.local"
# token = "..."          # if you set "token" in the Pi's /etc/hl-projector.json
```

Restart the player (House Lights control window → Restart). A **Projectors** tab appears.

## The routine

1. **Inside** (projector in sync mode, on the Wi-Fi): Projectors → **Edit playlist** →
   pick videos from the library (by collection and version — Window, Wall, TV, …) → Save → **Sync now**.
2. **Ready for outside** → it switches to show mode and reboots. Unplug it once it's back
   (or any time — show mode doesn't mind), take it outside, plug it in.
3. During show hours it plays; after, it goes black. **Stop** blacks it out until the next show.
4. To add new videos: bring it in, **Switch to sync mode**, edit, **Sync now**, **Ready for outside**.

Use **Shut down** before unplugging in sync mode if you can (it's safe in show mode either way).

## Notes

- Agent API and options: see the top of `hl_projector.py`. Config: `/etc/hl-projector.json`
  (`name`, `port`, `token`, `media_dir`, `mpv_profile`, `mpv_extra`, `audio`).
- Logs: `journalctl -u hl-projector` (kept in memory only; gone after a reboot).
- SSH works in both modes, but in show mode any change you make is thrown away at the next reboot.
- More projectors: repeat with `projector2`, or copy a finished SD card and change its
  hostname (`sudo raspi-config` → System → Hostname) and `name` in `/etc/hl-projector.json`.
- Audio is off by default (`"audio": true` sends it out HDMI).
