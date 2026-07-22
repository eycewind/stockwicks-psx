from app.services.stocktwits_symbols import (
    _dedupe_symbols,
    _symbols_from_html,
    _symbols_from_markdown,
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


def test_symbols_from_numbered_markdown_table_only() -> None:
    markdown = """
[AAPL](http://stocktwits.com/symbol/AAPL)
# Most Active
Rank
Symbol
1

[SPY](http://stocktwits.com/symbol/SPY)
2

[SMCI](http://stocktwits.com/symbol/SMCI)
Ad
QNTU
3

[MU](http://stocktwits.com/symbol/MU)
**Join the conversation and get full access!**
1

[SPY](http://stocktwits.com/symbol/SPY)
"""
    assert _symbols_from_markdown(markdown) == ["SPY", "SMCI", "MU"]
