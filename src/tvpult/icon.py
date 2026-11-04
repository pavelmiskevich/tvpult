"""A small TV icon (.ico) for the desktop shortcut, drawn in code — no image files or libraries."""

from __future__ import annotations

import struct
from pathlib import Path

SIZE = 64
_BODY = (0x2A, 0x1F, 0x1B, 0xFF)    # BGRA: dark frame
_SCREEN = (0xF6, 0x6D, 0x2F, 0xFF)  # #2f6df6, the remote's accent colour
_GLARE = (0xFF, 0x9C, 0x6B, 0xFF)
_CLEAR = (0, 0, 0, 0)


def _inside_rounded(x: int, y: int, x0: int, y0: int, x1: int, y1: int, r: int) -> bool:
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = x0 + r if x < x0 + r else x1 - r if x > x1 - r else x
    cy = y0 + r if y < y0 + r else y1 - r if y > y1 - r else y
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def pixel(x: int, y: int) -> tuple[int, int, int, int]:
    """Colour at (x, y), y = 0 at the top."""
    if _inside_rounded(x, y, 8, 18, 55, 47, 3) and x - 8 + y - 18 < 12:
        return _GLARE
    if _inside_rounded(x, y, 8, 18, 55, 47, 3):
        return _SCREEN
    if _inside_rounded(x, y, 4, 14, 59, 51, 6):
        return _BODY
    if 28 <= x <= 35 and 52 <= y <= 56:   # neck
        return _BODY
    if _inside_rounded(x, y, 18, 56, 45, 60, 2):  # foot
        return _BODY
    return _CLEAR


def icon_bytes(size: int = SIZE) -> bytes:
    """A one-image 32-bit .ico."""
    rows = []
    for y in range(size - 1, -1, -1):  # BMP rows go bottom-up
        rows.append(b"".join(bytes(pixel(x * SIZE // size, y * SIZE // size)) for x in range(size)))
    xor = b"".join(rows)
    and_row = ((size + 31) // 32) * 4
    and_mask = b"\x00" * and_row * size  # alpha channel does the masking
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(xor) + len(and_mask), 0, 0, 0, 0)
    image = header + xor + and_mask
    entry = struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(image), 6 + 16)
    return struct.pack("<HHH", 0, 1, 1) + entry + image


def write_icon(path: Path, size: int = SIZE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(icon_bytes(size))
    return path
