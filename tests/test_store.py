"""Store tests: api key lifecycle and dashboard/scan run separation."""

from delugearr.store import Store


def make_store(tmp_path, **defaults):
    return Store(tmp_path / "app.db", defaults=defaults)


def torrent(hash, name="Some.Release.2026", tracker_host="tracker.example.org"):
    return {
        "hash": hash,
        "name": name,
        "label": "tv-sonarr",
        "tracker_host": tracker_host,
        "total_size": 123,
    }


def test_api_key_auto_generated(tmp_path):
    store = make_store(tmp_path)
    key = store.api_key()
    assert key
    assert len(key) == 64  # secrets.token_hex(32)


def test_regenerate_api_key(tmp_path):
    store = make_store(tmp_path)
    first = store.api_key()
    second = store.regenerate_api_key()
    assert second != first
    assert store.api_key() == second


def test_latest_run_excludes_manual(tmp_path):
    store = make_store(tmp_path)
    store.log_detection(
        "20260101-000000",
        torrent("h1"),
        "Error: Unregistered torrent",
        "unregistered",
        "would_remove_data",
        True,
    )
    store.log_detection(
        "manual-20260101-010000",
        torrent("h2"),
        "manual removal",
        "unregistered",
        "manual_removed_data",
        False,
    )
    latest = store.latest_run()
    assert latest["run_id"] == "20260101-000000"
    assert latest["n"] == 1


def test_manual_removed_hashes(tmp_path):
    store = make_store(tmp_path)
    store.log_detection("manual-1", torrent("h1"), "x", "unregistered", "manual_removed_only", False)
    store.log_detection("scan-1", torrent("h2"), "x", "unregistered", "removed_data", False)
    assert store.manual_removed_hashes() == {"h1"}


def test_current_detections_excludes_removed_and_exempt(tmp_path):
    store = make_store(tmp_path)
    run = "20260101-000000"
    for hash in ("h1", "h2", "h3"):
        store.log_detection(run, torrent(hash), "x", "unregistered", "would_remove_data", True)
    store.log_detection(
        "manual-20260101-010000",
        torrent("h1"),
        "manual removal",
        "unregistered",
        "manual_removed_data",
        False,
    )
    store.add_exempt("h2", "user")
    hashes = {d["torrent_hash"] for d in store.current_detections()}
    assert hashes == {"h3"}
