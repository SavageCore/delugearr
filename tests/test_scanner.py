"""Scanner tests: nanoid-style run id generation."""

from delugearr.scanner import _NANOID_ALPHABET, _nanoid


def test_nanoid_length_and_charset():
    run_id = _nanoid()
    assert len(run_id) == 21
    assert all(c in _NANOID_ALPHABET for c in run_id)


def test_nanoid_is_random():
    assert _nanoid() != _nanoid()


def test_manual_run_id_prefixes_nanoid():
    run_id = _nanoid()
    assert f"manual-{run_id}".startswith("manual-")
