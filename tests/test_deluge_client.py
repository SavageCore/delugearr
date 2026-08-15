"""Deluge client tests: re-auth when the web session expires mid-run.

Deluge's web JSON-RPC session cookie can expire between scans, surfacing as
"Not authenticated" on an RPC (not on auth.login). The client must re-login
once and retry rather than raising immediately.
"""

import pytest

from delugearr.deluge_client import DelugeClient, DelugeError


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, method_queues):
        self.method_queues = method_queues
        self.calls = []

    def post(self, url, json, timeout=None):
        method = json["method"]
        self.calls.append(method)
        return _FakeResponse(self.method_queues[method].pop(0))


def _client(method_queues):
    client = DelugeClient("http://127.0.0.1:1", "pw")
    client.session = _FakeSession(method_queues)
    return client


def test_expired_session_is_reauthenticated():
    client = _client(
        {
            "auth.login": [{"result": True}],
            "core.get_torrents_status": [
                {"error": {"message": "Not authenticated", "code": 1}},
                {"result": {"abc": {"name": "x"}}},
            ],
        }
    )
    client._authed = True  # simulate a stale session: login() is a no-op

    result = client.get_torrents()

    assert result == {"abc": {"name": "x"}}
    assert client.session.calls.count("auth.login") == 1
    assert client._authed is True


def test_unrecoverable_auth_failure_raises_no_recursion():
    client = _client(
        {
            "auth.login": [{"result": True}],
            "core.get_torrents_status": [
                {"error": {"message": "Not authenticated", "code": 1}},
                {"error": {"message": "Not authenticated", "code": 1}},
            ],
        }
    )
    client._authed = True

    with pytest.raises(DelugeError):
        client.get_torrents()

    assert client.session.calls.count("auth.login") == 1


def test_non_auth_rpc_error_raises_without_retry():
    client = _client(
        {
            "auth.login": [{"result": True}],
            "core.get_torrents_status": [{"error": {"message": "boom", "code": 2}}],
        }
    )
    client._authed = True

    with pytest.raises(DelugeError, match="boom"):
        client.get_torrents()

    assert "auth.login" not in client.session.calls


def test_fresh_login_does_not_retry_on_auth_result_error():
    client = _client(
        {
            "auth.login": [{"error": {"message": "Not authenticated", "code": 1}}],
        }
    )

    with pytest.raises(DelugeError, match="auth failed"):
        client.login()

    assert client.session.calls.count("auth.login") == 1
