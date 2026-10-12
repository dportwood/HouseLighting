"""Route a global frame buffer to each controller over DDP."""

from __future__ import annotations

from .ddp import DDPSender
from .layout import Controller, Layout


class Output:
    """Holds one DDP sender per enabled controller and slices the global frame for each."""

    def __init__(self, layout: Layout, only: list[str] | None = None, skip: list[str] | None = None):
        self.layout = layout
        skip = set(skip or [])
        self.targets: list[tuple[Controller, DDPSender]] = []
        for c in layout.controllers:
            if not c.active or c.protocol.upper() != "DDP":
                continue
            if only and c.name not in only and c.ip not in only:
                continue
            if c.name in skip or c.ip in skip:
                continue
            self.targets.append((c, DDPSender(c.ip)))

    def send(self, frame: bytes | bytearray | memoryview) -> None:
        """`frame` is the full show: index 0 is global channel 1."""
        view = memoryview(frame)
        for c, sender in self.targets:
            chunk = view[c.start - 1 : c.end]
            offset = c.start - 1 if c.keep_channel_numbers else 0
            sender.send(bytes(chunk), offset)

    def close(self) -> None:
        for _, sender in self.targets:
            sender.close()
