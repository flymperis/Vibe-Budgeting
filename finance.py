from datetime import datetime, timedelta, timezone
import calendar

from helpers import normalize_optional_positive_int
from prices import prices_for_holdings_at_month_end


def _parse_row_created_date(row):
    raw = row["created_at"]
    if raw is None:
        return datetime.now().date()
    s = str(raw)
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return datetime.now().date()

def _due_date_in_month(ym: str, day_of_month: int):
    y, m = map(int, ym.split("-", 1))
    last = calendar.monthrange(y, m)[1]
    d = min(day_of_month, last)
    return datetime(y, m, d).date()

def _month_iter(start_ym: str, end_ym: str):
    y, m = map(int, start_ym.split("-", 1))
    ey, em = map(int, end_ym.split("-", 1))
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m > 12:
            m = 1
            y += 1

def apply_recurring_entries(conn, user_id):
    """Insert expense/income rows for due recurring rules (catch-up through current month)."""
    uid = int(user_id)
    today = datetime.now().date()
    today_ym = f"{today.year:04d}-{today.month:02d}"
    rules = conn.execute(
        """
        SELECT id, entry_type, amount, category_id, account_id, day_of_month, notes, created_at, months_to_run
        FROM recurring_entries
        WHERE enabled = 1 AND user_id = ?
        """,
        (uid,),
    ).fetchall()
    posted = 0
    for rule in rules:
        created_d = _parse_row_created_date(rule)
        start_ym = f"{created_d.year:04d}-{created_d.month:02d}"
        note_text = (rule["notes"] or "").strip()
        prefix = "[Recurring] "
        full_notes = f"{prefix}{note_text}" if note_text else prefix.strip()

        for ym in _month_iter(start_ym, today_ym):
            months_to_run = normalize_optional_positive_int(rule["months_to_run"])
            if months_to_run is not None:
                year_num, month_num = map(int, ym.split("-", 1))
                month_index = (year_num - created_d.year) * 12 + (month_num - created_d.month) + 1
                if month_index > months_to_run:
                    break
            exists = conn.execute(
                "SELECT 1 FROM recurring_applied WHERE recurring_id = ? AND ym = ?",
                (rule["id"], ym),
            ).fetchone()
            if exists:
                continue
            due = _due_date_in_month(ym, rule["day_of_month"])
            if due < created_d:
                continue
            if today < due:
                continue
            et = rule["entry_type"]
            if et == "expense":
                conn.execute(
                    """
                    INSERT INTO expenses (user_id, notes, amount, category_id, account_id, spent_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uid,
                        full_notes,
                        -abs(float(rule["amount"])),
                        int(rule["category_id"]),
                        int(rule["account_id"]),
                        due.isoformat(),
                    ),
                )
            elif et == "income":
                conn.execute(
                    """
                    INSERT INTO income_entries (user_id, notes, amount, category_id, account_id, received_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uid,
                        full_notes,
                        abs(float(rule["amount"])),
                        int(rule["category_id"]),
                        int(rule["account_id"]),
                        due.isoformat(),
                    ),
                )
            else:
                continue
            conn.execute(
                "INSERT INTO recurring_applied (recurring_id, ym) VALUES (?, ?)",
                (rule["id"], ym),
            )
            posted += 1
    return posted

def fetch_account_balances_through(conn, balance_cutoff_d, user_id):
    """Per-account balance with income, expenses, and transfers strictly before balance_cutoff_d (YYYY-MM-DD)."""
    uid = int(user_id)
    return conn.execute(
        """
        SELECT
            a.id,
            a.name,
            a.opening_balance
                + COALESCE(income_totals.total_income, 0)
                + COALESCE(expense_totals.total_expenses, 0)
                + COALESCE(transfer_in.t_in, 0)
                - COALESCE(transfer_out.t_out, 0) AS current_balance
        FROM accounts a
        LEFT JOIN (
            SELECT account_id, SUM(amount) AS total_income
            FROM income_entries
            WHERE user_id = ? AND date(received_at) < date(?)
            GROUP BY account_id
        ) income_totals ON income_totals.account_id = a.id
        LEFT JOIN (
            SELECT account_id, SUM(amount) AS total_expenses
            FROM expenses
            WHERE user_id = ? AND date(spent_at) < date(?)
            GROUP BY account_id
        ) expense_totals ON expense_totals.account_id = a.id
        LEFT JOIN (
            SELECT to_account_id AS account_id, SUM(amount) AS t_in
            FROM account_transfers
            WHERE user_id = ? AND date(transferred_at) < date(?)
            GROUP BY to_account_id
        ) transfer_in ON transfer_in.account_id = a.id
        LEFT JOIN (
            SELECT from_account_id AS account_id, SUM(amount) AS t_out
            FROM account_transfers
            WHERE user_id = ? AND date(transferred_at) < date(?)
            GROUP BY from_account_id
        ) transfer_out ON transfer_out.account_id = a.id
        WHERE a.user_id = ?
        ORDER BY a.name
        """,
        (
            uid,
            balance_cutoff_d,
            uid,
            balance_cutoff_d,
            uid,
            balance_cutoff_d,
            uid,
            balance_cutoff_d,
            uid,
        ),
    ).fetchall()

def month_snap_cutoff(year: int, month_num: int, today_d) -> str:
    """Exclusive date cutoff for balance/portfolio snapshots (matches bank balance chart)."""
    month_start = datetime(year, month_num, 1)
    month_end_exclusive = month_start + timedelta(days=32)
    month_end_exclusive = month_end_exclusive.replace(day=1)
    month_end_cutoff = month_end_exclusive.strftime("%Y-%m-%d")
    if year == today_d.year and month_num == today_d.month:
        snap = (today_d + timedelta(days=1)).isoformat()
    else:
        snap = month_end_cutoff
    # Never snapshot after today (future months in the selected year).
    cap = (today_d + timedelta(days=1)).isoformat()
    if snap > cap:
        return cap
    return snap

def account_balance_at_cutoff(
    conn, balance_cutoff_d: str, user_id, account_id: int | None = None
) -> float:
    """Single-account or all-accounts balance strictly before balance_cutoff_d."""
    rows = fetch_account_balances_through(conn, balance_cutoff_d, user_id)
    if account_id is None:
        return sum(float(r["current_balance"]) for r in rows)
    aid = int(account_id)
    for r in rows:
        if int(r["id"]) == aid:
            return float(r["current_balance"])
    return 0.0

def monthly_total_balances_for_year(
    conn, year: int, today_d, user_id, account_id: int | None = None
) -> list[float]:
    """Account balance(s) after each calendar month; optional single-account filter."""
    balances = []
    for month_num in range(1, 13):
        snap_cutoff = month_snap_cutoff(year, month_num, today_d)
        balances.append(
            account_balance_at_cutoff(conn, snap_cutoff, user_id, account_id)
        )
    return balances

def build_monthly_chart_rows(
    monthly_values: list[float], year: int, *, baseline: float | None = None
) -> list[dict]:
    """Line chart rows with month labels and month-to-month change."""
    report_rows = []
    prev_balance_total = baseline if baseline is not None else 0.0
    for month_num in range(1, 13):
        total_bal = monthly_values[month_num - 1]
        change = total_bal - prev_balance_total
        prev_balance_total = total_bal
        report_rows.append(
            {
                "month_label": calendar.month_name[month_num],
                "month_num": month_num,
                "total_balance": total_bal,
                "change": change,
            }
        )
    return report_rows

def portfolio_value_from_holdings(holdings, price_by_key: dict, price_key: str) -> float:
    total = 0.0
    for h in holdings:
        key = h[price_key]
        pinfo = price_by_key.get(key)
        if pinfo and pinfo.get("price") is not None:
            total += float(h["quantity"]) * float(pinfo["price"])
        else:
            total += float(h["total_cost"])
    return total

def portfolio_baseline_before_year(
    conn,
    transactions,
    year: int,
    today_d,
    compute_holdings_fn,
    price_key: str,
    asset_kind: str,
) -> float:
    jan1 = f"{year:04d}-01-01"
    subset = [t for t in transactions if str(t["transacted_at"])[:10] < jan1]
    holdings = compute_holdings_fn(subset)
    if not holdings:
        return 0.0
    return portfolio_value_from_holdings(
        holdings,
        prices_for_holdings_at_month_end(
            conn, holdings, year - 1, 12, today_d, price_key, asset_kind
        ),
        price_key,
    )

def monthly_crypto_portfolio_values_for_year(conn, transactions, year: int, today_d) -> list[float]:
    balances = []
    for month_num in range(1, 13):
        snap_cutoff = month_snap_cutoff(year, month_num, today_d)
        subset = [t for t in transactions if str(t["transacted_at"])[:10] < snap_cutoff[:10]]
        holdings = compute_crypto_holdings(subset)
        price_map = prices_for_holdings_at_month_end(
            conn, holdings, year, month_num, today_d, "coin_id", "crypto"
        )
        balances.append(portfolio_value_from_holdings(holdings, price_map, "coin_id"))
    return balances

def monthly_stock_portfolio_values_for_year(conn, transactions, year: int, today_d) -> list[float]:
    balances = []
    for month_num in range(1, 13):
        snap_cutoff = month_snap_cutoff(year, month_num, today_d)
        subset = [t for t in transactions if str(t["transacted_at"])[:10] < snap_cutoff[:10]]
        holdings = compute_stock_holdings(subset)
        price_map = prices_for_holdings_at_month_end(
            conn, holdings, year, month_num, today_d, "symbol", "stock"
        )
        balances.append(portfolio_value_from_holdings(holdings, price_map, "symbol"))
    return balances

def balance_line_chart_spec(rows: list, *, width: float = 720, height: float = 300) -> dict:
    """SVG line chart: rows need total_balance, month_label, change."""
    ml, mr, mt, mb = 58.0, 18.0, 24.0, 46.0
    pw = width - ml - mr
    ph = height - mt - mb
    vals = [float(r["total_balance"]) for r in rows]
    month_label_y = height - 12.0
    if not vals:
        return {
            "w": width,
            "h": height,
            "points": "",
            "dots": [],
            "plot_ml": ml,
            "plot_mt": mt,
            "plot_pw": pw,
            "plot_ph": ph,
            "y_ticks": [],
            "month_label_y": month_label_y,
        }
    y_lo = min(vals)
    y_hi = max(vals)
    span = y_hi - y_lo
    if span <= 0:
        eps = max(abs(y_lo) * 0.02, 1.0) if y_lo != 0 else 1.0
        y_min = y_lo - eps
        y_max = y_hi + eps
    else:
        pad = span * 0.05
        y_min = y_lo - pad
        y_max = y_hi + pad
    y_rng = y_max - y_min or 1.0
    y_mid = y_min + y_rng / 2
    n = len(vals)
    dots = []
    pts = []
    for i, r in enumerate(rows):
        v = float(r["total_balance"])
        ch = float(r.get("change", 0.0))
        x = ml + (i / (n - 1)) * pw if n > 1 else ml + pw / 2
        yi = mt + ph - ((v - y_min) / y_rng) * ph
        pts.append(f"{x:.1f},{yi:.1f}")
        label = r["month_label"]
        dots.append(
            {
                "cx": round(x, 1),
                "cy": round(yi, 1),
                "lx": round(x, 1),
                "short": label[:3],
                "title": f"{label}: {v:.2f} (Δ {ch:+.2f})",
            }
        )
    y_ticks = [
        {"x": 4, "y": mt + 8, "text": f"{y_max:.2f}"},
        {"x": 4, "y": mt + ph / 2, "text": f"{y_mid:.2f}"},
        {"x": 4, "y": mt + ph - 4, "text": f"{y_min:.2f}"},
    ]
    return {
        "w": width,
        "h": height,
        "points": " ".join(pts),
        "dots": dots,
        "plot_ml": ml,
        "plot_mt": mt,
        "plot_pw": pw,
        "plot_ph": ph,
        "y_ticks": y_ticks,
        "month_label_y": month_label_y,
    }

def _avg_over_nonzero_month_cells(months: list[float]):
    """Mean of monthly values counting only months where the cell is non-zero."""
    active = sum(1 for v in months if v != 0.0)
    if not active:
        return None
    return sum(months) / active

def _empty_expense_pivot() -> dict:
    """Placeholder pivot used when the Reports panel is not being rendered."""
    return {
        "rows": [],
        "month_headers": [f"{m:02d}.{calendar.month_abbr[m]}" for m in range(1, 13)],
        "month_totals": [0.0] * 12,
        "grand_total": 0.0,
        "active_month_count": 0,
        "avg_monthly_total": None,
    }

def expense_pivot_for_report_year(conn, year: int, user_id: int) -> dict:
    """Category × month sums of expense amounts for a calendar year (values as stored in DB)."""
    uid = int(user_id)
    y0 = f"{year:04d}-01-01"
    y1 = f"{year + 1:04d}-01-01"
    raw = conn.execute(
        """
        SELECT c.name AS category_name,
               CAST(strftime('%m', e.spent_at) AS INTEGER) AS m,
               SUM(e.amount) AS total
        FROM expenses e
        JOIN categories c ON c.id = e.category_id AND c.user_id = e.user_id
        WHERE e.user_id = ? AND date(e.spent_at) >= date(?) AND date(e.spent_at) < date(?)
        GROUP BY c.name, m
        """,
        (uid, y0, y1),
    ).fetchall()
    pivot: dict[str, list[float]] = {}
    for row in raw:
        cat = row["category_name"]
        m = int(row["m"])
        if not 1 <= m <= 12:
            continue
        if cat not in pivot:
            pivot[cat] = [0.0] * 12
        pivot[cat][m - 1] = float(row["total"])
    categories_sorted = sorted(pivot.keys())
    rows_out = []
    for cat in categories_sorted:
        months = pivot[cat]
        rows_out.append(
            {
                "name": cat,
                "months": months,
                "total": sum(months),
                "avg": _avg_over_nonzero_month_cells(months),
            }
        )
    month_totals = [0.0] * 12
    for cat in categories_sorted:
        for i in range(12):
            month_totals[i] += pivot[cat][i]
    grand_total = sum(month_totals)
    month_headers = [f"{m:02d}.{calendar.month_abbr[m]}" for m in range(1, 13)]
    active_month_count = sum(1 for t in month_totals if t != 0.0)
    avg_monthly_total = (
        (grand_total / active_month_count) if active_month_count else None
    )
    return {
        "rows": rows_out,
        "month_headers": month_headers,
        "month_totals": month_totals,
        "grand_total": grand_total,
        "active_month_count": active_month_count,
        "avg_monthly_total": avg_monthly_total,
    }

def compute_crypto_holdings(transactions):
    holdings: dict = {}
    for tx in transactions:
        cid = tx["coin_id"]
        if cid not in holdings:
            holdings[cid] = {
                "coin_id": cid,
                "coin_symbol": tx["coin_symbol"],
                "coin_name": tx["coin_name"],
                "quantity": 0.0,
                "total_cost": 0.0,
            }
        h = holdings[cid]
        qty = float(tx["quantity"])
        price = float(tx["price_per_unit"])
        fee = float(tx["fee"])
        if tx["tx_type"] == "buy":
            h["total_cost"] += qty * price + fee
            h["quantity"] += qty
        else:
            if h["quantity"] > 0:
                avg_cost = h["total_cost"] / h["quantity"]
                h["total_cost"] -= qty * avg_cost
            h["quantity"] -= qty
    result = []
    for _cid, h in sorted(holdings.items(), key=lambda x: x[1]["coin_name"].lower()):
        if abs(h["quantity"]) > 1e-9:
            h["avg_buy_price"] = h["total_cost"] / h["quantity"] if h["quantity"] > 0 else 0
            result.append(h)
    return result

def compute_stock_holdings(transactions):
    holdings: dict = {}
    for tx in transactions:
        sym = tx["symbol"]
        if sym not in holdings:
            holdings[sym] = {
                "symbol": sym,
                "ticker": tx["ticker"],
                "instrument_name": tx["instrument_name"],
                "quantity": 0.0,
                "total_cost": 0.0,
            }
        h = holdings[sym]
        qty = float(tx["quantity"])
        price = float(tx["price_per_unit"])
        fee = float(tx["fee"])
        if tx["tx_type"] == "buy":
            h["total_cost"] += qty * price + fee
            h["quantity"] += qty
        else:
            if h["quantity"] > 0:
                avg_cost = h["total_cost"] / h["quantity"]
                h["total_cost"] -= qty * avg_cost
            h["quantity"] -= qty
    result = []
    for _sym, h in sorted(holdings.items(), key=lambda x: x[1]["instrument_name"].lower()):
        if abs(h["quantity"]) > 1e-9:
            h["avg_buy_price"] = h["total_cost"] / h["quantity"] if h["quantity"] > 0 else 0
            result.append(h)
    return result
