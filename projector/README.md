# Projector Pi

A Raspberry Pi (3B or newer) on a projector's HDMI that plays videos for the
HouseLighting player. After boot it shows black and waits for commands.

It has two modes, switched from the app's **Projectors** tab (each switch
reboots, about a minute):

| Mode | Where | What it does |
|---|---|---|
| **Sync** | inside | Writable. The app copies the playlist's videos over and removes old ones. |
| **Show** | outside | Read-only (overlay file system), so losing power every day is safe. Plays and stops on command; won't change videos. |

Outside, the app plays the projector's playlist during the light show's hours
and blacks it out afterwards; after a power-up it's picked up again within a few
seconds. Reordering or trimming the playlist works outside. New videos need a
trip inside to sync; until then the projector plays the ones it has.

## Build the SD card (once per projector)

1. **Raspberry Pi Imager**: Device *Raspberry Pi 3*, OS *Raspberry Pi OS (other) →
   Raspberry Pi OS Lite (64-bit)*, your SD card (32 GB+ holds the whole library).
2. In the OS customisation settings:
   - hostname `projector1` (`projector2`, … for more)
   - username `pi` and a password
   - Wi-Fi network, password and country; time zone America/Chicago
   - Services: **enable SSH** with password authentication
3. Put the card in the Pi, connect the projector (or any HDMI screen) and power
   it. The first boot takes a few minutes and reboots once.
4. From the repo folder on the PC (asks for the Pi's password):

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\setup-projector.ps1 -PiHost projector1.local -Name "Projector 1"
   ```

   Add `-User yourname` if the Pi's user isn't `pi`. It installs mpv and the
   agent, removes the login prompt from HDMI, cuts down SD card writes, and starts
   the agent on port 8090 in sync mode. (Without the PC script: copy
   `hl_projector.py` and `setup.sh` to the Pi and run `sudo bash setup.sh "Projector 1"`.)

## Add it to the app

1. `media_dir` in `config.toml` points at the video library (top level, above
   the first `[section]`), e.g. `media_dir = 'C:\Users\david\Videos\AtmosFX'`.
2. **Projectors** tab → **Scan for projectors** → **Add**.

No addresses needed. The app finds agents by broadcast (UDP 8091) and by the
announcements they send after booting, matches them by id (or name), and
remembers the last address in `show.json`. If a projector stops answering it
rescans, so a new DHCP address is picked up within seconds.

A projector can also be listed in `config.toml` (useful with a token):

```toml
[[projectors]]
name = "Projector 1"          # the name given to setup.sh
host = "projector1.local"     # optional; tried first, then the scanned address
# token = "..."               # if "token" is set in the Pi's /etc/hl-projector.json
```

## Routine

1. **Inside**, in sync mode: **Edit playlist** → pick videos (by collection and
   version: Window, Wall, TV, …) → **Save** → **Sync now**.
2. **Ready for outside** → it switches to show mode and reboots. Take it out and plug it in.
3. During show hours it plays; afterwards it's black. **Stop** blacks it out
   until the next show.
4. New videos: bring it in → **Switch to sync mode** → edit → **Sync now** → **Ready for outside**.

In sync mode, use **Shut down** before unplugging if you can; in show mode
unplugging is always safe.

## Notes

* Scan finds nothing? The PC and Pi must be on the same network, and Windows
  Firewall must allow Python on private networks.
* `/etc/hl-projector.json` on the Pi: `name`, `port`, `token`, `media_dir`,
  `mpv_profile`, `mpv_extra`, `audio` (off by default; `true` sends it out HDMI).
  The agent's API is described at the top of `hl_projector.py`.
* Playback is hardware H.264 through mpv (`mpv_profile` `gpu-drm-hw`), which
  plays the AtmosFX 480p and 1080p files smoothly on a Pi 3B. To compare other
  settings: stop the service and run
  `sudo python3 /opt/hl-projector/hl_projector.py --probe "<video>"`.
* Boot messages appear on the projector for a few seconds at power-up (see the
  roadmap). If a Pi doesn't come back, they show where it stopped.
* Logs: `journalctl -u hl-projector` (in memory only; cleared by a reboot).
* SSH works in both modes, but in show mode changes are thrown away at the next reboot.
* More projectors: repeat with `projector2`, or copy a finished card and change
  its hostname (`sudo raspi-config` → System → Hostname) and `name` in `/etc/hl-projector.json`.
