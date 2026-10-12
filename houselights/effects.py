"""Live (computed per frame) effects.

Each effect renders into the global frame buffer one model at a time, so
parameters like colors and speed can change while it runs.
"""

from __future__ import annotations

import numpy as np

from .layout import Layout, Model

HALLOWEEN = [(0x40, 0x00, 0x80), (0xFF, 0x30, 0x00)]  # purple, orange (from Halloween 2024 palette)


def parse_color(text: str) -> tuple[int, int, int]:
    text = text.lstrip("#")
    if len(text) != 6:
        raise ValueError(f"Color must be RRGGBB, got {text!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def solid(frame: np.ndarray, models: list[Model], color: tuple[int, int, int]) -> None:
    for m in models:
        px = frame[m.start - 1 : m.end].reshape(-1, 3)
        px[:] = color


def chase(
    frame: np.ndarray,
    models: list[Model],
    colors: list[tuple[int, int, int]],
    t: float,
    band_pixels: int = 8,
    pixels_per_second: float = 20.0,
) -> None:
    """Alternating color bands that march along each string."""
    palette = np.array(colors, dtype=np.uint8)
    shift = t * pixels_per_second
    for m in models:
        idx = np.arange(m.nodes)
        band = ((idx + shift) // band_pixels).astype(np.int64) % len(palette)
        frame[m.start - 1 : m.end].reshape(-1, 3)[:] = palette[band]


LOCATE_COLORS = [(255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255)]


def locate(frame: np.ndarray, model: Model, first: int, block: int) -> list[tuple[int, int, tuple[int, int, int]]]:
    """Light pixels `first`..end of `model` (1-based) in color blocks of `block` pixels; the rest stay dark.

    Returns [(from_pixel, to_pixel, color)] so the caller can print a key.
    """
    px = frame[model.start - 1 : model.end].reshape(-1, 3)
    px[:] = 0
    key = []
    for i, lo in enumerate(range(first, model.nodes + 1, block)):
        hi = min(lo + block - 1, model.nodes)
        color = LOCATE_COLORS[i % len(LOCATE_COLORS)]
        px[lo - 1 : hi] = color
        key.append((lo, hi, color))
    return key


def models_for(layout: Layout, controllers: list[str] | None = None) -> list[Model]:
    """All models, or only those on the given controllers (by name or IP)."""
    if not controllers:
        return list(layout.models)
    names = {layout.controller(c).name for c in controllers}
    return [m for m in layout.models if m.controller in names]
