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
        "state_file": str(tmp_path / "show.json"),
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


# ---- schedule details ----

def test_window_dates_enabled_and_next_start():
    from houselights.config import parse_config

    cfg = parse_config({"windows": [
        {"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "start": "17:00", "end": "23:00",
         "from": "2026-10-01", "to": "2026-10-31"},
        {"days": ["sat"], "start": "10:00", "end": "11:00", "enabled": False},
    ]})
    assert cfg.in_window(datetime(2026, 10, 31, 18, 0))
    assert not cfg.in_window(datetime(2026, 11, 1, 18, 0))  # season over
    assert not cfg.in_window(datetime(2026, 10, 17, 10, 30))  # disabled window
    assert cfg.next_start(datetime(2026, 10, 12, 18, 0)) == datetime(2026, 10, 13, 17, 0)
    assert cfg.next_start(datetime(2026, 10, 31, 23, 30)) is None
    assert cfg.next_start(datetime(2026, 9, 1, 12, 0)) == datetime(2026, 10, 1, 17, 0)


def test_state_file_round_trip_overrides_toml(tmp_path):
    from houselights.config import load_config

    write_fseq(tmp_path / "a.fseq", 12, [bytes(12)])
    (tmp_path / "config.toml").write_text(
        f"show_dir = '{tmp_path}'\n"
        "[[windows]]\nstart = '16:50'\nend = '23:15'\n"
        "[[playlist]]\ntype = 'fseq'\nfile = 'a.fseq'\n"
    )
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.state_file == tmp_path / "show.json" and not cfg.state_file.exists()
    player = Player(cfg, tiny_layout(), NullOutput())
    player.update_show(windows=[], shuffle=True)
    again = load_config(tmp_path / "config.toml")
    assert again.windows == [] and again.shuffle is True
    assert [i.describe() for i in again.playlist] == ["a"]
    assert again.playlist[0].id == cfg.playlist[0].id


def test_update_show_keeps_the_current_item_playing(tmp_path):
    from houselights.config import parse_item

    player, _ = make_player(tmp_path)
    player.tick(0.0)  # playing item 0 (a.fseq)
    a, solid = player.config.playlist
    new = parse_item({"type": "effect", "effect": "solid", "colors": ["#FF3000"], "seconds": 5}, tmp_path)
    player.update_show(playlist=[solid, new, a])
    assert player.playlist(0.01)["current"] == 2  # still a.fseq, now at index 2
    assert pixels(player.tick(0.06))[0] == (2, 2, 2)  # carried on, no restart
    player.update_show(playlist=[solid, new])  # remove what's playing
    assert player.playlist(0.07)["current"] is None
    assert pixels(player.tick(0.2))[0] == PURPLE  # when it ends, playlist starts from the top


def test_api_edit_playlist_schedule_and_add(api, tmp_path):
    call, player, _ = api
    player.tick(0.0)
    tok = "sekret"
    items = call("/api/playlist")[1]["items"]
    assert items[0]["file"] == "a.fseq" and items[1]["effect"] == "solid" and items[1]["brightness"] == 100
    edited = [dict(items[1], name="Purple", seconds=20, brightness=50), items[0]]
    code, pl = call("/api/playlist", {"items": edited, "shuffle": False}, token=tok)
    assert code == 200 and [i["name"] for i in pl["items"]] == ["Purple", "a"]
    assert pl["items"][0]["seconds"] == 20 and pl["items"][0]["brightness"] == 50

    code, pl = call("/api/playlist/add", {"name": "Witchy", "effect": "twinkle", "colors": ["#400080", "#00C000"],
                                          "seconds": 120, "speed": 100, "brightness": 100}, token=tok)
    assert code == 200 and pl["items"][-1]["name"] == "Witchy" and pl["items"][-1]["speed"] == 100

    code, sched = call("/api/schedule", {"windows": [{"days": ["fri", "sat"], "start": "18:00", "end": "01:00",
                                                       "from": "2026-12-01", "to": None}]}, token=tok)
    assert code == 200 and sched["windows"][0]["days"] == ["fri", "sat"] and sched["windows"][0]["from"] == "2026-12-01"

    import json as _json
    saved = _json.loads((tmp_path / "show.json").read_text())
    assert [i["name"] for i in saved["playlist"]] == ["Purple", "a", "Witchy"]
    assert saved["windows"][0]["end"] == "01:00"


def test_api_edit_rejects_bad_input_and_needs_admin(api):
    call, _, _ = api
    tok = "sekret"
    assert call("/api/schedule", {"windows": []})[0] == 403
    assert call("/api/playlist", {"items": [{"type": "fseq", "file": "../../secret.fseq"}]}, token=tok)[0] == 400
    assert call("/api/playlist", {"items": [{"type": "fseq", "file": "missing.fseq"}]}, token=tok)[0] == 400
    assert call("/api/playlist", {"items": [{"type": "effect", "effect": "lasers"}]}, token=tok)[0] == 400
    assert call("/api/schedule", {"windows": [{"days": [], "start": "18:00", "end": "19:00"}]}, token=tok)[0] == 400
    assert call("/api/schedule", {"windows": [{"days": ["mon"], "start": "25:00", "end": "19:00"}]}, token=tok)[0] == 400
    assert call("/api/playlist")[1]["items"][0]["name"] == "a"  # nothing changed


def test_api_options_reports_admin_per_caller_and_lists_sequences(api):
    call, _, _ = api
    assert call("/api/options")[1]["admin"] is False
    assert call("/api/options", token="sekret")[1]["admin"] is True
    assert call("/api/sequences")[1]["files"] == ["a.fseq"]


def test_api_shutdown_from_localhost_stops_player(api):
    call, player, _ = api
    assert call("/api/shutdown", {})[0] == 200
    assert player._stop.is_set()
