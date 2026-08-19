"""TVDB v4 artwork lookup for deletion notifications.

qbit-manage delegates artwork fetching to Notifiarr's backend; delugearr has no
such proxy, so we implement the lookup directly against the TVDB v4 API. A
torrent name (e.g. ``My.Adventures.with.Superman.S03.1080p.AMZN.WEB-DL.
DDP5.1.H.264-NTb``) is reduced to a searchable series title, the top series
match is found, and its best wide "banner" artwork URL is returned. Results are
cached in the SQLite store keyed by normalised title so a cleanup doesn't
re-hit the API for every removal.
"""

import logging
import re
import time
from urllib.parse import quote

import requests

log = logging.getLogger("delugearr-artwork")

API_BASE = "https://api4.thetvdb.com/v4"
CACHE_TTL = 30 * 86400  # 30 days

_SEASON_TOKEN_RE = re.compile(r"^(?:s\d{1,2}(?:e\d{1,4})?|\d{4}|season)$", re.IGNORECASE)

# Tokens that typically trail a release and are never part of a title.
_TECH_TOKENS = {
    "remux",
    "bluray",
    "blu-ray",
    "hddvd",
    "webrip",
    "web",
    "web-dl",
    "bdrip",
    "dvdrip",
    "hdtv",
    "pdtv",
    "hdrip",
    "repack",
    "proper",
    "extended",
    "unrated",
    "imax",
    "directors",
    "internal",
}

_RES_RE = re.compile(r"^\d{3,4}p$", re.IGNORECASE)
_EPISODE_RE = re.compile(r"^(?:e|ep)\d{1,4}$", re.IGNORECASE)


def series_title(name):
    """Reduce a torrent name to a searchable series/movie title."""
    text = re.sub(r"[._\-]+", " ", (name or "").strip())
    tokens = text.split()

    # Cut at the first season/episode/year/collector token.
    cutoff = len(tokens)
    for i, tok in enumerate(tokens):
        low = tok.lower()
        if _SEASON_TOKEN_RE.match(low):
            cutoff = i
            break
        if _EPISODE_RE.match(low) or _RES_RE.match(low):
            cutoff = i
            break
    tokens = tokens[:cutoff]

    keep = []
    for tok in tokens:
        low = tok.lower()
        # Drop trailing codepoints (h264, x265, ddp5.1, aac, etc.) and tech words.
        if re.match(r"^[hv]26[45]$|^(?:dd|ddp|ac3|aac|mp3|flac|dts|dts-hd|truehd)[0-9.]*$", low):
            continue
        if low in _TECH_TOKENS:
            continue
        if re.match(r"^\(?\d{4}\)?$", low):
            continue
        keep.append(tok)

    title = " ".join(keep).strip().rstrip(".'\"()-")
    # A bare year was left behind (or an empty parse) - fall back to the raw name.
    if not title or re.fullmatch(r"\d{4}", title):
        title = " ".join(tokens).strip()
    return title or (name or "").strip()


class TvdbArtwork:
    """Minimal TVDB v4 client (search + banner artwork) with store-backed cache."""

    def __init__(self, api_key, store=None):
        self.api_key = (api_key or "").strip()
        self.store = store
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._token = None
        self._token_expires = 0.0

    def _login(self):
        if self._token and time.time() < self._token_expires:
            return self._token
        if not self.api_key:
            return None
        try:
            resp = self.session.post(f"{API_BASE}/login", json={"apikey": self.api_key}, timeout=15)
            resp.raise_for_status()
            data = resp.json().get("data") or {}
            token = data.get("token")
            expires = data.get("expires", "")
            self._token = token
            self._token_expires = _parse_expiry(expires, fallback=3600)
            if token:
                self.session.headers.update({"Authorization": f"Bearer {token}"})
            return token
        except Exception as exc:
            log.warning("TVDB login failed: %s", exc)
            return None

    def search(self, title):
        if not self._login():
            return None
        query = quote(title)
        try:
            resp = self.session.get(f"{API_BASE}/search?query={query}&type=series", timeout=15)
            resp.raise_for_status()
            data = resp.json().get("data") or []
            for item in data:
                if isinstance(item, dict) and item.get("id"):
                    return _numeric_id(item["id"])
            return None
        except Exception as exc:
            log.warning("TVDB search failed: %s", exc)
            return None

    def banner(self, series_id):
        if not self._login():
            return None
        try:
            resp = self.session.get(f"{API_BASE}/series/{series_id}/artworks?type=1", timeout=15)
            resp.raise_for_status()
            artworks = (resp.json().get("data") or {}).get("artworks") or []
            best = None
            best_score = -1
            for art in artworks:
                if not isinstance(art, dict) or not art.get("image"):
                    continue
                score = int(art.get("score") or 0)
                if score >= best_score:
                    best = art["image"]
                    best_score = score
            return best
        except Exception as exc:
            log.warning("TVDB banner lookup failed: %s", exc)
            return None

    def get_banner(self, name):
        """Return the best banner URL for a torrent name, or None (cached)."""
        title = series_title(name)
        if not title or not self.api_key:
            return None
        cache = self.store.get_artwork_cache(title) if self.store else None
        if cache:
            ts, url = cache
            if time.time() - ts < CACHE_TTL:
                return url or None
        series_id = self.search(title)
        url = self.banner(series_id) if series_id else None
        if self.store:
            self.store.set_artwork_cache(title, url or "", series_id or "")
        if url:
            log.debug("Artwork for %r: %s", title, url)
        return url


def _parse_expiry(iso, fallback=3600):
    if not iso:
        return time.time() + fallback
    try:
        from datetime import UTC, datetime

        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(UTC).timestamp()
    except (TypeError, ValueError):
        return time.time() + fallback


def _numeric_id(value):
    """Reduce a v4 id like ``series-403172`` to the numeric ``403172`` used by
    the series artworks endpoint; pass through a bare int untouched."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int("".join(ch for ch in value if ch.isdigit()) or "0") or None
    return None
