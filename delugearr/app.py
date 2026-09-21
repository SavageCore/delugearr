"""delugearr bootstrap: NiceGUI UI mounted at /delugearr, ops API, scheduler.

The web layer uses NiceGUI (Vue/Quasar) and is served behind nginx on a base
path (no subdomain). The FastAPI app owns the NiceGUI mount and a small JSON
API for ops/health checks. All NiceGUI pages are protected by an auth
middleware + login page (nginx basic auth stays as defense in depth).
"""

import logging
import os
import secrets
import shutil
import sqlite3
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from nicegui import app as nicegui_app
from nicegui import ui
from starlette.middleware.base import BaseHTTPMiddleware

from . import config
from . import ui as ui_module
from .api import build_router
from .notifier import DEFAULT_AVATAR
from .scanner import Scanner
from .security import effective_client_ip, should_bypass_auth
from .store import Store

log = logging.getLogger("delugearr")

UNRESTRICTED_PATHS = {"", "/login", "/favicon.ico"}

# Scheduler survival tuning: retry quickly on disk-full instead of dying,
# and skip scans before SQLite hits 0B free (WAL needs sidecar space).
SCHEDULER_RETRY_SECONDS = 60
MIN_FREE_BYTES = 100 * 1024 * 1024


def _is_disk_full_error(exc):
    msg = str(exc).lower()
    return "disk i/o" in msg or "disk is full" in msg or "database or disk is full" in msg


_store = None


class Scheduler(threading.Thread):
    """Runs a scan shortly after boot, then every interval_minutes."""

    def __init__(self, scanner, store):
        super().__init__(name="scheduler", daemon=True)
        self.scanner = scanner
        self.store = store

    def _db_free_bytes(self):
        try:
            parent = Path(self.store.path).parent
            return shutil.disk_usage(parent if parent.exists() else "/").free
        except OSError:
            return None

    def run(self):
        time.sleep(10)
        while True:
            try:
                free = self._db_free_bytes()
                if free is not None and free < MIN_FREE_BYTES:
                    msg = f"deferred: disk full ({free} bytes free), scan skipped, will retry"
                    try:
                        self.store.set_last_scan_error(msg)
                    except Exception:
                        log.debug("set_last_scan_error failed", exc_info=True)
                    log.warning("Skipping scan, disk full (free=%s bytes)", free)
                    time.sleep(SCHEDULER_RETRY_SECONDS)
                    continue
                self.scanner.scan()
            except sqlite3.OperationalError as exc:
                if _is_disk_full_error(exc):
                    try:
                        self.store.set_last_scan_error(f"disk I/O error: {exc}, scan deferred")
                    except Exception:
                        log.debug("set_last_scan_error failed", exc_info=True)
                    log.error(
                        "Scan deferred (disk full), will retry in %ss", SCHEDULER_RETRY_SECONDS, exc_info=True
                    )
                    time.sleep(SCHEDULER_RETRY_SECONDS)
                    continue
                log.exception("scheduled scan failed")
            except Exception:
                log.exception("scheduled scan failed")
            try:
                settings = self.store.get_settings()
                interval = max(60, int(settings.get("interval_minutes") or 30)) * 60
            except sqlite3.OperationalError as exc:
                if _is_disk_full_error(exc):
                    log.error(
                        "Could not read settings (disk full), retrying in %ss",
                        SCHEDULER_RETRY_SECONDS,
                        exc_info=True,
                    )
                else:
                    log.exception("failed to read settings")
                time.sleep(SCHEDULER_RETRY_SECONDS)
                continue
            except Exception:
                log.exception("failed to read settings, retrying in %ss", SCHEDULER_RETRY_SECONDS)
                time.sleep(SCHEDULER_RETRY_SECONDS)
                continue
            time.sleep(interval)


@nicegui_app.add_middleware
class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated visitors to /login.

    The middleware runs on the NiceGUI app where request paths may still carry
    the mount prefix (/delugearr/...), so paths are normalised against
    root_path before the unrestricted check and before building redirect_to.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        root_path = request.scope.get("root_path", "")
        relative = path[len(root_path) :] if root_path and path.startswith(root_path) else path
        relative = relative or "/"
        if (
            nicegui_app.storage.user.get("authenticated")
            or relative in UNRESTRICTED_PATHS
            or relative.startswith("/_nicegui")
        ):
            return await call_next(request)
        if _store is not None:
            settings = _store.get_settings()
            ip = effective_client_ip(request, settings.get("trusted_proxies"))
            if should_bypass_auth(ip, settings):
                return await call_next(request)
        return RedirectResponse(f"{root_path}/login?redirect_to={relative}")


def _storage_secret(store):
    secret = os.environ.get("STORAGE_SECRET") or store.get_settings().get("storage_secret")
    if not secret:
        secret = secrets.token_hex(32)
        store.update_settings(storage_secret=secret)
    return secret


def create_app(store, scanner):
    global _store
    _store = store
    base = config.base_path()
    fastapi_app = FastAPI(
        title="Delugearr",
        docs_url=base + "/api/docs",
        openapi_url=base + "/api/openapi.json",
        redoc_url=None,
    )
    fastapi_app.include_router(build_router(store, scanner))

    ui_module.build_pages(store, scanner)

    ui.run_with(
        fastapi_app,
        mount_path=config.base_path(),
        title="Delugearr",
        favicon=DEFAULT_AVATAR,
        dark=None,
        storage_secret=_storage_secret(store),
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
    config.apply_settings(store.get_settings())
    scanner = Scanner(store=store)
    Scheduler(scanner, store).start()
    app = create_app(store, scanner)
    log.info(
        "delugearr %s listening on %s:%s (mount %s)",
        __import__("delugearr").__version__,
        config.host(),
        config.port(),
        config.base_path(),
    )
    uvicorn.run(app, host=config.host(), port=config.port(), log_level="info")
