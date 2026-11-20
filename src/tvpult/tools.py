"""Running console programs (arp, tailscale, PowerShell) from tvpult.

From the desktop shortcut tvpult runs under pythonw, a process without a console. Windows then gives every
console program it starts a console window of its own — redirecting the output doesn't prevent that — so a
black window would flash each time the ARP table is read (once a minute per TV) and could take the focus from
a full-screen app. CREATE_NO_WINDOW stops that; elsewhere nothing changes.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)  # (defined on Windows only)


def no_window_flags(flags: int = 0) -> int:
    """creationflags for a console program that must not open a window (Windows); unchanged elsewhere."""
    return flags | CREATE_NO_WINDOW if sys.platform == "win32" else flags


def run_tool(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """subprocess.run for a console program, without a console window."""
    kwargs["creationflags"] = no_window_flags(kwargs.get("creationflags", 0))
    return subprocess.run(cmd, **kwargs)
