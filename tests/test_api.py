"""API tests: key auth, redaction, and the endpoint surface (no Deluge)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from delugearr import config
from delugearr.api import build_router
from delugearr.notifier import DiscordNotifier, NtfyNotifier
from delugearr.store import Store


class StubScanner:
    scanning = False
    deluge_ok = True

    def scan(self, dry_run=None, run_id=None):
        return {}

    def manual_remove(self, torrent_hash, remove_data=True):
        return {
            "torrent_hash": torrent_hash,
            "name": "Some.Release.2026",
            "action": "manual_removed_data" if remove_data else "manual_removed_only",
            "remove_data": remove_data,
        }


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path))
    monkeypatch.setenv("BASE_PATH", "/delugearr")
    store = Store(config.db_path(), defaults=config.store_defaults())
    scanner = StubScanner()
    app = FastAPI()
    app.include_router(build_router(store, scanner))
    return TestClient(app), store


def torrent(hash, name="Some.Release.2026", tracker_host="tracker.example.org"):
    return {
        "hash": hash,
        "name": name,
        "label": "tv-sonarr",
        "tracker_host": tracker_host,
        "total_size": 123,
    }


def test_health_is_open(api):
    client, _store = api
    resp = client.get("/delugearr/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_api_requires_key_even_from_trusted_network(api):
    client, store = api
    store.update_settings(auth_bypass_enabled=True, trusted_networks=["127.0.0.1/32"])
    resp = client.get("/delugearr/api/status")
    assert resp.status_code == 401
    resp = client.get(
        "/delugearr/api/status",
        headers={"X-Api-Key": store.api_key(), "X-Forwarded-For": "10.0.0.1"},
    )
    assert resp.status_code == 200


def test_status_requires_key(api):
    client, _store = api
    assert client.get("/delugearr/api/status").status_code == 401
    resp = client.get("/delugearr/api/status", headers={"X-Api-Key": "wrong"})
    assert resp.status_code == 401


def test_status_accepts_header_and_query(api):
    client, store = api
    key = store.api_key()
    assert client.get("/delugearr/api/status", headers={"X-Api-Key": key}).status_code == 200
    assert client.get(f"/delugearr/api/status?apikey={key}").status_code == 200


def test_detections_returns_scan_run(api):
    client, store = api
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
    key = store.api_key()
    resp = client.get("/delugearr/api/detections", headers={"X-Api-Key": key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "20260101-000000"
    assert len(body["rows"]) == 1
    assert body["rows"][0]["torrent_hash"] == "h1"


def test_history_lists_everything(api):
    client, store = api
    store.log_detection(
        "20260101-000000",
        torrent("h1"),
        "x",
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
    key = store.api_key()
    body = client.get("/delugearr/api/history", headers={"X-Api-Key": key}).json()
    assert len(body["rows"]) == 2


def test_history_action_filter_finds_rows_beyond_default_limit(api):
    """Regression: action filtering must run in SQL before LIMIT, so a rare
    action whose rows fall outside the newest N still shows up."""
    client, store = api
    for i in range(150):
        store.log_detection(
            "20260101-000000",
            torrent(f"h{i}"),
            "x",
            "unregistered",
            "would_remove_data",
            True,
        )
    store.log_detection(
        "manual-20260101-010000",
        torrent("h-manual"),
        "manual removal",
        "unregistered",
        "manual_removed_data",
        False,
    )
    key = store.api_key()
    body = client.get("/delugearr/api/history?action=manual_removed_data", headers={"X-Api-Key": key}).json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["torrent_hash"] == "h-manual"


def test_history_name_filter_finds_rows_beyond_default_limit(api):
    client, store = api
    for i in range(150):
        store.log_detection(
            "20260101-000000",
            torrent(f"h{i}", name=f"Common.Release.{i}"),
            "x",
            "unregistered",
            "would_remove_data",
            True,
        )
    store.log_detection(
        "manual-20260101-010000",
        torrent("h-needle", name="Rare.Album.2026"),
        "manual removal",
        "unregistered",
        "manual_removed_data",
        False,
    )
    key = store.api_key()
    body = client.get("/delugearr/api/history?name=Rare.Album", headers={"X-Api-Key": key}).json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["torrent_hash"] == "h-needle"


def test_settings_get_redacts_secrets(api):
    client, store = api
    store.update_settings(deluge_password="hunter2")
    body = client.get("/delugearr/api/settings", headers={"X-Api-Key": store.api_key()}).json()
    assert "deluge_url" in body
    assert "deluge_password" not in body
    assert "api_key" not in body


def test_settings_put_updates(api):
    client, store = api
    resp = client.put(
        "/delugearr/api/settings",
        headers={"X-Api-Key": store.api_key()},
        json={"interval_minutes": 15, "deluge_password": "newpass", "notify_max_items": 10},
    )
    assert resp.status_code == 200
    assert resp.json()["notify_max_items"] == 10
    assert "deluge_password" not in resp.json()
    settings = store.get_settings()
    assert settings["interval_minutes"] == 15
    assert settings["deluge_password"] == "newpass"


def test_settings_put_updates_host_port_base_path(api):
    client, store = api
    headers = {"X-Api-Key": store.api_key()}
    resp = client.put(
        "/delugearr/api/settings",
        headers=headers,
        json={"host": "0.0.0.0", "port": 8080, "base_path": "/delugearr"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["host"] == "0.0.0.0"
    assert body["port"] == 8080
    assert body["base_path"] == "/delugearr"
    settings = store.get_settings()
    assert (settings["host"], settings["port"], settings["base_path"]) == ("0.0.0.0", 8080, "/delugearr")


def test_settings_put_validates_server_options(api):
    client, store = api
    headers = {"X-Api-Key": store.api_key()}

    resp = client.put("/delugearr/api/settings", headers=headers, json={"host": "   "})
    assert resp.status_code == 400

    resp = client.put("/delugearr/api/settings", headers=headers, json={"base_path": "delugearr"})
    assert resp.status_code == 400

    resp = client.put("/delugearr/api/settings", headers=headers, json={"port": 0})
    assert resp.status_code == 422
    resp = client.put("/delugearr/api/settings", headers=headers, json={"port": 70000})
    assert resp.status_code == 422

    resp = client.put("/delugearr/api/settings", headers=headers, json={"base_path": "/delugearr/"})
    assert resp.status_code == 200
    assert resp.json()["base_path"] == "/delugearr"


def test_remove_and_exempt_roundtrip(api):
    client, store = api
    key = store.api_key()
    headers = {"X-Api-Key": key}

    resp = client.post("/delugearr/api/torrents/h1/remove", headers=headers, json={"remove_data": False})
    assert resp.status_code == 200
    assert resp.json()["action"] == "manual_removed_only"

    resp = client.post("/delugearr/api/torrents/h2/exempt", headers=headers, json={"reason": "keep"})
    assert resp.status_code == 200
    assert client.get("/delugearr/api/exempt", headers=headers).json()["rows"][0]["torrent_hash"] == "h2"

    resp = client.delete("/delugearr/api/exempt/h2", headers=headers)
    assert resp.status_code == 200
    assert client.get("/delugearr/api/exempt", headers=headers).json()["rows"] == []


def test_notifications_crud_and_redaction(api, monkeypatch):
    monkeypatch.setattr(DiscordNotifier, "send_test", lambda self: True)
    client, store = api
    key = store.api_key()
    headers = {"X-Api-Key": key}

    created = client.post(
        "/delugearr/api/notifications",
        headers=headers,
        json={"name": "Discord", "webhook_url": "https://discord/hook", "triggers": ["scan_summary"]},
    ).json()
    assert created["webhook_url"] == "***"
    cid = created["id"]

    listed = client.get("/delugearr/api/notifications", headers=headers).json()["rows"]
    assert listed[0]["webhook_url"] == "***"
    assert store.list_notifications()[0]["webhook_url"] == "https://discord/hook"

    updated = client.put(
        f"/delugearr/api/notifications/{cid}",
        headers=headers,
        json={"enabled": False, "triggers": ["errors"]},
    ).json()
    assert updated["enabled"] is False
    assert updated["triggers"] == ["errors"]

    assert client.delete(f"/delugearr/api/notifications/{cid}", headers=headers).status_code == 200
    assert client.get("/delugearr/api/notifications", headers=headers).json()["rows"] == []


def test_notification_save_requires_working_webhook(api, monkeypatch):
    monkeypatch.setattr(DiscordNotifier, "send_test", lambda self: False)
    client, store = api
    key = store.api_key()
    headers = {"X-Api-Key": key}

    resp = client.post(
        "/delugearr/api/notifications",
        headers=headers,
        json={"name": "Discord", "webhook_url": "https://discord/bad"},
    )
    assert resp.status_code == 400
    assert store.list_notifications() == []

    resp = client.post(
        "/delugearr/api/notifications",
        headers=headers,
        json={"name": "Discord", "webhook_url": ""},
    )
    assert resp.status_code == 400
    assert store.list_notifications() == []

    def boom(self):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(DiscordNotifier, "send_test", boom)
    resp = client.post(
        "/delugearr/api/notifications",
        headers=headers,
        json={"name": "Discord", "webhook_url": "https://discord/bad"},
    )
    assert resp.status_code == 400
    assert store.list_notifications() == []


def test_notification_test_requires_key_and_missing_returns_404(api):
    client, store = api
    key = store.api_key()
    headers = {"X-Api-Key": key}
    assert client.post("/delugearr/api/notifications/1/test").status_code == 401
    assert client.post("/delugearr/api/notifications/999/test", headers=headers).status_code == 404


def test_ntfy_notification_crud_and_redaction(api, monkeypatch):
    monkeypatch.setattr(NtfyNotifier, "send_test", lambda self: True)
    client, store = api
    key = store.api_key()
    headers = {"X-Api-Key": key}

    created = client.post(
        "/delugearr/api/notifications",
        headers=headers,
        json={
            "name": "Phone",
            "type": "ntfy",
            "webhook_url": "https://ntfy.sh/delugearr",
            "access_token": "tk_secret",
            "triggers": ["scan_summary", "errors"],
        },
    ).json()
    assert created["type"] == "ntfy"
    assert created["webhook_url"] == "***"
    assert created["access_token"] == "***"
    cid = created["id"]

    raw = store.list_notifications()[0]
    assert raw["type"] == "ntfy"
    assert raw["webhook_url"] == "https://ntfy.sh/delugearr"
    assert raw["access_token"] == "tk_secret"

    listed = client.get("/delugearr/api/notifications", headers=headers).json()["rows"][0]
    assert listed["access_token"] == "***"

    assert client.post(f"/delugearr/api/notifications/{cid}/test", headers=headers).json() == {"sent": True}
    assert client.delete(f"/delugearr/api/notifications/{cid}", headers=headers).status_code == 200


def test_ntfy_save_verifies_with_ntfy_notifier(api, monkeypatch):
    sent = []
    monkeypatch.setattr(NtfyNotifier, "send_test", lambda self: (sent.append(self), True)[1])
    monkeypatch.setattr(
        DiscordNotifier,
        "send_test",
        lambda self: (_ for _ in ()).throw(AssertionError("should not call discord")),
    )
    client, store = api
    key = store.api_key()
    headers = {"X-Api-Key": key}

    resp = client.post(
        "/delugearr/api/notifications",
        headers=headers,
        json={"name": "Phone", "type": "ntfy", "webhook_url": "https://ntfy.sh/delugearr"},
    )
    assert resp.status_code == 200
    assert len(sent) == 1


def test_notification_default_type_is_discord(api, monkeypatch):
    monkeypatch.setattr(DiscordNotifier, "send_test", lambda self: True)
    client, store = api
    key = store.api_key()
    headers = {"X-Api-Key": key}
    created = client.post(
        "/delugearr/api/notifications",
        headers=headers,
        json={"name": "Discord", "webhook_url": "https://discord/hook"},
    ).json()
    assert created["type"] == "discord"
