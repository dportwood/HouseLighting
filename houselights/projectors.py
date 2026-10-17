"""Projectors: the video library on this PC, and control/sync of projector Pis.

Each projector Pi runs projector/hl_projector.py. The workflow:
  inside   projector in *sync* mode (writable): Sync copies the playlist's videos
           over and removes ones no longer needed.
  outside  projector in *show* mode (read-only, safe to lose power): during show
           hours it's told to play its playlist, outside show hours to go black.
           After its daily power-up it's picked up again automatically.

Reordering the playlist outside works as long as the videos are already on the
projector; new videos need a trip inside to sync.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .config import Config, ProjectorConfig, save_state

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv"}
POLL_SECONDS = 5
LIBRARY_REFRESH_SECONDS = 60
RETRY_SECONDS = 60
UPLOAD_CHUNK = 1024 * 1024


class MediaLibrary:
    """Videos under media_dir, grouped by the AtmosFX folder layout: collection / version / file."""

    def __init__(self, root: Path | None):
        self.root = root
        self._cache: list[dict] = []
        self._scanned = 0.0
        self._lock = threading.Lock()

    def scan(self, max_age: float = 30.0) -> list[dict]:
        with self._lock:
            if self.root is None or not self.root.is_dir():
                return []
            if time.monotonic() - self._scanned < max_age:
                return self._cache
            files = []
            for p in sorted(self.root.rglob("*")):
                if not p.is_file() or p.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                rel = p.relative_to(self.root)
                parts = rel.parts
                files.append({
                    "path": rel.as_posix(),
                    "size": p.stat().st_size,
                    "collection": parts[0] if len(parts) > 1 else "Other",
                    "variant": parts[1] if len(parts) > 2 else "",
                    "title": p.stem,
                })
            self._cache, self._scanned = files, time.monotonic()
            return files

    def sizes(self) -> dict[str, int]:
        return {f["path"]: f["size"] for f in self.scan()}

    def path(self, rel: str) -> Path:
        if self.root is None:
            raise ValueError("No media_dir configured")
        parts = PurePosixPath(rel).parts
        if not parts or any(p in ("..", ".") or ":" in p for p in parts) or PurePosixPath(rel).is_absolute():
            raise ValueError(f"Bad file name {rel!r}")
        p = self.root.joinpath(*parts)
        if not p.is_file():
            raise FileNotFoundError(f"Not in the media folder: {rel}")
        return p


class ProjectorClient:
    def __init__(self, cfg: ProjectorConfig):
        self.cfg = cfg

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.cfg.token:
            h["X-Api-Token"] = self.cfg.token
        return h

    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 4.0) -> dict:
        conn = http.client.HTTPConnection(self.cfg.host, self.cfg.port, timeout=timeout)
        try:
            data = None if body is None else json.dumps(body).encode()
            conn.request(method, path, body=data, headers=self._headers())
            resp = conn.getresponse()
            payload = json.loads(resp.read() or b"{}")
            if resp.status >= 400:
                raise RuntimeError(payload.get("error") or f"HTTP {resp.status}")
            return payload
        finally:
            conn.close()

    def status(self) -> dict:
        return self.request("GET", "/api/status")

    def library(self) -> dict[str, int]:
        return {f["path"]: f["size"] for f in self.request("GET", "/api/library", timeout=15)["files"]}

    def play(self, files: list[str], shuffle: bool) -> dict:
        return self.request("POST", "/api/play", {"files": files, "loop": True, "shuffle": shuffle}, timeout=10)

    def stop(self) -> dict:
        return self.request("POST", "/api/stop", {})

    def mode(self, mode: str) -> dict:
        return self.request("POST", "/api/mode", {"mode": mode}, timeout=30)

    def power(self, action: str) -> dict:
        return self.request("POST", "/api/power", {"action": action})

    def delete(self, rel: str) -> None:
        self.request("DELETE", "/api/library/" + quote(rel), timeout=15)

    def upload(self, rel: str, source: Path, progress) -> None:
        size = source.stat().st_size
        conn = http.client.HTTPConnection(self.cfg.host, self.cfg.port, timeout=120)
        try:
            conn.putrequest("PUT", "/api/library/" + quote(rel))
            conn.putheader("Content-Length", str(size))
            conn.putheader("Content-Type", "application/octet-stream")
            if self.cfg.token:
                conn.putheader("X-Api-Token", self.cfg.token)
            conn.endheaders()
            with open(source, "rb") as f:
                while chunk := f.read(UPLOAD_CHUNK):
                    conn.send(chunk)
                    progress(len(chunk))
            resp = conn.getresponse()
            payload = json.loads(resp.read() or b"{}")
            if resp.status >= 400:
                raise RuntimeError(payload.get("error") or f"HTTP {resp.status}")
        finally:
            conn.close()


class _State:
    def __init__(self):
        self.online = False
        self.status: dict = {}
        self.error = ""
        self.last_seen = 0.0
        self.device_files: dict[str, int] | None = None
        self.files_checked = 0.0
        self.uptime = -1
        self.user_stopped = False
        self.started_by = ""  # "schedule" or "manual" for what we last started
        self.next_try = 0.0
        self.sync = {"state": "idle"}


class ProjectorManager:
    def __init__(self, config: Config, library: MediaLibrary, in_window, clock=time.monotonic):
        self.config = config
        self.library = library
        self.in_window = in_window  # callable -> bool, the light show's schedule
        self.clock = clock
        self.clients = [ProjectorClient(p) for p in config.projectors]
        self.states = [_State() for _ in config.projectors]
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._was_in_window = False

    # ---- lifecycle ----

    def start(self) -> None:
        threading.Thread(target=self._run, name="projectors", daemon=True).start()

    def stop_thread(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                print(f"{datetime.now():%Y-%m-%d %H:%M:%S} projector poll error:\n{traceback.format_exc()}", flush=True)
            self._stop.wait(POLL_SECONDS)

    # ---- polling and the schedule ----

    def poll_once(self) -> None:
        in_window = bool(self.in_window())
        window_started = in_window and not self._was_in_window
        self._was_in_window = in_window
        for i in range(len(self.clients)):
            self._poll(i, in_window, window_started)

    def _poll(self, i: int, in_window: bool, window_started: bool) -> None:
        client, st = self.clients[i], self.states[i]
        now = self.clock()
        try:
            status = client.status()
        except Exception as e:
            with self._lock:
                st.online, st.error = False, _short(e)
            return
        with self._lock:
            rebooted = status.get("uptime", 0) < st.uptime
            st.online, st.status, st.last_seen, st.error = True, status, now, ""
            st.uptime = status.get("uptime", 0)
            if window_started:
                st.user_stopped = False
            refresh = st.device_files is None or rebooted or now - st.files_checked > LIBRARY_REFRESH_SECONDS
        if refresh:
            try:
                files = client.library()
                with self._lock:
                    st.device_files, st.files_checked = files, now
            except Exception as e:
                with self._lock:
                    st.error = _short(e)
        self._follow_schedule(i, in_window)

    def _follow_schedule(self, i: int, in_window: bool) -> None:
        cfg, client, st = self.config.projectors[i], self.clients[i], self.states[i]
        with self._lock:
            playing = st.status.get("state") == "playing"
            syncing = st.sync.get("state") == "syncing"
            available = self._available(i)
            ready = bool(available)
            now = self.clock()
            want_start = in_window and not st.user_stopped and ready and not playing and not syncing and now >= st.next_try
            want_stop = not in_window and playing and st.started_by == "schedule"
        try:
            if want_start:
                st.status = client.play(available, cfg.shuffle)
                st.started_by = "schedule"
            elif want_stop:
                st.status = client.stop()
                st.started_by = ""
        except Exception as e:
            with self._lock:
                st.error, st.next_try = _short(e), self.clock() + RETRY_SECONDS

    # ---- what the page shows ----

    def _missing(self, i: int) -> list[str]:
        """Playlist videos that aren't on the projector (or differ in size)."""
        st, want = self.states[i], self.config.projectors[i].playlist
        if st.device_files is None:
            return list(want)
        sizes = self.library.sizes()
        return [f for f in want if f not in st.device_files or (f in sizes and st.device_files[f] != sizes[f])]

    def _available(self, i: int) -> list[str]:
        """The playlist minus videos that aren't on the projector yet: play what we can until the next sync."""
        missing = set(self._missing(i))
        return [f for f in self.config.projectors[i].playlist if f not in missing]

    def snapshot(self) -> list[dict]:
        out = []
        with self._lock:
            sizes = self.library.sizes()
            for i, (cfg, st) in enumerate(zip(self.config.projectors, self.states)):
                missing = self._missing(i)
                extra = [f for f in (st.device_files or {}) if f not in set(cfg.playlist)]
                s = st.status
                out.append({
                    "index": i, "name": cfg.name, "host": cfg.host, "online": st.online, "error": st.error,
                    "mode": s.get("mode"), "mode_after_reboot": s.get("mode_after_reboot"),
                    "state": s.get("state") if st.online else None, "file": s.get("file"),
                    "position": s.get("position"), "duration": s.get("duration"),
                    "disk_free": s.get("disk_free"), "disk_total": s.get("disk_total"), "version": s.get("version"),
                    "playlist": cfg.playlist, "shuffle": cfg.shuffle,
                    "missing": missing, "missing_bytes": sum(sizes.get(f, 0) for f in missing),
                    "not_in_library": [f for f in cfg.playlist if f not in sizes],
                    "extra": extra, "files_known": st.device_files is not None,
                    "user_stopped": st.user_stopped, "sync": dict(st.sync),
                })
        return out

    # ---- actions from the page ----

    def _check(self, i: int) -> None:
        if not 0 <= i < len(self.clients):
            raise ValueError(f"No projector {i}")

    def set_playlist(self, i: int, files: list[str], shuffle: bool) -> None:
        self._check(i)
        sizes = self.library.sizes()
        unknown = [f for f in files if f not in sizes]
        if unknown:
            raise ValueError("Not in the media folder: " + ", ".join(unknown[:3]))
        cfg, st = self.config.projectors[i], self.states[i]
        with self._lock:
            cfg.playlist, cfg.shuffle = list(dict.fromkeys(files)), bool(shuffle)  # no duplicates
            save_state(self.config)
            replay = st.online and st.status.get("state") == "playing" and bool(self._available(i))
        if replay:  # it's playing: switch to the edited playlist now (videos already on the projector)
            self.play(i)

    def play(self, i: int, file: str | None = None) -> None:
        self._check(i)
        cfg, st = self.config.projectors[i], self.states[i]
        with self._lock:
            files = [file] if file else self._available(i)
        if not files:
            raise ValueError("None of the playlist's videos are on the projector yet — sync it first")
        status = self.clients[i].play(files, False if file else cfg.shuffle)
        with self._lock:
            st.status, st.user_stopped, st.next_try = status, False, 0.0
            st.started_by = "schedule" if (not file and self.in_window()) else "manual"

    def stop(self, i: int) -> None:
        self._check(i)
        status = self.clients[i].stop()
        with self._lock:
            st = self.states[i]
            st.status, st.user_stopped, st.started_by = status, True, ""

    def set_mode(self, i: int, mode: str) -> None:
        self._check(i)
        status = self.clients[i].mode(mode)
        with self._lock:
            self.states[i].status = status  # shows "switching to ... mode" until it reboots
            self.states[i].device_files = None  # re-read after it comes back

    def power(self, i: int, action: str) -> None:
        self._check(i)
        self.clients[i].power(action)

    def sync(self, i: int, remove_extra: bool = True) -> None:
        """Copy the playlist's missing videos to the projector in the background (sync mode only)."""
        self._check(i)
        st = self.states[i]
        with self._lock:
            if st.sync.get("state") == "syncing":
                raise ValueError("Already syncing")
            if not st.online:
                raise ValueError("The projector is offline")
            if st.status.get("mode") != "sync" or st.status.get("mode_after_reboot") == "show":
                raise ValueError("Switch the projector to sync mode first (bring it inside)")
            st.sync = {"state": "syncing", "done_bytes": 0, "total_bytes": 0, "done_files": 0, "total_files": 0,
                       "current": "", "message": "Checking…"}
        threading.Thread(target=self._sync, args=(i, remove_extra), name=f"sync-{i}", daemon=True).start()

    def _sync(self, i: int, remove_extra: bool) -> None:
        cfg, client, st = self.config.projectors[i], self.clients[i], self.states[i]
        try:
            device = client.library()
            sizes = self.library.sizes()
            todo = [f for f in cfg.playlist if f in sizes and device.get(f) != sizes[f]]
            extra = [f for f in device if f not in set(cfg.playlist)] if remove_extra else []
            with self._lock:
                st.sync.update(total_bytes=sum(sizes[f] for f in todo), total_files=len(todo))
            for f in extra:  # free space first
                with self._lock:
                    st.sync["message"] = f"Removing {f}"
                client.delete(f)
            for n, f in enumerate(todo, 1):
                with self._lock:
                    st.sync.update(current=f, message=f"Copying {n} of {len(todo)}")

                def progress(k, st=st):
                    with self._lock:
                        st.sync["done_bytes"] += k

                client.upload(f, self.library.path(f), progress)
                with self._lock:
                    st.sync["done_files"] = n
            files = client.library()
            with self._lock:
                st.device_files, st.files_checked = files, self.clock()
                st.sync.update(state="done", current="", message=f"Copied {len(todo)}, removed {len(extra)}",
                               finished=datetime.now().isoformat(timespec="seconds"))
        except Exception as e:
            with self._lock:
                st.sync.update(state="error", message=_short(e))


def _short(e: Exception) -> str:
    msg = str(e) or type(e).__name__
    return msg if len(msg) < 200 else msg[:197] + "…"
