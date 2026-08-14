"""Environment / file configuration helpers.

All settings can be overridden through environment variables or a systemd
EnvironmentFile (see config.example). The app's runtime settings that the UI
exposes (dry_run, schedule, exclusions) live in the SQLite store and are seeded
from the defaults defined here.
"""

import json
import os
from pathlib import Path


def env(key, default=None):
    return os.environ.get(key, default)


def _bool(v, default=False):
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _json_list(v, default):
    if v is None:
        return default
    try:
        parsed = json.loads(v)
    except ValueError:
        return [v]
    if isinstance(parsed, list):
        return [str(x) for x in parsed]
    return [str(parsed)]


def config_path():
    return Path(env("CONFIG_PATH", "/etc/delugearr"))


def db_path():
    return config_path() / "app.db"


def log_path():
    return config_path() / "app.log"


def deluge_url():
    return env("DELUGE_URL", "http://127.0.0.1:10376")


def deluge_password():
    return env("DELUGE_PASSWORD", "")


def base_path():
    return env("BASE_PATH", "/delugearr").rstrip("/")


def port():
    return _int(env("PORT", "11012"), 11012)


def default_dry_run():
    return _bool(env("DRY_RUN", "1"), True)


def default_interval_minutes():
    return _int(env("SCAN_INTERVAL_MINUTES", "30"), 30)


def default_keep_data_paths():
    return _json_list(env("KEEP_DATA_PATHS", None), [])


def store_defaults():
    """Seed values used when the store is first initialised."""
    return {
        "dry_run": default_dry_run(),
        "interval_minutes": default_interval_minutes(),
        "keep_data_paths": default_keep_data_paths(),
    }
