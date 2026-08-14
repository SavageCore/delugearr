"""delugearr bootstrap: NiceGUI UI mounted at /delugearr, ops API, scheduler.

The web layer uses NiceGUI (Vue/Quasar) and is served behind nginx on a base
path (no subdomain). The FastAPI app owns the NiceGUI mount and a small JSON
API for ops/health checks. All NiceGUI pages are protected by an auth
middleware + login page (nginx basic auth stays as defense in depth).
"""

import logging
import os
import secrets
import threading
import time
from logging.handlers import RotatingFileHandler

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from nicegui import app as nicegui_app
from nicegui import ui
from starlette.middleware.base import BaseHTTPMiddleware

from . import config
from . import ui as ui_module
from .api import build_router
from .scanner import Scanner
from .store import Store

log = logging.getLogger("delugearr")

UNRESTRICTED_PATHS = {"", "/login", "/favicon.ico"}


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
        return RedirectResponse(f"{root_path}/login?redirect_to={relative}")


def _storage_secret(store):
    secret = os.environ.get("STORAGE_SECRET") or store.get_settings().get("storage_secret")
    if not secret:
        secret = secrets.token_hex(32)
        store.update_settings(storage_secret=secret)
    return secret


def create_app(store, scanner):
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
