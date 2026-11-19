"""#36: console programs started from the windowless remote must not flash a console window."""

import subprocess
from pathlib import Path

from tvpult import discovery, tools
from tvpult.tools import CREATE_NO_WINDOW, no_window_flags, run_tool

real_read_arp = discovery.read_arp  # taken before conftest replaces the module's one
SRC = Path(__file__).resolve().parent.parent / "src" / "tvpult"


def test_windows_gets_create_no_window(monkeypatch):
    monkeypatch.setattr(tools.sys, "platform", "win32")
    assert no_window_flags() == CREATE_NO_WINDOW == 0x08000000
    assert no_window_flags(0x200) == 0x200 | CREATE_NO_WINDOW  # other flags are kept
    monkeypatch.setattr(tools.sys, "platform", "linux")
    assert no_window_flags() == 0 and no_window_flags(0x200) == 0x200


def test_run_tool_passes_the_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(tools.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd, **kw))
    run_tool(["arp", "-a"], capture_output=True)
    assert seen["creationflags"] == CREATE_NO_WINDOW and seen["capture_output"]


def test_the_arp_table_is_read_without_a_window(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(tools.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", fake_run)
    real_read_arp()
    assert seen.get("creationflags") == CREATE_NO_WINDOW


def test_every_console_program_goes_through_run_tool():
    for f in SRC.glob("*.py"):
        if f.name != "tools.py":
            assert "subprocess.run(" not in f.read_text(encoding="utf-8"), f.name
