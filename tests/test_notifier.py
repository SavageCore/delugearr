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
        "errors": 0,
        "would_remove": 400,
        "would_remove_nodata": 26,
    }
    ok = n.send_summary(stats, "20260101-000000", [{"name": "A.Release.1"}], [("trk", 426)], max_items=25)
    assert ok
    body = post[0]["json"]
    assert "username" not in body
    assert "avatar_url" not in body
    embed = body["embeds"][0]
    assert "426 unregistered" in embed["title"]
    assert "<t:" in embed["title"]
    assert embed["color"] == notifier.COLOR_DRY
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Run"] == "`20260101-000000`"


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
    n.send_summary({"dry_run": True}, "manual-foo", [], [])
    title = post[0]["json"]["embeds"][0]["title"]
    assert title.startswith("Scan manual-foo")
    assert "<t:" not in title


def test_summary_sends_username_and_avatar(post):
    n = DiscordNotifier("https://discord/hook", username="My Bot", avatar="https://img/a.png")
    n.send_error("boom")
    body = post[0]["json"]
    assert body["username"] == "My Bot"
    assert body["avatar_url"] == "https://img/a.png"


def test_summary_caps_names(post):
    n = DiscordNotifier("https://discord/hook")
    sample = [{"name": f"Release.{i}"} for i in range(40)]
    n.send_summary({"dry_run": True}, "run1", sample, [], max_items=10)
    fields = {f["name"]: f["value"] for f in post[0]["json"]["embeds"][0]["fields"]}
    assert "1. Release.0" in fields["Torrents"]
    assert "30 more" in fields["+30 more"]


def test_summary_zero_cap_lists_none(post):
    n = DiscordNotifier("https://discord/hook")
    n.send_summary({"dry_run": True}, "run1", [{"name": "X"}], [], max_items=0)
    fields = {f["name"] for f in post[0]["json"]["embeds"][0]["fields"]}
    assert "Torrents" not in fields
    assert "+1 more" in fields


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
