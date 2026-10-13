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

Admin (need `api_token` from the config, as X-Api-Token header or ?token=, when one is set):
  POST /api/off            lights off until Resume or the next show window
  POST /api/skip           next playlist item
  POST /api/play           {"index": n} play playlist item n now
  POST /api/playlist       {"items": [...], "shuffle": bool} replace the playlist
  POST /api/playlist/add   {"name", "seconds", "effect", "colors", "speed", "band", "brightness"} append an effect
  POST /api/schedule       {"windows": [...]} replace the schedule

Local only (from this computer):
  POST /api/shutdown       black out the lights and stop the player
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import MAX_COLORS, parse_item, parse_window
from .effects import EFFECTS, parse_color
from .player import Player

WEB_DIR = Path(__file__).parent / "web"
MAX_BODY = 256 * 1024
MAX_ITEMS = 200
MAX_WINDOWS = 50


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind the same port, which would
    # mean two players fighting over the lights. Fail loudly instead.
    allow_reuse_address = os.name != "nt"


def make_server(player: Player, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"player": player})
    server = _Server((host, port), handler)
    server.daemon_threads = True
    return server


def serve_in_thread(server: ThreadingHTTPServer) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    t.start()
    return t


class _Handler(BaseHTTPRequestHandler):
    player: Player
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
            })
        if path == "/api/preview":
            return self._json(self._preview())
        if path == "/api/playlist":
            return self._json(self.player.playlist())
        if path == "/api/schedule":
            return self._json(self.player.schedule())
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
                self.player.set_live(_live_params(body))
            elif path == "/api/resume":
                self.player.resume()
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
            else:
                return self._error(404, "Not found")
        except (ValueError, TypeError, KeyError, OSError) as e:
            return self._error(400, _message(e))
        self._json(self.player.status())

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
        items = [parse_item(self._checked_item(i), self.player.config.show_dir) for i in raw_items]
        shuffle = body.get("shuffle")
        self.player.update_show(playlist=items, shuffle=None if shuffle is None else bool(shuffle))
        self._json(self.player.playlist())

    def _add_to_playlist(self, body):
        raw = {k: body[k] for k in ("name", "seconds", "effect", "colors", "speed", "band", "brightness") if k in body}
        raw["type"] = "effect"
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

    # ---- helpers ----

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
