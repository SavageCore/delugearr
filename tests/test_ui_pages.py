"""Smoke tests that the NiceGUI page builders render without errors.

These exercise the page construction (header, theme, filter bars, tables)
inside NiceGUI's in-process user simulation, catching build-time regressions
without needing a browser.
"""

import pytest
from nicegui import events, ui
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


async def test_dashboard_filters_reload_from_server(runtime):
    """Filtering/search must re-request with the filter applied (shared filters dict)."""
    store, scanner = runtime
    for i in range(20):
        store.log_detection(
            "20260101-000000",
            {
                "hash": f"h{i}",
                "name": f"Some.Release.{i}",
                "label": "tv-sonarr" if i % 2 == 0 else "radarr",
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
        assert len(dashboard_table.rows) == 20

        user.find(ui.input).trigger("update:value", "Some.Release.5")
        assert len(dashboard_table.rows) == 1
        assert dashboard_table.rows[0]["name"] == "Some.Release.5"
        assert dashboard_table.pagination["rowsNumber"] == 1

        # dropdown filters go through the same shared dict (browser sends {value, label})
        user.find(ui.input).trigger("update:value", "")
        label_select = next(
            e for e in user.client.elements.values() if isinstance(e, ui.select) and "radarr" in e.options
        )
        for listener in label_select._event_listeners.values():  # pylint: disable=protected-access
            if listener.type == "update:modelValue":
                with user.client:
                    events.handle_event(
                        listener.handler,
                        events.GenericEventArguments(
                            sender=label_select,
                            client=user.client,
                            args=label_select._value_to_model_value("radarr"),
                        ),
                    )
        assert len(dashboard_table.rows) == 10
        assert dashboard_table.pagination["rowsNumber"] == 10
    store, scanner = runtime
    async with user_simulation(root=lambda: _settings(store, scanner)) as user:
        await user.open("/")
        await user.should_see("Settings")
        await user.should_see("API key")
        await user.should_see("Deluge connection")


async def test_notification_dialog_builds_on_add(runtime):
    """Regression: the add-connection dialog must construct without raising
    (field hints are Quasar props, not ui.input kwargs)."""
    from nicegui import events

    store, scanner = runtime
    async with user_simulation(root=lambda: _settings(store, scanner)) as user:
        await user.open("/")
        add_btn = next(
            e
            for e in user.client.elements.values()
            if isinstance(e, ui.button) and e.props.get("icon") == "add"
        )
        for listener in add_btn._event_listeners.values():  # noqa: SLF001
            if listener.type == "click":
                with user.client:
                    events.handle_event(
                        listener.handler,
                        events.GenericEventArguments(sender=add_btn, client=user.client, args=[]),
                    )
        assert any(isinstance(e, ui.dialog) for e in user.client.elements.values())
        links = [e for e in user.client.elements.values() if isinstance(e, ui.link)]
        assert any("webhook" in (link.text or "").lower() for link in links)
