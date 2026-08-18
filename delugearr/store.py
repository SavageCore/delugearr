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
    "deluge_url": "",
    "deluge_password": "",
    "notify_max_items": 25,
    "notify_url_base": "",
    "api_key": None,
    "storage_secret": None,
    "last_scan_at": None,
    "last_scan_stats": None,
    "last_scan_error": None,
    "data_version": 0,
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
    "notify_max_items",
    "notify_url_base",
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
    dry_run      INTEGER,
    ratio        REAL,
    seeding_time INTEGER
);
CREATE TABLE IF NOT EXISTS exempt (
    torrent_hash TEXT PRIMARY KEY,
    reason       TEXT,
    added_ts     REAL
);
CREATE TABLE IF NOT EXISTS notification_connections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    type         TEXT DEFAULT 'discord',
    webhook_url  TEXT,
    username     TEXT,
    avatar       TEXT,
    access_token TEXT,
    triggers     TEXT,
    enabled      INTEGER DEFAULT 1
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
            self._add_columns(con)
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

    def _add_columns(self, con):
        """Add columns for databases created before they existed (IF NOT EXISTS won't alter)."""
        columns = {col["name"] for col in con.execute("PRAGMA table_info(detections)").fetchall()}
        for name, definition in (
            ("ratio", "REAL"),
            ("seeding_time", "INTEGER"),
        ):
            if name not in columns:
                con.execute(f"ALTER TABLE detections ADD COLUMN {name} {definition}")
        notif_cols = {
            col["name"] for col in con.execute("PRAGMA table_info(notification_connections)").fetchall()
        }
        for name, definition in (
            ("type", "TEXT DEFAULT 'discord'"),
            ("access_token", "TEXT"),
        ):
            if name not in notif_cols:
                con.execute(f"ALTER TABLE notification_connections ADD COLUMN {name} {definition}")

    def _run(self, fn):
        with self._lock:
            con = self._connect()
            try:
                result = fn(con)
                con.commit()
                return result
            finally:
                con.close()

    def _bump(self, con):
        """Increment the data version so open dashboards notice the change."""
        row = con.execute("SELECT value FROM settings WHERE key='data_version'").fetchone()
        version = int(json.loads(row["value"])) if row else 0
        con.execute(
            "INSERT INTO settings(key,value) VALUES('data_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(version + 1),),
        )

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
            self._bump(con)

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
                "INSERT INTO detections(run_id,ts,torrent_hash,name,label,tracker,message,status,action,size,dry_run,ratio,seeding_time) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                        rec["torrent"].get("ratio", 0) or 0,
                        int(rec["torrent"].get("seeding_time", 0) or 0),
                    )
                    for rec in records
                ],
            )
            self._bump(con)

        if not records:
            return
        self._run(fn)

    def search_history(
        self,
        name="",
        label="All",
        tracker="All",
        message="All",
        sort_by=None,
        descending=False,
        page=1,
        rows_per_page=25,
    ):
        """Paged history rows for the server-side table, filtered in SQL.

        ``message`` matches the message category (the segment before the first
        ``:``), same as the UI's message dropdown.
        """

        def fn(con):
            where, params = [], []
            if name:
                where.append("name LIKE ?")
                params.append(f"%{name}%")
            if label and label != "All":
                where.append("label = ?")
                params.append(label)
            if tracker and tracker != "All":
                where.append("tracker = ?")
                params.append(tracker)
            if message and message != "All":
                where.append(
                    "CASE WHEN instr(message,':') > 0 "
                    "THEN substr(message,1,instr(message,':')-1) ELSE message END = ?"
                )
                params.append(message)
            clause = f"WHERE {' AND '.join(where)}" if where else ""
            total = con.execute(f"SELECT COUNT(*) AS n FROM detections {clause}", params).fetchone()["n"]
            order = {
                "ts": "ts",
                "action": "action",
                "name": "name",
                "label": "label",
                "tracker": "tracker",
                "message": "message",
                "size": "size",
                "dry_run": "dry_run",
            }.get(sort_by)
            sql = f"SELECT * FROM detections {clause}"
            sql += f" ORDER BY {order} {'DESC' if descending else 'ASC'}" if order else " ORDER BY ts DESC"
            sql += " LIMIT ? OFFSET ?"
            params += [rows_per_page, (page - 1) * rows_per_page]
            return [dict(r) for r in con.execute(sql, params)], total

        return self._run(fn)

    def history_facets(self, limit=200):
        """Distinct label/tracker/message-category values for the filter bar."""

        def fn(con):
            labels = [
                r["label"]
                for r in con.execute("SELECT DISTINCT label FROM detections WHERE label != '' ORDER BY label")
            ]
            trackers = [
                r["tracker"]
                for r in con.execute(
                    "SELECT DISTINCT tracker FROM detections WHERE tracker != '' ORDER BY tracker"
                )
            ]
            categories = [
                r["c"]
                for r in con.execute(
                    "SELECT DISTINCT CASE WHEN instr(message,':') > 0 "
                    "THEN substr(message,1,instr(message,':')-1) ELSE message END AS c "
                    "FROM detections WHERE message != '' ORDER BY c"
                )
            ]
            return {"labels": labels[:limit], "trackers": trackers[:limit], "categories": categories[:limit]}

        return self._run(fn)

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
        """Latest scan's detections minus torrents already handled.

        Matches what the dashboard shows: the newest (non-manual) run, with
        exempted hashes and any torrent already removed (by a scan run or by
        hand, so it lives in history) filtered out. Only still-pending
        (would_remove_*) detections remain actionable.
        """
        latest = self.latest_run()
        if not latest:
            return []
        removed = self.handled_hashes()
        exempt = self.exempt_hashes()
        return [
            det
            for det in self.get_run_detections(latest["run_id"])
            if det["torrent_hash"] not in removed and det["torrent_hash"] not in exempt
        ]

    def handled_hashes(self):
        """Hashes whose most recent action was a removal (scan or manual).

        Removed torrents are gone from Deluge and belong in history, so the
        dashboard hides them rather than re-listing them from the latest run's
        snapshot.
        """

        def fn(con):
            return {
                r["torrent_hash"]
                for r in con.execute(
                    "SELECT torrent_hash FROM detections "
                    "WHERE ts = (SELECT MAX(d2.ts) FROM detections d2 "
                    "             WHERE d2.torrent_hash = detections.torrent_hash) "
                    "AND action IN ('removed_data', 'removed_only', "
                    "                'manual_removed_data', 'manual_removed_only')"
                )
            }

        return self._run(fn)

    # ---- exempt ---------------------------------------------------------
    def add_exempt(self, torrent_hash, reason=""):
        def fn(con):
            con.execute(
                "INSERT OR REPLACE INTO exempt(torrent_hash,reason,added_ts) VALUES(?,?,?)",
                (torrent_hash, reason, time.time()),
            )
            self._bump(con)

        self._run(fn)

    def remove_exempt(self, torrent_hash):
        def fn(con):
            con.execute("DELETE FROM exempt WHERE torrent_hash=?", (torrent_hash,))
            self._bump(con)

        self._run(fn)

    def list_exempt(self):
        def fn(con):
            return [dict(r) for r in con.execute("SELECT * FROM exempt ORDER BY added_ts DESC")]

        return self._run(fn)

    def exempt_hashes(self):
        return {r["torrent_hash"] for r in self.list_exempt()}

    # ---- notification connections ----------------------------------------
    TRIGGERS = ("scan_summary", "errors", "manual_actions", "removals")

    @staticmethod
    def _connection_row(row):
        return {
            "id": row["id"],
            "name": row["name"],
            "type": row["type"] or "discord",
            "webhook_url": row["webhook_url"] or "",
            "username": row["username"] or "",
            "avatar": row["avatar"] or "",
            "access_token": row["access_token"] or "",
            "triggers": json.loads(row["triggers"]) if row["triggers"] else [],
            "enabled": bool(row["enabled"]),
        }

    def list_notifications(self):
        def fn(con):
            rows = con.execute("SELECT * FROM notification_connections ORDER BY id").fetchall()
            return [self._connection_row(r) for r in rows]

        return self._run(fn)

    def add_notification(
        self,
        name,
        webhook_url="",
        type="discord",
        username="",
        avatar="",
        access_token="",
        triggers=None,
        enabled=True,
    ):
        def fn(con):
            con.execute(
                "INSERT INTO notification_connections(name,type,webhook_url,username,avatar,access_token,triggers,enabled) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    name,
                    type or "discord",
                    webhook_url,
                    username,
                    avatar,
                    access_token,
                    json.dumps(list(triggers or [])),
                    int(bool(enabled)),
                ),
            )
            self._bump(con)
            cid = con.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            row = con.execute("SELECT * FROM notification_connections WHERE id=?", (cid,)).fetchone()
            return self._connection_row(row)

        return self._run(fn)

    def update_notification(self, cid, **fields):
        def fn(con):
            row = con.execute("SELECT * FROM notification_connections WHERE id=?", (cid,)).fetchone()
            if row is None:
                return None
            current = self._connection_row(row)
            if "name" in fields:
                current["name"] = fields["name"]
            if "type" in fields:
                current["type"] = fields["type"] or "discord"
            if "webhook_url" in fields:
                current["webhook_url"] = fields["webhook_url"]
            if "username" in fields:
                current["username"] = fields["username"]
            if "avatar" in fields:
                current["avatar"] = fields["avatar"]
            if "access_token" in fields:
                current["access_token"] = fields["access_token"]
            if "triggers" in fields:
                current["triggers"] = list(fields["triggers"] or [])
            if "enabled" in fields:
                current["enabled"] = bool(fields["enabled"])
            con.execute(
                "UPDATE notification_connections SET name=?,type=?,webhook_url=?,username=?,avatar=?,access_token=?,triggers=?,enabled=? "
                "WHERE id=?",
                (
                    current["name"],
                    current["type"],
                    current["webhook_url"],
                    current["username"],
                    current["avatar"],
                    current["access_token"],
                    json.dumps(current["triggers"]),
                    int(current["enabled"]),
                    cid,
                ),
            )
            self._bump(con)
            return current

        return self._run(fn)

    def delete_notification(self, cid):
        def fn(con):
            con.execute("DELETE FROM notification_connections WHERE id=?", (cid,))
            self._bump(con)

        self._run(fn)

    def enabled_connections(self, trigger):
        """Enabled connections subscribed to a given trigger."""
        return [c for c in self.list_notifications() if c["enabled"] and trigger in c["triggers"]]
