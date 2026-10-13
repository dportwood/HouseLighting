"""The show player: decides what should be on the lights each frame.

Priority, highest first:
  1. live override from the tablet/web UI (expires after `override_minutes`)
  2. "off" (someone pressed Off; cleared by Resume or when the next window starts)
  3. the playlist, while inside a schedule window
  4. nothing: send black briefly, then stop sending so the controllers are free

All public methods are thread-safe; the HTTP server calls them while `run()`
drives frames on its own thread.
"""

from __future__ import annotations

import random
import threading
import time
import traceback
from datetime import datetime, timedelta

import numpy as np

from .config import Config, Item
from .effects import EFFECTS
from .fseq import FSEQ, read_fseq
from .layout import Layout

BLACKOUT_SECONDS = 1.0


class Source:
    def __init__(self, label: str):
        self.label = label
        self.t0 = 0.0
        self.brightness = 1.0  # 0.05-1.0, applied to the output only

    def render(self, frame: np.ndarray, t: float) -> bool:
        """Draw the frame for `t` seconds since start. Return False once finished."""
        raise NotImplementedError


class EffectSource(Source):
    def __init__(self, label: str, effect: str, params: dict, models, seconds: float | None):
        super().__init__(label)
        self.effect, self.params, self.models, self.seconds = effect, params, models, seconds

    def render(self, frame, t):
        if self.seconds is not None and t >= self.seconds:
            return False
        frame[:] = 0
        EFFECTS[self.effect](frame, self.models, self.params, t)
        return True


class FseqSource(Source):
    def __init__(self, label: str, seq: FSEQ, loops: int):
        super().__init__(label)
        self.seq, self.loops = seq, loops

    def render(self, frame, t):
        index = int(t * 1000 / self.seq.step_ms)
        if index >= self.seq.frame_count * self.loops:
            return False
        n = min(self.seq.channel_count, len(frame))
        frame[:n] = np.frombuffer(self.seq.frame(index % self.seq.frame_count)[:n], dtype=np.uint8)
        frame[n:] = 0
        return True


class Player:
    def __init__(self, config: Config, layout: Layout, output, clock=datetime.now):
        self.config = config
        self.layout = layout
        self.output = output
        self.clock = clock
        self.models = list(layout.models)
        skipped = set(config.skip_controllers)
        skipped |= {c.name for c in layout.controllers if c.ip in skipped}
        self.visible_models = [m for m in layout.models if m.controller not in skipped]  # for the preview
        self.frame = np.zeros(layout.total_channels, dtype=np.uint8)
        self.sent = np.zeros_like(self.frame)  # last frame after brightness, for preview
        self.live_brightness = 1.0  # the tablet's slider; only applies to its override
        self.live_params = dict(config.live_defaults)

        self._lock = threading.RLock()
        self._fseq_cache: dict[str, FSEQ] = {}
        self._order = list(range(len(config.playlist)))
        self._pos = -1
        self._current: Source | None = None
        self._current_index: int | None = None  # playlist index of _current
        self._override: Source | None = None
        self._override_until = 0.0
        self._off = False
        self._was_in_window = False
        self._black_until = 0.0
        self._fps = 0.0
        self._stop = threading.Event()

    # ---- control (called from the web server) ----

    def set_live(self, params: dict, now_t: float | None = None) -> None:
        """Start or update the live override. `params` may hold effect, colors, speed, band, brightness."""
        now_t = time.perf_counter() if now_t is None else now_t
        with self._lock:
            p = dict(self.live_params)
            p.update({k: v for k, v in params.items() if k in ("effect", "colors", "speed", "band")})
            if p["effect"] not in EFFECTS:
                raise ValueError(f"Unknown effect {p['effect']!r}")
            if not p["colors"]:
                raise ValueError("At least one color is needed")
            if "brightness" in params:
                self.live_brightness = min(1.0, max(0.05, float(params["brightness"])))
            self.live_params = p
            if isinstance(self._override, EffectSource) and self._override.effect == p["effect"]:
                self._override.params = p  # keep its clock so the motion doesn't jump
            else:
                self._override = EffectSource(p["effect"], p["effect"], p, self.models, None)
                self._override.t0 = now_t
            self._override.brightness = self.live_brightness
            self._override_until = now_t + self.config.override_minutes * 60
            self._off = False

    def resume(self) -> None:
        with self._lock:
            self._override = None
            self._off = False

    def turn_off(self) -> None:
        with self._lock:
            self._override = None
            self._off = True

    def skip(self, now_t: float | None = None) -> None:
        with self._lock:
            self._advance(time.perf_counter() if now_t is None else now_t)

    def play_item(self, index: int, now_t: float | None = None) -> None:
        """Jump to playlist item `index` now (drops any live override). The playlist carries on from there."""
        if not 0 <= index < len(self.config.playlist):
            raise ValueError(f"No playlist item {index}")
        now_t = time.perf_counter() if now_t is None else now_t
        with self._lock:
            self._override = None
            self._off = False
            self._current = self._make_source(self.config.playlist[index])
            self._current.t0 = now_t
            self._current_index = index
            if index in self._order:
                self._pos = self._order.index(index)

    def playlist(self, now_t: float | None = None) -> dict:
        """The playlist with what's playing, how far in, and what's next."""
        now_t = time.perf_counter() if now_t is None else now_t
        with self._lock:
            items = []
            for i, item in enumerate(self.config.playlist):
                entry = {"index": i, "name": item.describe(), "type": item.type, "seconds": self._duration(item)}
                if item.type == "effect":
                    entry["effect"] = item.effect
                    entry["colors"] = ["#%02X%02X%02X" % c for c in item.params["colors"]]
                items.append(entry)
            current = self._current_index if self._current is not None else None
            if self._pos + 1 < len(self._order):
                up_next = self._order[self._pos + 1]
            else:
                up_next = None if self.config.shuffle else (self._order[0] if self._order else None)
            return {
                "items": items,
                "current": current,
                "position": round(now_t - self._current.t0, 1) if current is not None else None,
                "paused": current is not None and (self._override is not None or self._off),
                "next": up_next if current is not None else None,
                "shuffle": self.config.shuffle,
            }

    def in_window(self) -> bool:
        return self.config.in_window(self.clock())

    def status(self, now_t: float | None = None) -> dict:
        now_t = time.perf_counter() if now_t is None else now_t
        with self._lock:
            now = self.clock()
            if self._override is not None:
                mode, label = "live", self._override.label
            elif self._off:
                mode, label = "off", None
            elif self._current is not None:
                mode, label = "show", self._current.label
            else:
                mode, label = "idle", None
            end = self.config.window_end(now)
            return {
                "mode": mode,
                "playing": label,
                "in_window": end is not None,
                "window_end": end.strftime("%H:%M") if end else None,
                "override_seconds_left": max(0, round(self._override_until - now_t)) if self._override else None,
                "live": {**self.live_params, "colors": ["#%02X%02X%02X" % c for c in self.live_params["colors"]]},
                "brightness": self.live_brightness,
                "fps": round(self._fps, 1),
                "time": now.strftime("%H:%M:%S"),
            }

    # ---- frame loop ----

    def tick(self, now_t: float) -> np.ndarray | None:
        """Work out and render the frame for this instant. Returns the bytes to send, or None to stay quiet."""
        with self._lock:
            in_window = self.config.in_window(self.clock())
            if in_window and not self._was_in_window:
                self._off = False  # a new night starts fresh
            self._was_in_window = in_window

            if self._override is not None and now_t >= self._override_until:
                self._override = None
            if self._override is not None:
                self._override.render(self.frame, now_t - self._override.t0)
                return self._output_frame(self._override.brightness)

            if self._off or not in_window or not self.config.playlist:
                self._current = None
                self._current_index = None
                return self._blackout(now_t)

            if self._current is None:
                self._advance(now_t)
            if not self._current.render(self.frame, now_t - self._current.t0):
                self._advance(now_t)
                self._current.render(self.frame, 0.0)
            return self._output_frame(self._current.brightness)

    def run(self) -> None:
        period = 1.0 / self.config.fps
        start = time.perf_counter()
        n = 0
        window_start, window_frames = start, 0
        last_error = 0.0
        while not self._stop.is_set():
            now_t = time.perf_counter()
            try:
                data = self.tick(now_t)
                if data is not None:
                    self.output.send(data)
                    window_frames += 1
            except Exception:  # keep the show going (e.g. a network hiccup); log at most every 10 s
                if now_t - last_error > 10:
                    last_error = now_t
                    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} frame error:\n{traceback.format_exc()}", flush=True)
            if now_t - window_start >= 2.0:
                self._fps = window_frames / (now_t - window_start)
                window_start, window_frames = now_t, 0
            n += 1
            delay = start + n * period - time.perf_counter()
            if delay < -period:  # fell behind (e.g. machine was busy); don't try to catch up
                start, n = time.perf_counter(), 0
            elif delay > 0:
                time.sleep(delay)

    def stop(self) -> None:
        self._stop.set()

    def preload(self) -> None:
        """Read and decompress every playlist .fseq up front so the frame loop never waits on disk."""
        for item in self.config.playlist:
            if item.type == "fseq":
                self._load(item)

    # ---- internals ----

    def _advance(self, now_t: float) -> None:
        if not self.config.playlist:
            self._current = None
            return
        self._pos += 1
        if self._pos >= len(self._order):
            self._pos = 0
            if self.config.shuffle:
                last = self._order[-1]
                random.shuffle(self._order)
                if len(self._order) > 1 and self._order[0] == last:
                    self._order.append(self._order.pop(0))
        self._current_index = self._order[self._pos]
        item = self.config.playlist[self._current_index]
        self._current = self._make_source(item)
        self._current.t0 = now_t

    def _make_source(self, item: Item) -> Source:
        if item.type == "fseq":
            source: Source = FseqSource(item.describe(), self._load(item), item.loops)
        else:
            source = EffectSource(item.describe(), item.effect, item.params, self.models, item.seconds)
        source.brightness = item.brightness
        return source

    def _duration(self, item: Item) -> float:
        if item.type == "fseq":
            return round(self._load(item).duration_ms * item.loops / 1000, 1)
        return item.seconds

    def _load(self, item: Item) -> FSEQ:
        key = str(item.file)
        if key not in self._fseq_cache:
            self._fseq_cache[key] = read_fseq(item.file)
        return self._fseq_cache[key]

    def _output_frame(self, brightness: float) -> np.ndarray:
        if brightness >= 0.999:
            self.sent[:] = self.frame
        else:
            np.multiply(self.frame, brightness, out=self.sent, casting="unsafe")
        self._black_until = -1.0  # arm a blackout for when we stop
        return self.sent

    def _blackout(self, now_t: float) -> np.ndarray | None:
        if self._black_until < 0:
            self._black_until = now_t + BLACKOUT_SECONDS
        if now_t < self._black_until:
            self.sent[:] = 0
            return self.sent
        return None
