"""ControllerMonitor: is each FPP light controller online and receiving the player's frames?"""

from houselights.controllers import ControllerMonitor
from houselights.layout import Controller, Layout


def make(responses, skipped=None):
    layout = Layout([Controller("House", "10.0.0.10", "DDP", 1, 30), Controller("Left", "10.0.0.11", "DDP", 31, 15)], [], {})

    def fetch(ip, path):
        r = responses[ip]
        if isinstance(r, Exception):
            raise r
        return r[path]

    return ControllerMonitor(layout, skipped, fetch=fetch)


def status(packets, errors=0):
    return {"/api/fppd/status": {"mode_name": "remote", "status_name": "idle", "version": "10.2", "host_name": "FPP-House",
                                 "uptime": "1 day"},
            "/api/channel/input/stats": {"universes": [{"id": "DDP", "packetsReceived": str(packets), "errors": str(errors)}]}}


def by_name(mon):
    return {c["name"]: c for c in mon.snapshot()}


def test_online_receiving_and_new_errors():
    responses = {"10.0.0.10": status(100), "10.0.0.11": status(50)}
    mon = make(responses)
    assert by_name(mon)["House"]["online"] is None  # not checked yet
    mon.poll_once()
    house = by_name(mon)["House"]
    assert house["online"] and house["mode"] == "remote" and house["receiving"] is False  # nothing to compare yet
    responses["10.0.0.10"] = status(160, errors=2)
    mon.poll_once()
    snap = by_name(mon)
    assert snap["House"]["receiving"] is True and snap["House"]["new_errors"] == 2
    assert snap["Left"]["receiving"] is False  # count didn't move
    assert (snap["House"]["first_channel"], snap["House"]["last_channel"], snap["House"]["ip"]) == (1, 30, "10.0.0.10")


def test_offline_keeps_last_seen_and_says_why():
    responses = {"10.0.0.10": status(100), "10.0.0.11": status(1)}
    mon = make(responses, skipped=["Left"])
    mon.poll_once()
    seen = by_name(mon)["House"]["last_seen_at"]
    responses["10.0.0.10"] = TimeoutError("timed out")
    mon.poll_once()
    house = by_name(mon)["House"]
    assert house["online"] is False and "timed out" in house["error"] and house["last_seen_at"] == seen
    assert house["version"] == "10.2" and by_name(mon)["Left"]["skipped"] is True


def test_controller_without_ddp_counters_is_still_online():
    responses = {"10.0.0.10": {"/api/fppd/status": {"mode_name": "remote"}, "/api/channel/input/stats": {"universes": []}},
                 "10.0.0.11": status(1)}
    mon = make(responses)
    mon.poll_once()
    house = by_name(mon)["House"]
    assert house["online"] and house["receiving"] is False and "ddp_packets" not in house
