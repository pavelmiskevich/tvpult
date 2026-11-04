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
