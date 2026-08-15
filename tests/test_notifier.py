"""Notifier tests: payload shape, username/avatar overrides, spam cap, error swallowing."""

from datetime import datetime

import pytest

from delugearr import notifier
from delugearr.notifier import DiscordNotifier


@pytest.fixture
def post(monkeypatch):
    calls = []

    class FakeResp:
        def raise_for_status(self):
            return None

    def fake_post(url, json=None, timeout=10):
        calls.append({"url": url, "json": json})
        return FakeResp()

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    return calls


def test_summary_omits_overrides_when_unset(post):
    n = DiscordNotifier("https://discord/hook")
    stats = {
        "dry_run": True,
        "total": 426,
        "unregistered": 426,
        "transient": 0,
    }
    sample = [{"torrent": {"name": "A.Release.1", "hash": "abcd1234", "seeding_time": 3600, "ratio": 1.5}}]
    ok = n.send_summary(stats, "20260101-000000", sample, max_items=25)
    assert ok
    body = post[0]["json"]
    assert "username" not in body
    assert "avatar_url" not in body
    embed = body["embeds"][0]
    assert "<t:" in embed["title"]
    assert embed["color"] == notifier.COLOR_DRY
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Run"] == "`20260101-000000`"
    assert "By tracker" not in fields
    assert "A.Release.1" in fields["Torrents"]


def test_summary_title_uses_discord_timestamp(post):
    n = DiscordNotifier("https://discord/hook")
    run_id = "20260815-010539"
    n.send_summary({"dry_run": True}, run_id, [], [])
    title = post[0]["json"]["embeds"][0]["title"]
    epoch = int(datetime.strptime(run_id, "%Y%m%d-%H%M%S").timestamp())
    assert f"<t:{epoch}:f>" in title
    fields = {f["name"]: f["value"] for f in post[0]["json"]["embeds"][0]["fields"]}
    assert fields["Run"] == f"`{run_id}`"


def test_summary_title_falls_back_to_raw_run_id(post):
    n = DiscordNotifier("https://discord/hook")
    n.send_summary({"dry_run": True}, "aB3_xY9_zQw", [], [])
    title = post[0]["json"]["embeds"][0]["title"]
    assert title.startswith("Scan aB3_xY9_zQw")
    assert "<t:" not in title


def test_summary_sends_username_and_avatar(post):
    n = DiscordNotifier("https://discord/hook", username="My Bot", avatar="https://img/a.png")
    n.send_error("boom")
    body = post[0]["json"]
    assert body["username"] == "My Bot"
    assert body["avatar_url"] == "https://img/a.png"


def test_summary_caps_names(post):
    n = DiscordNotifier("https://discord/hook")
    sample = [
        {"torrent": {"name": f"Release.{i}", "hash": f"hash{i:02d}", "seeding_time": i * 3600, "ratio": 1.0}}
        for i in range(40)
    ]
    n.send_summary({"dry_run": True}, "run1", sample, max_items=10)
    fields = {f["name"]: f["value"] for f in post[0]["json"]["embeds"][0]["fields"]}
    assert "1. **Release.0**" in fields["Torrents"]
    assert "`hash00`" in fields["Torrents"]
    assert "seeded 0s" in fields["Torrents"]
    assert "ratio 1" in fields["Torrents"]
    assert "30 more" in fields["+30 more"]


def test_summary_zero_cap_lists_none(post):
    n = DiscordNotifier("https://discord/hook")
    n.send_summary({"dry_run": True}, "run1", [{"torrent": {"name": "X"}}], max_items=0)
    fields = {f["name"] for f in post[0]["json"]["embeds"][0]["fields"]}
    assert "Torrents" not in fields
    assert "+1 more" in fields


def test_summary_renders_run_link_when_url(post):
    n = DiscordNotifier("https://discord/hook")
    n.send_summary({"dry_run": True}, "20260101-000000", [], run_url="https://x.example/run/20260101-000000")
    fields = {f["name"]: f["value"] for f in post[0]["json"]["embeds"][0]["fields"]}
    assert fields["Run"] == "`20260101-000000`\n[Open full run](https://x.example/run/20260101-000000)"


def test_fmt_seeding():
    assert notifier.fmt_seeding(0) == "0s"
    assert notifier.fmt_seeding(30) == "30s"
    assert notifier.fmt_seeding(3600) == "1h 0m"
    assert notifier.fmt_seeding(90061) == "1d 1h 1m"
    assert notifier.fmt_seeding(None) == "-"


def test_fmt_ratio():
    assert notifier.fmt_ratio(1.0) == "1"
    assert notifier.fmt_ratio(1.45) == "1.45"
    assert notifier.fmt_ratio(0) == "0"
    assert notifier.fmt_ratio(None) == "-"


def test_webhook_exception_is_swallowed(post, monkeypatch):
    def boom(url, json=None, timeout=10):
        raise notifier.requests.ConnectionError("down")

    monkeypatch.setattr(notifier.requests, "post", boom)
    n = DiscordNotifier("https://discord/hook")
    assert n.send_error("x") is False


def test_send_test_probe(post):
    n = DiscordNotifier("https://discord/hook", username="Bot")
    assert n.send_test() is True
    body = post[0]["json"]
    assert body["username"] == "Bot"
    assert body["content"] == "delugearr test notification"
