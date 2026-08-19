"""Notification channels (Discord webhooks and ntfy) for delugearr.

Connections are configured in the UI/API and fan out per event trigger.
``make_notifier`` dispatches to ``DiscordNotifier`` or ``NtfyNotifier`` based
on the connection's ``type``. Each may override channel-specific defaults. To
avoid spam, scans emit a single capped summary message rather than one per
torrent; the per-removal trigger is optional and still capped per scan.
"""

import logging
from datetime import UTC, datetime

import requests

log = logging.getLogger("delugearr-notifier")

COLOR_DRY = 0xF59E0B
COLOR_LIVE = 0xDC2626
COLOR_ERROR = 0xEF4444
COLOR_OK = 0x16A34A

DEFAULT_AVATAR = "https://cdn.jsdelivr.net/gh/SavageCore/delugearr@main/Logo/256.png"


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
        self.username = username or "Delugearr"
        self.avatar = avatar or DEFAULT_AVATAR

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

    def send_removal(
        self, name="", label="", tag="", tracker_url="", message="", remove_data=True, artwork_url=None
    ):
        """qbit-manage-style deletion embed."""
        fields = [
            {"name": "Contents Deleted", "value": "Yes" if remove_data else "No", "inline": True},
            {"name": "Status", "value": message or "-", "inline": True},
            {"name": "Category", "value": label or "-", "inline": True},
            {"name": "Tag", "value": tag or "-", "inline": True},
            {"name": "Tracker", "value": tracker_url or "-", "inline": True},
            {"name": "Torrents (1)", "value": f"```\n{name}\n```", "inline": False},
        ]
        embed = {
            "author": {
                "name": "Delugearr: Removing Unregistered Torrents",
                "icon_url": self.avatar,
            },
            "color": COLOR_LIVE if remove_data else COLOR_OK,
            "fields": fields,
            "footer": {"text": "Delugearr", "icon_url": self.avatar},
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if artwork_url:
            embed["image"] = {"url": artwork_url}
        payload = self._payload(embeds=[embed])
        return self._send(payload)

    # ---- scan summary ----------------------------------------------------
    def send_summary(self, stats, run_id, sample, run_url="", max_items=25):
        dry_run = bool(stats.get("dry_run", True))
        n = int(stats.get("unregistered", 0))
        color = COLOR_DRY if dry_run else COLOR_LIVE

        pending = int(stats.get("pending", 0))
        fields = [
            {
                "name": "Mode",
                "value": "DRY RUN, nothing was removed" if dry_run else "LIVE, removals executed",
                "inline": True,
            }
        ]
        fields.append({"name": "Unregistered", "value": f"{n}", "inline": True})
        if pending:
            fields.append({"name": "Pending confirmation", "value": f"{pending}", "inline": True})

        listed, more = _chunk_torrents(sample, max_items)
        if listed:
            fields.append({"name": "Torrents", "value": listed})
        if more:
            fields.append({"name": f"+{more} more", "value": f"…and {more} more"})

        fields.append({"name": "Run", "value": f"`{run_id}`"})

        embed = {
            "title": f"Scan {_discord_ts(run_id)}",
            "color": color,
            "fields": fields,
            "thumbnail": {"url": self.avatar},
            "footer": {"text": "Delugearr", "icon_url": self.avatar},
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if run_url:
            embed["url"] = run_url  # makes the whole title a rich, clickable link

        payload = self._payload(embeds=[embed])
        return self._send(payload)


def _chunk_torrents(records, max_items):
    """Return a (list_text, more_count) pair, capping listed torrents.

    Each record carries the torrent under ``record["torrent"]``; details shown
    are name, short hash, seeding time and ratio. ``max_items <= 0`` means no
    torrents are listed (summary only).
    """
    torrents = [r.get("torrent") or {} for r in records if r.get("torrent")]
    if max_items is not None and max_items <= 0:
        return "", len(torrents)
    cap = max_items if max_items else len(torrents)
    listed = torrents[:cap]
    more = len(torrents) - len(listed)
    if not listed:
        return "", more
    lines = [
        f"{i + 1}. **{t.get('name') or '(unnamed)'}** · `{short_hash(t.get('hash'))}` "
        f"· seeded {fmt_seeding(t.get('seeding_time'))} · ratio {fmt_ratio(t.get('ratio'))}"
        for i, t in enumerate(listed)
    ]
    text = "\n".join(lines)
    if len(text) > 1000:
        text = text[:1000].rstrip() + "…"
    return text, more


def short_hash(value):
    value = value or ""
    return value[:8] or "-"


def fmt_seeding(seconds):
    """Render a seeding_time (seconds) as a compact human duration."""
    if seconds is None:
        return "-"
    try:
        secs = int(float(seconds))
    except (TypeError, ValueError):
        return "-"
    if secs <= 0:
        return "0s"
    days, secs = divmod(secs, 86400)
    hours, secs = divmod(secs, 3600)
    minutes, secs = divmod(secs, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def fmt_ratio(ratio):
    """Render a ratio float with up to two decimals, skipping trailing zeros."""
    if ratio is None:
        return "-"
    try:
        value = float(ratio)
    except (TypeError, ValueError):
        return "-"
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


# ---- ntfy ------------------------------------------------------------------

# ntfy priority integers: 1=min, 2=low, 3=default, 4=high, 5=urgent.
NTFY_PRIO_DEFAULT = 3
NTFY_PRIO_HIGH = 4
NTFY_PRIO_URGENT = 5


class NtfyNotifier:
    """ntfy.sh / self-hosted push notifier.

    The connection's ``webhook_url`` is the full publish URL (topic in the
    path), e.g. ``https://ntfy.sh/mytopic``. An optional ``access_token`` is
    sent as a Bearer Authorization header when the server requires auth.
    """

    def __init__(self, topic_url, access_token=None):
        self.topic_url = topic_url
        self.access_token = access_token or ""

    def _payload(self, message, **kwargs):
        payload = {"message": message, "priority": NTFY_PRIO_DEFAULT}
        for k, v in kwargs.items():
            if v is not None and v != "":
                payload[k] = v
        return payload

    def _post(self, payload):
        # ntfy publishes the message as a plain-text body with metadata as
        # HTTP headers (Title/Priority/Tags/Click). JSON-publishing requires a
        # body with a "topic" field posted to the server root; posting a JSON
        # body to the topic path makes the client show the raw JSON instead.
        headers = {"Priority": str(payload.get("priority", NTFY_PRIO_DEFAULT))}
        if payload.get("title"):
            headers["Title"] = payload["title"]
        if payload.get("tags"):
            headers["Tags"] = ",".join(payload["tags"])
        if payload.get("click"):
            headers["Click"] = payload["click"]
        if payload.get("attach"):
            headers["Attach"] = payload["attach"]
        # Messages carry Markdown (**bold**, `code`), so ask ntfy to render it.
        headers["Content-Type"] = "text/markdown"
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        resp = requests.post(self.topic_url, data=payload["message"], headers=headers, timeout=10)
        resp.raise_for_status()
        return True

    def _send(self, payload):
        """Send and log; returns bool so callers can decide on failures."""
        try:
            self._post(payload)
            return True
        except requests.RequestException as exc:
            log.error("ntfy publish failed: %s", exc)
            return False

    # ---- individual messages --------------------------------------------
    def send_error(self, message):
        payload = self._payload(
            f"**Delugearr error**\n{message}",
            title="Delugearr error",
            priority=NTFY_PRIO_URGENT,
            tags=["warning"],
        )
        return self._send(payload)

    def send_manual(self, action, name, remove_data):
        verb = "Removed" if remove_data else "Removed torrent (kept data)"
        payload = self._payload(
            f"{verb} `{name}` ({action})",
            title=verb,
            priority=NTFY_PRIO_DEFAULT,
        )
        return self._send(payload)

    def send_test(self):
        """Send a tiny probe message; used to verify a topic before saving."""
        payload = self._payload("delugearr test notification", title="Delugearr")
        return self._send(payload)

    def send_removal(
        self, name="", label="", tag="", tracker_url="", message="", remove_data=True, artwork_url=None
    ):
        verb = "Removed + data" if remove_data else "Removed (kept data)"
        lines = [
            f"**Contents Deleted:** {'Yes' if remove_data else 'No'}",
            f"**Status:** {message or '-'}",
            f"**Category:** {label or '-'}",
            f"**Tag:** {tag or '-'}",
            f"**Tracker:** {tracker_url or '-'}",
            f"```\n{name}\n```",
        ]
        payload = self._payload(
            "\n".join(lines),
            title=verb,
            priority=NTFY_PRIO_HIGH,
            tags=["tada"] if remove_data else ["mute"],
            attach=artwork_url,
        )
        return self._send(payload)

    # ---- scan summary ----------------------------------------------------
    def send_summary(self, stats, run_id, sample, run_url="", max_items=25):
        dry_run = bool(stats.get("dry_run", True))
        n = int(stats.get("unregistered", 0))

        pending = int(stats.get("pending", 0))
        lines = [
            ("DRY RUN, nothing was removed" if dry_run else "LIVE, removals executed"),
            f"Unregistered: {n}",
        ]
        if pending:
            lines.append(f"Pending confirmation: {pending}")
        listed, more = _chunk_torrents(sample, max_items)
        if listed:
            lines.append("")
            lines.append(listed)
        if more:
            lines.append(f"+{more} more")
        lines.append(f"Run: `{run_id}`")
        if run_url:
            lines.append(f"Details: {run_url}")

        payload = self._payload(
            "\n".join(lines),
            title=f"Scan {run_id}",
            priority=NTFY_PRIO_HIGH if not dry_run else NTFY_PRIO_DEFAULT,
            tags=["mag"] if not dry_run else ["eyes"],
            click=run_url or None,
        )
        return self._send(payload)


def make_notifier(conn):
    """Return the notifier matching a connection's ``type``."""
    if conn.get("type") == "ntfy":
        return NtfyNotifier(conn.get("webhook_url", ""), conn.get("access_token"))
    return DiscordNotifier(conn.get("webhook_url", ""), conn.get("username"), conn.get("avatar"))
