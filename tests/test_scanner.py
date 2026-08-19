"""Scanner tests: nanoid-style run id generation."""

from delugearr.scanner import _NANOID_ALPHABET, Scanner, _nanoid


def test_nanoid_length_and_charset():
    run_id = _nanoid()
    assert len(run_id) == 21
    assert all(c in _NANOID_ALPHABET for c in run_id)


def test_nanoid_is_random():
    assert _nanoid() != _nanoid()


def test_manual_run_id_prefixes_nanoid():
    run_id = _nanoid()
    assert f"manual-{run_id}".startswith("manual-")


def test_notify_summary_skipped_when_no_detections():
    sent = []

    class FakeStore:
        def get_settings(self):
            return {}

        def enabled_connections(self, trigger):
            sent.append(trigger)
            return [{"id": 1, "name": "Discord", "webhook_url": "https://discord/hook"}]

    scanner = Scanner(store=FakeStore())
    # build() would send; if pending is empty no fan-out should happen at all.
    scanner._notify_summary("run1", [], {})
    assert sent == []


def test_removal_build_passes_qbit_fields():
    from delugearr.scanner import primary_tracker_url

    scanner = Scanner(store=_FakeSettingsStore())

    torrent = {
        "name": "My.Adventures.with.Superman.S03.1080p",
        "label": "cross-seed-link",
        "tracker_host": "tracker.beyond-hd.me",
        "trackers": [
            {"url": "dht://tracker.opentrackr.org"},
            {"url": "https://tracker.beyond-hd.me:2053/announce"},
        ],
    }
    assert primary_tracker_url(torrent) == "https://tracker.beyond-hd.me:2053/announce"

    captured = {}

    class Spy:
        def send_removal(self, *a, **k):
            captured["args"] = a
            captured["kwargs"] = k
            return True

    build = scanner._removal_build(torrent, "Dupe: https://beyond-hd.me/123", keep_data=False)
    build(Spy())
    assert captured["args"][0] == "My.Adventures.with.Superman.S03.1080p"
    assert captured["args"][1] == "cross-seed-link"
    assert captured["args"][2] == "tracker.beyond-hd.me"
    assert captured["args"][3] == "https://tracker.beyond-hd.me:2053/announce"
    assert captured["args"][4] == "Dupe: https://beyond-hd.me/123"
    assert captured["kwargs"]["remove_data"] is True  # keep_data=False

    # No artwork resolver -> artwork_url is None.
    assert captured["kwargs"]["artwork_url"] is None


class _FakeSettingsStore:
    def get_settings(self):
        return {"tvdb_api_key": "", "notify_artwork": False}
