"""Read the xLights show folder to learn controllers, channel ranges and models.

xLights numbers channels globally across all controllers in the order they
appear in xlights_networks.xml. Our FPP controllers use "KeepChannelNumbers",
so the DDP offset we send is that same global (0-based) channel number.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

NETWORKS_FILE = "xlights_networks.xml"
MODELS_FILE = "xlights_rgbeffects.xml"


@dataclass
class Controller:
    name: str
    ip: str
    protocol: str
    start: int  # first global channel, 1-based
    channels: int
    active: bool = True
    keep_channel_numbers: bool = True

    @property
    def end(self) -> int:
        """Last global channel, 1-based, inclusive."""
        return self.start + self.channels - 1


@dataclass
class Model:
    name: str
    display_as: str
    controller: str | None
    start: int  # first global channel, 1-based
    nodes: int
    channels_per_node: int = 3
    # For custom models: node number -> (row, col) in the xLights grid.
    custom_positions: dict[int, tuple[int, int]] = field(default_factory=dict)

    @property
    def channels(self) -> int:
        return self.nodes * self.channels_per_node

    @property
    def end(self) -> int:
        return self.start + self.channels - 1


@dataclass
class Layout:
    controllers: list[Controller]
    models: list[Model]
    groups: dict[str, list[str]]

    @property
    def total_channels(self) -> int:
        return max((c.end for c in self.controllers), default=0)

    def controller(self, name_or_ip: str) -> Controller:
        for c in self.controllers:
            if name_or_ip in (c.name, c.ip):
                return c
        raise KeyError(f"No controller named or at {name_or_ip!r}")

    def model(self, name: str) -> Model:
        for m in self.models:
            if m.name == name:
                return m
        raise KeyError(f"No model named {name!r}")


def load_layout(show_dir: str | Path) -> Layout:
    show_dir = Path(show_dir)
    controllers = parse_networks(show_dir / NETWORKS_FILE)
    models, groups = parse_models(show_dir / MODELS_FILE, controllers)
    return Layout(controllers, models, groups)


def parse_networks(path: str | Path) -> list[Controller]:
    root = ET.parse(path).getroot()
    controllers = []
    next_start = 1
    for el in root.iter("Controller"):
        net = el.find("network")
        channels = int(net.get("MaxChannels", "0")) if net is not None else 0
        controllers.append(
            Controller(
                name=el.get("Name", ""),
                ip=el.get("IP", ""),
                protocol=el.get("Protocol", ""),
                start=next_start,
                channels=channels,
                active=el.get("ActiveState", "Active") == "Active",
                keep_channel_numbers=net is not None and net.get("KeepChannelNumbers", "1") == "1",
            )
        )
        next_start += channels
    return controllers


def parse_models(path: str | Path, controllers: list[Controller]) -> tuple[list[Model], dict[str, list[str]]]:
    root = ET.parse(path).getroot()
    by_name = {c.name: c for c in controllers}
    models = []
    for el in root.iter("model"):
        display_as = el.get("DisplayAs", "")
        string_type = el.get("StringType", "RGB Nodes")
        cpn = 3 if string_type.startswith("RGB") or string_type.endswith("Nodes") else 1
        positions: dict[int, tuple[int, int]] = {}
        if display_as == "Custom":
            positions = _parse_custom_compressed(el.get("CustomModelCompressed", ""))
            nodes = max(positions) if positions else _count_custom_grid(el.get("CustomModel", ""))
        else:
            nodes = int(el.get("parm1", "1")) * int(el.get("parm2", "1"))
        controller, start = _resolve_start(el.get("StartChannel", "1"), by_name)
        models.append(
            Model(
                name=el.get("name", ""),
                display_as=display_as,
                controller=controller,
                start=start,
                nodes=nodes,
                channels_per_node=cpn,
                custom_positions=positions,
            )
        )
    groups = {
        g.get("name", ""): [n for n in g.get("models", "").split(",") if n]
        for g in root.iter("modelGroup")
    }
    return models, groups


def _resolve_start(spec: str, controllers: dict[str, Controller]) -> tuple[str | None, int]:
    """Convert an xLights StartChannel string to (controller name, global 1-based channel)."""
    if spec.startswith("!"):
        name, _, offset = spec[1:].rpartition(":")
        ctrl = controllers[name]
        return name, ctrl.start + int(offset) - 1
    if spec.isdigit():
        start = int(spec)
        for c in controllers.values():
            if c.start <= start <= c.end:
                return c.name, start
        return None, start
    raise ValueError(f"Unsupported StartChannel format {spec!r} (only '!Controller:N' and absolute are handled)")


def _parse_custom_compressed(data: str) -> dict[int, tuple[int, int]]:
    """CustomModelCompressed is 'node,row,col;node,row,col;...' (single layer)."""
    positions = {}
    for item in data.split(";"):
        parts = item.split(",")
        if len(parts) >= 3 and parts[0].strip():
            positions[int(parts[0])] = (int(parts[1]), int(parts[2]))
    return positions


def _count_custom_grid(data: str) -> int:
    nums = [int(x) for x in data.replace(";", ",").replace("|", ",").split(",") if x.strip()]
    return max(nums, default=0)
