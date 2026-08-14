"""Unit tests for unregistered-torrent detection."""

import pytest

from delugearr.detector import classify_torrent
from delugearr.ui import message_category


def torrent(tracker_status=None, tracker_host="tracker.example.org", trackers=None):
    return {
        "hash": "abc123",
        "name": "Some.Release.2026.1080p",
        "tracker_status": tracker_status or "",
        "tracker_host": tracker_host,
        "trackers": trackers or [],
    }


@pytest.mark.parametrize(
    "message",
    [
        "Error: Unregistered torrent",
        "Error: unregistered torrent",
        "Error: Torrent has been deleted.",
        "Error: torrent not registered with this tracker",
        "Error: InfoHash not found.",
        "Error: Unknown torrent",
        "Error: Unregistered torrent: Dupe: Dupe - https://passthepopcorn.me/torrents.php",
    ],
)
def test_unregistered_messages(message):
    status, _, _ = classify_torrent(torrent(tracker_status=message))
    assert status == "unregistered"


@pytest.mark.parametrize(
    "message",
    [
        "Error: Nuked: wrong episode, grab https://beyond-hd.me/torrents/x",
        "Error: Dupe: https://beyond-hd.me/torrents/y",
        "Error: Trumped: internal available",
        "Error: Complete Season Uploaded: https://beyond-hd.me/torrents/z",
    ],
)
def test_bhd_messages_unregistered(message):
    status, _, _ = classify_torrent(torrent(tracker_status=message, tracker_host="beyond-hd.me"))
    assert status == "unregistered"


@pytest.mark.parametrize(
    "message",
    [
        "Error: Nuked: wrong episode",
        "Error: Dupe: something",
    ],
)
def test_bhd_messages_ignored_on_non_bhd_tracker(message):
    status, _, _ = classify_torrent(torrent(tracker_status=message))
    assert status != "unregistered"


@pytest.mark.parametrize(
    "message",
    [
        "Error: timed out",
        "Error: Host not found (non-authoritative), try again later",
        "Error: Bad Gateway",
        "Error: stream truncated",
        "Error: expected value (list, dict, int or string) in bencoded string",
        "Error: Internal Server Error",
    ],
)
def test_transient_messages_never_unregistered(message):
    status, _, _ = classify_torrent(torrent(tracker_status=message))
    assert status == "transient"


def test_passkey_guard_beats_unregistered():
    status, _, _ = classify_torrent(torrent(tracker_status="Error: Unregistered torrent: passkey rejected"))
    assert status == "transient"


def test_healthy_messages_ok():
    for message in ("", "Announce OK", "Error: "):
        status, _, _ = classify_torrent(torrent(tracker_status=message))
        assert status == "ok"


def test_extra_ignore():
    status, _, _ = classify_torrent(
        torrent(tracker_status="Error: Unregistered torrent"), extra_ignore=["UNREGISTERED"]
    )
    assert status == "transient"


def test_tracker_message_field_used():
    status, _, _ = classify_torrent(
        torrent(
            tracker_host="example.org",
            trackers=[{"url": "https://example.org/announce", "message": "Unregistered torrent"}],
        )
    )
    assert status == "unregistered"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("UNREGISTERED TORRENT", "UNREGISTERED TORRENT"),
        ("COMPLETE SEASON UPLOADED: https://beyond-hd.me/torrents/x", "COMPLETE SEASON UPLOADED"),
        ("NUKED: wrong episode", "NUKED"),
        ("TRUMPED: internal available", "TRUMPED"),
        ("", ""),
        (None, ""),
    ],
)
def test_message_category(message, expected):
    assert message_category(message) == expected
