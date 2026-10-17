#!/usr/bin/env python3
"""HouseLighting projector agent: runs on a projector's Raspberry Pi.

Plays videos full screen out of HDMI with mpv and takes commands from the
HouseLighting player over HTTP. Standard library only, so it needs nothing
beyond what Raspberry Pi OS Lite ships plus mpv.

Two modes, switched from the app (each switch reboots the Pi):
  sync  writable: the app copies videos here. Use it indoors.
  show  read-only root (overlay file system): safe to lose power at any time.
        Uploads are refused; play/stop/status work as normal.

API (writes need the token from the config when one is set, as X-Api-Token):
  GET    /api/status              name, mode, what's playing, disk space
  GET    /api/library             videos on this Pi: [{"path", "size"}]
  PUT    /api/library/<path>      store a video (sync mode only)
  DELETE /api/library/<path>      remove a video (sync mode only)
  POST   /api/play                {"files": [...], "loop": true, "shuffle": false}
  POST   /api/stop                stop and show black
  POST   /api/mode                {"mode": "show" | "sync"}  then reboots
  POST   /api/power               {"action": "reboot" | "poweroff"}

    python3 hl_projector.py --config /etc/hl-projector.json
    python3 hl_projector.py --config ... --probe /path/to/video.mp4   # try mpv settings on this Pi
    python3 hl_projector.py --fake --media-dir ./media --port 8090    # no mpv (development)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

VERSION = "0.1.0"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv"}
CHUNK = 1024 * 1024

# Hardware H.264 decoding on the Pi 3 goes through the V4L2 memory-to-memory decoder.
# `--probe` tries these on the real Pi; the winner goes in the config's "mpv_profile".
MPV_PROFILES = {
    "gpu-drm-hw": ["--vo=gpu", "--gpu-context=drm", "--hwdec=v4l2m2m-copy"],
    "drm-hw": ["--vo=drm", "--hwdec=v4l2m2m-copy"],
    "gpu-drm-sw": ["--vo=gpu", "--gpu-context=drm", "--hwdec=no"],
    "drm-sw": ["--vo=drm", "--hwdec=no"],
}
MPV_COMMON = [
    "--idle=yes", "--force-window=yes", "--fullscreen", "--keep-open=no",
    "--no-osc", "--osd-level=0", "--no-input-default-bindings", "--input-vo-keyboard=no",
    "--background-color=#000000", "--cursor-autohide=always", "--really-quiet",
]

DEFAULT_CONFIG = {
    "name": socket.gethostname(),
    "port": 8090,
    "token": "",
    "media_dir": "/var/lib/hl-projector/media",
    "mpv_profile": "gpu-drm-hw",
    "mpv_extra": [],
    "audio": False,
    "socket": "/run/hl-projector/mpv.sock",
}


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------- players

class MpvPlayer:
    """One long-running mpv, driven over its JSON IPC socket. Restarted if it dies."""

    def __init__(self, args: list[str], sock_path: str):
        self.sock_path = sock_path
        self.cmd = ["mpv", *args, f"--input-ipc-server={sock_path}"]
        self.proc: subprocess.Popen | None = None
        self._req = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.files: list[str] = []
        self.loop = True

    def start(self) -> None:
        Path(self.sock_path).parent.mkdir(parents=True, exist_ok=True)
        self._spawn()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _spawn(self) -> None:
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        log("starting " + " ".join(self.cmd))
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.DEVNULL)
        for _ in range(100):  # wait for the socket
            if os.path.exists(self.sock_path):
                return
            time.sleep(0.1)
        log("mpv didn't open its control socket")

    def _watchdog(self) -> None:
        while not self._stop.is_set():
            time.sleep(2)
            if self.proc and self.proc.poll() is not None:
                log(f"mpv exited ({self.proc.returncode}); restarting")
                self._spawn()
                if self.files:  # pick up where we were
                    try:
                        self.play(self.files, self.loop)
                    except Exception:
                        log(traceback.format_exc())

    def close(self) -> None:
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def command(self, *args, timeout: float = 3.0):
        with self._lock:
            self._req += 1
            req = self._req
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(self.sock_path)
                s.sendall((json.dumps({"command": list(args), "request_id": req}) + "\n").encode())
                buf = b""
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        raise ConnectionError("mpv closed the connection")
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        msg = json.loads(line)
                        if msg.get("request_id") == req:
                            if msg.get("error") not in ("success", None):
                                raise RuntimeError(f"mpv {args[0]}: {msg['error']}")
                            return msg.get("data")

    def get(self, prop: str):
        try:
            return self.command("get_property", prop)
        except RuntimeError:
            return None  # e.g. "property unavailable" while idle

    def play(self, files: list[str], loop: bool = True) -> None:
        self.files, self.loop = list(files), loop
        self.command("set_property", "loop-playlist", "inf" if loop else "no")
        self.command("loadfile", files[0], "replace")
        for f in files[1:]:
            self.command("loadfile", f, "append")

    def stop(self) -> None:
        self.files = []
        self.command("stop")

    def status(self) -> dict:
        idle = self.get("idle-active")
        if idle or idle is None:
            return {"state": "idle"}
        return {
            "state": "playing",
            "path": self.get("path"),
            "index": self.get("playlist-pos"),
            "count": self.get("playlist-count"),
            "position": self.get("time-pos"),
            "duration": self.get("duration"),
            "dropped_frames": self.get("frame-drop-count"),
        }


class FakePlayer:
    """Stands in for mpv in tests and on a PC: remembers what it was told."""

    def __init__(self):
        self.files: list[str] = []
        self.loop = True
        self.started = 0.0

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def play(self, files: list[str], loop: bool = True) -> None:
        self.files, self.loop, self.started = list(files), loop, time.monotonic()

    def stop(self) -> None:
        self.files = []

    def status(self) -> dict:
        if not self.files:
            return {"state": "idle"}
        return {"state": "playing", "path": self.files[0], "index": 0, "count": len(self.files),
                "position": round(time.monotonic() - self.started, 1), "duration": None, "dropped_frames": 0}


# ---------------------------------------------------------------- the agent

def boot_cmdline() -> Path | None:
    for p in (Path("/boot/firmware/cmdline.txt"), Path("/boot/cmdline.txt")):
        if p.exists():
            return p
    return None


class Agent:
    def __init__(self, config: dict, player, fake_mode: str | None = None):
        self.config = config
        self.player = player
        self.media = Path(config["media_dir"])
        self.media.mkdir(parents=True, exist_ok=True)
        self.fake_mode = fake_mode  # tests/dev: pretend to be in this mode
        self.started = time.time()
        self.playlist: list[str] = []
        self.shuffle = False
        self.lock = threading.Lock()

    # ---- mode ----

    def mode(self) -> str:
        if self.fake_mode:
            return self.fake_mode
        try:
            for line in Path("/proc/mounts").read_text().splitlines():
                parts = line.split()
                if len(parts) > 2 and parts[1] == "/":
                    return "show" if parts[2] == "overlay" else "sync"
        except OSError:
            pass
        return "sync"

    def mode_after_reboot(self) -> str:
        if self.fake_mode:
            return self.fake_mode
        cmd = boot_cmdline()
        try:
            return "show" if cmd and "overlayroot=tmpfs" in cmd.read_text() else "sync"
        except OSError:
            return self.mode()

    def set_mode(self, mode: str) -> None:
        if mode not in ("show", "sync"):
            raise ValueError("mode must be 'show' or 'sync'")
        if self.fake_mode:
            self.fake_mode = mode
            return
        # raspi-config's overlay switch: 0 = enable (read-only), 1 = disable. Takes effect on reboot.
        r = subprocess.run(["raspi-config", "nonint", "do_overlayfs", "0" if mode == "show" else "1"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"raspi-config failed: {r.stderr.strip() or r.stdout.strip()}")
        self.power("reboot")

    def power(self, action: str) -> None:
        if action not in ("reboot", "poweroff"):
            raise ValueError("action must be 'reboot' or 'poweroff'")
        log(f"{action} requested")
        if self.fake_mode:
            return

        def later():
            time.sleep(2)  # let the HTTP reply go out first
            subprocess.run(["systemctl", action])

        threading.Thread(target=later, daemon=True).start()

    # ---- library ----

    def resolve(self, rel: str) -> Path:
        """Media path for a relative name, refusing anything that escapes the media folder."""
        parts = PurePosixPath(rel.replace("\\", "/")).parts
        if not parts or PurePosixPath(rel).is_absolute() or any(p in ("..", ".") or "/" in p or ":" in p for p in parts):
            raise ValueError(f"Bad file name {rel!r}")
        path = self.media.joinpath(*parts)
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError(f"Not a video file: {rel!r}")
        return path

    def library(self) -> list[dict]:
        out = []
        for p in sorted(self.media.rglob("*")):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
                out.append({"path": p.relative_to(self.media).as_posix(), "size": p.stat().st_size})
        return out

    def require_writable(self) -> None:
        if self.mode() == "show":
            raise PermissionError("This projector is in show mode (read-only). Switch it to sync mode to change videos.")

    def store(self, rel: str, stream, length: int) -> None:
        self.require_writable()
        path = self.resolve(rel)
        free = shutil.disk_usage(self.media).free
        if length > free - 200 * 1024 * 1024:
            raise OSError(f"Not enough space: need {length // 2**20} MB, {free // 2**20} MB free")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        remaining = length
        with open(tmp, "wb") as f:
            while remaining > 0:
                chunk = stream.read(min(CHUNK, remaining))
                if not chunk:
                    raise ConnectionError("Upload ended early")
                f.write(chunk)
                remaining -= len(chunk)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def remove(self, rel: str) -> None:
        self.require_writable()
        path = self.resolve(rel)
        path.unlink(missing_ok=True)
        for parent in path.parents:  # tidy empty folders
            if parent == self.media:
                break
            try:
                parent.rmdir()
            except OSError:
                break

    # ---- playback ----

    def play(self, files: list[str], loop: bool = True, shuffle: bool = False) -> None:
        if not files:
            raise ValueError("No files to play")
        paths = [self.resolve(f) for f in files]
        missing = [f for f, p in zip(files, paths) if not p.is_file()]
        if missing:
            raise FileNotFoundError("Not on this projector: " + ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else ""))
        order = list(range(len(paths)))
        if shuffle:
            random.shuffle(order)
        with self.lock:
            self.playlist, self.shuffle = list(files), shuffle
            self.player.play([str(paths[i]) for i in order], loop)

    def stop(self) -> None:
        with self.lock:
            self.playlist = []
            self.player.stop()

    def status(self) -> dict:
        st = self.player.status()
        if st.get("path"):
            try:
                st["file"] = Path(st["path"]).resolve().relative_to(self.media.resolve()).as_posix()
            except ValueError:
                st["file"] = Path(st["path"]).name
        usage = shutil.disk_usage(self.media)
        return {
            "name": self.config["name"],
            "version": VERSION,
            "mode": self.mode(),
            "mode_after_reboot": self.mode_after_reboot(),
            "uptime": round(time.time() - self.started),
            "playlist": self.playlist,
            "shuffle": self.shuffle,
            "disk_free": usage.free,
            "disk_total": usage.total,
            **st,
        }


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    agent: Agent
    server_version = "HLProjector/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/status":
            return self._json(self.agent.status())
        if path == "/api/library":
            return self._json({"files": self.agent.library()})
        self._json({"error": "Not found"}, 404)

    def do_PUT(self):
        self._guarded(self._put)

    def do_DELETE(self):
        self._guarded(self._delete)

    def do_POST(self):
        self._guarded(self._post)

    def _put(self, path):
        if not path.startswith("/api/library/"):
            return self._json({"error": "Not found"}, 404)
        length = int(self.headers.get("Content-Length") or -1)
        if length < 0:
            return self._json({"error": "Content-Length required"}, 411)
        self.agent.store(unquote(path[len("/api/library/"):]), self.rfile, length)
        self._json({"ok": True})

    def _delete(self, path):
        if not path.startswith("/api/library/"):
            return self._json({"error": "Not found"}, 404)
        self.agent.remove(unquote(path[len("/api/library/"):]))
        self._json({"ok": True})

    def _post(self, path):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if 0 < length < 1_000_000 else {}
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object")
        if path == "/api/play":
            files = body.get("files")
            if not isinstance(files, list):
                raise ValueError("files must be a list")
            self.agent.play([str(f) for f in files], bool(body.get("loop", True)), bool(body.get("shuffle", False)))
        elif path == "/api/stop":
            self.agent.stop()
        elif path == "/api/mode":
            self.agent.set_mode(str(body.get("mode", "")))
        elif path == "/api/power":
            self.agent.power(str(body.get("action", "")))
        else:
            return self._json({"error": "Not found"}, 404)
        self._json(self.agent.status())

    def _guarded(self, fn):
        token = self.agent.config.get("token")
        if token and self.headers.get("X-Api-Token") != token:
            return self._json({"error": "Token required"}, 403)
        try:
            fn(urlparse(self.path).path)
        except PermissionError as e:
            self._json({"error": str(e)}, 409)
        except FileNotFoundError as e:
            self._json({"error": str(e)}, 400)
        except (ValueError, KeyError, TypeError, OSError, RuntimeError, json.JSONDecodeError) as e:
            self._json({"error": str(e)}, 400)

    def _json(self, data, code=200):
        payload = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            log(f"error from {client_address[0]}:\n{traceback.format_exc()}")


def make_server(agent: Agent, host: str, port: int) -> Server:
    return Server((host, port), type("H", (Handler,), {"agent": agent}))


# ---------------------------------------------------------------- setup helpers

def load_config(path: str | None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path and Path(path).exists():
        cfg.update(json.loads(Path(path).read_text()))
    return cfg


def mpv_args(cfg: dict, profile: str | None = None) -> list[str]:
    args = [*MPV_COMMON, *MPV_PROFILES[profile or cfg["mpv_profile"]], *cfg.get("mpv_extra", [])]
    if not cfg.get("audio"):
        args.append("--audio=no")
    return args


def probe(cfg: dict, video: str, seconds: int = 20) -> None:
    """Play `video` with each mpv profile and report dropped frames, to pick the best one for this Pi."""
    print(f"Testing {video} for {seconds}s per profile (watch the projector too)...")
    results = []
    for name in MPV_PROFILES:
        sock = f"/tmp/hl-probe-{name}.sock"
        player = MpvPlayer(mpv_args(cfg, name), sock)
        try:
            player.start()
            player.play([video], loop=True)
            time.sleep(seconds)
            st = player.status()
            hw = player.get("hwdec-current")
            drops = (player.get("frame-drop-count") or 0) + (player.get("decoder-frame-drop-count") or 0)
            results.append((name, st.get("state"), hw, drops))
        except Exception as e:
            results.append((name, f"failed: {e}", None, None))
        finally:
            player.close()
            time.sleep(1)
    print(f"\n{'profile':<12} {'state':<10} {'hwdec':<14} dropped frames")
    for name, state, hw, drops in results:
        print(f"{name:<12} {str(state):<10} {str(hw):<14} {drops}")
    ok = [r for r in results if r[1] == "playing" and r[3] is not None]
    if ok:
        best = min(ok, key=lambda r: (r[3], r[0] not in ("gpu-drm-hw", "drm-hw")))
        print(f"\nBest: {best[0]}  ->  set \"mpv_profile\": \"{best[0]}\" in the config and restart hl-projector")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="HouseLighting projector agent")
    p.add_argument("--config", default="/etc/hl-projector.json")
    p.add_argument("--port", type=int)
    p.add_argument("--media-dir")
    p.add_argument("--fake", action="store_true", help="no mpv; for development on a PC")
    p.add_argument("--probe", metavar="VIDEO", help="try each mpv profile with this video and report")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    if args.port:
        cfg["port"] = args.port
    if args.media_dir:
        cfg["media_dir"] = args.media_dir
    if args.probe:
        return probe(cfg, args.probe)

    player = FakePlayer() if args.fake else MpvPlayer(mpv_args(cfg), cfg["socket"])
    agent = Agent(cfg, player, fake_mode="sync" if args.fake else None)
    player.start()
    server = make_server(agent, "0.0.0.0", cfg["port"])
    log(f"hl-projector {VERSION} '{cfg['name']}' on port {cfg['port']}, mode {agent.mode()}, "
        f"{len(agent.library())} videos in {agent.media}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        player.close()


if __name__ == "__main__":
    main()
