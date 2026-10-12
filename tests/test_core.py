import struct

import numpy as np
import pytest

from houselights import effects
from houselights.ddp import build_packets
from houselights.fseq import read_fseq
from houselights.layout import Model, load_layout

NETWORKS = """<?xml version="1.0" encoding="UTF-8"?>
<Networks>
  <Controller Name="A" Type="Ethernet" ActiveState="Active" IP="10.0.0.1" Protocol="DDP">
    <network KeepChannelNumbers="1" MaxChannels="30"/>
  </Controller>
  <Controller Name="B" Type="Ethernet" ActiveState="Inactive" IP="10.0.0.2" Protocol="DDP">
    <network KeepChannelNumbers="1" MaxChannels="15"/>
  </Controller>
</Networks>
"""

MODELS = """<?xml version="1.0" encoding="UTF-8"?>
<xrgb>
  <models>
    <model name="Line" DisplayAs="Single Line" StringType="RGB Nodes" parm1="1" parm2="10" StartChannel="!A:1"/>
    <model name="Joy" DisplayAs="Custom" StringType="RGB Nodes" parm1="20" parm2="10" StartChannel="!B:1"
           CustomModelCompressed="2,0,5;1,3,4;5,6,7;3,1,1;4,2,2"/>
  </models>
  <modelGroups>
    <modelGroup name="All" models="Line,Joy"/>
  </modelGroups>
</xrgb>
"""


@pytest.fixture
def layout(tmp_path):
    (tmp_path / "xlights_networks.xml").write_text(NETWORKS)
    (tmp_path / "xlights_rgbeffects.xml").write_text(MODELS)
    return load_layout(tmp_path)


def test_controllers_numbered_globally(layout):
    a, b = layout.controllers
    assert (a.start, a.end) == (1, 30)
    assert (b.start, b.end, b.active) == (31, 45, False)
    assert layout.total_channels == 45


def test_models_resolve_to_global_channels(layout):
    line = layout.model("Line")
    assert (line.start, line.end, line.nodes) == (1, 30, 10)
    joy = layout.model("Joy")
    assert (joy.controller, joy.start, joy.nodes) == ("B", 31, 5)
    assert joy.custom_positions[1] == (3, 4)
    assert layout.groups["All"] == ["Line", "Joy"]


def test_ddp_packets_split_offset_push_and_sequence():
    data = bytes(range(256)) * 12  # 3072 bytes -> 1440 + 1440 + 192
    packets = build_packets(data, offset=4062, sequence=14)
    assert [len(p) - 10 for p in packets] == [1440, 1440, 192]
    flags, seqs, offsets = [], [], []
    for p in packets:
        f, s, t, i, off, ln = struct.unpack(">BBBBIH", p[:10])
        flags.append(f)
        seqs.append(s)
        offsets.append(off)
        assert (t, i) == (0x01, 0x01)
    assert flags == [0x40, 0x40, 0x41]  # PUSH only on the last packet
    assert seqs == [14, 15, 1]  # advances per packet and wraps past 15
    assert offsets == [4062, 4062 + 1440, 4062 + 2880]
    assert b"".join(p[10:] for p in packets) == data


def test_read_uncompressed_fseq(tmp_path):
    channels, frames = 6, 3
    header = bytearray(32)
    header[0:4] = b"PSEQ"
    struct.pack_into("<HBB", header, 4, 32, 0, 2)  # data offset, minor, major
    struct.pack_into("<H", header, 8, 32)
    struct.pack_into("<II", header, 10, channels, frames)
    header[18] = 50
    body = bytes(range(channels * frames))
    path = tmp_path / "t.fseq"
    path.write_bytes(bytes(header) + body)
    seq = read_fseq(path)
    assert (seq.channel_count, seq.frame_count, seq.step_ms) == (6, 3, 50)
    assert bytes(seq.frame(1)) == bytes(range(6, 12))


def test_chase_moves_bands(layout):
    frame = np.zeros(layout.total_channels, dtype=np.uint8)
    line = [layout.model("Line")]
    purple, orange = effects.HALLOWEEN
    effects.chase(frame, line, effects.HALLOWEEN, t=0, band_pixels=2, pixels_per_second=1)
    px = frame[:30].reshape(-1, 3)
    assert [tuple(p) for p in px[:4]] == [purple, purple, orange, orange]
    effects.chase(frame, line, effects.HALLOWEEN, t=1, band_pixels=2, pixels_per_second=1)
    assert [tuple(p) for p in px[:4]] == [purple, orange, orange, purple]


def test_parse_color():
    assert effects.parse_color("#FF3000") == (255, 48, 0)
    with pytest.raises(ValueError):
        effects.parse_color("FFF")


def test_locate_lights_blocks_from_first_pixel(layout):
    frame = np.zeros(layout.total_channels, dtype=np.uint8)
    line = layout.model("Line")
    key = effects.locate(frame, line, first=5, block=3)
    assert [(a, b) for a, b, _ in key] == [(5, 7), (8, 10)]
    px = frame[:30].reshape(-1, 3)
    assert not px[:4].any()
    assert tuple(px[4]) == effects.LOCATE_COLORS[0]
    assert tuple(px[9]) == effects.LOCATE_COLORS[1]


def test_strobe_flashes_few_pixels_and_changes_each_slot():
    model = Model("Big", "Single Line", "A", 1, 1000)
    colors = [(255, 255, 255), (255, 48, 0)]
    a = np.zeros(3000, dtype=np.uint8)
    b = np.zeros(3000, dtype=np.uint8)
    effects.strobe(a, [model], colors, t=0.0, density=0.05)
    lit = a.reshape(-1, 3).any(axis=1)
    assert 20 < lit.sum() < 90  # ~5% of 1000
    assert {tuple(p) for p in a.reshape(-1, 3)[lit]} <= set(colors)
    effects.strobe(b, [model], colors, t=0.01, density=0.05)
    assert (a == b).all()  # same flash slot -> same pattern
    effects.strobe(b, [model], colors, t=0.06, density=0.05)
    assert not (a == b).all()  # next slot -> new flashes


def test_led_rainbow_is_distinct_saturated_and_starts_red():
    from houselights.colors import led_rainbow, led_value

    colors = led_rainbow(16)
    assert len(set(colors)) == 16
    assert colors[0] == (255, 0, 0)
    for c in colors:
        assert max(c) == 255 and min(c) <= 20  # full brightness, saturated
    # the config's Rainbow theme matches the generator
    import tomllib
    with open("config.example.toml", "rb") as f:
        preset = tomllib.load(f)["presets"]["Rainbow"]
    assert [effects.parse_color(h) for h in preset] == colors
    assert led_value((255, 128, 0)) == (255, 55, 0)  # screen orange -> LED orange
