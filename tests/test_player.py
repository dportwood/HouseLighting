import json
import struct
import urllib.error
import urllib.request
from datetime import datetime

import numpy as np
import pytest

from houselights.config import parse_config
from houselights.layout import Controller, Layout, Model
from houselights.output import NullOutput
from houselights.player import Player
from houselights.server import make_server, serve_in_thread

PURPLE, ORANGE = (0x40, 0x00, 0x80), (0xFF, 0x30, 0x00)


class Clock:
    def __init__(self, when: str):
        self.now = datetime.fromisoformat(when)

    def __call__(self):
        return self.now


def tiny_layout() -> Layout:
    return Layout([Controller("A", "10.0.0.1", "DDP", 1, 12)], [Model("Line", "Single Line", "A", 1, 4)], {})


def write_fseq(path, channels: int, frames: list[bytes], step_ms: int = 50):
    header = bytearray(32)
    header[0:4] = b"PSEQ"
    struct.pack_into("<HBB", header, 4, 32, 0, 2)
    struct.pack_into("<H", header, 8, 32)
    struct.pack_into("<II", header, 10, channels, len(frames))
    header[18] = step_ms
    path.write_bytes(bytes(header) + b"".join(frames))


def make_player(tmp_path, when="2026-10-12T18:00:00", **extra):
    write_fseq(tmp_path / "a.fseq", 12, [bytes([1] * 12), bytes([2] * 12)])  # 2 frames = 0.1 s
    raw = {
        "show_dir": str(tmp_path),
        "windows": [{"start": "16:50", "end": "23:15"}],
        "playlist": [
            {"type": "fseq", "file": "a.fseq"},
            {"type": "effect", "effect": "solid", "seconds": 1, "colors": ["#400080"]},
        ],
        "live": {"effect": "solid", "colors": ["#FF3000"]},
        "override_minutes": 1,
    }
    raw.update(extra)
    clock = Clock(when)
    player = Player(parse_config(raw), tiny_layout(), NullOutput(), clock=clock)
    return player, clock


def pixels(frame) -> list[tuple]:
    return [tuple(int(v) for v in p) for p in np.asarray(frame).reshape(-1, 3)]


# ---- schedule windows ----

def test_window_across_midnight():
    cfg = parse_config({"windows": [{"days": ["fri"], "start": "22:00", "end": "01:00"}]})
    assert cfg.in_window(datetime(2026, 10, 16, 23, 0))  # Friday night
    assert cfg.in_window(datetime(2026, 10, 17, 0, 30))  # early Saturday, still Friday's show
    assert not cfg.in_window(datetime(2026, 10, 17, 1, 0))
    assert not cfg.in_window(datetime(2026, 10, 15, 23, 0))  # Thursday
    assert cfg.window_end(datetime(2026, 10, 16, 23, 0)) == datetime(2026, 10, 17, 1, 0)


def test_unknown_effect_rejected():
    with pytest.raises(ValueError):
        parse_config({"playlist": [{"type": "effect", "effect": "lasers"}]})


# ---- player priorities ----

def test_playlist_plays_in_order_and_advances(tmp_path):
    player, _ = make_player(tmp_path)
    assert pixels(player.tick(0.0))[0] == (1, 1, 1)
    assert pixels(player.tick(0.06))[0] == (2, 2, 2)
    assert pixels(player.tick(0.11))[0] == PURPLE  # fseq finished -> solid effect
    assert player.status(0.11)["playing"] == "Solid"
    assert pixels(player.tick(1.2))[0] == (1, 1, 1)  # effect's 1 s is up -> back to the fseq


def test_outside_window_blacks_out_then_goes_quiet(tmp_path):
    player, clock = make_player(tmp_path)
    player.tick(0.0)
    clock.now = datetime(2026, 10, 12, 23, 30)
    assert not np.asarray(player.tick(1.0)).any()  # black frame first
    assert player.tick(1.5) is not None
    assert player.tick(2.1) is None  # then stop sending
    assert player.status(2.1)["mode"] == "idle"


def test_live_override_wins_then_expires(tmp_path):
    player, _ = make_player(tmp_path)
    player.tick(0.0)
    player.set_live({"colors": [PURPLE, ORANGE], "effect": "chase", "band": 1}, now_t=1.0)
    frame = pixels(player.tick(1.0))
    assert frame[:2] == [PURPLE, ORANGE]
    assert player.status(1.0)["mode"] == "live"
    assert player.status(1.0)["override_seconds_left"] == 60
    assert pixels(player.tick(62.0))[0] in [(1, 1, 1), (2, 2, 2), PURPLE]  # back to the show
    assert player.status(62.0)["mode"] == "show"


def test_live_update_same_effect_keeps_clock(tmp_path):
    player, _ = make_player(tmp_path)
    player.set_live({"effect": "chase", "colors": [PURPLE]}, now_t=5.0)
    first = player._override
    player.set_live({"colors": [ORANGE]}, now_t=7.0)
    assert player._override is first and first.t0 == 5.0
    assert pixels(player.tick(7.0))[0] == ORANGE


def test_brightness_scales_output_only(tmp_path):
    player, _ = make_player(tmp_path)
    player.set_live({"colors": [(200, 100, 50)], "brightness": 0.5}, now_t=0.0)
    assert pixels(player.tick(0.0))[0] == (100, 50, 25)
    assert pixels(player.frame)[0] == (200, 100, 50)


def test_item_brightness_and_name_and_live_brightness_only_while_live(tmp_path):
    player, _ = make_player(tmp_path, playlist=[
        {"type": "effect", "effect": "solid", "seconds": 10, "colors": ["#C86432"], "brightness": 50, "name": "Dim"},
    ])
    assert pixels(player.tick(0.0))[0] == (100, 50, 25)
    assert player.status(0.0)["playing"] == "Dim"
    player.set_live({"colors": [(200, 100, 50)], "brightness": 1.0}, now_t=1.0)
    assert pixels(player.tick(1.0))[0] == (200, 100, 50)
    player.set_live({"brightness": 0.1}, now_t=1.5)
    player.resume()
    assert pixels(player.tick(2.0))[0] == (100, 50, 25)  # tablet brightness doesn't leak into the show


def test_off_until_resume(tmp_path):
    player, _ = make_player(tmp_path)
    player.tick(0.0)
    player.turn_off()
    assert not np.asarray(player.tick(0.1)).any()
    assert player.status()["mode"] == "off"
    player.resume()
    assert player.tick(2.0) is not None and player.status()["mode"] == "show"


# ---- HTTP API ----

@pytest.fixture
def api(tmp_path):
    player, clock = make_player(tmp_path, api_token="sekret")
    server = make_server(player, "127.0.0.1", 0)
    serve_in_thread(server)
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path, body=None, token=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method="POST" if body is not None else "GET")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Api-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    yield call, player, clock
    server.shutdown()


def test_api_live_and_status(api):
    call, player, _ = api
    code, status = call("/api/live", {"effect": "chase", "colors": ["#400080", "#ff3000"], "speed": 70})
    assert code == 200 and status["mode"] == "live"
    assert status["live"]["colors"] == ["#400080", "#FF3000"]
    assert player.live_params["speed"] == 70


def test_api_rejects_bad_input(api):
    call, _, _ = api
    assert call("/api/live", {"colors": ["purple"]})[0] == 400
    assert call("/api/live", {"effect": "lasers"})[0] == 400
    assert call("/api/live", {"colors": []})[0] == 400


def test_api_admin_needs_token(api):
    call, _, _ = api
    assert call("/api/off", {})[0] == 403
    code, status = call("/api/off", {}, token="sekret")
    assert code == 200 and status["mode"] == "off"


def test_api_live_refused_outside_window(api):
    call, _, clock = api
    clock.now = datetime(2026, 10, 12, 3, 0)
    assert call("/api/live", {"colors": ["#FF0000"]})[0] == 409


def test_api_page_options_preview(api):
    call, player, _ = api
    player.tick(0.0)
    assert call("/api/options")[1]["effects"] == ["chase", "fade", "twinkle", "strobe", "solid"]
    preview = call("/api/preview")[1]["models"]
    assert preview == [{"name": "Line", "controller": "A", "rgb": "01" * 12}]


def test_playlist_reports_current_position_and_next(tmp_path):
    player, _ = make_player(tmp_path)
    player.tick(0.0)
    pl = player.playlist(0.05)
    assert [(i["name"], i["type"], i["seconds"]) for i in pl["items"]] == [("a", "fseq", 0.1), ("Solid", "effect", 1.0)]
    assert pl["items"][1]["colors"] == ["#400080"]
    assert (pl["current"], pl["position"], pl["next"], pl["paused"]) == (0, 0.1, 1, False)
    player.set_live({"colors": [ORANGE]}, now_t=0.06)
    assert player.playlist(0.06)["paused"] is True


def test_play_item_jumps_and_continues_from_there(tmp_path):
    player, _ = make_player(tmp_path)
    player.tick(0.0)
    player.set_live({"colors": [ORANGE]}, now_t=0.01)
    player.play_item(1, now_t=0.02)
    assert pixels(player.tick(0.02))[0] == PURPLE  # override dropped, item 1 (solid) playing
    assert player.playlist(0.02)["current"] == 1
    assert pixels(player.tick(1.1))[0] == (1, 1, 1)  # then wraps to item 0
    with pytest.raises(ValueError):
        player.play_item(5)


def test_api_playlist_and_play(api):
    call, player, _ = api
    player.tick(0.0)
    code, pl = call("/api/playlist")
    assert code == 200 and len(pl["items"]) == 2
    assert call("/api/play", {"index": 1})[0] == 403  # admin only
    assert call("/api/play", {"index": 1}, token="sekret")[0] == 200
    assert call("/api/playlist")[1]["current"] == 1
    assert call("/api/play", {"index": 9}, token="sekret")[0] == 400
