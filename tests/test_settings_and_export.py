import app as vb_app
import config
import integrations

from .conftest import csrf_token, register_and_login


def _user_id(username):
    conn = vb_app.get_connection()
    row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    assert row is not None
    return row["id"]


def test_settings_sections_load(client):
    register_and_login(client, "settingstester")
    for section in sorted(config.SETTINGS_SECTIONS):
        resp = client.get(f"/?panel=settings&settings_section={section}")
        assert resp.status_code == 200, f"settings_section={section} failed to load"


def test_income_category_add_edit_delete(client):
    register_and_login(client, "inccattester")
    uid = _user_id("inccattester")

    resp = client.post(
        "/income-categories/add",
        data={"name": "Freelance", "_csrf_token": csrf_token(client)},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    row = conn.execute(
        "SELECT id FROM income_categories WHERE name = 'Freelance' AND user_id = ?", (uid,)
    ).fetchone()
    conn.close()
    assert row is not None
    cat_id = row["id"]

    resp = client.post(
        f"/income-categories/{cat_id}/edit",
        data={"name": "Consulting", "_csrf_token": csrf_token(client)},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    row = conn.execute("SELECT name FROM income_categories WHERE id = ?", (cat_id,)).fetchone()
    conn.close()
    assert row["name"] == "Consulting"

    resp = client.post(
        f"/income-categories/{cat_id}/delete",
        data={"_csrf_token": csrf_token(client)},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    assert conn.execute("SELECT id FROM income_categories WHERE id = ?", (cat_id,)).fetchone() is None
    conn.close()


def test_account_edit_and_delete(client):
    register_and_login(client, "accttester")
    uid = _user_id("accttester")

    client.post(
        "/accounts/add",
        data={"name": "Brokerage", "opening_balance": "500", "_csrf_token": csrf_token(client)},
        follow_redirects=True,
    )
    conn = vb_app.get_connection()
    row = conn.execute(
        "SELECT id, opening_balance FROM accounts WHERE name = 'Brokerage' AND user_id = ?", (uid,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["opening_balance"] == 500.0
    acct_id = row["id"]

    resp = client.post(
        f"/accounts/{acct_id}/edit",
        data={"name": "Brokerage EU", "opening_balance": "750.25", "_csrf_token": csrf_token(client)},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    row = conn.execute("SELECT name, opening_balance FROM accounts WHERE id = ?", (acct_id,)).fetchone()
    conn.close()
    assert row["name"] == "Brokerage EU"
    assert row["opening_balance"] == 750.25

    resp = client.post(
        f"/accounts/{acct_id}/delete",
        data={"_csrf_token": csrf_token(client)},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    assert conn.execute("SELECT id FROM accounts WHERE id = ?", (acct_id,)).fetchone() is None
    conn.close()


def test_integrations_save(client):
    register_and_login(client, "integrationstester")
    uid = _user_id("integrationstester")

    resp = client.post(
        "/settings/integrations/save",
        data={
            "ai_provider": "ollama",
            "ai_enabled": "1",
            "ai_base_url": "http://127.0.0.1:11434",
            "ai_model": "llama3",
            "ai_timeout": "30",
            "_csrf_token": csrf_token(client),
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    saved = integrations.get_user_integrations(conn, uid)
    conn.close()
    assert saved["ai_enabled"] is True
    assert saved["ai_base_url"] == "http://127.0.0.1:11434"
    assert saved["ai_model"] == "llama3"


def test_excel_export_downloads(client):
    register_and_login(client, "exporttester")

    resp = client.get("/export/excel")
    assert resp.status_code == 200
    assert "spreadsheetml" in resp.headers["Content-Type"]
    assert resp.data[:2] == b"PK"  # xlsx is a zip archive

    resp = client.get("/export/migration-template")
    assert resp.status_code == 200
    assert resp.data[:2] == b"PK"
