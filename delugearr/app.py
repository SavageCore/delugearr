"""delugearr bootstrap: NiceGUI UI mounted at /delugearr, ops API, scheduler.

The web layer is rebuilt with NiceGUI (Vue/Quasar) and served behind nginx on a
base path (no subdomain). The FastAPI app owns both the NiceGUI mount and a
small JSON API used for ops/health checks.
"""

import logging
import threading
import time
from logging.handlers import RotatingFileHandler

import uvicorn
from fastapi import FastAPI
from nicegui import ui

from . import config
from . import ui as ui_module
from .scanner import Scanner
from .store import Store

log = logging.getLogger("delugearr")


class Scheduler(threading.Thread):
    """Runs a scan shortly after boot, then every interval_minutes."""

    def __init__(self, scanner, store):
        super().__init__(name="scheduler", daemon=True)
        self.scanner = scanner
        self.store = store

    def run(self):
        time.sleep(10)
        while True:
            try:
                self.scanner.scan()
            except Exception:
                log.exception("scheduled scan failed")
            settings = self.store.get_settings()
            interval = max(60, int(settings.get("interval_minutes") or 30)) * 60
            time.sleep(interval)


def create_app(store, scanner):
    fastapi_app = FastAPI(title="Delugearr")

    @fastapi_app.get(config.base_path() + "/api/status")
    def api_status():
        settings = store.get_settings()
        return {
            "dry_run": bool(settings.get("dry_run", True)),
            "interval_minutes": settings.get("interval_minutes"),
            "filter_completed": bool(settings.get("filter_completed", True)),
            "scanning": scanner.scanning,
            "deluge_connected": scanner.deluge_ok,
            "last_scan_at": settings.get("last_scan_at"),
            "last_scan_stats": settings.get("last_scan_stats"),
            "last_scan_error": settings.get("last_scan_error"),
        }

    @fastapi_app.post(config.base_path() + "/api/scan")
    def api_scan():
        if scanner.scanning:
            return {"started": False, "reason": "already scanning"}
        threading.Thread(target=scanner.scan, daemon=True).start()
        return {"started": True}

    ui_module.build_pages(store, scanner)

    ui.run_with(
        fastapi_app,
        mount_path=config.base_path(),
        title="Delugearr",
        dark=True,
        storage_secret=None,
        show_welcome_message=False,
    )
    return fastapi_app


def _setup_logging():
    handlers = [logging.StreamHandler()]
    try:
        handlers.append(RotatingFileHandler(config.log_path(), maxBytes=2 * 1024 * 1024, backupCount=5))
    except OSError as exc:
        log.warning("could not open log file %s: %s", config.log_path(), exc)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def main():
    _setup_logging()
    store = Store(config.db_path(), defaults=config.store_defaults())
    scanner = Scanner(store=store)
    Scheduler(scanner, store).start()
    app = create_app(store, scanner)
    log.info(
        "delugearr %s listening on 127.0.0.1:%s (mount %s)",
        __import__("delugearr").__version__,
        config.port(),
        config.base_path(),
    )
    uvicorn.run(app, host="127.0.0.1", port=config.port(), log_level="info")
