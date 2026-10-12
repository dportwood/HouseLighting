"""Minimal DDP (Distributed Display Protocol) sender.

Spec: http://www.3waylabs.com/ddp/
Header (10 bytes):
  0     flags   0x40 = version 1, 0x01 = PUSH (display the frame now)
  1     sequence number (low 4 bits, 0 = unused)
  2     data type (0x01 = RGB 8-bit, what xLights sends)
  3     destination id (0x01 = default output device)
  4-7   data offset in bytes, big-endian
  8-9   data length, big-endian
"""

from __future__ import annotations

import socket
import struct

DDP_PORT = 4048
DDP_FLAG_VER1 = 0x40
DDP_FLAG_PUSH = 0x01
DDP_TYPE_RGB8 = 0x01
DDP_ID_DISPLAY = 0x01
# Matches xLights' ChannelsPerPacket for these controllers; keeps packets under a 1500-byte MTU.
MAX_DATA_PER_PACKET = 1440


def build_packets(data: bytes, offset: int = 0, sequence: int = 0, chunk: int = MAX_DATA_PER_PACKET) -> list[bytes]:
    """Split one frame of channel data into DDP packets. PUSH is set only on the last one.

    `sequence` is the number for the first packet; it advances per packet (1..15, wrapping),
    since receivers like FPP count a repeated or skipped number as an error. 0 disables it.
    """
    packets = []
    total = len(data)
    pos = 0
    while pos < total or (total == 0 and not packets):
        part = data[pos : pos + chunk]
        last = pos + len(part) >= total
        flags = DDP_FLAG_VER1 | (DDP_FLAG_PUSH if last else 0)
        header = struct.pack(">BBBBIH", flags, sequence, DDP_TYPE_RGB8, DDP_ID_DISPLAY, offset + pos, len(part))
        packets.append(header + part)
        if sequence:
            sequence = sequence % 15 + 1
        pos += len(part)
        if total == 0:
            break
    return packets


class DDPSender:
    def __init__(self, ip: str, port: int = DDP_PORT):
        self.addr = (ip, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._seq = 0

    def send(self, data: bytes, offset: int = 0) -> int:
        """Send one frame's worth of channels starting at byte `offset`. Returns packets sent."""
        packets = build_packets(data, offset, self._seq % 15 + 1)
        for p in packets:
            self.sock.sendto(p, self.addr)
        self._seq = (self._seq + len(packets)) % 15
        return len(packets)

    def close(self) -> None:
        self.sock.close()
