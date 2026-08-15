"""Thin client over the Deluge Web JSON-RPC API.

Connects to the Deluge Web UI (default http://127.0.0.1:8112/json) using its
Web UI password.
"""

import logging
from urllib.parse import urlsplit

import requests

log = logging.getLogger("deluge-client")

STATUS_KEYS = [
    "hash",
    "name",
    "label",
    "state",
    "tracker_host",
    "tracker_status",
    "trackers",
    "save_path",
    "total_size",
    "ratio",
    "total_uploaded",
    "total_done",
    "time_added",
    "seeding_time",
    "num_seeds",
    "num_peers",
    "is_finished",
    "progress",
]


class DelugeError(Exception):
    pass


def _is_auth_error(error):
    """True when a Deluge RPC error means the web session is not authenticated.

    Deluge's web JSON-RPC session cookie can expire between scans; the error
    surfaces on whatever RPC was running (e.g. core.get_torrents_status), not
    on auth.login.
    """
    text = error.get("message") if isinstance(error, dict) else str(error)
    return "Not authenticated" in text


class DelugeClient:
    def __init__(self, url, password, timeout=30):
        self.url = (url or "http://127.0.0.1:8112").rstrip("/") + "/json"
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self._id = 0
        self._authed = False

    def call(self, method, params=None, _retry=True):
        self._id += 1
        payload = {"method": method, "params": params or [], "id": self._id}
        try:
            resp = self.session.post(self.url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise DelugeError(f"HTTP error calling {method}: {exc}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise DelugeError(f"Non-JSON response calling {method}") from exc
        if data.get("error"):
            error = data["error"]
            if _retry and _is_auth_error(error):
                self._authed = False
                self.login()
                return self.call(method, params, _retry=False)
            raise DelugeError(f"Deluge RPC error calling {method}: {error}")
        return data.get("result")

    def login(self):
        if self._authed:
            return
        try:
            result = self.call("auth.login", [self.password], _retry=False)
        except DelugeError as exc:
            raise DelugeError(f"Deluge auth failed: {exc}") from exc
        if result is not True:
            raise DelugeError("Deluge auth.login failed (check DELUGE_PASSWORD)")
        self._authed = True

    def get_torrents(self, keys=None):
        self.login()
        result = self.call("core.get_torrents_status", [{}, keys or STATUS_KEYS])
        return result or {}

    def remove_torrents(self, hashes, remove_data=True):
        self.login()
        if not hashes:
            return
        self.call("core.remove_torrents", [list(hashes), bool(remove_data)])

    def reannounce(self, hashes):
        self.login()
        if not hashes:
            return
        self.call("core.force_reannounce", [list(hashes)])

    def connected(self):
        """Cheap connectivity probe; returns True when the web UI responds."""
        try:
            self.login()
            return True
        except DelugeError:
            return False


def host_from_url(url):
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""
