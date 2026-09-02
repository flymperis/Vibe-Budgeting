import os
import tempfile

import pytest

_db_fd, _db_path = tempfile.mkstemp(prefix="vibe-budgeting-test-", suffix=".db")
os.close(_db_fd)
os.remove(_db_path)
os.environ["DATABASE_PATH"] = _db_path
os.environ["FLASK_SECRET_KEY"] = "test-secret-key"
os.environ["ALLOW_REGISTRATION"] = "true"

import app as vb_app  # noqa: E402  (must import after env vars are set)
import prices  # noqa: E402
from routes import dashboard  # noqa: E402


@pytest.fixture(scope="session")
def app():
    vb_app.app.config.update(TESTING=True, WTF_CSRF_ENABLED=True)
    yield vb_app.app


@pytest.fixture(autouse=True)
def no_external_price_calls(monkeypatch):
    """The dashboard fetches live prices whenever a user holds stocks or crypto.
    Stub those out so tests never reach the network. The live quote helpers are
    patched where the dashboard binds them; the month-close helpers are patched
    in prices, where ensure_*_month_price looks them up."""
    monkeypatch.setattr(dashboard, "fetch_finnhub_quotes", lambda symbols, force=False: {})
    monkeypatch.setattr(dashboard, "fetch_coingecko_prices", lambda coin_ids, force=False: {})
    monkeypatch.setattr(prices, "fetch_stock_month_close_usd", lambda symbol, price_date_iso: None)
    monkeypatch.setattr(prices, "fetch_coingecko_history_eur", lambda coin_id, price_date: None)


@pytest.fixture
def client(app):
    return app.test_client()


def register_and_login(client, username, password="correct-horse-battery-staple"):
    """Registers a fresh user and returns their logged-in client plus a csrf() helper."""
    resp = client.get("/register")
    token = _extract_csrf(resp.get_data(as_text=True))
    resp = client.post(
        "/register",
        data={
            "username": username,
            "password": password,
            "password2": password,
            "_csrf_token": token,
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    return client


def _extract_csrf(html: str) -> str:
    for marker in ('name="_csrf_token" value="', 'name="csrf-token" content="'):
        if marker in html:
            start = html.index(marker) + len(marker)
            end = html.index('"', start)
            return html[start:end]
    raise AssertionError("No CSRF token found in response HTML")


def csrf_token(client) -> str:
    resp = client.get("/")
    return _extract_csrf(resp.get_data(as_text=True))
