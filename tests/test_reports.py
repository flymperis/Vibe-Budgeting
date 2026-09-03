"""Reports panel: the cash-flow, savings-rate and category-ranking maths.

These are pure functions over the ledger, so they are tested directly against a
seeded database rather than by scraping numbers out of rendered HTML.
"""

import app as vb_app
from finance import (
    cash_flow_chart_spec,
    category_spend_ranking,
    monthly_cash_flow_for_year,
)
from tests.conftest import csrf_token, register_and_login


def _seed(client, rows):
    """rows: (kind, amount, month, category, account) — amounts as the user types them."""
    conn = vb_app.get_connection()
    uid = conn.execute(
        "SELECT id FROM users ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]
    cat = conn.execute(
        "SELECT id FROM categories WHERE user_id = ? LIMIT 1", (uid,)
    ).fetchone()["id"]
    inc_cat = conn.execute(
        "SELECT id FROM income_categories WHERE user_id = ? LIMIT 1", (uid,)
    ).fetchone()["id"]
    acc = conn.execute(
        "SELECT id FROM accounts WHERE user_id = ? LIMIT 1", (uid,)
    ).fetchone()["id"]
    for kind, amount, date, category_name in rows:
        if kind == "expense":
            cid = cat
            if category_name:
                existing = conn.execute(
                    "SELECT id FROM categories WHERE user_id = ? AND name = ?",
                    (uid, category_name),
                ).fetchone()
                if existing:
                    cid = existing["id"]
                else:
                    cur = conn.execute(
                        "INSERT INTO categories (user_id, name) VALUES (?, ?)",
                        (uid, category_name),
                    )
                    cid = cur.lastrowid
            conn.execute(
                "INSERT INTO expenses (user_id, notes, amount, category_id, account_id, spent_at)"
                " VALUES (?, '', ?, ?, ?, ?)",
                (uid, amount, cid, acc, date),
            )
        else:
            conn.execute(
                "INSERT INTO income_entries (user_id, notes, amount, category_id, account_id, received_at)"
                " VALUES (?, '', ?, ?, ?, ?)",
                (uid, amount, inc_cat, acc, date),
            )
    conn.commit()
    conn.close()
    return uid


def test_cash_flow_nets_income_against_signed_expenses(client):
    """Expenses are stored signed, so a refund must reduce spending rather than
    add to it — the whole report is wrong if this is taken as an absolute."""
    register_and_login(client, "reports-cashflow")
    csrf_token(client)
    uid = _seed(
        client,
        [
            ("income", 2000.0, "2024-01-15", None),
            ("expense", -500.0, "2024-01-20", None),
            ("expense", 100.0, "2024-01-25", None),  # refund
            ("income", 1000.0, "2024-02-10", None),
            ("expense", -1500.0, "2024-02-11", None),
        ],
    )
    conn = vb_app.get_connection()
    cf = monthly_cash_flow_for_year(conn, 2024, uid)
    conn.close()

    jan, feb = cf["months"][0], cf["months"][1]
    assert jan["income"] == 2000.0
    assert jan["expenses"] == -400.0  # -500 spent, +100 refunded
    assert jan["net"] == 1600.0
    assert feb["net"] == -500.0

    assert cf["total_income"] == 3000.0
    assert cf["total_expenses"] == -1900.0
    assert cf["total_net"] == 1100.0
    assert cf["active_month_count"] == 2
    # income + expenses is the net, by construction
    assert cf["total_income"] + cf["total_expenses"] == cf["total_net"]


def test_savings_rate_and_month_extremes(client):
    register_and_login(client, "reports-savings")
    csrf_token(client)
    uid = _seed(
        client,
        [
            ("income", 1000.0, "2024-03-01", None),
            ("expense", -250.0, "2024-03-02", None),
            ("income", 1000.0, "2024-04-01", None),
            ("expense", -1200.0, "2024-04-02", None),
        ],
    )
    conn = vb_app.get_connection()
    cf = monthly_cash_flow_for_year(conn, 2024, uid)
    conn.close()

    # kept 550 of 2000
    assert round(cf["savings_rate"], 2) == 27.5
    assert cf["best_month"]["label"] == "March"
    assert cf["worst_month"]["label"] == "April"


def test_savings_rate_is_none_without_income(client):
    """Guards a divide-by-zero, and stops a spend-only year reporting a rate."""
    register_and_login(client, "reports-nosavings")
    csrf_token(client)
    uid = _seed(client, [("expense", -80.0, "2024-05-04", None)])
    conn = vb_app.get_connection()
    cf = monthly_cash_flow_for_year(conn, 2024, uid)
    conn.close()
    assert cf["savings_rate"] is None
    assert cf["total_net"] == -80.0


def test_category_ranking_orders_by_outflow_with_shares_and_deltas(client):
    register_and_login(client, "reports-categories")
    csrf_token(client)
    uid = _seed(
        client,
        [
            ("expense", -300.0, "2024-01-10", "Rent"),
            ("expense", -100.0, "2024-01-11", "Food"),
            ("expense", -60.0, "2024-02-12", "NewThing"),
            ("expense", -200.0, "2023-01-10", "Rent"),
        ],
    )
    conn = vb_app.get_connection()
    ranking = category_spend_ranking(conn, 2024, uid)
    conn.close()

    by_name = {r["name"]: r for r in ranking["rows"]}
    names = [r["name"] for r in ranking["rows"]]
    assert names[0] == "Rent"  # biggest outflow first
    assert ranking["outflow_total"] == 460.0
    assert round(by_name["Rent"]["share"], 1) == 65.2
    # shares are of outflow only, so they total 100%
    assert round(sum(r["share"] for r in ranking["rows"]), 1) == 100.0

    # spending more than last year is a negative delta, so it reads red
    assert by_name["Rent"]["delta"] == -100.0
    # nothing to compare against gets None rather than a bogus -100%
    assert by_name["NewThing"]["delta"] is None
    assert ranking["prev_year"] == 2023


def test_refund_only_category_does_not_distort_shares(client):
    """A category that nets positive has no outflow, so it must not take share
    from the others or sort above real spending."""
    register_and_login(client, "reports-refunds")
    csrf_token(client)
    uid = _seed(
        client,
        [
            ("expense", -100.0, "2024-06-01", "Groceries"),
            ("expense", 40.0, "2024-06-02", "Refunds"),
        ],
    )
    conn = vb_app.get_connection()
    ranking = category_spend_ranking(conn, 2024, uid)
    conn.close()

    by_name = {r["name"]: r for r in ranking["rows"]}
    assert ranking["outflow_total"] == 100.0
    assert by_name["Refunds"]["share"] == 0.0
    assert by_name["Groceries"]["share"] == 100.0
    assert ranking["rows"][-1]["name"] == "Refunds"  # sorts last, not first


def test_chart_only_plots_months_that_happened(client):
    """A flat zero line across the rest of the year reads as "broke even"
    rather than "no data yet", so empty months get no net point."""
    register_and_login(client, "reports-chart")
    csrf_token(client)
    uid = _seed(
        client,
        [
            ("income", 900.0, "2024-01-05", None),
            ("expense", -400.0, "2024-01-06", None),
            ("income", 900.0, "2024-02-05", None),
        ],
    )
    conn = vb_app.get_connection()
    cf = monthly_cash_flow_for_year(conn, 2024, uid)
    conn.close()

    spec = cash_flow_chart_spec(cf["months"])
    assert len(spec["net_dots"]) == 2
    assert len(spec["month_labels"]) == 12  # axis still spans the year
    assert spec["bars"], "expected income/spending bars"


def test_chart_spec_handles_an_empty_year():
    spec = cash_flow_chart_spec([])
    assert spec["bars"] == []
    assert spec["net_points"] == ""
    assert spec["net_dots"] == []


def test_legacy_report_section_links_still_resolve(client):
    """Old bookmarks used bank/crypto/stocks; they must not 404 or land nowhere."""
    register_and_login(client, "reports-legacy")
    # Every section div is always in the DOM — only the active class differs —
    # so assert on that rather than on the id being present.
    for legacy, expected in (
        ("bank", "reports-overview"),
        ("crypto", "reports-investments"),
        ("stocks", "reports-investments"),
        ("nonsense", "reports-overview"),
        ("", "reports-overview"),
    ):
        resp = client.get(f"/?panel=reports&reports_section={legacy}")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert f'class="reports-section active" id="{expected}"' in html, (
            f"{legacy!r} should activate {expected}"
        )
