"""Dwell-timer (#1360) and tracker-guard (#1359) scan-level tests."""

import time

from delugearr.scanner import Scanner
from delugearr.store import Store

UNREG = "Error: unregistered torrent"


class FakeClient:
    def __init__(self, torrents):
        self._torrents = dict(torrents)
        self.removed = []

    def get_torrents(self):
        return dict(self._torrents)

    def remove_torrents(self, hashes, remove_data=True):
        self.removed.extend(list(hashes))


def torrent(hash, tracker_status=UNREG, **kw):
    data = {
        "hash": hash,
        "name": hash,
        "tracker_host": "tracker.example.org",
        "tracker_status": tracker_status,
        "state": "Seeding",
        "time_added": time.time() - 3600,
    }
    data.update(kw)
    return data


def make_scanner(tmp_path, torrents, **settings):
    store = Store(tmp_path / "app.db")
    base = {"dry_run": False, "rem_unregistered_confirm_minutes": 0}
    base.update(settings)
    store.update_settings(**base)
    client = FakeClient(torrents)
    return Scanner(client=client, store=store), client, store


def test_confirm_zero_removes_immediately(tmp_path):
    scanner, client, _ = make_scanner(tmp_path, {"h1": torrent("h1")})
    stats = scanner.scan()
    assert stats["unregistered"] == 1
    assert stats["pending"] == 0
    assert client.removed == ["h1"]


def test_first_sighting_flags_and_waits(tmp_path):
    scanner, client, store = make_scanner(
        tmp_path, {"h1": torrent("h1")}, rem_unregistered_confirm_minutes=30
    )
    stats = scanner.scan()
    assert stats["unregistered"] == 1
    assert stats["pending"] == 1
    assert client.removed == []
    assert store.get_pending_removal("h1") is not None


def test_removes_after_dwell_elapses(tmp_path):
    store = Store(tmp_path / "app.db")
    store.update_settings(dry_run=False, rem_unregistered_confirm_minutes=5)
    # Pre-dwell the torrent long enough that the window has passed.
    store.set_pending_removal("h1", "unregisteredCheck", time.time() - 600)
    client = FakeClient({"h1": torrent("h1")})
    scanner = Scanner(client=client, store=store)
    stats = scanner.scan()
    assert stats["unregistered"] == 1
    assert stats["pending"] == 0
    assert client.removed == ["h1"]


def test_waits_when_dwell_not_elapsed(tmp_path):
    store = Store(tmp_path / "app.db")
    store.update_settings(dry_run=False, rem_unregistered_confirm_minutes=30)
    store.set_pending_removal("h1", "unregisteredCheck", time.time() - 10)
    client = FakeClient({"h1": torrent("h1")})
    scanner = Scanner(client=client, store=store)
    stats = scanner.scan()
    assert stats["pending"] == 1
    assert client.removed == []


def test_dry_run_does_not_stamp_marker(tmp_path):
    scanner, client, store = make_scanner(
        tmp_path, {"h1": torrent("h1")}, dry_run=True, rem_unregistered_confirm_minutes=30
    )
    stats = scanner.scan()
    assert stats["pending"] == 1
    assert client.removed == []
    assert store.get_pending_removal("h1") is None


def test_recovery_clears_pending(tmp_path):
    client = FakeClient({"h1": torrent("h1", "Error: host not found (non-authoritative), try again later")})
    store = Store(tmp_path / "app.db")
    store.update_settings(dry_run=False, rem_unregistered_confirm_minutes=30)
    store.set_pending_removal("h1", "unregisteredCheck", time.time() - 600)
    scanner = Scanner(client=client, store=store)
    stats = scanner.scan()
    assert store.get_pending_removal("h1") is None
    assert client.removed == []
    assert stats["unregistered"] == 0


def test_working_tracker_never_removes(tmp_path):
    # A WORKING sibling tracker means the torrent is alive; the combined-message
    # "unregistered" signal must not trigger a removal (#1359 guard).
    client = FakeClient(
        {
            "h1": torrent(
                "h1",
                trackers=[
                    {"url": "https://tracker.example.org/announce", "status": 1},
                    {"url": "https://tracker2.example.org/announce", "status": 3, "message": UNREG},
                ],
            )
        }
    )
    store = Store(tmp_path / "app.db")
    store.update_settings(dry_run=False)
    scanner = Scanner(client=client, store=store)
    stats = scanner.scan()
    assert client.removed == []
    assert stats["unregistered"] == 0


def test_working_tracker_clears_pending(tmp_path):
    client = FakeClient(
        {
            "h1": torrent(
                "h1",
                trackers=[
                    {"url": "https://tracker.example.org/announce", "status": 1},
                    {"url": "https://tracker2.example.org/announce", "status": 3, "message": UNREG},
                ],
            )
        }
    )
    store = Store(tmp_path / "app.db")
    store.update_settings(dry_run=False, rem_unregistered_confirm_minutes=30)
    store.set_pending_removal("h1", "unregisteredCheck", time.time() - 600)
    scanner = Scanner(client=client, store=store)
    scanner.scan()
    assert store.get_pending_removal("h1") is None


def test_inconclusive_tracker_skips_removal(tmp_path):
    client = FakeClient(
        {
            "h1": torrent(
                "h1",
                trackers=[
                    {"url": "https://tracker.example.org/announce", "status": 2},  # UPDATING
                    {"url": "https://tracker2.example.org/announce", "status": 3, "message": UNREG},
                ],
            )
        }
    )
    store = Store(tmp_path / "app.db")
    store.update_settings(dry_run=False)
    scanner = Scanner(client=client, store=store)
    stats = scanner.scan()
    assert client.removed == []
    assert stats["unregistered"] == 0


def test_orphan_pending_pruned(tmp_path):
    client = FakeClient({"h1": torrent("h1")})
    store = Store(tmp_path / "app.db")
    store.update_settings(dry_run=False, rem_unregistered_confirm_minutes=30)
    store.set_pending_removal("h1", "unregisteredCheck", time.time() - 10)
    store.set_pending_removal("ghost", "unregisteredCheck", time.time() - 10)
    scanner = Scanner(client=client, store=store)
    scanner.scan()
    assert store.get_pending_removal("h1") is not None
    assert store.get_pending_removal("ghost") is None
