"""HTTP API and the tablet web page (stdlib only, so it runs anywhere Python does).

Open to anyone on the network (the tablet):
  GET  /                   control page
  GET  /api/status         what's playing, live settings, schedule window
  GET  /api/options        effects, color presets, and whether this caller is admin
  GET  /api/preview        last frame sent, per model, as hex RGB
  GET  /api/playlist       playlist items, what's playing and how far in, what's next
  GET  /api/schedule       time windows, whether the show is on, next start
  GET  /api/sequences      .fseq files available in the xLights show folder
  POST /api/live           {"effect", "colors": ["#RRGGBB"...], "speed", "band", "brightness"}
  POST /api/resume         drop the live override and go back to the show
  POST /api/kiosk/play     same body as /api/live; plays for kiosk_seconds, then back to the show
  POST /api/kiosk/exit     {"pin"} check the PIN that unlocks a device out of kiosk mode

Admin (need `api_token` from the config, as X-Api-Token header or ?token=, when one is set):
  POST /api/off            lights off until Resume or the next show window
  POST /api/skip           next playlist item
  POST /api/play           {"index": n} play playlist item n now
  POST /api/playlist       {"items": [...], "shuffle": bool} replace the playlist
  POST /api/playlist/add   {"name", "seconds", "effect", "colors", "speed", "band", "brightness"} append an effect
  POST /api/schedule       {"windows": [...]} replace the schedule
  POST /api/projectors/<n>/playlist  {"files": [...], "shuffle": bool}
  POST /api/projectors/<n>/play      {"file"?}  whole playlist, or one video on loop
  POST /api/projectors/<n>/stop      black until the next show starts
  POST /api/projectors/<n>/sync      copy missing videos (projector must be in sync mode)
  POST /api/projectors/<n>/mode      {"mode": "show" | "sync"}  (the projector reboots)
  POST /api/projectors/<n>/power     {"action": "reboot" | "poweroff"}
  POST /api/projectors/<n>/remove    forget a projector added from the page
  POST /api/projectors/scan          look for projector agents on the network
  POST /api/projectors/add           {"id"} add a projector the scan found

Also open: GET /api/projectors (status of each), GET /api/media (video library).

Local only (from this computer):
  POST /api/shutdown       black out the lights and stop the player
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import MAX_COLORS, parse_item, parse_strings, parse_window
from .effects import EFFECTS, parse_color
from .player import Player

WEB_DIR = Path(__file__).parent / "web"
KIOSK_MAX_TRIES = 5
KIOSK_LOCKOUT_SECONDS = 60
_kiosk_lock = threading.Lock()
_kiosk_state = {"failures": 0, "locked_until": 0.0}
MAX_BODY = 256 * 1024
MAX_ITEMS = 200
MAX_WINDOWS = 50


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind the same port, which would
    # mean two players fighting over the lights. Fail loudly instead.
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return  # a browser/tablet went away mid-request (reload, sleep, closed tab); nothing to do
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} error handling a request from {client_address[0]}:\n"
              f"{traceback.format_exc()}", flush=True)


def make_server(player: Player, host: str, port: int, projectors=None) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"player": player, "projectors": projectors})
    server = _Server((host, port), handler)
    server.daemon_threads = True
    return server


def serve_in_thread(server: ThreadingHTTPServer) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    t.start()
    return t


class _Handler(BaseHTTPRequestHandler):
    player: Player
    projectors = None  # ProjectorManager when projectors are configured
    server_version = "HouseLights"

    def log_message(self, fmt, *args):  # keep the console quiet; status polling is frequent
        pass

    # ---- routing ----

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/status":
            return self._json(self.player.status())
        if path == "/api/options":
            cfg = self.player.config
            return self._json({
                "effects": list(EFFECTS),
                "presets": {k: ["#%02X%02X%02X" % c for c in v] for k, v in cfg.presets.items()},
                "admin": self._admin_ok(),
                "token_required": bool(cfg.api_token),
                "kiosk_available": bool(cfg.kiosk_pin or cfg.api_token),
                "kiosk_seconds": cfg.kiosk_seconds,
                "projectors": len(cfg.projectors),
                "strings": [{"name": m.name, "pixels": m.nodes} for m in self.player.visible_models],
                "groups": self._groups(),
            })
        if path == "/api/preview":
            return self._json(self._preview())
        if path == "/api/playlist":
            return self._json(self.player.playlist())
        if path == "/api/schedule":
            return self._json(self.player.schedule())
        if path == "/api/projectors":
            return self._json(self._projectors_payload())
        if path == "/api/media":
            return self._json({"files": self.projectors.library.scan() if self.projectors else []})
        if path == "/api/sequences":
            show_dir = self.player.config.show_dir
            return self._json({"files": sorted(p.name for p in show_dir.glob("*.fseq"))})
        self._error(404, "Not found")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/live":
                if not self.player.config.allow_live_outside_window and not self.player.in_window() and not self._admin_ok():
                    return self._error(409, "The show is off right now")
                self.player.set_live(self._live_params(body))
            elif path == "/api/resume":
                self.player.resume()
            elif path == "/api/kiosk/play":
                if not self.player.config.allow_live_outside_window and not self.player.in_window():
                    return self._error(409, "The show is off right now")
                self.player.set_live(self._live_params(body), seconds=self.player.config.kiosk_seconds, source="kiosk")
            elif path == "/api/kiosk/exit":
                return self._kiosk_exit(body)
            elif path == "/api/shutdown":
                if self.client_address[0] not in ("127.0.0.1", "::1"):
                    return self._error(403, "Only from this computer")
                self._json({"stopping": True})
                self.player.stop()
                return
            elif path in ADMIN_POSTS:
                if not self._admin_ok():
                    return self._error(403, "Admin PIN required")
                return ADMIN_POSTS[path](self, body)
            elif path.startswith("/api/projectors/"):
                if not self._admin_ok():
                    return self._error(403, "Admin PIN required")
                return self._projector_action(path, body)
            else:
                return self._error(404, "Not found")
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as e:
            return self._error(400, _message(e))
        self._json(self.player.status())

    def _projector_action(self, path: str, body: dict):
        if not self.projectors:
            return self._error(404, "No projectors configured")
        parts = path.split("/")  # ["", "api", "projectors", n, action]  or  [..., "scan" | "add"]
        mgr = self.projectors
        if len(parts) == 4 and parts[3] == "scan":
            mgr.scan_now()
            return self._json(self._projectors_payload())
        if len(parts) == 4 and parts[3] == "add":
            mgr.add_discovered(str(body.get("id", "")))
            return self._json(self._projectors_payload())
        if len(parts) != 5 or not parts[3].isdigit():
            return self._error(404, "Not found")
        i, action = int(parts[3]), parts[4]
        if action == "playlist":
            files = body.get("files")
            if not isinstance(files, list) or len(files) > 500:
                raise ValueError("files must be a list of up to 500 videos")
            mgr.set_playlist(i, [str(f) for f in files], bool(body.get("shuffle", False)))
        elif action == "play":
            mgr.play(i, str(body["file"]) if body.get("file") else None)
        elif action == "stop":
            mgr.stop(i)
        elif action == "sync":
            mgr.sync(i, bool(body.get("remove_extra", True)))
        elif action == "mode":
            mgr.set_mode(i, str(body.get("mode", "")))
        elif action == "power":
            mgr.power(i, str(body.get("action", "")))
        elif action == "remove":
            mgr.remove(i)
        else:
            return self._error(404, "Not found")
        self._json(self._projectors_payload())

    def _projectors_payload(self) -> dict:
        mgr = self.projectors
        if not mgr:
            return {"projectors": [], "discovered": [], "last_scan": "", "media": False}
        return {"projectors": mgr.snapshot(), "discovered": mgr.discovered_list(),
                "last_scan": mgr.last_scan_at, "media": bool(mgr.library.root)}

    # ---- admin actions ----

    def _off(self, body):
        self.player.turn_off()
        self._json(self.player.status())

    def _skip(self, body):
        self.player.skip()
        self._json(self.player.status())

    def _play(self, body):
        if not self.player.in_window():
            return self._error(409, "The show is off right now")
        self.player.play_item(int(body["index"]))
        self._json(self.player.status())

    def _set_playlist(self, body):
        raw_items = body.get("items")
        if not isinstance(raw_items, list) or len(raw_items) > MAX_ITEMS:
            raise ValueError(f"items must be a list of up to {MAX_ITEMS} playlist entries")
        for i in raw_items:
            if isinstance(i, dict) and i.get("strings"):
                parse_strings(i["strings"], self._string_names())  # names must exist
        items = [parse_item(self._checked_item(i), self.player.config.show_dir) for i in raw_items]
        shuffle = body.get("shuffle")
        self.player.update_show(playlist=items, shuffle=None if shuffle is None else bool(shuffle))
        self._json(self.player.playlist())

    def _add_to_playlist(self, body):
        raw = {k: body[k] for k in ("name", "seconds", "effect", "colors", "speed", "band", "brightness") if k in body}
        raw["type"] = "effect"
        raw["strings"] = parse_strings(body.get("strings"), self._string_names())
        self.player.add_item(parse_item(raw, self.player.config.show_dir))
        self._json(self.player.playlist())

    def _set_schedule(self, body):
        raw = body.get("windows")
        if not isinstance(raw, list) or len(raw) > MAX_WINDOWS:
            raise ValueError(f"windows must be a list of up to {MAX_WINDOWS} time windows")
        self.player.update_show(windows=[parse_window(w) for w in raw])
        self._json(self.player.schedule())

    def _checked_item(self, raw) -> dict:
        """Playlist entries from the page may only name .fseq files that are in the show folder."""
        if not isinstance(raw, dict):
            raise ValueError("Each playlist entry must be an object")
        if raw.get("type", "fseq") == "fseq":
            name = Path(str(raw.get("file", ""))).name
            if not name.lower().endswith(".fseq") or not (self.player.config.show_dir / name).is_file():
                raise ValueError(f"Sequence {name!r} isn't in the show folder")
            raw = {**raw, "file": name}
        return raw

    def _kiosk_exit(self, body):
        """PIN check for leaving kiosk mode, with a short lockout so it can't be guessed by trial."""
        cfg = self.player.config
        now = time.monotonic()
        with _kiosk_lock:
            if now < _kiosk_state["locked_until"]:
                return self._error(429, f"Too many tries; wait {int(_kiosk_state['locked_until'] - now) + 1} s")
            pins = {p for p in (cfg.kiosk_pin, cfg.api_token) if p}
            if pins and str(body.get("pin", "")) in pins:
                _kiosk_state["failures"] = 0
                return self._json({"ok": True})
            _kiosk_state["failures"] += 1
            if _kiosk_state["failures"] >= KIOSK_MAX_TRIES:
                _kiosk_state["failures"] = 0
                _kiosk_state["locked_until"] = now + KIOSK_LOCKOUT_SECONDS
        return self._error(403, "Wrong PIN")

    # ---- helpers ----

    def _string_names(self) -> set[str]:
        return {m.name for m in self.player.visible_models}

    def _groups(self) -> dict[str, list[str]]:
        """xLights model groups, limited to strings we drive; groups of everything are left out."""
        names = self._string_names()
        groups = {}
        for g, members in self.player.layout.groups.items():
            visible = [m for m in members if m in names]
            if 1 < len(visible) < len(names):
                groups[g] = visible
        return groups

    def _live_params(self, body: dict) -> dict:
        params = _live_params(body)
        if "strings" in body:
            params["strings"] = parse_strings(body["strings"], self._string_names())
        return params

    def _admin_ok(self) -> bool:
        token = self.player.config.api_token
        if not token:
            return True
        given = self.headers.get("X-Api-Token") or parse_qs(urlparse(self.path).query).get("token", [""])[0]
        return given == token

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("Request too large")
        if not length:
            return {}
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return data

    def _preview(self) -> dict:
        sent = self.player.sent
        return {
            "models": [
                {"name": m.name, "controller": m.controller, "rgb": sent[m.start - 1 : m.end].tobytes().hex()}
                for m in self.player.visible_models
            ]
        }

    def _json(self, data, code: int = 200):
        payload = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, code: int, message: str):
        self._json({"error": message}, code)

    def _file(self, path: Path, content_type: str):
        payload = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(payload)


ADMIN_POSTS = {
    "/api/off": _Handler._off,
    "/api/skip": _Handler._skip,
    "/api/play": _Handler._play,
    "/api/playlist": _Handler._set_playlist,
    "/api/playlist/add": _Handler._add_to_playlist,
    "/api/schedule": _Handler._set_schedule,
}


def _message(e: Exception) -> str:
    if isinstance(e, KeyError):
        return f"Missing {e.args[0]!r}"
    if isinstance(e, FileNotFoundError):
        return f"File not found: {Path(e.filename).name if e.filename else e}"
    return str(e)


def _live_params(body: dict) -> dict:
    params = {}
    if "effect" in body:
        params["effect"] = str(body["effect"])
    if "colors" in body:
        colors = body["colors"]
        if not isinstance(colors, list) or not 1 <= len(colors) <= MAX_COLORS:
            raise ValueError(f"colors must be a list of 1-{MAX_COLORS} '#RRGGBB' strings")
        params["colors"] = [parse_color(str(c)) for c in colors]
    if "speed" in body:
        params["speed"] = min(100.0, max(1.0, float(body["speed"])))
    if "band" in body:
        params["band"] = min(100, max(1, int(body["band"])))
    if "brightness" in body:
        params["brightness"] = float(body["brightness"])
    return params
