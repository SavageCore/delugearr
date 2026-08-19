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
    return env("DELUGE_URL", "")


def deluge_password():
    return env("DELUGE_PASSWORD", "")


def auth_user():
    return env("AUTH_USER", "admin")


def auth_password():
    return env("AUTH_PASSWORD", "")


def storage_secret():
    return env("STORAGE_SECRET", "")


def base_path():
    return env("BASE_PATH", "").rstrip("/")


def host():
    return env("HOST", "127.0.0.1")


def port():
    return _int(env("PORT", "11012"), 11012)


def default_dry_run():
    return _bool(env("DRY_RUN", "1"), True)


def default_interval_minutes():
    return _int(env("SCAN_INTERVAL_MINUTES", "30"), 30)


def default_keep_data_paths():
    return _json_list(env("KEEP_DATA_PATHS", None), [])


def default_notify_max_items():
    return _int(env("NOTIFY_MAX_ITEMS", "25"), 25)


def notify_url_base():
    return env("NOTIFY_URL_BASE", "")


def tvdb_api_key():
    return env("TVDB_API_KEY", "")


def default_notify_artwork():
    return _bool(env("NOTIFY_ARTWORK", "1"), True)


def auth_bypass_enabled():
    return _bool(env("AUTH_BYPASS_ENABLED", None), False)


def trusted_networks():
    return _json_list(env("TRUSTED_NETWORKS", None), ["127.0.0.1/32", "::1/128"])


def trusted_proxies():
    return _json_list(env("TRUSTED_PROXIES", None), ["127.0.0.1/32", "::1/128"])


def store_defaults():
    """Seed values used when the store is first initialised."""
    return {
        "dry_run": default_dry_run(),
        "interval_minutes": default_interval_minutes(),
        "keep_data_paths": default_keep_data_paths(),
        "deluge_url": deluge_url(),
        "deluge_password": deluge_password(),
        "notify_max_items": default_notify_max_items(),
        "notify_url_base": notify_url_base(),
        "tvdb_api_key": tvdb_api_key(),
        "notify_artwork": default_notify_artwork(),
        "auth_bypass_enabled": auth_bypass_enabled(),
        "trusted_networks": trusted_networks(),
        "trusted_proxies": trusted_proxies(),
    }
