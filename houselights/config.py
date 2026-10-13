"""Show configuration.

Two files:
  config.toml  hand-edited settings (show folder, ports, token, presets, ...).
               Its `windows`/`playlist`/`shuffle` seed the show the first time.
  show.json    the schedule and playlist as edited from the web page. When it
               exists it wins over config.toml for those three settings.
"""

from __future__ import annotations

import json
import os
import tomllib
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .effects import EFFECTS, parse_color

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MAX_COLORS = 16

Color = tuple[int, int, int]


@dataclass
class Window:
    days: set[int]  # 0 = Monday
    start: time
    end: time  # end <= start means the window runs past midnight
    enabled: bool = True
    date_from: date | None = None  # inclusive; applies to the night the window starts
    date_to: date | None = None

    def _runs_on(self, day: date) -> bool:
        """Whether a window starting on `day` is scheduled."""
        return (
            self.enabled
            and day.weekday() in self.days
            and (self.date_from is None or day >= self.date_from)
            and (self.date_to is None or day <= self.date_to)
        )

    def contains(self, now: datetime) -> bool:
        t, today = now.time(), now.date()
        if self.start < self.end:
            return self._runs_on(today) and self.start <= t < self.end
        return (self._runs_on(today) and t >= self.start) or (self._runs_on(today - timedelta(days=1)) and t < self.end)

    def next_start(self, now: datetime, horizon_days: int = 400) -> datetime | None:
        for d in range(horizon_days):
            day = now.date() + timedelta(days=d)
            start = datetime.combine(day, self.start)
            if start > now and self._runs_on(day):
                return start
        return None


@dataclass
class Item:
    """One playlist entry: a rendered .fseq or a live effect."""

    type: str  # "fseq" or "effect"
    file: Path | None = None
    loops: int = 1
    effect: str = ""
    seconds: float = 60.0
    params: dict = field(default_factory=dict)
    brightness: float = 1.0  # 0.05-1.0
    name: str = ""
    id: str = ""

    def describe(self) -> str:
        if self.name:
            return self.name
        if self.type == "fseq":
            return self.file.stem
        return self.effect.capitalize()


@dataclass
class Config:
    show_dir: Path
    fps: float = 20.0
    skip_controllers: list[str] = field(default_factory=list)
    http_host: str = "0.0.0.0"
    http_port: int = 8080
    override_minutes: float = 30.0
    allow_live_outside_window: bool = False
    api_token: str = ""
    shuffle: bool = False
    windows: list[Window] = field(default_factory=list)
    playlist: list[Item] = field(default_factory=list)
    presets: dict[str, list[Color]] = field(default_factory=dict)
    live_defaults: dict = field(default_factory=dict)
    state_file: Path | None = None

    def in_window(self, now: datetime) -> bool:
        return any(w.contains(now) for w in self.windows)

    def window_end(self, now: datetime) -> datetime | None:
        """When the window containing `now` ends, or None if not in one."""
        for w in self.windows:
            if w.contains(now):
                end = datetime.combine(now.date(), w.end)
                return end if end > now else end + timedelta(days=1)
        return None

    def next_start(self, now: datetime) -> datetime | None:
        starts = [s for w in self.windows if (s := w.next_start(now)) is not None]
        return min(starts, default=None)


# ---- loading ----

def load_config(path: str | Path) -> Config:
    path = Path(path)
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    cfg = parse_config(raw, path.parent)
    load_state(cfg)
    return cfg


def parse_config(raw: dict, base_dir: Path = Path(".")) -> Config:
    show_dir = Path(raw.get("show_dir", r"Z:\xlights"))
    presets = {name: [parse_color(c) for c in colors] for name, colors in raw.get("presets", {}).items()}
    live = dict(raw.get("live", {}))
    live["colors"] = [parse_color(c) for c in live.get("colors", ["#400080", "#FF3000"])]
    live.setdefault("effect", "chase")
    live.setdefault("speed", 40)
    live.setdefault("band", 8)
    live["strings"] = parse_strings(live.get("strings"))
    _check_effect(live["effect"])
    state_file = raw.get("state_file", "show.json")
    return Config(
        show_dir=show_dir,
        fps=float(raw.get("fps", 20)),
        skip_controllers=list(raw.get("skip_controllers", [])),
        http_host=raw.get("http_host", "0.0.0.0"),
        http_port=int(raw.get("http_port", 8080)),
        override_minutes=float(raw.get("override_minutes", 30)),
        allow_live_outside_window=bool(raw.get("allow_live_outside_window", False)),
        api_token=str(raw.get("api_token", "")),
        shuffle=bool(raw.get("shuffle", False)),
        windows=[parse_window(w) for w in raw.get("windows", [])],
        playlist=[parse_item(i, show_dir) for i in raw.get("playlist", [])],
        presets=presets,
        live_defaults=live,
        state_file=(base_dir / state_file) if state_file else None,
    )


def parse_window(w: dict) -> Window:
    days = w.get("days", DAYS)
    if not days:
        raise ValueError("A time window needs at least one day")
    try:
        day_set = {DAYS.index(str(d).lower()[:3]) for d in days}
    except ValueError:
        raise ValueError(f"Days must be from {', '.join(DAYS)}") from None
    return Window(
        days=day_set,
        start=time.fromisoformat(str(w["start"])),
        end=time.fromisoformat(str(w["end"])),
        enabled=bool(w.get("enabled", True)),
        date_from=date.fromisoformat(str(w["from"])) if w.get("from") else None,
        date_to=date.fromisoformat(str(w["to"])) if w.get("to") else None,
    )


def parse_item(i: dict, show_dir: Path) -> Item:
    item = _parse_item_kind(i, show_dir)
    item.brightness = min(100.0, max(5.0, float(i.get("brightness", 100)))) / 100  # percent
    item.name = str(i.get("name") or "").strip()[:60] or item.describe()
    item.id = str(i.get("id") or uuid.uuid4().hex[:8])
    return item


def _parse_item_kind(i: dict, show_dir: Path) -> Item:
    kind = i.get("type", "fseq")
    if kind == "fseq":
        path = Path(str(i["file"]))
        return Item("fseq", file=path if path.is_absolute() else show_dir / path, loops=max(1, int(i.get("loops", 1))))
    if kind == "effect":
        _check_effect(str(i["effect"]))
        colors = i.get("colors", ["#400080", "#FF3000"])
        if not isinstance(colors, list) or not 1 <= len(colors) <= MAX_COLORS:
            raise ValueError(f"An effect needs 1-{MAX_COLORS} colors")
        params = {
            "colors": [parse_color(str(c)) for c in colors],
            "speed": min(100.0, max(1.0, float(i.get("speed", 40)))),
            "band": min(100, max(1, int(i.get("band", 8)))),
            "strings": parse_strings(i.get("strings")),
        }
        seconds = float(i.get("seconds", 60))
        if not 1 <= seconds <= 24 * 3600:
            raise ValueError("Effect length must be between 1 second and 24 hours")
        return Item("effect", effect=str(i["effect"]), seconds=seconds, params=params)
    raise ValueError(f"Unknown playlist item type {kind!r}")


MAX_STRINGS = 200


def parse_strings(raw, names: set[str] | None = None) -> dict:
    """Per-string settings: {model name: {effect, colors, speed, band, brightness (percent), off}}.

    Each entry replaces the overall look for that one string. `names`, when given,
    is the set of string (model) names allowed.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_STRINGS:
        raise ValueError("strings must be an object of string name -> settings")
    out = {}
    for name, s in raw.items():
        name = str(name)
        if names is not None and name not in names:
            raise ValueError(f"Unknown string {name!r}")
        if not isinstance(s, dict):
            raise ValueError(f"Settings for {name!r} must be an object")
        effect = str(s.get("effect", "solid"))
        _check_effect(effect)
        colors = s.get("colors", ["#FFFFFF"])
        if not isinstance(colors, list) or not 1 <= len(colors) <= MAX_COLORS:
            raise ValueError(f"{name}: needs 1-{MAX_COLORS} colors")
        out[name] = {
            "effect": effect,
            "colors": [c if isinstance(c, tuple) else parse_color(str(c)) for c in colors],
            "speed": min(100.0, max(1.0, float(s.get("speed", 40)))),
            "band": min(100, max(1, int(s.get("band", 8)))),
            "brightness": min(100.0, max(5.0, float(s.get("brightness", 100)))),
            "off": bool(s.get("off", False)),
        }
    return out


def strings_to_json(strings: dict) -> dict:
    return {
        name: {**s, "colors": ["#%02X%02X%02X" % c for c in s["colors"]]}
        for name, s in (strings or {}).items()
    }


def _check_effect(name: str) -> None:
    if name not in EFFECTS:
        raise ValueError(f"Unknown effect {name!r}; choose from {', '.join(EFFECTS)}")


# ---- saving (show.json) ----

def window_to_dict(w: Window) -> dict:
    return {
        "days": [DAYS[d] for d in sorted(w.days)],
        "start": w.start.strftime("%H:%M"),
        "end": w.end.strftime("%H:%M"),
        "enabled": w.enabled,
        "from": w.date_from.isoformat() if w.date_from else None,
        "to": w.date_to.isoformat() if w.date_to else None,
    }


def item_to_dict(item: Item, show_dir: Path) -> dict:
    d = {"id": item.id, "name": item.name, "type": item.type, "brightness": round(item.brightness * 100)}
    if item.type == "fseq":
        try:
            d["file"] = str(item.file.relative_to(show_dir))
        except ValueError:
            d["file"] = str(item.file)
        d["loops"] = item.loops
    else:
        d.update({
            "effect": item.effect,
            "seconds": item.seconds,
            "colors": ["#%02X%02X%02X" % c for c in item.params["colors"]],
            "speed": item.params["speed"],
            "band": item.params["band"],
        })
        if item.params.get("strings"):
            d["strings"] = strings_to_json(item.params["strings"])
    return d


def load_state(cfg: Config) -> None:
    """Replace the schedule/playlist from config.toml with show.json, if it exists."""
    if cfg.state_file is None or not cfg.state_file.exists():
        return
    raw = json.loads(cfg.state_file.read_text(encoding="utf-8"))
    cfg.windows = [parse_window(w) for w in raw.get("windows", [])]
    cfg.playlist = [parse_item(i, cfg.show_dir) for i in raw.get("playlist", [])]
    cfg.shuffle = bool(raw.get("shuffle", cfg.shuffle))


def save_state(cfg: Config) -> None:
    """Write the schedule and playlist to show.json (atomically, so a crash can't leave half a file)."""
    if cfg.state_file is None:
        return
    data = {
        "windows": [window_to_dict(w) for w in cfg.windows],
        "shuffle": cfg.shuffle,
        "playlist": [item_to_dict(i, cfg.show_dir) for i in cfg.playlist],
    }
    tmp = cfg.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, cfg.state_file)
