"""Reader for xLights/FPP .fseq files (v2, uncompressed or zstd; non-sparse)."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FSEQ:
    channel_count: int
    frame_count: int
    step_ms: int
    data: bytes  # frame_count * channel_count bytes

    def frame(self, index: int) -> memoryview:
        start = index * self.channel_count
        return memoryview(self.data)[start : start + self.channel_count]

    @property
    def duration_ms(self) -> int:
        return self.frame_count * self.step_ms


def read_fseq(path: str | Path) -> FSEQ:
    raw = Path(path).read_bytes()
    if raw[:4] != b"PSEQ":
        raise ValueError(f"{path} is not an fseq file")
    data_offset, = struct.unpack_from("<H", raw, 4)
    major = raw[7]
    if major != 2:
        raise ValueError(f"Only fseq v2 is supported (got v{major})")
    channel_count, frame_count = struct.unpack_from("<II", raw, 10)
    step_ms = raw[18]
    compression = raw[20] & 0x0F
    block_count = raw[21] | ((raw[20] & 0xF0) << 4)
    sparse_count = raw[22]
    if sparse_count:
        raise ValueError("Sparse fseq files are not supported yet")

    expected = channel_count * frame_count
    if compression == 0:
        data = raw[data_offset : data_offset + expected]
    elif compression == 1:
        import zstandard

        blocks = [struct.unpack_from("<II", raw, 32 + 8 * i) for i in range(block_count)]
        out = bytearray()
        pos = data_offset
        dctx = zstandard.ZstdDecompressor()
        for _first_frame, size in blocks:
            if size == 0:
                continue
            out += dctx.decompress(raw[pos : pos + size], max_output_size=expected)
            pos += size
        data = bytes(out)
    else:
        raise ValueError(f"Unsupported compression type {compression} (zlib not implemented)")

    if len(data) < expected:
        raise ValueError(f"fseq data truncated: {len(data)} of {expected} bytes")
    return FSEQ(channel_count, frame_count, step_ms, data[:expected])
