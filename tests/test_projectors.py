"""Projector agent (projector/hl_projector.py) and the app's ProjectorManager, end to end over HTTP."""

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from houselights.config import ProjectorConfig, parse_config
from houselights.projectors import MediaLibrary, ProjectorManager

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hl_projector", ROOT / "projector" / "hl_projector.py")
hl = importlib.util.module_from_spec(spec)
sys.modules["hl_projector"] = hl
spec.loader.exec_module(hl)


@pytest.fixture
def agent(tmp_path):
    cfg = dict(hl.DEFAULT_CONFIG, name="Test projector", media_dir=str(tmp_path / "pi-media"), token="pitoken")
    a = hl.Agent(cfg, hl.FakePlayer(), fake_mode="sync")
    server = hl.make_server(a, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield a, server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "AtmosFX"
    for rel, size in [("Bone Chillers/Wall/Dancing Dead.mp4", 3000), ("Bone Chillers/Window/Jittery Bones.mp4", 2000),
                      ("Phantasms/Siren.mp4", 1500), ("Loose.mov", 100)]:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(bytes([len(rel) % 256]) * size)
    (root / "Bone Chillers" / "disc.ISO").write_bytes(b"x")
    return MediaLibrary(root)


def call(port, method, path, body=None, token="pitoken"):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    if token:
        req.add_header("X-Api-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ---- the agent on its own ----

def test_agent_upload_play_stop_and_path_safety(agent):
    a, port = agent
    assert call(port, "PUT", "/api/library/Bone%20Chillers/a.mp4", b"12345")[0] == 200
    assert call(port, "GET", "/api/library")[1]["files"] == [{"path": "Bone Chillers/a.mp4", "size": 5}]
    for bad in ("..%2Fescape.mp4", "%2Fetc%2Fpasswd.mp4", "C%3A%2Fx.mp4", "notes.txt"):
        assert call(port, "PUT", f"/api/library/{bad}", b"x")[0] == 400, bad
    assert call(port, "PUT", "/api/library/x.mp4", b"x", token="wrong")[0] == 403
    assert call(port, "POST", "/api/play", {"files": ["missing.mp4"]})[0] == 400
    code, st = call(port, "POST", "/api/play", {"files": ["Bone Chillers/a.mp4"]})
    assert code == 200 and st["state"] == "playing" and st["file"] == "Bone Chillers/a.mp4"
    assert call(port, "POST", "/api/stop", {})[1]["state"] == "idle"
    assert call(port, "DELETE", "/api/library/Bone%20Chillers/a.mp4")[0] == 200
    assert not (a.media / "Bone Chillers").exists()  # empty folder tidied


def test_agent_refuses_changes_in_show_mode(agent):
    a, port = agent
    call(port, "PUT", "/api/library/a.mp4", b"1")
    a.fake_mode = "show"
    code, body = call(port, "PUT", "/api/library/b.mp4", b"2")
    assert code == 409 and "show mode" in body["error"]
    assert call(port, "DELETE", "/api/library/a.mp4")[0] == 409
    assert call(port, "POST", "/api/play", {"files": ["a.mp4"]})[0] == 200  # playing still works
    st = call(port, "GET", "/api/status")[1]
    assert st["mode"] == "show" and st["name"] == "Test projector" and st["disk_free"] > 0


def test_mpv_args_follow_profile_and_audio():
    args = hl.mpv_args(dict(hl.DEFAULT_CONFIG, mpv_profile="drm-hw"))
    assert "--vo=drm" in args and "--audio=no" in args and "--idle=yes" in args
    assert "--audio=no" not in hl.mpv_args(dict(hl.DEFAULT_CONFIG, audio=True))


# ---- the library on the PC ----

def test_library_groups_by_collection_and_version_and_skips_isos(library):
    files = {f["path"]: f for f in library.scan()}
    assert set(files) == {"Bone Chillers/Wall/Dancing Dead.mp4", "Bone Chillers/Window/Jittery Bones.mp4",
                          "Phantasms/Siren.mp4", "Loose.mov"}
    f = files["Bone Chillers/Wall/Dancing Dead.mp4"]
    assert (f["collection"], f["variant"], f["title"], f["size"]) == ("Bone Chillers", "Wall", "Dancing Dead", 3000)
    assert files["Phantasms/Siren.mp4"]["variant"] == "" and files["Loose.mov"]["collection"] == "Other"
    with pytest.raises(ValueError):
        library.path("../secret.mp4")


# ---- manager <-> agent ----

def make_manager(tmp_path, port, library, window):
    cfg = parse_config({"show_dir": str(tmp_path), "state_file": str(tmp_path / "show.json")})
    cfg.projectors = [ProjectorConfig("Front", "127.0.0.1", port, "pitoken")]
    return ProjectorManager(cfg, library, lambda: window["on"])


def wait_sync(mgr):
    for _ in range(200):
        if mgr.snapshot()[0]["sync"]["state"] != "syncing":
            return
        threading.Event().wait(0.05)


def test_sync_copies_playlist_and_removes_extras_then_schedule_plays(tmp_path, agent, library):
    a, port = agent
    window = {"on": False}
    mgr = make_manager(tmp_path, port, library, window)
    call(port, "PUT", "/api/library/old/stale.mp4", b"stale")
    playlist = ["Bone Chillers/Wall/Dancing Dead.mp4", "Phantasms/Siren.mp4"]
    mgr.set_playlist(0, playlist, shuffle=False)
    mgr.poll_once()
    snap = mgr.snapshot()[0]
    assert snap["online"] and snap["mode"] == "sync" and snap["missing"] == playlist
    assert snap["missing_bytes"] == 4500 and snap["extra"] == ["old/stale.mp4"]

    mgr.sync(0)
    wait_sync(mgr)
    snap = mgr.snapshot()[0]
    assert snap["sync"]["state"] == "done", snap["sync"]
    assert snap["sync"]["done_bytes"] == 4500 and snap["missing"] == [] and snap["extra"] == []
    assert (a.media / "Phantasms" / "Siren.mp4").read_bytes() == library.path("Phantasms/Siren.mp4").read_bytes()

    a.fake_mode = "show"  # carried outside
    mgr.poll_once()
    assert mgr.snapshot()[0]["state"] == "idle"  # not show time yet
    window["on"] = True
    mgr.poll_once()
    snap = mgr.snapshot()[0]
    assert snap["state"] == "playing" and a.playlist == playlist
    window["on"] = False
    mgr.poll_once()
    assert mgr.snapshot()[0]["state"] == "idle"  # black after the show


def test_sync_refused_in_show_mode_and_manual_stop_holds_until_next_show(tmp_path, agent, library):
    a, port = agent
    window = {"on": True}
    mgr = make_manager(tmp_path, port, library, window)
    a.fake_mode = "show"
    mgr.set_playlist(0, ["Loose.mov"], shuffle=False)
    mgr.poll_once()
    with pytest.raises(ValueError, match="sync mode"):
        mgr.sync(0)
    assert mgr.snapshot()[0]["state"] == "idle"  # can't play: the video isn't on the projector

    a.fake_mode = "sync"
    call(port, "PUT", "/api/library/Loose.mov", library.path("Loose.mov").read_bytes())
    mgr.states[0].files_checked = -1e9  # force a re-read of the projector's files
    mgr.poll_once()
    assert mgr.snapshot()[0]["state"] == "playing"
    mgr.stop(0)
    mgr.poll_once()
    assert mgr.snapshot()[0]["state"] == "idle"  # stays stopped
    window["on"] = False
    mgr.poll_once()
    window["on"] = True
    mgr.poll_once()
    assert mgr.snapshot()[0]["state"] == "playing"  # the next show starts it again


def test_power_up_outside_is_picked_up_again(tmp_path, agent, library):
    a, port = agent
    window = {"on": True}
    mgr = make_manager(tmp_path, port, library, window)
    call(port, "PUT", "/api/library/Loose.mov", library.path("Loose.mov").read_bytes())
    mgr.set_playlist(0, ["Loose.mov"], shuffle=True)
    mgr.poll_once()
    assert mgr.snapshot()[0]["state"] == "playing"
    a.player.stop()  # it lost power and came back up idle
    a.started += 10_000  # uptime goes back to ~0
    mgr.poll_once()
    assert mgr.snapshot()[0]["state"] == "playing"


def test_offline_projector_reports_error(tmp_path, library):
    mgr = make_manager(tmp_path, 1, library, {"on": True})  # nothing listens on port 1
    mgr.poll_once()
    snap = mgr.snapshot()[0]
    assert snap["online"] is False and snap["error"]
    with pytest.raises(ValueError, match="offline"):
        mgr.sync(0)


def test_playlist_saved_to_show_json(tmp_path, agent, library):
    _, port = agent
    mgr = make_manager(tmp_path, port, library, {"on": False})
    mgr.set_playlist(0, ["Loose.mov", "Loose.mov", "Phantasms/Siren.mp4"], shuffle=True)
    saved = json.loads((tmp_path / "show.json").read_text())
    assert saved["projectors"]["Front"] == {"playlist": ["Loose.mov", "Phantasms/Siren.mp4"], "shuffle": True}
    with pytest.raises(ValueError, match="media folder"):
        mgr.set_playlist(0, ["Nope.mp4"], shuffle=False)


def test_plays_the_videos_it_has_while_others_wait_for_a_sync(tmp_path, agent, library):
    a, port = agent
    mgr = make_manager(tmp_path, port, library, {"on": True})
    call(port, "PUT", "/api/library/Loose.mov", library.path("Loose.mov").read_bytes())
    a.fake_mode = "show"
    mgr.set_playlist(0, ["Phantasms/Siren.mp4", "Loose.mov"], shuffle=False)  # Siren isn't on it
    mgr.poll_once()
    snap = mgr.snapshot()[0]
    assert snap["state"] == "playing" and a.playlist == ["Loose.mov"] and snap["missing"] == ["Phantasms/Siren.mp4"]
