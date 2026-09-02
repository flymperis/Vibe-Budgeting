import app as vb_app

from .conftest import csrf_token, register_and_login


def _account_id(client, username):
    conn = vb_app.get_connection()
    row = conn.execute(
        """
        SELECT a.id FROM accounts a
        JOIN users u ON u.id = a.user_id
        WHERE u.username = ? ORDER BY a.id LIMIT 1
        """,
        (username,),
    ).fetchone()
    conn.close()
    assert row is not None
    return row["id"]


def _category_id(client, username, table="categories"):
    conn = vb_app.get_connection()
    row = conn.execute(
        f"""
        SELECT c.id FROM {table} c
        JOIN users u ON u.id = c.user_id
        WHERE u.username = ? ORDER BY c.id LIMIT 1
        """,
        (username,),
    ).fetchone()
    conn.close()
    assert row is not None
    return row["id"]


def test_stock_add_edit_delete(client):
    register_and_login(client, "stocktester")

    resp = client.post(
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
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    row = conn.execute(
        "SELECT id, quantity, price_per_unit FROM stock_transactions WHERE notes = 'first buy'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["quantity"] == 10.0
    tx_id = row["id"]

    resp = client.post(
        f"/stocks/{tx_id}/edit",
        data={
            "symbol": "AAPL",
            "ticker": "AAPL",
            "instrument_name": "Apple Inc",
            "tx_type": "buy",
            "quantity": "12",
            "price_per_unit": "155.00",
            "fee": "1.50",
            "broker": "TestBroker",
            "transacted_at": "2026-01-15",
            "notes": "first buy",
            "_csrf_token": csrf_token(client),
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    row = conn.execute("SELECT quantity FROM stock_transactions WHERE id = ?", (tx_id,)).fetchone()
    conn.close()
    assert row["quantity"] == 12.0

    resp = client.post(
        f"/stocks/{tx_id}/delete",
        data={"_csrf_token": csrf_token(client)},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    row = conn.execute("SELECT id FROM stock_transactions WHERE id = ?", (tx_id,)).fetchone()
    conn.close()
    assert row is None


def test_crypto_add_edit_delete(client):
    register_and_login(client, "cryptotester")

    resp = client.post(
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
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    row = conn.execute(
        "SELECT id, quantity FROM crypto_transactions WHERE notes = 'btc buy'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["quantity"] == 0.5
    tx_id = row["id"]

    resp = client.post(
        f"/crypto/{tx_id}/edit",
        data={
            "coin_id": "bitcoin",
            "coin_symbol": "BTC",
            "coin_name": "Bitcoin",
            "tx_type": "buy",
            "quantity": "0.75",
            "price_per_unit": "41000",
            "fee": "10",
            "exchange": "TestExchange",
            "transacted_at": "2026-02-01",
            "notes": "btc buy",
            "_csrf_token": csrf_token(client),
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    row = conn.execute("SELECT quantity FROM crypto_transactions WHERE id = ?", (tx_id,)).fetchone()
    conn.close()
    assert row["quantity"] == 0.75

    resp = client.post(
        f"/crypto/{tx_id}/delete",
        data={"_csrf_token": csrf_token(client)},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    assert conn.execute("SELECT id FROM crypto_transactions WHERE id = ?", (tx_id,)).fetchone() is None
    conn.close()


def test_dashboard_loads_with_holdings(client):
    """Investments and reports panels do the most work; make sure they render
    once the user actually holds something."""
    register_and_login(client, "holder")
    client.post(
        "/stocks/add",
        data={
            "symbol": "MSFT",
            "ticker": "MSFT",
            "instrument_name": "Microsoft",
            "tx_type": "buy",
            "quantity": "5",
            "price_per_unit": "300",
            "transacted_at": "2026-03-01",
            "_csrf_token": csrf_token(client),
        },
        follow_redirects=True,
    )
    client.post(
        "/crypto/add",
        data={
            "coin_id": "ethereum",
            "coin_symbol": "ETH",
            "coin_name": "Ethereum",
            "tx_type": "buy",
            "quantity": "2",
            "price_per_unit": "2000",
            "transacted_at": "2026-03-01",
            "_csrf_token": csrf_token(client),
        },
        follow_redirects=True,
    )

    for url in (
        "/?panel=investments&investments_section=stocks",
        "/?panel=investments&investments_section=crypto",
        "/?panel=reports&reports_section=bank",
        "/?panel=reports&reports_section=stocks",
        "/?panel=reports&reports_section=crypto",
        "/?panel=summary",
        "/?panel=yearly",
    ):
        assert client.get(url).status_code == 200, f"{url} failed to load"


def test_recurring_add_edit_delete(client):
    register_and_login(client, "recurringtester")
    account_id = _account_id(client, "recurringtester")
    category_id = _category_id(client, "recurringtester")

    resp = client.post(
        "/recurring/add",
        data={
            "category_choice": f"e-{category_id}",
            "amount": "50",
            "account_id": str(account_id),
            "day_of_month": "5",
            "indefinitely": "1",
            "notes": "monthly rent",
            "_csrf_token": csrf_token(client),
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    row = conn.execute(
        "SELECT id, amount, day_of_month, months_to_run FROM recurring_entries WHERE notes = 'monthly rent'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["amount"] == 50.0
    assert row["day_of_month"] == 5
    assert row["months_to_run"] is None  # "indefinitely"
    rule_id = row["id"]

    resp = client.post(
        f"/recurring/{rule_id}/edit",
        data={
            "category_choice": f"e-{category_id}",
            "amount": "75",
            "account_id": str(account_id),
            "day_of_month": "10",
            "months_to_run": "6",
            "notes": "monthly rent",
            "_csrf_token": csrf_token(client),
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    row = conn.execute(
        "SELECT amount, day_of_month, months_to_run FROM recurring_entries WHERE id = ?",
        (rule_id,),
    ).fetchone()
    conn.close()
    assert row["amount"] == 75.0
    assert row["day_of_month"] == 10
    assert row["months_to_run"] == 6

    resp = client.post(
        f"/recurring/{rule_id}/delete",
        data={"_csrf_token": csrf_token(client)},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    assert conn.execute("SELECT id FROM recurring_entries WHERE id = ?", (rule_id,)).fetchone() is None
    conn.close()


def test_transfer_between_accounts(client):
    register_and_login(client, "transfertester")
    from_id = _account_id(client, "transfertester")

    client.post(
        "/accounts/add",
        data={"name": "Savings", "opening_balance": "0", "_csrf_token": csrf_token(client)},
        follow_redirects=True,
    )
    conn = vb_app.get_connection()
    to_row = conn.execute(
        """
        SELECT a.id FROM accounts a
        JOIN users u ON u.id = a.user_id
        WHERE u.username = ? AND a.name = 'Savings'
        """,
        ("transfertester",),
    ).fetchone()
    conn.close()
    assert to_row is not None

    resp = client.post(
        "/transfers/add",
        data={
            "from_account_id": str(from_id),
            "to_account_id": str(to_row["id"]),
            "amount": "200",
            "transferred_at": "2026-04-01",
            "notes": "to savings",
            "_csrf_token": csrf_token(client),
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    row = conn.execute(
        """
        SELECT t.id, t.amount FROM account_transfers t
        JOIN users u ON u.id = t.user_id
        WHERE u.username = ? AND t.notes = 'to savings'
        """,
        ("transfertester",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["amount"] == 200.0

    resp = client.post(
        f"/transfers/{row['id']}/delete",
        data={"_csrf_token": csrf_token(client)},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    assert conn.execute("SELECT id FROM account_transfers WHERE id = ?", (row["id"],)).fetchone() is None
    conn.close()


def test_transfer_rejects_same_account(client):
    register_and_login(client, "sameacct")
    account_id = _account_id(client, "sameacct")

    client.post(
        "/transfers/add",
        data={
            "from_account_id": str(account_id),
            "to_account_id": str(account_id),
            "amount": "50",
            "_csrf_token": csrf_token(client),
        },
        follow_redirects=True,
    )
    conn = vb_app.get_connection()
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM account_transfers WHERE from_account_id = ? AND to_account_id = ?",
        (account_id, account_id),
    ).fetchone()["n"]
    conn.close()
    assert count == 0
