"""Color helpers tuned for the pixels.

The FPP controllers run with gamma 1.0: a channel value is LED power, linear
light. Screens (and the colors people pick on them) are gamma-encoded sRGB, so a
half-green orange that looks right on screen comes out yellow on the pixels.
`led_value` converts a screen color to the power the LED needs to look the same.

`led_rainbow` also spaces hues by how different they look (OKLab hue angle),
not by HSV degrees, which would waste several steps on near-identical greens
and blues.
"""

from __future__ import annotations

import colorsys
import math


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def led_value(color: tuple[int, int, int]) -> tuple[int, int, int]:
    """sRGB screen color -> channel values that look like it on linear (gamma 1.0) pixels."""
    return tuple(round(255 * srgb_to_linear(c / 255)) for c in color)


def oklab_hue(rgb_linear: tuple[float, float, float]) -> float:
    """Hue angle in degrees in OKLab (https://bottosson.github.io/posts/oklab/)."""
    r, g, b = rgb_linear
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l, m, s = (math.copysign(abs(x) ** (1 / 3), x) for x in (l, m, s))
    a = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
    bb = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    return math.degrees(math.atan2(bb, a)) % 360


def led_rainbow(n: int = 16, samples: int = 3600) -> list[tuple[int, int, int]]:
    """`n` fully saturated colors, red first, evenly spaced by perceived hue, as LED channel values."""
    # Walk the edge of the color gamut (full-saturation HSV) and note each point's perceived hue.
    edge = []
    for i in range(samples):
        screen = colorsys.hsv_to_rgb(i / samples, 1.0, 1.0)
        linear = tuple(srgb_to_linear(c) for c in screen)
        edge.append((oklab_hue(linear), linear))
    start = edge[0][0]  # red
    edge.sort(key=lambda e: (e[0] - start) % 360)
    colors = []
    for k in range(n):
        target = k * 360 / n
        _, linear = min(edge, key=lambda e: abs(((e[0] - start) % 360) - target))
        colors.append(tuple(round(255 * c) for c in linear))
    return colors


def to_hex(color: tuple[int, int, int]) -> str:
    return "#%02X%02X%02X" % color
