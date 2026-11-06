"""Credit card accounts: owed / available, the statement cycle, paying a card
by transfer, and the card settings surviving an export/import."""

import io
from datetime import date

import app as vb_app
import finance

from .conftest import csrf_token, register_and_login

# The money filter puts a non-breaking space before the euro sign.
EUR = "\u00a0€"


def _uid(username):
    conn = vb_app.get_connection()
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
    conn.close()
    return uid


def _account(uid, name):
    conn = vb_app.get_connection()
    row = conn.execute(
        "SELECT * FROM accounts WHERE user_id = ? AND name = ?", (uid, name)
    ).fetchone()
    conn.close()
    return row


def _add_card(client, name="Eurobank Visa", **fields):
    data = {
        "name": name,
        "account_type": "credit",
        "opening_balance": "0",
        "credit_limit": "1000",
        "statement_day": "5",
        "due_day": "25",
        "_csrf_token": csrf_token(client),
    }
    data.update(fields)
    return client.post("/accounts/add", data=data, follow_redirects=True)


def _status_on(uid, account_id, as_of):
    conn = vb_app.get_connection()
    cutoff = date.fromordinal(as_of.toordinal() + 1).isoformat()
    row = next(
        r for r in finance.fetch_account_balances_through(conn, cutoff, uid)
        if r["id"] == account_id
    )
    status = finance.credit_card_status(conn, uid, row, as_of)
    conn.close()
    return status


def test_statement_close_and_due_dates():
    # Closing day not reached yet this month -> last month's statement.
    assert finance.last_statement_close(date(2026, 3, 3), 5) == date(2026, 2, 5)
    assert finance.last_statement_close(date(2026, 3, 5), 5) == date(2026, 3, 5)
    # Day 31 closes on the last day of short months.
    assert finance.last_statement_close(date(2026, 2, 28), 31) == date(2026, 2, 28)
    assert finance.last_statement_close(date(2026, 2, 27), 31) == date(2026, 1, 31)
    # Due later the same month, or the next month when the due day comes first.
    assert finance.statement_due_date(date(2026, 3, 5), 25) == date(2026, 3, 25)
    assert finance.statement_due_date(date(2026, 3, 25), 25) == date(2026, 4, 25)
    assert finance.statement_due_date(date(2026, 1, 31), 15) == date(2026, 2, 15)


def test_add_card_stores_owed_opening_balance_as_negative(client):
    register_and_login(client, "cardopen")
    uid = _uid("cardopen")
    _add_card(client, opening_balance="300")

    card = _account(uid, "Eurobank Visa")
    assert card["account_type"] == "credit"
    assert card["opening_balance"] == -300
    assert card["credit_limit"] == 1000
    assert (card["statement_day"], card["due_day"]) == (5, 25)


def test_card_fields_are_validated(client):
    register_and_login(client, "cardbad")
    uid = _uid("cardbad")

    resp = _add_card(client, name="Bad due", due_day="40")
    assert b"between 1 and 31" in resp.data
    assert _account(uid, "Bad due") is None

    resp = _add_card(client, name="No close", statement_day="")
    assert b"Set the statement day too" in resp.data
    assert _account(uid, "No close") is None

    # A bank account ignores card inputs entirely.
    client.post(
        "/accounts/add",
        data={"name": "Bank", "opening_balance": "50", "credit_limit": "999", "_csrf_token": csrf_token(client)},
        follow_redirects=True,
    )
    bank = _account(uid, "Bank")
    assert bank["account_type"] == "bank"
    assert bank["opening_balance"] == 50
    assert bank["credit_limit"] is None


def test_statement_follows_spending_and_payments(client):
    register_and_login(client, "cardcycle")
    uid = _uid("cardcycle")
    _add_card(client)
    card_id = _account(uid, "Eurobank Visa")["id"]
    bank_id = _account(uid, "Main")["id"]

    conn = vb_app.get_connection()
    category_id = conn.execute("SELECT id FROM categories WHERE user_id = ? LIMIT 1", (uid,)).fetchone()["id"]
    for spent_at, amount in (("2026-03-01", -200), ("2026-03-10", -50)):
        conn.execute(
            "INSERT INTO expenses (user_id, notes, amount, category_id, account_id, spent_at) VALUES (?, '', ?, ?, ?, ?)",
            (uid, amount, category_id, card_id, spent_at),
        )
    conn.execute(
        "INSERT INTO account_transfers (user_id, from_account_id, to_account_id, amount, transferred_at, notes) VALUES (?, ?, ?, 120, '2026-03-12', '')",
        (uid, bank_id, card_id),
    )
    conn.commit()
    conn.close()

    status = _status_on(uid, card_id, date(2026, 3, 15))
    assert status["owed"] == 130
    assert status["available"] == 870
    statement = status["statement"]
    # Only the 1 Mar purchase was on the 5 Mar statement; the payment counts against it.
    assert statement["closed_on"] == date(2026, 3, 5)
    assert statement["amount"] == 200
    assert statement["remaining"] == 80
    assert statement["due_on"] == date(2026, 3, 25)
    assert statement["state"] == "open"

    assert _status_on(uid, card_id, date(2026, 3, 20))["statement"]["state"] == "soon"
    assert _status_on(uid, card_id, date(2026, 3, 26))["statement"]["state"] == "overdue"

    conn = vb_app.get_connection()
    conn.execute(
        "INSERT INTO account_transfers (user_id, from_account_id, to_account_id, amount, transferred_at, notes) VALUES (?, ?, ?, 80, '2026-03-20', '')",
        (uid, bank_id, card_id),
    )
    conn.commit()
    conn.close()
    assert _status_on(uid, card_id, date(2026, 3, 26))["statement"]["state"] == "paid"


def test_home_splits_totals_and_pay_link_prefills_transfer(client):
    register_and_login(client, "cardhome")
    uid = _uid("cardhome")
    _add_card(client, opening_balance="400")
    card_id = _account(uid, "Eurobank Visa")["id"]
    bank_id = _account(uid, "Main")["id"]

    home = client.get("/").get_data(as_text=True)
    assert "Net (all accounts)" in home
    assert "Credit cards" in home
    assert f"600,00{EUR} available of 1.000,00{EUR}" in home
    assert f"pay_card={card_id}" in home

    transfer = client.get(f"/?panel=transfer&pay_card={card_id}").get_data(as_text=True)
    assert f'<option value="{bank_id}" data-balance="0,00{EUR}" selected>' in transfer
    assert f'<option value="{card_id}" data-balance="-400,00{EUR}" selected>' in transfer


def test_card_settings_survive_export_import(client, app):
    register_and_login(client, "cardexport")
    _add_card(client, opening_balance="75")
    workbook_bytes = client.get("/export/excel").data

    other = app.test_client()
    register_and_login(other, "cardimport")
    resp = other.post(
        "/import/excel",
        data={
            "file": (io.BytesIO(workbook_bytes), "budget-export.xlsx"),
            "replace_movements": "1",
            "sync_opening_balances": "1",
            "_csrf_token": csrf_token(other),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"Import failed" not in resp.data

    card = _account(_uid("cardimport"), "Eurobank Visa")
    assert card["account_type"] == "credit"
    assert card["opening_balance"] == -75
    assert card["credit_limit"] == 1000
    assert (card["statement_day"], card["due_day"]) == (5, 25)
