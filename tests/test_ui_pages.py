"""Smoke tests that the NiceGUI page builders render without errors.

These exercise the page construction (header, theme, filter bars, tables)
inside NiceGUI's in-process user simulation, catching build-time regressions
without needing a browser.
"""

import pytest
from nicegui import ui
from nicegui.testing import user_simulation

from delugearr import config
from delugearr.scanner import Scanner
from delugearr.store import Store
from delugearr.ui import _dashboard, _history, _settings


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path))
    monkeypatch.setenv("BASE_PATH", "/delugearr")
    monkeypatch.setenv("AUTH_USER", "savagecore")
    monkeypatch.setenv("AUTH_PASSWORD", "testpass")
    monkeypatch.setenv("DELUGE_URL", "http://127.0.0.1:1")
    store = Store(config.db_path(), defaults=config.store_defaults())
    scanner = Scanner(store=store)
    return store, scanner


async def test_dashboard_renders(runtime):
    store, scanner = runtime
    async with user_simulation(root=lambda: _dashboard(store, scanner)) as user:
        await user.open("/")
        await user.should_see("Delugearr")


async def test_history_renders(runtime):
    store, _scanner = runtime
    async with user_simulation(root=lambda: _history(store)) as user:
        await user.open("/")
        await user.should_see("History")


async def test_dashboard_shows_detections_in_server_side_table(runtime):
    store, scanner = runtime
    for i in range(60):
        store.log_detection(
            "20260101-000000",
            {
                "hash": f"h{i}",
                "name": f"Some.Release.{i}",
                "label": "tv-sonarr",
                "tracker_host": "tracker.example.org",
                "total_size": 123,
            },
            "Error: Unregistered torrent",
            "unregistered",
            "would_remove_data",
            True,
        )
    async with user_simulation(root=lambda: _dashboard(store, scanner)) as user:
        await user.open("/")
        tables = [e for e in user.client.elements.values() if isinstance(e, ui.table)]
        dashboard_table = max(tables, key=lambda t: len(t.rows))
        # server-side: only the visible page (25 rows) is kept, not all 60
        assert len(dashboard_table.rows) == 25
        assert dashboard_table.pagination["rowsNumber"] == 60
        assert dashboard_table.rows[0]["name"] == "Some.Release.0"


async def test_history_server_side_pagination(runtime):
    store, _scanner = runtime
    for i in range(60):
        store.log_detection(
            "20260101-000000",
            {
                "hash": f"h{i}",
                "name": f"Some.Release.{i}",
                "label": "tv-sonarr",
                "tracker_host": "tracker.example.org",
                "total_size": 123,
            },
            "Error: Unregistered torrent",
            "unregistered",
            "would_remove_data",
            True,
        )
    async with user_simulation(root=lambda: _history(store)) as user:
        await user.open("/")
        await user.should_see("History")
        tables = [e for e in user.client.elements.values() if isinstance(e, ui.table)]
        assert len(tables) == 1
        assert len(tables[0].rows) == 25
        assert tables[0].pagination["rowsNumber"] == 60


async def test_dashboard_request_event_serves_page(runtime):
    """The Quasar `request` event (page change / filter) is served server-side."""
    store, scanner = runtime
    for i in range(60):
        store.log_detection(
            "20260101-000000",
            {
                "hash": f"h{i}",
                "name": f"Some.Release.{i}",
                "label": "tv-sonarr",
                "tracker_host": "tracker.example.org",
                "total_size": 123,
            },
            "Error: Unregistered torrent",
            "unregistered",
            "would_remove_data",
            True,
        )
    async with user_simulation(root=lambda: _dashboard(store, scanner)) as user:
        await user.open("/")
        tables = [e for e in user.client.elements.values() if isinstance(e, ui.table)]
        dashboard_table = max(tables, key=lambda t: len(t.rows))
        user.find(kind=ui.table).trigger(
            "request",
            [{"pagination": {"page": 3, "rowsPerPage": 25, "sortBy": None, "descending": False}}],
        )
        assert len(dashboard_table.rows) == 10
        assert dashboard_table.pagination["page"] == 3
        assert dashboard_table.rows[0]["name"] == "Some.Release.50"


async def test_settings_renders(runtime):
    store, scanner = runtime
    async with user_simulation(root=lambda: _settings(store, scanner)) as user:
        await user.open("/")
        await user.should_see("Settings")
        await user.should_see("API key")
        await user.should_see("Deluge connection")
