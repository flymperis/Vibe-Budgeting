import re

import app as vb_app

from .conftest import csrf_token, register_and_login


def test_register_and_dashboard_load(client):
    register_and_login(client, "alice")

    resp = client.get("/")
    assert resp.status_code == 200


def test_logout_requires_csrf_then_succeeds(client):
    register_and_login(client, "bob")
    token = csrf_token(client)

    resp = client.post("/logout", data={}, follow_redirects=True)
    assert resp.status_code == 400  # missing CSRF token is rejected

    resp = client.post("/logout", data={"_csrf_token": token}, follow_redirects=True)
    assert resp.status_code == 200
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 308)  # bounced to /login once logged out


def test_dashboard_loads_every_panel(client):
    register_and_login(client, "carol")
    for panel in sorted(vb_app.ALLOWED_PANELS):
        resp = client.get(f"/?panel={panel}")
        assert resp.status_code == 200, f"panel={panel} failed to load"


def _panel_html(html: str, panel: str) -> str:
    start_marker = f'id="panel-{panel}"'
    start = html.index(start_marker)
    end = html.find('<section class="panel', start + 1)
    return html[start : end if end != -1 else len(html)]


def _first_id(html: str, name: str, panel: str) -> str:
    scoped = _panel_html(html, panel)
    match = re.search(rf'name="{name}"[^>]*>\s*<option value="(\d+)"', scoped)
    assert match, f"could not find a seeded <option> for select[name={name}] in panel={panel}"
    return match.group(1)


def test_add_edit_delete_expense(client):
    register_and_login(client, "dave")
    home = client.get("/").get_data(as_text=True)
    token = csrf_token(client)

    category_id = _first_id(home, "category_id", "expenses")
    account_id = _first_id(home, "account_id", "expenses")

    resp = client.post(
        "/expenses/add",
        data={
            "notes": "coffee",
            "amount": "3.50",
            "category_id": category_id,
            "account_id": account_id,
            "_csrf_token": token,
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"coffee" in resp.data

    conn = vb_app.get_connection()
    row = conn.execute("SELECT id FROM expenses WHERE notes = 'coffee'").fetchone()
    conn.close()
    assert row is not None
    expense_id = row["id"]

    token = csrf_token(client)
    resp = client.post(
        f"/expenses/{expense_id}/edit",
        data={
            "notes": "coffee and cake",
            "amount": "5.00",
            "category_id": category_id,
            "account_id": account_id,
            "_csrf_token": token,
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"coffee and cake" in resp.data

    token = csrf_token(client)
    resp = client.post(
        f"/expenses/{expense_id}/delete",
        data={"_csrf_token": token},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"coffee and cake" not in resp.data


def test_add_income_entry(client):
    register_and_login(client, "erin")
    home = client.get("/?panel=income").get_data(as_text=True)
    token = csrf_token(client)

    category_id = _first_id(home, "category_id", "income")
    account_id = _first_id(home, "account_id", "income")

    resp = client.post(
        "/income/add",
        data={
            "notes": "salary",
            "amount": "1500",
            "category_id": category_id,
            "account_id": account_id,
            "_csrf_token": token,
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    row = conn.execute(
        "SELECT amount FROM income_entries WHERE notes = 'salary'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["amount"] == 1500.0


def test_add_account_and_category(client):
    register_and_login(client, "frank")
    token = csrf_token(client)

    resp = client.post(
        "/accounts/add",
        data={"name": "Savings", "opening_balance": "100", "_csrf_token": token},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Savings" in resp.data

    token = csrf_token(client)
    resp = client.post(
        "/categories/add",
        data={"name": "Groceries", "_csrf_token": token},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Groceries" in resp.data
