"""Wake-on-LAN magic packet."""

from __future__ import annotations

import re
import socket

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([-:]?[0-9A-Fa-f]{2}){5}$")


def magic_packet(mac: str) -> bytes:
    if not _MAC_RE.match(mac.strip()):
        raise ValueError(f"bad MAC address: {mac!r}")
    raw = bytes.fromhex(re.sub(r"[-:]", "", mac.strip()))
    return b"\xff" * 6 + raw * 16


def send_wol(mac: str, broadcasts: tuple[str, ...] = ("255.255.255.255",), repeat: int = 3) -> None:
    pkt = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(repeat):
            for addr in broadcasts:
                for port in (9, 7):
                    s.sendto(pkt, (addr, port))


def subnet_broadcast(host: str) -> str:
    """Assume a /24 home subnet: 192.168.1.50 -> 192.168.1.255."""
    parts = host.split(".")
    return ".".join(parts[:3] + ["255"])
