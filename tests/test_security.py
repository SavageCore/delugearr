"""Trusted-network security helpers: localhost is always trusted regardless of
the stored trusted_networks / trusted_proxies list."""

import pytest

from delugearr import security


class FakeRequest:
    def __init__(self, ip, xff=None):
        self._ip = ip
        self._xff = xff
        self.headers = {"x-forwarded-for": xff} if xff else {}

    @property
    def client(self):
        return type("Peer", (), {"host": self._ip})()


@pytest.mark.parametrize(
    "ip,trusted,bypass",
    [
        ("127.0.0.1", [], True),  # localhost trusted even with empty list
        ("::1", [], True),
        ("127.0.0.1", ["100.64.0.0/10"], True),  # list overwritten without localhost
        ("100.64.0.1", ["100.64.0.0/10"], True),
        ("100.64.0.1", ["127.0.0.1/32"], False),  # tailscale IP no longer trusted
    ],
)
def test_should_bypass_auth_localhost_always_trusted(ip, trusted, bypass):
    assert (
        security.should_bypass_auth(ip, {"auth_bypass_enabled": True, "trusted_networks": trusted}) is bypass
    )


def test_no_bypass_when_disabled():
    assert (
        security.should_bypass_auth("127.0.0.1", {"auth_bypass_enabled": False, "trusted_networks": []})
        is False
    )


def test_effective_client_ip_honours_xff_only_from_trusted_proxies():
    settings_proxies = ["127.0.0.1/32"]
    # localhost peer is an always-trusted proxy -> XFF honoured
    request = FakeRequest("127.0.0.1", xff="10.0.0.1")
    assert security.effective_client_ip(request, settings_proxies) == "10.0.0.1"
    # remote peer is not a trusted proxy -> XFF ignored
    request = FakeRequest("203.0.113.5", xff="10.0.0.1")
    assert security.effective_client_ip(request, settings_proxies) == "203.0.113.5"
    # proxies list overwritten without localhost still trusts localhost peer
    request = FakeRequest("::1", xff="10.0.0.1")
    assert security.effective_client_ip(request, ["100.64.0.0/10"]) == "10.0.0.1"
