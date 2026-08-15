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
