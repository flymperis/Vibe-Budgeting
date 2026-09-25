"""Live price lookups (CoinGecko/Finnhub) are synchronous outbound HTTP calls.
They must only run when the Investments panel is actually being requested, or
when a refresh is explicitly asked for -- not on every page load regardless of
which panel is showing (panels are switched client-side, see static/app.js)."""

from routes import dashboard

from .conftest import csrf_token, register_and_login


def _add_holdings(client):
    """Give the logged-in user one crypto and one stock holding."""
    client.post(
        "/crypto/add",
        data={
            "coin_id": "bitcoin",
            "coin_symbol": "BTC",
            "coin_name": "Bitcoin",
            "tx_type": "buy",
            "quantity": "0.5",
            "price_per_unit": "40000",
            "fee": "10",
            "exchange": "TestExchange",
            "transacted_at": "2026-02-01",
            "notes": "btc buy",
            "_csrf_token": csrf_token(client),
        },
        follow_redirects=True,
    )
    client.post(
        "/stocks/add",
        data={
            "symbol": "AAPL",
            "ticker": "AAPL",
            "instrument_name": "Apple Inc",
            "tx_type": "buy",
            "quantity": "10",
            "price_per_unit": "150.00",
            "fee": "1.50",
            "broker": "TestBroker",
            "transacted_at": "2026-01-15",
            "notes": "first buy",
            "_csrf_token": csrf_token(client),
        },
        follow_redirects=True,
    )


class _CountingStub:
    def __init__(self):
        self.calls = 0

    def __call__(self, items, force=False):
        self.calls += 1
        return {}


def _patch_fetchers(monkeypatch):
    coingecko_stub = _CountingStub()
    finnhub_stub = _CountingStub()
    monkeypatch.setattr(dashboard, "fetch_coingecko_prices", coingecko_stub)
    monkeypatch.setattr(dashboard, "fetch_finnhub_quotes", finnhub_stub)
    return coingecko_stub, finnhub_stub


def test_non_investments_panel_does_not_fetch_live_prices(client, monkeypatch):
    register_and_login(client, "gatinghome")
    _add_holdings(client)

    coingecko_stub, finnhub_stub = _patch_fetchers(monkeypatch)

    resp = client.get("/?panel=home")
    assert resp.status_code == 200
    assert coingecko_stub.calls == 0
    assert finnhub_stub.calls == 0


def test_investments_panel_fetches_live_prices(client, monkeypatch):
    register_and_login(client, "gatinginvest")
    _add_holdings(client)

    coingecko_stub, finnhub_stub = _patch_fetchers(monkeypatch)

    resp = client.get("/?panel=investments")
    assert resp.status_code == 200
    assert coingecko_stub.calls == 1
    assert finnhub_stub.calls == 1


def test_refresh_param_fetches_live_prices_on_other_panels(client, monkeypatch):
    register_and_login(client, "gatingrefresh")
    _add_holdings(client)

    coingecko_stub, finnhub_stub = _patch_fetchers(monkeypatch)

    resp = client.get("/?panel=home&refresh_prices=1&refresh_stock_prices=1")
    assert resp.status_code == 200
    assert coingecko_stub.calls == 1
    assert finnhub_stub.calls == 1


def test_investments_data_shows_dash_not_zero_when_unloaded(client, monkeypatch):
    """When prices weren't fetched (cold cache, non-investments panel), the
    Investments markup embedded in the page must not present misleading
    zero/-100% totals -- it should show a dash and flag itself as unloaded so
    the client-side panel switch (static/app.js) knows to force a reload."""
    register_and_login(client, "gatingdash")
    _add_holdings(client)

    _patch_fetchers(monkeypatch)

    resp = client.get("/?panel=home")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    crypto_start = html.index('id="investments-crypto"')
    crypto_end = html.index('id="investments-stocks"')
    crypto_section = html[crypto_start:crypto_end]
    assert 'data-prices-loaded="0"' in crypto_section
    assert "-100" not in crypto_section
    assert "—" in crypto_section
    # "Total Invested" is still meaningful without live prices and should
    # keep showing the real figure, not a dash.
    assert "20.010,00" in crypto_section or "20010" in crypto_section

    stocks_section = html[crypto_end:]
    assert 'data-prices-loaded="0"' in stocks_section
    assert "-100" not in stocks_section
    assert "—" in stocks_section
