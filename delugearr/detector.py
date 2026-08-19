"""Unregistered-torrent detection, ported from qbit_manage.

qbit_manage's `rem_unregistered` reads each qBittorrent torrent's tracker error
message and removes the torrent when the tracker reports it as no longer
registered. Deluge exposes the same information through the per-torrent
`tracker_status` string and each tracker's `message` field, so the matching
logic ports over (see modules/util.py `TorrentMessages` and
modules/core/remove_unregistered.py upstream).

Two upstream safeguards carry over too:

* `tracker_state()` mirrors qbit_manage's guard that a torrent is only ever
  removed once every real tracker has reported a terminal state - a WORKING
  tracker, or one still UPDATING / NOT_CONTACTED, blocks removal.
* The unregistered confirmation dwell timer is handled by the scanner and the
  store; this module only classifies messages.
"""

import re

_PREFIX_RE = re.compile(r"^error\s*:\s*", re.IGNORECASE)

# Per-tracker state enum Deluge reports in the numeric ``trackers[].status``
# field (libtorrent tracker_status). Kept in one place so the mapping can be
# corrected for a given Deluge/libtorrent build without touching the scanner.
TRACKER_NOT_CONTACTED = 0
TRACKER_WORKING = 1
TRACKER_UPDATING = 2
TRACKER_NOT_WORKING = 3

# Schemes that count as real announce trackers (vs DHT/PeX/LSD pseudo-entries).
_REAL_SCHEMES = ("http", "https", "udp", "ws", "wss")


def real_trackers(torrent):
    """Return the real announce trackers of a torrent dict.

    Deluge's ``trackers`` list leads with DHT/PeX/LSD entries that have no
    host; they provide no signal for unregistered detection and must be
    excluded, mirroring qbit_manage's real-tracker filter.
    """
    out = []
    for tr in torrent.get("trackers") or []:
        if not isinstance(tr, dict):
            continue
        url = (tr.get("url") or "").lower()
        if url and url.split(":", 1)[0] in _REAL_SCHEMES:
            out.append(tr)
    return out


def tracker_state(torrent):
    """Guard-state for a torrent from its per-tracker status.

    Returns a dict::

        working       - any real tracker reporting WORKING
        inconclusive  - any real tracker still UPDATING / NOT_CONTACTED
        status_known  - True when numeric per-tracker status was present, so
                        the caller can trust working/inconclusive. When Deluge
                        exposes no status field the guard is skipped and the
                        message-only classification is authoritative instead.

    A torrent is never removed on incomplete tracker state, so a "dead"
    conclusion requires every real tracker to have reported a terminal state.
    """
    working = False
    inconclusive = False
    status_known = False
    for tr in real_trackers(torrent):
        raw = tr.get("status")
        if raw is None:
            continue
        try:
            status = int(raw)
        except (TypeError, ValueError):
            continue
        status_known = True
        if status == TRACKER_WORKING:
            working = True
        elif status in (TRACKER_UPDATING, TRACKER_NOT_CONTACTED):
            inconclusive = True
    return {"working": working, "inconclusive": inconclusive, "status_known": status_known}


class TorrentMessages:
    """Tracker messages treated as "unregistered". Mirrors qbit_manage."""

    UNREGISTERED_MSGS = [
        "UNREGISTERED",
        "TORRENT NOT FOUND",
        "TORRENT IS NOT FOUND",
        "NOT REGISTERED",
        "NOT EXIST",
        "UNKNOWN TORRENT",
        "TRUMP",
        "RETITLED",
        "TRUNCATED",
        "TORRENT IS NOT AUTHORIZED FOR USE ON THIS TRACKER",
        "INFOHASH NOT FOUND.",
        "TORRENT HAS BEEN DELETED.",
        "TRACKER NICHT REGISTRIERT.",
        "TORRENT EXISTIERT NICHT",
        "TORRENT NICHT GEFUNDEN",
        "TORRENT DELETED",
        "TORRENT BANNED",
    ]

    # BeyondHD reports its deletion reasons ("Trumped: ...", "Dupe: ...",
    # "Complete Season Uploaded: ...", "Nuked", ...) as tracker messages.
    # qbit_manage only treats these as unregistered for BHD trackers.
    UNREGISTERED_MSGS_BHD = [
        "DEAD",
        "DUPE",
        "COMPLETE SEASON UPLOADED",
        "COMPLETE SEASON UPLOADED:",
        "PROBLEM WITH DESCRIPTION",
        "PROBLEM WITH FILE",
        "PROBLEM WITH PACK",
        "SPECIFICALLY BANNED",
        "TRUMPED",
        "OTHER",
        "TORRENT HAS BEEN DELETED",
        "NUKED",
        "SEASON PACK",
        "SEASON PACK OUT",
        "SEASON PACK UPLOADED",
    ]

    # Never remove on these, even if a substring matches UNREGISTERED_MSGS.
    IGNORE_MSGS = [
        "YOU HAVE REACHED THE CLIENT LIMIT FOR THIS TORRENT",
        "PASSKEY",
        "MISSING INFO_HASH",
        "EXPECTED VALUE (LIST, DICT, INT OR STRING) IN BENCODED STRING",
        "COULD NOT PARSE BENCODED DATA",
        "STREAM TRUNCATED",
        "GATEWAY TIMEOUT",
        "ANNOUNCE IS CURRENTLY UNAVAILABLE",
        "TORRENT HAS BEEN POSTPONED",
        "520 (UNKNOWN HTTP ERROR)",
    ]

    # Tracker-down conditions. Also never remove on these.
    EXCEPTIONS_MSGS = [
        "DOWN",
        "DOWN.",
        "IT MAY BE DOWN,",
        "UNREACHABLE",
        "(UNREACHABLE)",
        "BAD GATEWAY",
        "TRACKER UNAVAILABLE",
    ]

    # Additional transient messages observed from Deluge trackers that must
    # never trigger removal.
    TRANSIENT_MSGS = [
        "TIMED OUT",
        "TIME OUT",
        "HOST NOT FOUND",
        "NAME RESOLUTION",
        "RESOLUTION FAILED",
        "INTERNAL SERVER ERROR",
        "CONNECTION RESET",
        "CONNECTION REFUSED",
        "UNABLE TO CONNECT",
        "REQUEST TIMEOUT",
    ]

    HEALTHY_MSGS = ("OK", "ANNOUNCE OK", "ANNOUNCE SENT")

    BHD_HINTS = ("beyond-hd",)


def _normalize(message):
    """Uppercase and strip the leading 'Error: ' prefix Deluge prepends."""
    return _PREFIX_RE.sub("", (message or "").strip()).upper()


def list_in_text(text, search_list, match_all=False):
    """qbit_manage's list_in_text: word or phrase containment check.

    Single-word entries must appear as a whole word; multi-word entries must
    appear as a substring.
    """
    if not text:
        return False
    if isinstance(search_list, list):
        search_list = set(search_list)
    contains = {x for x in search_list if " " in x}
    exception = search_list - contains
    words = text.split(" ")
    if match_all:
        if all(x in words for x in exception) and all(x in text for x in contains):
            return True
    else:
        if any(x in exception for x in words) or any(x in text for x in contains):
            return True
    return False


def is_bhd_tracker(torrent):
    tracker_host = (torrent.get("tracker_host") or "").lower()
    if any(h in tracker_host for h in TorrentMessages.BHD_HINTS):
        return True
    for tr in torrent.get("trackers") or []:
        url = (tr.get("url") or "").lower()
        if any(h in url for h in TorrentMessages.BHD_HINTS):
            return True
    return False


def classify_torrent(torrent, extra_ignore=None):
    """Classify a Deluge torrent dict.

    Returns (status, message, tracker_host) where status is one of:
        "ok"           - tracker is fine / no relevant signal
        "transient"    - tracker error that is NOT unregistered (skip)
        "unregistered" - tracker reports the torrent is no longer registered
    """
    msgs = []
    ts = (torrent.get("tracker_status") or "").strip()
    if ts:
        msgs.append(ts)
    for tr in torrent.get("trackers") or []:
        if not isinstance(tr, dict):
            continue
        m = (tr.get("message") or "").strip()
        if m:
            msgs.append(m)

    combined = " ".join(_normalize(m) for m in msgs)
    tracker_host = torrent.get("tracker_host") or ""

    if not combined:
        return "ok", "", tracker_host
    if combined in TorrentMessages.HEALTHY_MSGS or combined.startswith("ANNOUNCE OK"):
        return "ok", combined, tracker_host

    ignore_all = (
        TorrentMessages.IGNORE_MSGS
        + TorrentMessages.EXCEPTIONS_MSGS
        + TorrentMessages.TRANSIENT_MSGS
        + list(extra_ignore or [])
    )
    if list_in_text(combined, ignore_all):
        return "transient", combined, tracker_host

    if list_in_text(combined, TorrentMessages.UNREGISTERED_MSGS):
        return "unregistered", combined, tracker_host

    if is_bhd_tracker(torrent):
        status_filtered = combined.split(":")[0]
        if list_in_text(status_filtered, TorrentMessages.UNREGISTERED_MSGS_BHD):
            return "unregistered", combined, tracker_host

    return "ok", combined, tracker_host
