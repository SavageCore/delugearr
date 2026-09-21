"""Scheduler survival: disk-full must defer, not kill the thread."""

import contextlib
import sqlite3

from delugearr import app as app_module


class Done(BaseException):
    pass


class FakeStore:
    def __init__(self, path="/tmp/app.db", fail_scan=False):
        self.path = path
        self.errors = []
        self._fail = fail_scan

    def get_settings(self):
        if self._fail:
            raise sqlite3.OperationalError("disk I/O error")
        return {"interval_minutes": 30, "last_scan_error": None}

    def set_last_scan_error(self, msg):
        self.errors.append(msg)


class FakeScanner:
    def __init__(self, exc=None):
        self.calls = 0
        self._exc = exc

    def scan(self):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return {}


def _run_one_iteration(scheduler, monkeypatch, sleeps):
    """Run a single scheduler loop iteration without the 10s boot sleep."""
    booted = {"n": 0}

    def fake_sleep(s):
        if booted["n"] == 0 and s == 10:
            booted["n"] += 1
            return
        sleeps.append(s)
        raise Done

    monkeypatch.setattr(app_module.time, "sleep", fake_sleep)
    with contextlib.suppress(Done):
        scheduler.run()
    return sleeps


def test_scheduler_survives_scan_disk_io_error(tmp_path, monkeypatch):
    store = FakeStore()
    scanner = FakeScanner(exc=sqlite3.OperationalError("disk I/O error"))
    sched = app_module.Scheduler(scanner, store)
    monkeypatch.setattr(sched, "_db_free_bytes", lambda: 10 * 1024 * 1024 * 1024)
    sleeps = []
    _run_one_iteration(sched, monkeypatch, sleeps)
    assert scanner.calls == 1
    assert store.errors and "disk I/O error" in store.errors[0]
    assert sleeps[-1] == app_module.SCHEDULER_RETRY_SECONDS


def test_scheduler_skips_scan_when_disk_full(tmp_path, monkeypatch):
    store = FakeStore()
    scanner = FakeScanner()
    sched = app_module.Scheduler(scanner, store)
    monkeypatch.setattr(sched, "_db_free_bytes", lambda: 0)
    sleeps = []
    _run_one_iteration(sched, monkeypatch, sleeps)
    assert scanner.calls == 0
    assert store.errors and "disk full" in store.errors[0]
    assert sleeps[-1] == app_module.SCHEDULER_RETRY_SECONDS


def test_scheduler_survives_get_settings_disk_io_error(tmp_path, monkeypatch):
    store = FakeStore(fail_scan=True)
    scanner = FakeScanner()
    sched = app_module.Scheduler(scanner, store)
    monkeypatch.setattr(sched, "_db_free_bytes", lambda: 10 * 1024 * 1024 * 1024)
    sleeps = []
    # scan succeeds, get_settings raises -> must sleep RETRY, not propagate.
    _run_one_iteration(sched, monkeypatch, sleeps)
    assert scanner.calls == 1
    assert sleeps[-1] == app_module.SCHEDULER_RETRY_SECONDS
