"""SQLite persistence: settings, detection/audit history, exempt list."""

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

DEFAULTS = {
    "dry_run": True,
    "interval_minutes": 30,
    "filter_completed": True,
    "grace_minutes": 0,
    "max_torrents_per_tracker": 0,
    "excluded_labels": [],
    "keep_data_paths": [],
    "extra_ignore": [],
    "deluge_url": "http://127.0.0.1:10376",
    "deluge_password": "",
    "api_key": None,
    "storage_secret": None,
    "last_scan_at": None,
    "last_scan_stats": None,
    "last_scan_error": None,
}

EDITABLE_KEYS = {
    "dry_run",
    "interval_minutes",
    "filter_completed",
    "grace_minutes",
    "max_torrents_per_tracker",
    "excluded_labels",
    "keep_data_paths",
    "extra_ignore",
    "deluge_url",
    "deluge_password",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS detections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT,
    ts           REAL,
    torrent_hash TEXT,
    name         TEXT,
    label        TEXT,
    tracker      TEXT,
    message      TEXT,
    status       TEXT,
    action       TEXT,
    size         INTEGER,
    dry_run      INTEGER
);
CREATE TABLE IF NOT EXISTS exempt (
    torrent_hash TEXT PRIMARY KEY,
    reason       TEXT,
    added_ts     REAL
);
CREATE INDEX IF NOT EXISTS idx_detections_ts   ON detections(ts);
CREATE INDEX IF NOT EXISTS idx_detections_run  ON detections(run_id);
CREATE INDEX IF NOT EXISTS idx_detections_hash ON detections(torrent_hash);
"""


class Store:
    def __init__(self, path, defaults=None):
        self.path = str(path)
        self._defaults = dict(DEFAULTS)
        if defaults:
            self._defaults.update(defaults)
        self._lock = threading.Lock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)
            for key, value in self._defaults.items():
                if con.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone() is None:
                    con.execute("INSERT INTO settings(key,value) VALUES(?,?)", (key, json.dumps(value)))
            row = con.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()
            if row is None or not row["value"] or row["value"] == json.dumps(None):
                con.execute(
                    "INSERT INTO settings(key,value) VALUES('api_key',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(secrets.token_hex(32)),),
                )

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def _run(self, fn):
        with self._lock:
            con = self._connect()
            try:
                result = fn(con)
                con.commit()
                return result
            finally:
                con.close()

    # ---- settings -------------------------------------------------------
    def get_settings(self):
        def fn(con):
            rows = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM settings")}
            out = dict(self._defaults)
            for key, raw in rows.items():
                try:
                    out[key] = json.loads(raw)
                except ValueError:
                    out[key] = raw
            return out

        return self._run(fn)

    def update_settings(self, **kwargs):
        def fn(con):
            for key, value in kwargs.items():
                if key not in DEFAULTS:
                    continue
                con.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value)),
                )

        self._run(fn)

    def api_key(self):
        return self.get_settings().get("api_key")

    def regenerate_api_key(self):
        def fn(con):
            key = secrets.token_hex(32)
            con.execute("UPDATE settings SET value=? WHERE key='api_key'", (json.dumps(key),))
            return key

        return self._run(fn)

    # ---- detections -----------------------------------------------------
    def log_detection(self, run_id, torrent, message, status, action, dry_run):
        return self.log_detections(
            [
                {
                    "run_id": run_id,
                    "torrent": torrent,
                    "message": message,
                    "status": status,
                    "action": action,
                    "dry_run": dry_run,
                }
            ]
        )

    def log_detections(self, records):
        """Insert many detection records in a single transaction (fast)."""

        def fn(con):
            con.executemany(
                "INSERT INTO detections(run_id,ts,torrent_hash,name,label,tracker,message,status,action,size,dry_run) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        rec["run_id"],
                        time.time(),
                        rec["torrent"].get("hash", ""),
                        rec["torrent"].get("name", ""),
                        rec["torrent"].get("label", "") or "",
                        rec["torrent"].get("tracker_host", "") or "",
                        rec["message"],
                        rec["status"],
                        rec["action"],
                        int(rec["torrent"].get("total_size", 0) or 0),
                        int(bool(rec["dry_run"])),
                    )
                    for rec in records
                ],
            )

        if not records:
            return
        self._run(fn)

    def get_detections(self, limit=500, action=None, name=None):
        def fn(con):
            where = []
            params = []
            if action:
                where.append("action = ?")
                params.append(action)
            if name:
                where.append("name LIKE ?")
                params.append(f"%{name}%")
            clause = f"WHERE {' AND '.join(where)}" if where else ""
            params.append(limit)
            return [
                dict(r)
                for r in con.execute(
                    f"SELECT * FROM detections {clause} ORDER BY ts DESC LIMIT ?",
                    params,
                )
            ]

        return self._run(fn)

    def get_run_detections(self, run_id):
        def fn(con):
            return [
                dict(r)
                for r in con.execute("SELECT * FROM detections WHERE run_id=? ORDER BY ts ASC", (run_id,))
            ]

        return self._run(fn)

    def latest_run(self):
        def fn(con):
            row = con.execute(
                "SELECT run_id, MAX(ts) AS ts, COUNT(*) AS n "
                "FROM detections WHERE run_id NOT LIKE 'manual%' "
                "GROUP BY run_id ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

        return self._run(fn)

    def manual_removed_hashes(self):
        """Hashes removed by hand (manual_* actions) since any scan run.

        The dashboard hides these so a torrent the user removed does not linger
        in the latest scan's snapshot; the audit rows still live in history.
        """

        def fn(con):
            return {
                r["torrent_hash"]
                for r in con.execute(
                    "SELECT DISTINCT torrent_hash FROM detections WHERE action LIKE 'manual%'"
                )
            }

        return self._run(fn)

    def current_detections(self):
        """Latest scan's detections minus torrents already handled by hand.

        Matches what the dashboard shows: the newest (non-manual) run, with
        manually-removed and exempted hashes filtered out.
        """
        latest = self.latest_run()
        if not latest:
            return []
        removed = self.manual_removed_hashes()
        exempt = self.exempt_hashes()
        return [
            det
            for det in self.get_run_detections(latest["run_id"])
            if det["torrent_hash"] not in removed and det["torrent_hash"] not in exempt
        ]

    # ---- exempt ---------------------------------------------------------
    def add_exempt(self, torrent_hash, reason=""):
        def fn(con):
            con.execute(
                "INSERT OR REPLACE INTO exempt(torrent_hash,reason,added_ts) VALUES(?,?,?)",
                (torrent_hash, reason, time.time()),
            )

        self._run(fn)

    def remove_exempt(self, torrent_hash):
        def fn(con):
            con.execute("DELETE FROM exempt WHERE torrent_hash=?", (torrent_hash,))

        self._run(fn)

    def list_exempt(self):
        def fn(con):
            return [dict(r) for r in con.execute("SELECT * FROM exempt ORDER BY added_ts DESC")]

        return self._run(fn)

    def exempt_hashes(self):
        return {r["torrent_hash"] for r in self.list_exempt()}
