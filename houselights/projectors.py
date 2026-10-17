"""Projectors: the video library on this PC, and control/sync of projector Pis.

Each projector Pi runs projector/hl_projector.py. The workflow:
  inside   projector in *sync* mode (writable): Sync copies the playlist's videos
           over and removes ones no longer needed.
  outside  projector in *show* mode (read-only, safe to lose power): during show
           hours it's told to play its playlist, outside show hours to go black.
           After its daily power-up it's picked up again automatically.

Reordering the playlist outside works as long as the videos are already on the
projector; new videos need a trip inside to sync.

Finding projectors: a configured host name (projector1.local) is tried first;
otherwise, or when it fails, the last IP found by discovery. Discovery is a UDP
broadcast of b"HLPROJECTOR?" on port 8091 that every agent answers; agents also
announce themselves after boot. A projector that stops answering triggers a
rescan, so a new DHCP address is picked up within about half a minute.
"""

from __future__ import annotations

import http.client
import json
import socket
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
DISCOVERY_PORT = 8091
DISCOVERY_QUERY = b"HLPROJECTOR?"
RESCAN_SECONDS = 30  # at most this often while a projector isn't answering


def broadcast_targets() -> list[str]:
    """255.255.255.255 plus each local IPv4 network's broadcast (assumed /24), so every adapter is covered."""
    targets = ["255.255.255.255"]
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                bcast = ip.rsplit(".", 1)[0] + ".255"
                if bcast not in targets:
                    targets.append(bcast)
    except OSError:
        pass
    return targets


def scan(timeout: float = 1.5, port: int = DISCOVERY_PORT, targets: list[str] | None = None) -> list[dict]:
    """Broadcast a discovery query and collect the agents that answer, one entry per agent id."""
    found: dict[str, dict] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))
        for t in targets or broadcast_targets():
            try:
                sock.sendto(DISCOVERY_QUERY, (t, port))
            except OSError:
                pass
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            sock.settimeout(left)
            try:
                data, addr = sock.recvfrom(4096)
            except (socket.timeout, OSError):
                break
            info = _parse_announcement(data, addr[0])
            if info:
                found[info["id"]] = info
    return list(found.values())


def _parse_announcement(data: bytes, address: str) -> dict | None:
    try:
        info = json.loads(data)
    except ValueError:
        return None
    if not isinstance(info, dict) or info.get("type") != "hl-projector" or not info.get("id"):
        return None
    return {"id": str(info["id"]), "name": str(info.get("name", "")), "port": int(info.get("port", 8090)),
            "mode": info.get("mode"), "version": info.get("version"), "hostname": info.get("hostname"),
            "address": address}


class AnnouncementListener:
    """Hears the 'hello' broadcasts agents send after they boot."""

    def __init__(self, on_found, port: int = DISCOVERY_PORT):
        self.on_found = on_found
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", port))

    def start(self) -> None:
        threading.Thread(target=self._run, name="announcements", daemon=True).start()

    def close(self) -> None:
        self.sock.close()

    def _run(self) -> None:
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                return
            info = _parse_announcement(data, addr[0])
            if info:
                try:
                    self.on_found([info])
                except Exception:
                    print(traceback.format_exc(), flush=True)


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
        self.working = ""  # the address that last answered

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.cfg.token:
            h["X-Api-Token"] = self.cfg.token
        return h

    def targets(self) -> list[str]:
        """The configured host name, then the last address discovery found."""
        return list(dict.fromkeys(t for t in (self.cfg.host, self.cfg.address) if t))

    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 4.0) -> dict:
        targets = self.targets()
        if not targets:
            raise ConnectionError("Not found on the network yet — is it powered on?")
        last: Exception | None = None
        for target in targets:
            conn = http.client.HTTPConnection(target, self.cfg.port, timeout=timeout)
            try:
                data = None if body is None else json.dumps(body).encode()
                conn.request(method, path, body=data, headers=self._headers())
                resp = conn.getresponse()
                payload = json.loads(resp.read() or b"{}")
                if resp.status >= 400:
                    raise RuntimeError(payload.get("error") or f"HTTP {resp.status}")
                self.working = target
                return payload
            except OSError as e:  # can't connect/resolve: try the next address
                last = e
            finally:
                conn.close()
        raise last

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
        conn = http.client.HTTPConnection(self.working or self.cfg.target, self.cfg.port, timeout=120)
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
        self.discovered: dict[str, dict] = {}  # agent id -> what it last told us
        self.last_scan = 0.0
        self.last_scan_at = ""
        self.scan_port = DISCOVERY_PORT
        self.scan_targets: list[str] | None = None  # tests point this at 127.0.0.1
        self._listener: AnnouncementListener | None = None

    # ---- lifecycle ----

    def start(self) -> None:
        try:
            self._listener = AnnouncementListener(self.found, self.scan_port)
            self._listener.start()
        except OSError as e:
            print(f"{datetime.now():%Y-%m-%d %H:%M:%S} projector announcements unavailable: {e}", flush=True)
        threading.Thread(target=self._run, name="projectors", daemon=True).start()

    def stop_thread(self) -> None:
        self._stop.set()
        if self._listener:
            self._listener.close()

    # ---- discovery ----

    def scan_now(self, timeout: float = 1.5) -> list[dict]:
        found = scan(timeout, self.scan_port, self.scan_targets)
        with self._lock:
            self.last_scan = self.clock()
            self.last_scan_at = datetime.now().isoformat(timespec="seconds")
        self.found(found)
        return found

    def found(self, agents: list[dict]) -> None:
        """Record agents heard from, and point configured projectors at them (by id, else name)."""
        changed = False
        with self._lock:
            for a in agents:
                self.discovered[a["id"]] = {**a, "seen": datetime.now().isoformat(timespec="seconds")}
                for cfg in self.config.projectors:
                    if not (cfg.id == a["id"] or (not cfg.id and cfg.name.casefold() == a["name"].casefold())):
                        continue
                    if (cfg.id, cfg.address, cfg.port) != (a["id"], a["address"], a["port"]):
                        cfg.id, cfg.address, cfg.port = a["id"], a["address"], a["port"]
                        changed = True
            if changed:
                save_state(self.config)

    def discovered_list(self) -> list[dict]:
        with self._lock:
            ids = {p.id for p in self.config.projectors}
            return [{**a, "added": a["id"] in ids} for a in self.discovered.values()]

    def add_discovered(self, agent_id: str) -> None:
        with self._lock:
            a = self.discovered.get(agent_id)
            if a is None:
                raise ValueError("That projector hasn't been found — scan again")
            if any(p.id == agent_id for p in self.config.projectors):
                raise ValueError(f"{a['name']} is already added")
            name, n = a["name"] or a["hostname"] or "Projector", 2
            while any(p.name == name for p in self.config.projectors):
                name, n = f"{a['name']} {n}", n + 1
            cfg = ProjectorConfig(name=name, port=a["port"], id=a["id"], address=a["address"], added_from_page=True)
            self.config.projectors.append(cfg)
            self.clients.append(ProjectorClient(cfg))
            self.states.append(_State())
            save_state(self.config)

    def remove(self, i: int) -> None:
        self._check(i)
        with self._lock:
            if not self.config.projectors[i].added_from_page:
                raise ValueError("This projector is set up in config.toml; remove it there")
            for lst in (self.config.projectors, self.clients, self.states):
                del lst[i]
            save_state(self.config)

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
        with self._lock:
            n = len(self.clients)
        for i in range(n):
            try:
                self._poll(i, in_window, window_started)
            except IndexError:  # removed while we were polling
                break

    def _poll(self, i: int, in_window: bool, window_started: bool) -> None:
        client, st = self.clients[i], self.states[i]
        now = self.clock()
        try:
            status = client.status()
        except Exception as e:
            with self._lock:
                st.online, st.error = False, _short(e)
                rescan = now - self.last_scan >= RESCAN_SECONDS
            if rescan:  # maybe it came back on a different address
                self.scan_now()
                try:
                    status = client.status()
                except Exception:
                    return
            else:
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
                    "index": i, "name": cfg.name, "host": cfg.host, "address": cfg.address, "id": cfg.id,
                    "connected_via": self.clients[i].working if st.online else "",
                    "added_from_page": cfg.added_from_page, "online": st.online, "error": st.error,
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
