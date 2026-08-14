"""Smoke tests that the NiceGUI page builders render without errors.

These exercise the page construction (header, theme, filter bars, tables)
inside NiceGUI's in-process user simulation, catching build-time regressions
without needing a browser.
"""

import pytest
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


async def test_settings_renders(runtime):
    store, _scanner = runtime
    async with user_simulation(root=lambda: _settings(store)) as user:
        await user.open("/")
        await user.should_see("Settings")
