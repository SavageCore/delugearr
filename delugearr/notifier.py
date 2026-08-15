"""Discord webhook notifications for delugearr.

Connections (webhooks) are configured in the UI/API and fan out per event
trigger. Each webhook may override the default bot name and avatar. To avoid
spam, scans emit a single capped summary message rather than one per torrent;
the per-removal trigger is optional and still capped per scan.
"""

import logging
from datetime import datetime

import requests

log = logging.getLogger("delugearr-notifier")

COLOR_DRY = 0xF59E0B
COLOR_LIVE = 0xDC2626
COLOR_ERROR = 0xEF4444
COLOR_OK = 0x16A34A


def _discord_ts(run_id):
    """Render a %Y%m%d-%H%M%S scan run_id as a Discord `<t:…:f>` timestamp.

    Discord renders the markup in each viewer's local timezone; the raw id is
    left untouched when it can't be parsed as a scan run id.
    """
    try:
        dt = datetime.strptime(run_id, "%Y%m%d-%H%M%S")
    except (TypeError, ValueError):
        return run_id
    return f"<t:{int(dt.timestamp())}:f>"


class DiscordNotifier:
    def __init__(self, webhook_url, username=None, avatar=None):
        self.webhook_url = webhook_url
        self.username = username or None
        self.avatar = avatar or None

    def _payload(self, **kwargs):
        payload = {k: v for k, v in kwargs.items() if v is not None}
        if self.username:
            payload["username"] = self.username
        if self.avatar:
            payload["avatar_url"] = self.avatar
        return payload

    def _post(self, payload):
        resp = requests.post(self.webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        return True

    def _send(self, payload):
        """Send and log; returns bool so callers can decide on failures."""
        try:
            self._post(payload)
            return True
        except requests.RequestException as exc:
            log.error("Discord webhook failed: %s", exc)
            return False

    # ---- individual messages --------------------------------------------
    def send_error(self, message):
        payload = self._payload(
            content=f"**Delugearr error**\n{message}",
            embeds=[{"color": COLOR_ERROR, "description": message}],
        )
        return self._send(payload)

    def send_manual(self, action, name, remove_data):
        verb = "Removed" if remove_data else "Removed torrent (kept data)"
        payload = self._payload(
            content=f"**{verb}** `{name}`",
            embeds=[{"color": COLOR_OK, "description": f"{verb} `{name}` ({action})"}],
        )
        return self._send(payload)

    def send_test(self):
        """Send a tiny probe message; used to verify a webhook before saving."""
        payload = self._payload(content="delugearr test notification")
        return self._send(payload)

    def send_removal(self, name, label="", tracker="", message="", remove_data=True):
        parts = [f"**{'Removed + data' if remove_data else 'Removed (kept data)'}** `{name}`"]
        if label:
            parts.append(f"Label: {label}")
        if tracker:
            parts.append(f"Tracker: {tracker}")
        if message:
            parts.append(f"Message: {message}")
        payload = self._payload(content="\n".join(parts))
        return self._send(payload)

    # ---- scan summary ----------------------------------------------------
    def send_summary(self, stats, run_id, sample, tracker_breakdown, max_items=25):
        dry_run = bool(stats.get("dry_run", True))
        n = int(stats.get("unregistered", 0))
        color = COLOR_DRY if dry_run else COLOR_LIVE

        fields = [
            {
                "name": "Mode",
                "value": "DRY RUN, nothing was removed" if dry_run else "LIVE, removals executed",
                "inline": True,
            }
        ]
        fields.append({"name": "Unregistered", "value": f"{n}", "inline": True})

        if tracker_breakdown:
            lines = [f"{tracker or 'unknown'}: {count}" for tracker, count in tracker_breakdown]
            fields.append({"name": "By tracker", "value": "\n".join(lines) or "-"})

        listed, more = _chunk_names([r.get("name") or "" for r in sample], max_items)
        if listed:
            fields.append({"name": "Torrents", "value": listed})
        if more:
            fields.append({"name": f"+{more} more", "value": f"…and {more} more (see the UI)"})

        fields.append({"name": "Run", "value": f"`{run_id}`"})

        payload = self._payload(
            embeds=[
                {
                    "title": f"Scan {_discord_ts(run_id)}",
                    "color": color,
                    "fields": fields,
                }
            ]
        )
        return self._send(payload)


def _chunk_names(names, max_items):
    """Return a (list_text, more_count) pair, capping listed names.

    ``max_items <= 0`` means no names are listed (summary only).
    """
    names = [n for n in names if n]
    if max_items is not None and max_items <= 0:
        return "", len(names)
    cap = max_items if max_items else len(names)
    listed = names[:cap]
    more = len(names) - len(listed)
    if not listed:
        return "", more
    text = "\n".join(f"{i + 1}. {name}" for i, name in enumerate(listed))
    if len(text) > 1000:
        text = text[:1000].rstrip() + "…"
    return text, more
