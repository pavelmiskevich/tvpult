"""Shared test guards."""

import pytest

from tvpult import discovery


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Tests must never touch the live network: not the ARP table, not HTTP, not the TV's remote channel.

    (A test that reaches a real TV could even pop up its "Allow?" prompt.)
    """
    def boom(*a, **k):
        raise AssertionError("a test tried to use the real network")

    import requests
    import samsungtvws

    monkeypatch.setattr(discovery, "read_arp", boom)
    monkeypatch.setattr(requests.Session, "request", boom)
    monkeypatch.setattr(samsungtvws, "SamsungTVWS", boom)


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path):
    """Every test gets its own config and data directories: nothing touches the real user folders."""
    from tvpult import config

    saved = config._paths
    config.use_paths(tmp_path / "config" / "devices.toml", tmp_path / "data")
    yield
    config._paths = saved


@pytest.fixture(autouse=True)
def no_real_adapters(monkeypatch):
    """Tests don't depend on this machine's network adapters (masks differ between machines)."""
    monkeypatch.setattr(discovery, "adapter_addresses", lambda: [])
