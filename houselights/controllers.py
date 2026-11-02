"""Status of the light controllers (Kulp K8-Pi boards running FPP), for the Devices tab.

Every POLL_SECONDS each controller's FPP API is asked for its status and DDP
input counters. A controller counts as *receiving* when its DDP packet count
went up since the previous check — i.e. the player's frames are arriving.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .layout import Layout

POLL_SECONDS = 10
TIMEOUT = 3.0


def _get(ip: str, path: str) -> dict:
    conn = http.client.HTTPConnection(ip, 80, timeout=TIMEOUT)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status}")
        return json.loads(body)
    finally:
        conn.close()


class ControllerMonitor:
    def __init__(self, layout: Layout, skipped: list[str] | None = None, clock=time.monotonic, fetch=_get):
        self.layout = layout
        self.skipped = set(skipped or [])
        self.clock = clock
        self.fetch = fetch  # (ip, path) -> dict; tests replace it
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._state: dict[str, dict] = {c.name: {"online": None} for c in layout.controllers}

    def start(self) -> None:
        threading.Thread(target=self._run, name="controllers", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                print(f"{datetime.now():%Y-%m-%d %H:%M:%S} controller poll error:\n{traceback.format_exc()}", flush=True)
            self._stop.wait(POLL_SECONDS)

    def poll_once(self) -> None:
        controllers = [c for c in self.layout.controllers if c.active]
        with ThreadPoolExecutor(max_workers=max(1, len(controllers))) as pool:
            for c, result in zip(controllers, pool.map(lambda c: self._check(c.ip), controllers)):
                self._record(c.name, result)

    def _check(self, ip: str) -> dict:
        try:
            status = self.fetch(ip, "/api/fppd/status")
        except Exception as e:
            return {"online": False, "error": _short(e)}
        result = {
            "online": True, "error": "",
            "mode": status.get("mode_name"), "status": status.get("status_name"),
            "version": status.get("version"), "hostname": status.get("host_name"),
            "uptime": status.get("uptime"),
        }
        try:
            stats = self.fetch(ip, "/api/channel/input/stats")
            ddp = next((u for u in stats.get("universes", []) if u.get("id") == "DDP"), None)
            if ddp:
                result["ddp_packets"] = int(ddp.get("packetsReceived", 0))
                result["ddp_errors"] = int(ddp.get("errors", 0))
        except Exception:
            pass  # status alone is enough to call it online
        return result

    def _record(self, name: str, result: dict) -> None:
        now = self.clock()
        with self._lock:
            prev = self._state.get(name, {})
            if result["online"]:
                packets = result.get("ddp_packets")
                before = prev.get("ddp_packets")
                result["receiving"] = packets is not None and before is not None and packets > before
                errs, errs_before = result.get("ddp_errors"), prev.get("ddp_errors")
                result["new_errors"] = (errs - errs_before) if errs is not None and errs_before is not None and errs >= errs_before else 0
                result["last_seen"] = now
                result["last_seen_at"] = datetime.now().isoformat(timespec="seconds")
            else:  # keep what we last knew, for "last seen"
                result = {**{k: prev.get(k) for k in ("version", "hostname", "last_seen", "last_seen_at")}, **result}
            self._state[name] = result

    def snapshot(self) -> list[dict]:
        with self._lock:
            out = []
            for c in self.layout.controllers:
                s = dict(self._state.get(c.name, {}))
                s.pop("last_seen", None)
                out.append({"name": c.name, "ip": c.ip, "first_channel": c.start, "last_channel": c.end,
                            "active": c.active, "skipped": c.name in self.skipped or c.ip in self.skipped,
                            **s})
            return out


def _short(e: Exception) -> str:
    if isinstance(e, TimeoutError) or "timed out" in str(e):
        return "Not responding (timed out)"
    if isinstance(e, ConnectionRefusedError):
        return "Refused the connection (FPP not running?)"
    msg = str(e) or type(e).__name__
    return msg if len(msg) < 160 else msg[:157] + "…"
