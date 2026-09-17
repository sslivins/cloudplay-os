#!/usr/bin/env python3
"""Generate the small checked-in Plymouth frames using only Python's stdlib."""
import math
from pathlib import Path
import struct
import zlib


def chunk(kind, value):
    return struct.pack(">I", len(value)) + kind + value + struct.pack(
        ">I", zlib.crc32(kind + value) & 0xffffffff)


def frame(index):
    dots = [(20 + 14 * math.sin(i * math.tau / 12),
             20 - 14 * math.cos(i * math.tau / 12),
             0.15 + 0.85 * ((i - index) % 12) / 11) for i in range(12)]
    rows = bytearray()
    for y in range(40):
        rows.append(0)
        for x in range(40):
            alpha = 0
            for sy in range(4):
                for sx in range(4):
                    px, py = x + (sx + 0.5) / 4, y + (sy + 0.5) / 4
                    alpha += max((opacity for dx, dy, opacity in dots
                                  if (px - dx) ** 2 + (py - dy) ** 2 < 2.5 ** 2), default=0)
            rows.extend((99, 229, 224, round(alpha * 255 / 16)))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 40, 40, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    destination = Path(__file__).resolve().parents[1] / "stage-cloudplay/00-appliance/files"
    for index in range(12):
        (destination / f"spinner-{index:02}.png").write_bytes(frame(index))
