"""Show configuration (TOML). See config.example.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path

from .effects import EFFECTS, parse_color

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

Color = tuple[int, int, int]


@dataclass
class Window:
    days: set[int]  # 0 = Monday
    start: time
    end: time  # end <= start means the window runs past midnight

    def contains(self, now: datetime) -> bool:
        t, day = now.time(), now.weekday()
        if self.start < self.end:
            return day in self.days and self.start <= t < self.end
        prev = (day - 1) % 7
        return (day in self.days and t >= self.start) or (prev in self.days and t < self.end)


@dataclass
class Item:
    """One playlist entry: a rendered .fseq or a live effect."""

    type: str  # "fseq" or "effect"
    file: Path | None = None
    loops: int = 1
    effect: str = ""
    seconds: float = 60.0
    params: dict = field(default_factory=dict)

    def describe(self) -> str:
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

    def in_window(self, now: datetime) -> bool:
        return any(w.contains(now) for w in self.windows)

    def window_end(self, now: datetime) -> datetime | None:
        """When the window containing `now` ends, or None if not in one."""
        for w in self.windows:
            if w.contains(now):
                end = datetime.combine(now.date(), w.end)
                return end if end > now else end + timedelta(days=1)
        return None


def load_config(path: str | Path) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return parse_config(raw, Path(path).parent)


def parse_config(raw: dict, base_dir: Path = Path(".")) -> Config:
    show_dir = Path(raw.get("show_dir", r"Z:\xlights"))
    presets = {name: [parse_color(c) for c in colors] for name, colors in raw.get("presets", {}).items()}
    live = dict(raw.get("live", {}))
    live["colors"] = [parse_color(c) for c in live.get("colors", ["#400080", "#FF3000"])]
    live.setdefault("effect", "chase")
    live.setdefault("speed", 40)
    live.setdefault("band", 8)
    _check_effect(live["effect"])
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
        windows=[_parse_window(w) for w in raw.get("windows", [])],
        playlist=[_parse_item(i, show_dir) for i in raw.get("playlist", [])],
        presets=presets,
        live_defaults=live,
    )


def _parse_window(w: dict) -> Window:
    days = w.get("days", DAYS)
    return Window(
        days={DAYS.index(d.lower()[:3]) for d in days},
        start=time.fromisoformat(w["start"]),
        end=time.fromisoformat(w["end"]),
    )


def _parse_item(i: dict, show_dir: Path) -> Item:
    kind = i.get("type", "fseq")
    if kind == "fseq":
        path = Path(i["file"])
        return Item("fseq", file=path if path.is_absolute() else show_dir / path, loops=int(i.get("loops", 1)))
    if kind == "effect":
        _check_effect(i["effect"])
        params = {
            "colors": [parse_color(c) for c in i.get("colors", ["#400080", "#FF3000"])],
            "speed": float(i.get("speed", 40)),
            "band": int(i.get("band", 8)),
        }
        return Item("effect", effect=i["effect"], seconds=float(i.get("seconds", 60)), params=params)
    raise ValueError(f"Unknown playlist item type {kind!r}")


def _check_effect(name: str) -> None:
    if name not in EFFECTS:
        raise ValueError(f"Unknown effect {name!r}; choose from {', '.join(EFFECTS)}")
