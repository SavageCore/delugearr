"""Artwork tests: title parsing, TVDB client flows, and store-backed caching."""

from delugearr.artwork import TvdbArtwork, series_title


def test_series_title_strips_release_tags():
    assert (
        series_title("My.Adventures.with.Superman.S03.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb")
        == "My Adventures with Superman"
    )


def test_series_title_strips_episode_season():
    assert series_title("Show.Name.S01E02.720p.WEBRip.x264") == "Show Name"


def test_series_title_strips_year_for_movie():
    assert series_title("Dune.Part.One.2021.1080p.BluRay.x264-GROUP") == "Dune Part One"


def test_series_title_handles_underscores_and_dashes():
    assert series_title("A_B-C.Series.2020") == "A B C Series"


def test_series_title_empty_fallback():
    assert series_title("") == ""
    assert series_title("2046") == "2046"


def test_numeric_id_strips_type_prefix():
    from delugearr.artwork import _numeric_id

    assert _numeric_id("series-403172") == 403172
    assert _numeric_id("movie-12") == 12
    assert _numeric_id(403172) == 403172
    assert _numeric_id("") is None


# ---- TvdbArtwork client robots -----------------------------------------


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.headers = {}

    def post(self, url, json=None, timeout=0):
        resp = self.responses.pop(0)
        return _FakeResp(resp)

    def get(self, url, timeout=0):
        resp = self.responses.pop(0)
        return _FakeResp(resp)


class _FakeResp:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self.ok = ok
        self.status_code = 200

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("http error")

    def json(self):
        return self._payload


def _client_with(responses, store=None):
    client = TvdbArtwork("k", store=store)
    client.session = FakeSession(responses)
    return client


def test_login_then_search_then_banner():
    client = _client_with(
        [
            {"data": {"token": "abc123", "expires": "2099-01-01T00:00:00Z"}},
            {"data": [{"id": 403172, "type": "series"}]},
            {
                "data": {
                    "artworks": [
                        {
                            "id": 1,
                            "type": 1,
                            "score": 2,
                            "image": "https://artworks.thetvdb.com/banners/lo.jpg",
                        },
                        {
                            "id": 2,
                            "type": 1,
                            "score": 9,
                            "image": "https://artworks.thetvdb.com/banners/hi.jpg",
                        },
                    ]
                }
            },
        ]
    )
    client.get_banner("My.Adventures.with.Superman.S03.1080p")
    assert client.session.headers["Authorization"] == "Bearer abc123"


def test_banner_picks_highest_score():
    client = _client_with(
        [
            {"data": {"token": "t", "expires": ""}},
            {"data": [{"id": "series-1"}]},
            {
                "data": {
                    "artworks": [
                        {"type": 1, "score": 1, "image": "one"},
                        {"type": 1, "score": 7, "image": "best"},
                        {"type": 1, "score": 3, "image": "mid"},
                    ]
                }
            },
        ]
    )
    assert client.get_banner("Some.Show.S01") == "best"


def test_banner_returns_none_when_no_artwork():
    client = _client_with(
        [
            {"data": {"token": "t", "expires": ""}},
            {"data": [{"id": 1}]},
            {"data": {"artworks": []}},
        ]
    )
    assert client.get_banner("Some.Show.S01") is None


def test_no_api_key_returns_none():
    client = _client_with([])
    client.api_key = ""
    assert client.get_banner("Some.Show.S01") is None


def test_login_failure_returns_none():
    client = _client_with([])

    def boom(url, json=None, timeout=0):
        raise RuntimeError("down")

    client.session.post = boom
    assert client.get_banner("Some.Show.S01") is None


def test_cache_avoids_repeated_network(tmp_path):
    from delugearr.store import Store

    store = Store(tmp_path / "app.db")
    calls = []
    client = _client_with(
        [
            {"data": {"token": "t", "expires": ""}},
            {"data": [{"id": 1}]},
            {"data": {"artworks": [{"type": 1, "score": 5, "image": "art"}]}},
            # no more responses - cache must short-circuit
            {"data": {"token": "t2", "expires": ""}},
        ],
        store=store,
    )
    real_search = client.search

    def tracking(*a, **k):
        calls.append(1)
        return real_search(*a, **k)

    client.search = tracking
    assert client.get_banner("Show.S01") == "art"
    assert client.get_banner("Show.S01") == "art"
    assert len(calls) == 1


def test_negative_result_cached(tmp_path):
    from delugearr.store import Store

    store = Store(tmp_path / "app.db")
    client = _client_with([], store=store)
    client.search = lambda title: None
    assert client.get_banner("Ghost.Show.S01") is None
    assert client.get_banner("Ghost.Show.S01") is None
    ts, url = store.get_artwork_cache("Ghost Show")
    assert url == ""
