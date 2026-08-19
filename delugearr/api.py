"""Delugearr REST API, protected by an API key.

Endpoints mirror the arr convention: every request needs the key via the
``X-Api-Key`` header or the ``apikey`` query parameter, except the liveness
probe and the OpenAPI docs/spec. The spec is served under the base path so the
MCP server can fetch it: GET {BASE_PATH}/api/openapi.json (docs UI at
{BASE_PATH}/api/docs).
"""

import threading

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from . import config
from .deluge_client import DelugeError
from .notifier import make_notifier

REDACTED_KEYS = {
    "deluge_password",
    "api_key",
    "storage_secret",
    "webhook_url",
    "access_token",
    "tvdb_api_key",
}


def _redact(conn):
    if conn is None:
        return None
    out = dict(conn)
    out["webhook_url"] = "***" if out.get("webhook_url") else ""
    out["access_token"] = "***" if out.get("access_token") else ""
    return out


def _verify_webhook_or_400(webhook_url, type="discord", username="", avatar="", access_token=""):
    """Reject saving a connection whose endpoint cannot be reached."""
    if not webhook_url:
        raise HTTPException(status_code=400, detail="webhook URL required")
    conn = {
        "type": type or "discord",
        "webhook_url": webhook_url,
        "username": username,
        "avatar": avatar,
        "access_token": access_token,
    }
    try:
        ok = make_notifier(conn).send_test()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"webhook verification failed: {exc}") from exc
    if not ok:
        raise HTTPException(status_code=400, detail="webhook verification failed")


class Notification(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    type: str = "discord"
    webhook_url: str = ""
    username: str = ""
    avatar: str = ""
    access_token: str = ""
    tags: list[str] = []
    triggers: list[str] = []
    enabled: bool = True


class NotificationUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    type: str | None = None
    webhook_url: str | None = None
    username: str | None = None
    avatar: str | None = None
    access_token: str | None = None
    tags: list[str] | None = None
    triggers: list[str] | None = None
    enabled: bool | None = None


class ScanRequest(BaseModel):
    dry_run: bool | None = None
    run_id: str | None = None


class RemoveRequest(BaseModel):
    remove_data: bool = True


class ExemptRequest(BaseModel):
    reason: str = ""


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dry_run: bool | None = None
    interval_minutes: int | None = None
    filter_completed: bool | None = None
    grace_minutes: int | None = None
    rem_unregistered_confirm_minutes: int | None = None
    max_torrents_per_tracker: int | None = None
    excluded_labels: list[str] | None = None
    keep_data_paths: list[str] | None = None
    extra_ignore: list[str] | None = None
    deluge_url: str | None = None
    deluge_password: str | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    base_path: str | None = None
    notify_max_items: int | None = None
    notify_url_base: str | None = None
    tvdb_api_key: str | None = None
    notify_artwork: bool | None = None
    auth_bypass_enabled: bool | None = None
    trusted_networks: list[str] | None = None
    trusted_proxies: list[str] | None = None


def build_router(store, scanner) -> APIRouter:
    router = APIRouter(prefix=config.base_path())

    def require_api_key(
        x_api_key: str = Header(default="", alias="X-Api-Key"),
        apikey: str = Query(default=""),
    ) -> None:
        key = store.get_settings().get("api_key")
        if key and (x_api_key == key or apikey == key):
            return
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    keyed = {"dependencies": [Depends(require_api_key)]}

    @router.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @router.get("/api/status", **keyed)
    def status() -> dict:
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

    @router.post("/api/scan", **keyed)
    def scan(body: ScanRequest | None = None) -> dict:
        if scanner.scanning:
            return {"started": False, "reason": "already scanning"}
        body = body or ScanRequest()

        def run():
            try:
                scanner.scan(dry_run=body.dry_run, run_id=body.run_id)
            except Exception:  # keep the background scan from killing the app
                import logging

                logging.getLogger("delugearr-api").exception("api scan failed")

        threading.Thread(target=run, daemon=True).start()
        return {"started": True}

    @router.get("/api/detections", **keyed)
    def detections(
        run_id: str | None = None,
        limit: int = Query(500, ge=1, le=5000),
        message: str | None = None,
        label: str | None = None,
        tracker: str | None = None,
        action: str | None = None,
    ) -> dict:
        rows = store.current_detections()
        if run_id:
            rows = store.get_run_detections(run_id)
        if message:
            rows = [r for r in rows if message.lower() in (r.get("message") or "").lower()]
        if label:
            rows = [r for r in rows if r.get("label") == label]
        if tracker:
            rows = [r for r in rows if r.get("tracker") == tracker]
        if action:
            rows = [r for r in rows if r.get("action") == action]
        return {"run_id": run_id or (store.latest_run() or {}).get("run_id"), "rows": rows[:limit]}

    @router.get("/api/history", **keyed)
    def history(
        limit: int = Query(100, ge=1, le=5000),
        action: str | None = None,
        name: str | None = None,
    ) -> dict:
        return {"rows": store.get_detections(limit, action=action, name=name)}

    @router.post("/api/torrents/{torrent_hash}/remove", **keyed)
    def remove(torrent_hash: str, body: RemoveRequest | None = None) -> dict:
        try:
            return scanner.manual_remove(torrent_hash, remove_data=(body or RemoveRequest()).remove_data)
        except DelugeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/torrents/{torrent_hash}/exempt", **keyed)
    def exempt(torrent_hash: str, body: ExemptRequest | None = None) -> dict:
        reason = (body or ExemptRequest()).reason or "exempted via API"
        store.add_exempt(torrent_hash, reason)
        return {"torrent_hash": torrent_hash, "reason": reason}

    @router.get("/api/exempt", **keyed)
    def list_exempt() -> dict:
        return {"rows": store.list_exempt()}

    @router.delete("/api/exempt/{torrent_hash}", **keyed)
    def unexempt(torrent_hash: str) -> dict:
        store.remove_exempt(torrent_hash)
        return {"torrent_hash": torrent_hash}

    @router.get("/api/settings", **keyed)
    def settings_get() -> dict:
        return {k: v for k, v in store.get_settings().items() if k not in REDACTED_KEYS}

    @router.put("/api/settings", **keyed)
    def settings_put(body: SettingsUpdate) -> dict:
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        if "host" in updates:
            host = (updates["host"] or "").strip()
            if not host:
                raise HTTPException(status_code=400, detail="host cannot be empty")
            updates["host"] = "0.0.0.0" if host == "*" else host
        if "base_path" in updates:
            base = (updates["base_path"] or "").strip()
            if base and not base.startswith("/"):
                raise HTTPException(status_code=400, detail="base_path must start with '/' or be empty")
            updates["base_path"] = base.rstrip("/") or "/"
        store.update_settings(**updates)
        return settings_get()

    @router.get("/api/notifications", **keyed)
    def notifications_get() -> dict:
        return {"rows": [_redact(c) for c in store.list_notifications()]}

    @router.post("/api/notifications", **keyed)
    def notifications_post(body: Notification) -> dict:
        data = body.model_dump()
        _verify_webhook_or_400(
            data["webhook_url"],
            data.get("type") or "discord",
            data.get("username") or "",
            data.get("avatar") or "",
            data.get("access_token") or "",
        )
        return _redact(store.add_notification(**data))

    @router.put("/api/notifications/{cid}", **keyed)
    def notifications_put(cid: int, body: NotificationUpdate) -> dict:
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        if "webhook_url" in updates:
            _verify_webhook_or_400(
                updates["webhook_url"],
                updates.get("type") or "discord",
                updates.get("username") or "",
                updates.get("avatar") or "",
                updates.get("access_token") or "",
            )
        row = store.update_notification(cid, **updates)
        if row is None:
            raise HTTPException(status_code=404, detail="connection not found")
        return _redact(row)

    @router.delete("/api/notifications/{cid}", **keyed)
    def notifications_delete(cid: int) -> dict:
        store.delete_notification(cid)
        return {"id": cid}

    @router.post("/api/notifications/{cid}/test", **keyed)
    def notifications_test(cid: int) -> dict:
        conn = next((c for c in store.list_notifications() if c["id"] == cid), None)
        if conn is None:
            raise HTTPException(status_code=404, detail="connection not found")
        if not conn.get("webhook_url"):
            raise HTTPException(status_code=400, detail="webhook URL not set")
        try:
            ok = make_notifier(conn).send_test()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=502, detail="webhook send failed")
        return {"sent": True}

    return router
