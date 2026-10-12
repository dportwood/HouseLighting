"""HTTP API and the tablet web page (stdlib only, so it runs anywhere Python does).

Open to anyone on the network (the tablet):
  GET  /                 control page
  GET  /api/status       what's playing, live settings, schedule window
  GET  /api/options      effects and color presets for the page
  GET  /api/preview      last frame sent, per model, as hex RGB
  GET  /api/playlist     playlist items, what's playing and how far in, what's next
  POST /api/live         {"effect", "colors": ["#RRGGBB"...], "speed", "band", "brightness"}
  POST /api/resume       drop the live override and go back to the show

Admin (need `api_token` from the config, as X-Api-Token header or ?token=, when one is set):
  POST /api/off          lights off until Resume or the next show window
  POST /api/skip         next playlist item
  POST /api/play         {"index": n} play playlist item n now
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .effects import EFFECTS, parse_color
from .player import Player

WEB_DIR = Path(__file__).parent / "web"
MAX_BODY = 16 * 1024
MAX_COLORS = 16


def make_server(player: Player, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"player": player})
    server = ThreadingHTTPServer((host, port), handler)
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
                "admin": not cfg.api_token,
            })
        if path == "/api/preview":
            return self._json(self._preview())
        if path == "/api/playlist":
            return self._json(self.player.playlist())
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
            elif path in ("/api/off", "/api/skip", "/api/play"):
                if not self._admin_ok():
                    return self._error(403, "Admin token required")
                if path == "/api/off":
                    self.player.turn_off()
                elif path == "/api/skip":
                    self.player.skip()
                else:
                    if not self.player.in_window():
                        return self._error(409, "The show is off right now")
                    self.player.play_item(int(body["index"]))
            else:
                return self._error(404, "Not found")
        except (ValueError, TypeError, KeyError) as e:
            return self._error(400, str(e))
        self._json(self.player.status())

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
