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
        "ratio": 1.5,
        "seeding_time": 7200,
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


def test_data_version_bumps_on_mutation(tmp_path):
    store = make_store(tmp_path)
    version = store.get_settings()["data_version"]
    store.log_detection("scan-1", torrent("h1"), "x", "unregistered", "would_remove_data", True)
    assert store.get_settings()["data_version"] == version + 1
    store.update_settings(interval_minutes=5)
    assert store.get_settings()["data_version"] == version + 2
    store.add_exempt("h2", "user")
    assert store.get_settings()["data_version"] == version + 3
    store.remove_exempt("h2")
    assert store.get_settings()["data_version"] == version + 4


def test_search_history_paginates_and_filters(tmp_path):
    store = make_store(tmp_path)
    for i in range(60):
        store.log_detection(
            "20260101-000000",
            torrent(f"h{i}", name=f"Release.{i}", tracker_host="tracker.example.org"),
            "Error: Unregistered torrent",
            "unregistered",
            "would_remove_data",
            True,
        )
    store.log_detection(
        "20260101-000000",
        torrent("h-manual", name="Manual.Release", tracker_host="other.example.org"),
        "manual removal",
        "unregistered",
        "manual_removed_data",
        False,
    )

    page1, total = store.search_history(page=1, rows_per_page=25)
    assert total == 61
    assert len(page1) == 25

    page3, _ = store.search_history(page=3, rows_per_page=25)
    assert len(page3) == 11

    rows, total = store.search_history(name="Manual", page=1, rows_per_page=25)
    assert total == 1
    assert rows[0]["torrent_hash"] == "h-manual"

    rows, total = store.search_history(tracker="other.example.org", page=1, rows_per_page=25)
    assert total == 1

    rows, total = store.search_history(message="Error", page=1, rows_per_page=25)
    assert total == 60

    rows, total = store.search_history(message="manual removal", page=1, rows_per_page=25)
    assert total == 1

    rows, total = store.search_history(sort_by="name", descending=True, page=1, rows_per_page=10)
    assert rows[0]["name"] == "Release.9"  # SQLite string sort, not numeric
    assert total == 61


def test_history_facets(tmp_path):
    store = make_store(tmp_path)
    store.log_detection(
        "20260101-000000",
        torrent("h1", tracker_host="tracker.example.org"),
        "Error: Unregistered torrent",
        "unregistered",
        "would_remove_data",
        True,
    )
    store.log_detection(
        "20260101-000000",
        torrent("h2", name="NoLabel", tracker_host="other.example.org"),
        "Foo",
        "unregistered",
        "would_remove_data",
        True,
    )
    facets = store.history_facets()
    assert facets["labels"] == ["tv-sonarr"]
    assert facets["trackers"] == ["other.example.org", "tracker.example.org"]
    assert facets["categories"] == ["Error", "Foo"]


def test_notification_connection_crud(tmp_path):
    store = make_store(tmp_path)
    conn = store.add_notification(
        "Discord",
        "https://discord/hook",
        username="Bot",
        avatar="https://img/a.png",
        triggers=["scan_summary", "errors"],
    )
    assert conn["id"] == 1
    assert conn["enabled"] is True

    assert store.enabled_connections("scan_summary") == [conn]
    assert store.enabled_connections("removals") == []

    store.update_notification(conn["id"], enabled=False, triggers=["errors"])
    assert store.enabled_connections("errors") == []

    conn2 = store.add_notification("Off", "https://discord/hook2", triggers=["errors"], enabled=False)
    assert store.enabled_connections("errors") == []

    store.update_notification(conn2["id"], enabled=True)
    assert len(store.enabled_connections("errors")) == 1

    store.delete_notification(conn["id"])
    assert store.list_notifications() == [store.list_notifications()[0]]


def test_notification_ntfy_type_and_access_token_roundtrip(tmp_path):
    store = make_store(tmp_path)
    conn = store.add_notification(
        "Phone",
        "https://ntfy.sh/delugearr",
        type="ntfy",
        access_token="tk_secret",
        triggers=["errors"],
    )
    assert conn["type"] == "ntfy"
    assert conn["access_token"] == "tk_secret"
    assert conn["webhook_url"] == "https://ntfy.sh/delugearr"

    assert store.enabled_connections("errors") == [conn]

    store.update_notification(conn["id"], access_token="tk_new", webhook_url="https://ntfy.sh/other")
    updated = store.list_notifications()[0]
    assert updated["access_token"] == "tk_new"
    assert updated["webhook_url"] == "https://ntfy.sh/other"
    assert updated["type"] == "ntfy"


def test_notification_legacy_db_migrates_type_to_discord(tmp_path):
    """A DB created before the type/access_token columns must default to discord."""
    import sqlite3

    db = tmp_path / "app.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO settings(key,value) VALUES('api_key','"oldkey"');
        CREATE TABLE notification_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            webhook_url TEXT,
            username TEXT,
            avatar TEXT,
            triggers TEXT,
            enabled INTEGER DEFAULT 1
        );
        INSERT INTO notification_connections(name,webhook_url,triggers,enabled)
        VALUES('Discord','https://discord/hook','["errors"]',1);
        """
    )
    con.commit()
    con.close()

    store = Store(db, defaults={"api_key": "oldkey"})
    conn = store.list_notifications()[0]
    assert conn["type"] == "discord"
    assert conn["access_token"] == ""
    assert conn["enabled"] is True
    # new ntfy connection works on the migrated schema
    ntfy = store.add_notification("Phone", "https://ntfy.sh/x", type="ntfy", access_token="t")
    assert ntfy["type"] == "ntfy"


def test_notify_max_items_default(tmp_path):
    store = make_store(tmp_path)
    assert store.get_settings()["notify_max_items"] == 25
    store.update_settings(notify_max_items=0)
    assert store.get_settings()["notify_max_items"] == 0


def test_log_detection_persists_ratio_and_seeding(tmp_path):
    store = make_store(tmp_path)
    store.log_detection("run-1", torrent("h1"), "x", "unregistered", "would_remove_data", True)
    row = store.get_run_detections("run-1")[0]
    assert row["ratio"] == 1.5
    assert row["seeding_time"] == 7200


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "app.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE detections ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts REAL, torrent_hash TEXT,"
        "name TEXT, label TEXT, tracker TEXT, message TEXT, status TEXT, action TEXT,"
        "size INTEGER, dry_run INTEGER)"
    )
    con.commit()
    con.close()

    store = Store(path)
    store.log_detection("run-1", torrent("h1"), "x", "unregistered", "would_remove_data", True)
    row = store.get_run_detections("run-1")[0]
    assert row["ratio"] == 1.5
    assert row["seeding_time"] == 7200
