from app.services.stocktwits_symbols import (
    _dedupe_symbols,
    _symbols_from_html,
    _symbols_from_payload,
)


def test_symbols_from_stocktwits_payload() -> None:
    payload = {"symbols": [{"symbol": "SPY"}, {"symbol": "SMCI"}, {"symbol": "MU"}]}
    assert _symbols_from_payload(payload) == ["SPY", "SMCI", "MU"]


def test_symbols_from_ranked_html() -> None:
    html = '''
    <div class="Rankings_tickerTableNewColumns__abc">
      <a href="/symbol/SPY">SPY</a><a href="/symbol/SMCI">SMCI</a>
    </div>
    '''
    assert _symbols_from_html(html) == ["SPY", "SMCI"]


def test_symbols_are_validated_and_deduplicated() -> None:
    assert _dedupe_symbols(["spy", "SPY", "BRK.B", "bad symbol", "BTC.X"]) == [
        "SPY",
        "BRK.B",
    ]
