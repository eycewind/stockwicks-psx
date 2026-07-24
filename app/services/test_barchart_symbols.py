from app.services import barchart_symbols as rankings


class _FakeCookies:
    def get(self, _name):
        return None


class _FakeResponse:
    def __init__(self, payload=None):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.cookies = _FakeCookies()
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == rankings.BARCHART_TOP_100_API:
            return _FakeResponse({"data": self.rows})
        return _FakeResponse()


def _ranking_rows(rank_field):
    return [
        {
            "symbol": f"T{rank:03}",
            "symbolName": f"Test {rank}",
            "weightedAlpha": str(101 - rank),
            rank_field: str(rank),
            "previousRank": str(rank),
        }
        for rank in range(100, 0, -1)
    ]


def test_bullish_ranking_uses_top_feed_and_rank(monkeypatch):
    session = _FakeSession(_ranking_rows("currentRankUsTop100"))
    monkeypatch.setattr(rankings.requests, "Session", lambda: session)
    rankings._CACHE["bullish"] = {"rows": [], "expires_at": 0.0}

    rows = rankings.barchart_ranked_rows("bullish", force_refresh=True)

    assert len(rows) == 100
    assert rows[0]["currentRankUsTop100"] == "1"
    assert session.calls[1][1]["params"]["list"] == "stocks.us.weighted_alpha.advances"
    assert session.calls[1][1]["params"]["orderDir"] == "desc"


def test_bearish_ranking_uses_bottom_feed_and_rank(monkeypatch):
    session = _FakeSession(_ranking_rows("currentRankUsBottom100"))
    monkeypatch.setattr(rankings.requests, "Session", lambda: session)
    rankings._CACHE["bearish"] = {"rows": [], "expires_at": 0.0}

    rows = rankings.barchart_ranked_rows("bearish", force_refresh=True)

    assert len(rows) == 100
    assert rows[0]["currentRankUsBottom100"] == "1"
    assert session.calls[1][1]["params"]["list"] == "stocks.us.weighted_alpha.declines"
    assert session.calls[1][1]["params"]["orderDir"] == "asc"


def test_weekly_universe_uses_200_price_volume_leaders(monkeypatch):
    raw_rows = [
        {
            "symbol": f"PV{rank:03}",
            "symbolName": f"Price Volume {rank}",
            "lastPrice": str(250 - rank),
            "volume": str(1_000_000 + rank),
            "priceVolume": str(300_000_000 - rank),
            "symbolType": 1,
        }
        for rank in range(200)
    ]
    session = _FakeSession(raw_rows)
    monkeypatch.setattr(rankings.requests, "Session", lambda: session)
    rankings._CACHE["price_volume"] = {"rows": [], "expires_at": 0.0}

    rows, source = rankings.barchart_weekly_rows_with_source(force_refresh=True)

    assert len(rows) == 200
    assert rows[0]["sparkieUniverseRank"] == 1
    assert rows[-1]["sparkieUniverseRank"] == 200
    assert all(row["sparkieUniverseType"] == "price_volume" for row in rows)
    assert source == "barchart:price_volume_leaders:live"
    params = session.calls[1][1]["params"]
    assert params["list"] == "stocks.us.price_volume.advances.overall"
    assert params["orderBy"] == "priceVolume"
    assert params["orderDir"] == "desc"
    assert params["limit"] == 200
