"""Scan cycle: fetch torrents, detect unregistered, (dry-run) remove."""

import logging
import secrets
import threading
import time
from pathlib import Path

from . import config
from .deluge_client import DelugeClient, DelugeError
from .detector import classify_torrent
from .notifier import DiscordNotifier
from .store import Store

log = logging.getLogger("scanner")

# States where a torrent is still actively downloading/processing. These are
# skipped when filter_completed is enabled so we never yank an in-progress
# download on a (possibly transient) tracker error.
ACTIVE_STATES = {
    "Allocating",
    "Checking",
    "Downloading",
    "Fetching",
    "Moving",
    "Metadata",
    "Queued",
    "Endgame",
}

# nanoid-style alphabet (64 URL-safe chars), same size as a default nanoid.
_NANOID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz-"


def _nanoid(size=21):
    return "".join(secrets.choice(_NANOID_ALPHABET) for _ in range(size))


def _path_under(path, parent):
    if not path or not parent:
        return False
    try:
        p = str(Path(path).expanduser().resolve())
    except OSError:
        p = str(path)
    try:
        pr = str(Path(parent).expanduser().resolve())
    except OSError:
        pr = str(parent)
    return p == pr or p.startswith(pr + "/")


class Scanner:
    def __init__(self, client=None, store=None):
        self.store = store or Store(config.db_path(), defaults=config.store_defaults())
        self._client_owned = client is None
        self._cfg_url = None
        self._cfg_password = None
        self.client = client or DelugeClient(config.deluge_url(), config.deluge_password())
        if self._client_owned:
            self._sync_client()
        self._lock = threading.Lock()
        self.scanning = False
        self.deluge_ok = None
        self.last_error = None

    def _sync_client(self):
        """Recreate the Deluge client when the stored connection settings changed."""
        if not self._client_owned:
            return
        settings = self.store.get_settings()
        url = (settings.get("deluge_url") or config.deluge_url()).strip() or config.deluge_url()
        password = settings.get("deluge_password") or ""
        if url != self._cfg_url or password != self._cfg_password:
            self.client = DelugeClient(url, password)
            self._cfg_url = url
            self._cfg_password = password

    def _fan_out(self, trigger, build):
        """Send a notification to every enabled connection subscribed to a trigger.

        Each send runs on its own daemon thread so a slow/unreachable webhook
        never blocks the scan; build(notifier) raises are caught and logged.
        """
        for conn in self.store.enabled_connections(trigger):
            if not conn.get("webhook_url"):
                continue
            notifier = DiscordNotifier(conn["webhook_url"], conn.get("username"), conn.get("avatar"))

            def run(n=notifier, b=build, c=conn):
                try:
                    b(n)
                except Exception:
                    log.exception("discord notification failed (%s)", c.get("name"))

            threading.Thread(target=run, daemon=True).start()

    def _notify_summary(self, run_id, pending, stats):
        if not pending:
            return
        settings = self.store.get_settings()
        max_items = int(settings.get("notify_max_items", 25) or 25)
        base = (settings.get("notify_url_base") or "").rstrip("/")
        run_url = f"{base}/run/{run_id}" if base else ""

        def build(n):
            n.send_summary(stats, run_id, pending, run_url=run_url, max_items=max_items)

        self._fan_out("scan_summary", build)

    def scan(self, dry_run=None, run_id=None):
        with self._lock:
            self.scanning = True
            try:
                return self._scan_locked(dry_run=dry_run, run_id=run_id)
            finally:
                self.scanning = False

    def _scan_locked(self, dry_run=None, run_id=None):
        self._sync_client()
        start = time.time()
        settings = self.store.get_settings()
        if dry_run is None:
            dry_run = bool(settings["dry_run"])
        run_id = run_id or _nanoid()
        stats = {
            "total": 0,
            "unregistered": 0,
            "transient": 0,
            "exempt": 0,
            "skipped_completed": 0,
            "skipped_grace": 0,
            "skipped_limit": 0,
            "removed": 0,
            "removed_nodata": 0,
            "dry_run": bool(dry_run),
            "seconds": 0.0,
        }
        try:
            torrents = self.client.get_torrents()
            self.deluge_ok = True
            self.last_error = None
        except DelugeError as exc:
            self.deluge_ok = False
            self.last_error = str(exc)
            log.error("Deluge unreachable: %s", exc)
            self._fan_out("errors", lambda n, exc=exc: n.send_error(str(exc)))
            self.store.update_settings(
                last_scan_at=time.time(), last_scan_stats=stats, last_scan_error=str(exc)
            )
            return stats

        exempt = self.store.exempt_hashes()
        excluded = {label.lower() for label in settings.get("excluded_labels") or []}
        keep_paths = settings.get("keep_data_paths") or []
        extra_ignore = settings.get("extra_ignore") or []
        filter_completed = bool(settings.get("filter_completed", True))
        grace_min = int(settings.get("grace_minutes", 0) or 0)
        max_per_tracker = int(settings.get("max_torrents_per_tracker", 0) or 0)
        now = time.time()
        tracker_count = {}
        pending = []

        def record(torrent, message, status, action, dry_run):
            pending.append(
                {
                    "run_id": run_id,
                    "torrent": torrent,
                    "message": message,
                    "status": status,
                    "action": action,
                    "dry_run": dry_run,
                }
            )

        for torrent_hash, torrent in torrents.items():
            if not isinstance(torrent, dict):
                continue
            stats["total"] += 1
            label = torrent.get("label") or ""
            if label.lower() in excluded:
                continue
            if torrent_hash in exempt:
                stats["exempt"] += 1
                continue

            status, message, tracker_host = classify_torrent(torrent, extra_ignore=extra_ignore)
            if status == "ok":
                continue
            if status == "transient":
                stats["transient"] += 1
                continue

            stats["unregistered"] += 1

            if filter_completed and torrent.get("state") in ACTIVE_STATES:
                stats["skipped_completed"] += 1
                continue

            if grace_min > 0:
                added = torrent.get("time_added")
                if isinstance(added, (int, float)) and added > 0:
                    age_min = (now - added) / 60.0
                    if age_min < grace_min:
                        stats["skipped_grace"] += 1
                        continue

            if max_per_tracker > 0:
                host_key = tracker_host or "unknown"
                if tracker_count.get(host_key, 0) >= max_per_tracker:
                    stats["skipped_limit"] += 1
                    continue
                tracker_count[host_key] = tracker_count.get(host_key, 0) + 1

            keep_data = any(_path_under(torrent.get("save_path"), p) for p in keep_paths)

            if dry_run:
                action = "would_remove_only" if keep_data else "would_remove_data"
                record(torrent, message, status, action, True)
                continue

            try:
                self.client.remove_torrents([torrent_hash], remove_data=not keep_data)
            except DelugeError as exc:
                log.error("Failed removing %s (%s): %s", torrent.get("name"), torrent_hash, exc)
                record(torrent, message, status, "error", False)
                continue
            if keep_data:
                stats["removed_nodata"] += 1
                action = "removed_only"
            else:
                stats["removed"] += 1
                action = "removed_data"
            record(torrent, message, status, action, False)
            self._fan_out(
                "removals",
                lambda n, t=torrent, m=message, kd=keep_data: n.send_removal(
                    t.get("name", ""),
                    t.get("label", ""),
                    t.get("tracker_host", ""),
                    m,
                    remove_data=not kd,
                ),
            )

        stats["seconds"] = round(time.time() - start, 2)
        if pending:
            self.store.log_detections(pending)
        self.store.update_settings(last_scan_at=time.time(), last_scan_stats=stats, last_scan_error=None)
        self._notify_summary(run_id, pending, stats)
        log.info("Scan %s: %s", run_id, {k: v for k, v in stats.items() if k != "dry_run"})
        return stats

    def manual_remove(self, torrent_hash, remove_data=True):
        """Explicit user-initiated removal of a single torrent."""
        self._sync_client()
        torrents = self.client.get_torrents()
        torrent = torrents.get(torrent_hash)
        if not isinstance(torrent, dict):
            raise DelugeError(f"torrent {torrent_hash} not found in Deluge")
        self.client.remove_torrents([torrent_hash], remove_data=remove_data)
        action = "manual_removed_data" if remove_data else "manual_removed_only"
        self._fan_out("manual_actions", lambda n: n.send_manual(action, torrent.get("name", ""), remove_data))
        self._fan_out(
            "removals",
            lambda n: n.send_removal(
                torrent.get("name", ""),
                torrent.get("label", ""),
                torrent.get("tracker_host", ""),
                torrent.get("tracker_status", ""),
                remove_data=remove_data,
            ),
        )
        self.store.log_detection(
            run_id=f"manual-{_nanoid()}",
            torrent=torrent,
            message=torrent.get("tracker_status") or "manual removal",
            status="unregistered",
            action=action,
            dry_run=False,
        )
        return {
            "torrent_hash": torrent_hash,
            "name": torrent.get("name", ""),
            "action": action,
            "remove_data": remove_data,
        }
