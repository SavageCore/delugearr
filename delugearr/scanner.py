"""Scan cycle: fetch torrents, detect unregistered, (dry-run) remove."""

import logging
import threading
import time
from pathlib import Path

from . import config
from .deluge_client import DelugeClient, DelugeError
from .detector import classify_torrent
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
        run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
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
            "would_remove": 0,
            "would_remove_nodata": 0,
            "errors": 0,
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
            stats["errors"] += 1
            log.error("Deluge unreachable: %s", exc)
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

            cross_seed = any(_path_under(torrent.get("save_path"), p) for p in keep_paths)

            if dry_run:
                action = "would_remove_only" if cross_seed else "would_remove_data"
                if cross_seed:
                    stats["would_remove_nodata"] += 1
                else:
                    stats["would_remove"] += 1
                record(torrent, message, status, action, True)
                continue

            try:
                self.client.remove_torrents([torrent_hash], remove_data=not cross_seed)
            except DelugeError as exc:
                log.error("Failed removing %s (%s): %s", torrent.get("name"), torrent_hash, exc)
                stats["errors"] += 1
                record(torrent, message, status, "error", False)
                continue
            if cross_seed:
                stats["removed_nodata"] += 1
                action = "removed_only"
            else:
                stats["removed"] += 1
                action = "removed_data"
            record(torrent, message, status, action, False)

        stats["seconds"] = round(time.time() - start, 2)
        if pending:
            self.store.log_detections(pending)
        self.store.update_settings(last_scan_at=time.time(), last_scan_stats=stats, last_scan_error=None)
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
        self.store.log_detection(
            run_id=f"manual-{time.strftime('%Y%m%d-%H%M%S')}",
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
