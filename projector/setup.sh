#!/bin/bash
# One-time setup of a projector Pi (Raspberry Pi OS Lite, Bookworm or newer, Pi 3B or later).
#
#   sudo bash setup.sh [name]        e.g. sudo bash setup.sh "Projector 1"
#
# Re-running is safe. The Pi needs internet access the first time (apt).
# Afterwards it boots straight to a black screen running the projector agent on
# port 8090, in sync (writable) mode. Switch to show (read-only) mode from the
# HouseLighting app before taking it outside.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo"; exit 1; fi
HERE="$(cd "$(dirname "$0")" && pwd)"
NAME="${1:-$(hostname)}"
CONF=/etc/hl-projector.json
MEDIA=/var/lib/hl-projector/media

echo "== Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends mpv python3 avahi-daemon overlayroot initramfs-tools raspi-config

echo "== Installing the agent"
install -d /opt/hl-projector "$MEDIA"
install -m 755 "$HERE/hl_projector.py" /opt/hl-projector/hl_projector.py
if [ ! -f "$CONF" ]; then
  python3 - "$NAME" "$CONF" <<'PY'
import json, sys
name, path = sys.argv[1], sys.argv[2]
json.dump({"name": name, "port": 8090, "token": "", "media_dir": "/var/lib/hl-projector/media",
           "mpv_profile": "gpu-drm-hw", "mpv_extra": [], "audio": False,
           "socket": "/run/hl-projector/mpv.sock"}, open(path, "w"), indent=2)
PY
  echo "   wrote $CONF"
else
  echo "   keeping existing $CONF"
fi

cat > /etc/systemd/system/hl-projector.service <<'UNIT'
[Unit]
Description=HouseLighting projector agent
After=network.target systemd-user-sessions.service
Conflicts=getty@tty1.service

[Service]
ExecStart=/usr/bin/python3 /opt/hl-projector/hl_projector.py --config /etc/hl-projector.json
Restart=always
RestartSec=3
RuntimeDirectory=hl-projector
# mpv draws straight to the screen (DRM/KMS) on the first console
StandardInput=tty
TTYPath=/dev/tty1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

# Announce the agent on the network so the app can find it (projector1.local works regardless).
cat > /etc/avahi/services/hl-projector.service <<'AVAHI'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">%h projector</name>
  <service><type>_hl-projector._tcp</type><port>8090</port></service>
</service-group>
AVAHI

echo "== Black screen at boot: no splash, no boot text, no login prompt on HDMI"
CMDLINE=/boot/firmware/cmdline.txt; [ -f "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt
CONFIGTXT=/boot/firmware/config.txt; [ -f "$CONFIGTXT" ] || CONFIGTXT=/boot/config.txt
for opt in quiet loglevel=1 logo.nologo consoleblank=0 vt.global_cursor_default=0; do
  grep -qw -- "$opt" "$CMDLINE" || sed -i "1 s/\$/ $opt/" "$CMDLINE"
done
sed -i 's/ console=tty1//' "$CMDLINE"
grep -q '^disable_splash=1' "$CONFIGTXT" || echo 'disable_splash=1' >> "$CONFIGTXT"
# Keep HDMI on even if the projector is switched on after the Pi
grep -q '^hdmi_force_hotplug=1' "$CONFIGTXT" || echo 'hdmi_force_hotplug=1' >> "$CONFIGTXT"
systemctl disable getty@tty1.service >/dev/null 2>&1 || true

echo "== Fewer SD card writes (it loses power every day)"
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=20M\n' > /etc/systemd/journald.conf.d/hl-projector.conf
systemctl disable --now dphys-swapfile >/dev/null 2>&1 || true
systemctl disable --now rpi-swap >/dev/null 2>&1 || true
systemctl mask --now systemd-zram-setup@zram0.service >/dev/null 2>&1 || true
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer man-db.timer >/dev/null 2>&1 || true

echo "== Starting"
systemctl daemon-reload
systemctl enable --now avahi-daemon hl-projector
sleep 3
systemctl --no-pager --lines=5 status hl-projector || true

cat <<EOF

Done. '$NAME' is listening on http://$(hostname).local:8090/api/status
Next:
  1. Test playback (watch the projector):
       sudo systemctl stop hl-projector
       sudo python3 /opt/hl-projector/hl_projector.py --probe /path/to/a/1080p/video.mp4
       sudo systemctl start hl-projector
     If the best profile isn't gpu-drm-hw, set "mpv_profile" in $CONF.
  2. Add the projector to config.toml on the PC, sync videos from the app,
     then press "Ready for outside" (switches to read-only show mode and reboots).
EOF
