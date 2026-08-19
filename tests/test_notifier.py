"""Notifier tests: payload shape, username/avatar overrides, spam cap, error swallowing."""

from datetime import datetime

import pytest

from delugearr import notifier
from delugearr.notifier import DiscordNotifier, NtfyNotifier, make_notifier


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
    assert body["username"] == "Delugearr"
    assert body["avatar_url"] == notifier.DEFAULT_AVATAR
    embed = body["embeds"][0]
    assert "<t:" in embed["title"]
    assert embed["color"] == notifier.COLOR_DRY
    assert embed["thumbnail"]["url"] == notifier.DEFAULT_AVATAR
    assert embed["footer"]["text"] == "Delugearr"
    assert embed["footer"]["icon_url"] == notifier.DEFAULT_AVATAR
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
    embed = post[0]["json"]["embeds"][0]
    assert embed["url"] == "https://x.example/run/20260101-000000"
    assert "T" in embed["timestamp"]  # ISO-8601
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Run"] == "`20260101-000000`"
    assert "Open full run" not in fields["Run"]


def test_summary_omits_url_when_unset(post):
    n = DiscordNotifier("https://discord/hook")
    n.send_summary({"dry_run": True}, "20260101-000000", [])
    embed = post[0]["json"]["embeds"][0]
    assert "url" not in embed


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


def test_discord_removal_embed(post):
    n = DiscordNotifier("https://discord/hook")
    ok = n.send_removal(
        "My.Adventures.with.Superman.S03.1080p.AMZN.WEB-DL",
        label="cross-seed-link",
        tag="tracker.beyond-hd.me",
        tracker_url="https://tracker.beyond-hd.me:2053",
        message="Dupe: https://beyond-hd.me/torrents/123",
        remove_data=True,
        artwork_url="https://artworks.thetvdb.com/banners/xx.jpg",
    )
    assert ok
    embed = post[0]["json"]["embeds"][0]
    assert embed["author"]["name"] == "Delugearr: Removing Unregistered Torrents"
    assert embed["author"]["icon_url"] == notifier.DEFAULT_AVATAR
    assert embed["image"]["url"] == "https://artworks.thetvdb.com/banners/xx.jpg"
    assert embed["color"] == notifier.COLOR_LIVE
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Contents Deleted"] == "Yes"
    assert fields["Status"] == "Dupe: https://beyond-hd.me/torrents/123"
    assert fields["Category"] == "cross-seed-link"
    assert fields["Tag"] == "tracker.beyond-hd.me"
    assert fields["Tracker"] == "https://tracker.beyond-hd.me:2053"
    assert "My.Adventures.with.Superman" in fields["Torrents (1)"]
    assert "```" in fields["Torrents (1)"]
    assert "T" in embed["timestamp"]


def test_discord_removal_kept_data_omits_image(post):
    n = DiscordNotifier("https://discord/hook")
    n.send_removal("X", remove_data=False)
    embed = post[0]["json"]["embeds"][0]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Contents Deleted"] == "No"
    assert embed["color"] == notifier.COLOR_OK
    assert "image" not in embed


# ---- ntfy -----------------------------------------------------------------


@pytest.fixture
def ntfy_post(monkeypatch):
    calls = []

    class FakeResp:
        def raise_for_status(self):
            return None

    def fake_post(url, data=None, headers=None, timeout=10):
        calls.append({"url": url, "data": data, "headers": headers or {}})
        return FakeResp()

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    return calls


def test_ntfy_summary_payload(ntfy_post):
    n = NtfyNotifier("https://ntfy.sh/delugearr")
    stats = {"dry_run": True, "unregistered": 3}
    sample = [{"torrent": {"name": "A.Release.1", "hash": "abcd1234", "seeding_time": 3600, "ratio": 1.5}}]
    assert n.send_summary(stats, "20260815-010539", sample, max_items=25) is True
    req = ntfy_post[0]
    assert req["url"] == "https://ntfy.sh/delugearr"
    # Body is plain-text Markdown (never a JSON payload) so ntfy renders it.
    assert req["data"].startswith("**") or req["data"].startswith("DRY")
    assert not req["data"].lstrip().startswith("{")
    # All metadata travels as headers, not a JSON body.
    assert "json" not in req
    assert req["headers"]["Title"] == "Scan 20260815-010539"
    assert req["headers"]["Priority"] == str(notifier.NTFY_PRIO_DEFAULT)  # dry run
    assert "A.Release.1" in req["data"]
    assert "Unregistered: 3" in req["data"]
    assert "DRY RUN" in req["data"]


def test_ntfy_summary_live_raises_priority(ntfy_post):
    n = NtfyNotifier("https://ntfy.sh/delugearr")
    n.send_summary({"dry_run": False, "unregistered": 1}, "run1", [], max_items=0)
    assert ntfy_post[0]["headers"]["Priority"] == str(notifier.NTFY_PRIO_HIGH)


def test_ntfy_summary_click_url(ntfy_post):
    n = NtfyNotifier("https://ntfy.sh/delugearr")
    n.send_summary({"dry_run": True}, "run1", [], run_url="https://x/run/1", max_items=0)
    req = ntfy_post[0]
    assert req["headers"]["Click"] == "https://x/run/1"
    assert "Details: https://x/run/1" in req["data"]


def test_ntfy_sends_bearer_token(ntfy_post):
    n = NtfyNotifier("https://ntfy.example.com/delugearr", access_token="tk_secret")
    n.send_test()
    req = ntfy_post[0]
    assert req["headers"]["Authorization"] == "Bearer tk_secret"
    assert req["data"] == "delugearr test notification"


def test_ntfy_error_uses_urgent_and_warning_tag(ntfy_post):
    n = NtfyNotifier("https://ntfy.sh/delugearr")
    n.send_error("boom")
    req = ntfy_post[0]
    assert req["headers"]["Priority"] == str(notifier.NTFY_PRIO_URGENT)
    assert req["headers"]["Tags"] == "warning"
    assert "boom" in req["data"]


def test_ntfy_removal_tag_depends_on_data(ntfy_post):
    n = NtfyNotifier("https://ntfy.sh/delugearr")
    n.send_removal("X", remove_data=True)
    req = ntfy_post[0]
    assert req["headers"]["Tags"] == "tada"
    assert "**Contents Deleted:** Yes" in req["data"]
    assert "Category" in req["data"]
    n.send_removal("Y", remove_data=False)
    assert ntfy_post[1]["headers"]["Tags"] == "mute"


def test_ntfy_removal_attaches_artwork(ntfy_post):
    n = NtfyNotifier("https://ntfy.sh/delugearr")
    n.send_removal("X", remove_data=True, artwork_url="https://artworks.thetvdb.com/banners/xx.jpg")
    req = ntfy_post[0]
    assert req["headers"]["Attach"] == "https://artworks.thetvdb.com/banners/xx.jpg"
    # No artwork -> attach header is omitted.
    n.send_removal("Y", remove_data=True)
    assert "Attach" not in ntfy_post[1]["headers"]


def test_ntfy_exception_is_swallowed(ntfy_post, monkeypatch):
    def boom(url, data=None, headers=None, timeout=10):
        raise notifier.requests.ConnectionError("down")

    monkeypatch.setattr(notifier.requests, "post", boom)
    n = NtfyNotifier("https://ntfy.sh/delugearr")
    assert n.send_test() is False


def test_make_notifier_dispatches_by_type():
    assert isinstance(
        make_notifier({"type": "ntfy", "webhook_url": "https://ntfy.sh/x", "access_token": "t"}), NtfyNotifier
    )
    assert isinstance(
        make_notifier({"type": "discord", "webhook_url": "https://discord/hook"}), DiscordNotifier
    )
    # missing type defaults to discord (back-compat)
    assert isinstance(make_notifier({"webhook_url": "https://discord/hook"}), DiscordNotifier)
