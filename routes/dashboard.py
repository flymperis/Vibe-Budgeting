from flask import Blueprint, flash, g, render_template, request
from datetime import datetime, timedelta, timezone
import calendar
import integrations
import telegram_bot
import time

from config import ALLOWED_PANELS, FINNHUB_API_KEY, LIST_PAGE_SIZE, TRANSFER_LOG_LIMIT
from db import get_connection
from finance import _empty_expense_pivot, account_balance_at_cutoff, apply_recurring_entries, balance_line_chart_spec, build_monthly_chart_rows, compute_crypto_holdings, compute_stock_holdings, expense_pivot_for_report_year, fetch_account_balances_through, monthly_crypto_portfolio_values_for_year, monthly_stock_portfolio_values_for_year, monthly_total_balances_for_year, portfolio_baseline_before_year
from helpers import month_bounds_dates, normalize_investments_section, normalize_list_page, normalize_optional_category_id, normalize_report_account, normalize_reports_section, normalize_settings_section, normalize_year, resolve_list_month_filter, resolve_month_filter_from_request
from prices import PRICE_CACHE_TTL, _price_cache, _stock_price_cache, fetch_coingecko_prices, fetch_finnhub_quotes

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def index():
    conn = get_connection()
    uid = g.user_id

    posted_recurring = apply_recurring_entries(conn, uid)
    if posted_recurring > 0:
        conn.commit()
        flash(
            f"Posted {posted_recurring} recurring entr{'ies' if posted_recurring != 1 else 'y'}.",
            "success",
        )

    raw_panel = request.args.get("panel", "").strip()
    if raw_panel == "export":
        active_panel = "settings"
        section_from_legacy = "export"
    elif raw_panel == "migration":
        active_panel = "settings"
        section_from_legacy = "migration"
    elif raw_panel in ALLOWED_PANELS:
        active_panel = raw_panel
        section_from_legacy = None
    else:
        active_panel = "home"
        section_from_legacy = None

    month_filter = resolve_month_filter_from_request()
    year_filter = normalize_year(request.args.get("year"))
    report_year = normalize_year(request.args.get("report_year"))
    year_str, month_str = month_filter.split("-", 1)
    month_start = datetime(int(year_str), int(month_str), 1)
    month_end = month_start + timedelta(days=32)
    month_end = month_end.replace(day=1)
    month_start_d = month_start.strftime("%Y-%m-%d")
    month_end_d = month_end.strftime("%Y-%m-%d")
    month_heading = f"{calendar.month_name[int(month_str)]} {year_str}"
    settings_section = normalize_settings_section(
        section_from_legacy or request.args.get("settings_section")
    )

    categories = conn.execute(
        "SELECT id, name FROM categories WHERE user_id = ? ORDER BY name",
        (uid,),
    ).fetchall()
    income_categories = conn.execute(
        "SELECT id, name FROM income_categories WHERE user_id = ? ORDER BY name",
        (uid,),
    ).fetchall()

    recurring_entries = conn.execute(
        """
        SELECT
            r.id,
            r.entry_type,
            r.amount,
            r.category_id,
            r.account_id,
            r.day_of_month,
            r.months_to_run,
            r.notes,
            r.enabled,
            r.created_at,
            a.name AS account_name,
            COALESCE(c.name, ic.name) AS category_name
        FROM recurring_entries r
        JOIN accounts a ON a.id = r.account_id
        LEFT JOIN categories c ON r.entry_type = 'expense' AND c.id = r.category_id
        LEFT JOIN income_categories ic ON r.entry_type = 'income' AND ic.id = r.category_id
        WHERE r.user_id = ?
        ORDER BY r.day_of_month, r.id
        """,
        (uid,),
    ).fetchall()

    expense_filter_month = resolve_list_month_filter("exp_month", "exp_cal_year", "exp_cal_month")
    expense_where = "WHERE e.user_id = ?"
    expense_where_params = [uid]
    if expense_filter_month:
        eb = month_bounds_dates(expense_filter_month)
        expense_where += " AND date(e.spent_at) >= date(?) AND date(e.spent_at) < date(?)"
        expense_where_params.extend([eb[0], eb[1]])
    expense_filter_category_id = normalize_optional_category_id(
        request.args.get("exp_category") or request.form.get("exp_category")
    )
    if expense_filter_category_id is not None:
        if not conn.execute(
            "SELECT 1 FROM categories WHERE id = ? AND user_id = ?",
            (expense_filter_category_id, uid),
        ).fetchone():
            expense_filter_category_id = None
        else:
            expense_where += " AND e.category_id = ?"
            expense_where_params.append(expense_filter_category_id)

    expense_total = conn.execute(
        f"SELECT COUNT(*) AS n FROM expenses e {expense_where}",
        expense_where_params,
    ).fetchone()["n"]
    expense_num_pages = max(1, (expense_total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    expense_page = min(normalize_list_page(request.args.get("exp_page")), expense_num_pages)
    expense_offset = (expense_page - 1) * LIST_PAGE_SIZE
    expenses = conn.execute(
        f"""
        SELECT
            e.id,
            e.category_id,
            e.account_id,
            e.notes,
            e.amount,
            e.spent_at,
            e.created_at,
            c.name AS category_name,
            a.name AS account_name
        FROM expenses e
        JOIN categories c ON c.id = e.category_id
        JOIN accounts a ON a.id = e.account_id
        {expense_where}
        ORDER BY e.spent_at DESC
        LIMIT ? OFFSET ?
        """,
        (*expense_where_params, LIST_PAGE_SIZE, expense_offset),
    ).fetchall()

    accounts = conn.execute(
        "SELECT id, name, opening_balance FROM accounts WHERE user_id = ? ORDER BY name",
        (uid,),
    ).fetchall()

    income_filter_month = resolve_list_month_filter("inc_month", "inc_cal_year", "inc_cal_month")
    income_where = "WHERE i.user_id = ?"
    income_where_params = [uid]
    if income_filter_month:
        ib = month_bounds_dates(income_filter_month)
        income_where += " AND date(i.received_at) >= date(?) AND date(i.received_at) < date(?)"
        income_where_params.extend([ib[0], ib[1]])
    income_filter_category_id = normalize_optional_category_id(
        request.args.get("inc_category") or request.form.get("inc_category")
    )
    if income_filter_category_id is not None:
        if not conn.execute(
            "SELECT 1 FROM income_categories WHERE id = ? AND user_id = ?",
            (income_filter_category_id, uid),
        ).fetchone():
            income_filter_category_id = None
        else:
            income_where += " AND i.category_id = ?"
            income_where_params.append(income_filter_category_id)

    income_total = conn.execute(
        f"SELECT COUNT(*) AS n FROM income_entries i {income_where}",
        income_where_params,
    ).fetchone()["n"]
    income_num_pages = max(1, (income_total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    income_page = min(normalize_list_page(request.args.get("inc_page")), income_num_pages)
    income_offset = (income_page - 1) * LIST_PAGE_SIZE
    income_entries = conn.execute(
        f"""
        SELECT
            i.id,
            i.category_id,
            i.account_id,
            i.notes,
            i.amount,
            i.received_at,
            i.created_at,
            c.name AS category_name,
            a.name AS account_name
        FROM income_entries i
        JOIN income_categories c ON c.id = i.category_id
        JOIN accounts a ON a.id = i.account_id
        {income_where}
        ORDER BY i.received_at DESC
        LIMIT ? OFFSET ?
        """,
        (*income_where_params, LIST_PAGE_SIZE, income_offset),
    ).fetchall()

    total_expenses = conn.execute(
        """
        SELECT COALESCE(-SUM(amount), 0) AS total
        FROM expenses
        WHERE user_id = ? AND date(spent_at) >= date(?) AND date(spent_at) < date(?)
        """,
        (uid, month_start_d, month_end_d),
    ).fetchone()["total"]
    total_income = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM income_entries
        WHERE user_id = ? AND date(received_at) >= date(?) AND date(received_at) < date(?)
        """,
        (uid, month_start_d, month_end_d),
    ).fetchone()["total"]

    expense_breakdown = conn.execute(
        """
        SELECT c.name AS category_name, COALESCE(-SUM(e.amount), 0) AS total_amount
        FROM expenses e
        JOIN categories c ON c.id = e.category_id
        WHERE e.user_id = ? AND date(e.spent_at) >= date(?) AND date(e.spent_at) < date(?)
        GROUP BY c.name
        ORDER BY total_amount DESC
        """,
        (uid, month_start_d, month_end_d),
    ).fetchall()

    income_breakdown = conn.execute(
        """
        SELECT c.name AS category_name, SUM(i.amount) AS total_amount
        FROM income_entries i
        JOIN income_categories c ON c.id = i.category_id
        WHERE i.user_id = ? AND date(i.received_at) >= date(?) AND date(i.received_at) < date(?)
        GROUP BY c.name
        ORDER BY total_amount DESC
        """,
        (uid, month_start_d, month_end_d),
    ).fetchall()

    today_d = datetime.now().date()
    sel_y, sel_m = int(year_str), int(month_str)
    if (today_d.year, today_d.month) == (sel_y, sel_m):
        balance_cutoff_d = (today_d + timedelta(days=1)).isoformat()
        balance_scope_hint = (
            "Account balances use opening balance plus movements through today "
            "(only while this month is selected)."
        )
    else:
        balance_cutoff_d = month_end_d
        balance_scope_hint = (
            f"Account balances are through the end of {month_heading} "
            "(opening balance plus movements up to then)."
        )

    account_balances = fetch_account_balances_through(conn, balance_cutoff_d, uid)
    account_balance_by_id = {
        int(row["id"]): float(row["current_balance"]) for row in account_balances
    }
    account_transfers = conn.execute(
        """
        SELECT
            t.id,
            t.amount,
            t.transferred_at,
            t.notes,
            fa.name AS from_account_name,
            ta.name AS to_account_name
        FROM account_transfers t
        JOIN accounts fa ON fa.id = t.from_account_id
        JOIN accounts ta ON ta.id = t.to_account_id
        WHERE t.user_id = ?
        ORDER BY date(t.transferred_at) DESC, t.id DESC
        LIMIT ?
        """,
        (uid, TRANSFER_LOG_LIMIT),
    ).fetchall()
    accounts_total_balance = sum(float(r["current_balance"]) for r in account_balances)

    year_start_d = f"{year_filter:04d}-01-01"
    year_end_d = f"{year_filter + 1:04d}-01-01"
    expense_by_month = {
        row["m"]: float(row["total"])
        for row in conn.execute(
            """
            SELECT CAST(strftime('%m', spent_at) AS INTEGER) AS m,
                   COALESCE(-SUM(amount), 0) AS total
            FROM expenses
            WHERE user_id = ? AND date(spent_at) >= date(?) AND date(spent_at) < date(?)
            GROUP BY m
            """,
            (uid, year_start_d, year_end_d),
        )
    }
    income_by_month = {
        row["m"]: float(row["total"])
        for row in conn.execute(
            """
            SELECT CAST(strftime('%m', received_at) AS INTEGER) AS m,
                   COALESCE(SUM(amount), 0) AS total
            FROM income_entries
            WHERE user_id = ? AND date(received_at) >= date(?) AND date(received_at) < date(?)
            GROUP BY m
            """,
            (uid, year_start_d, year_end_d),
        )
    }

    month_names = calendar.month_name
    yearly_month_balances = monthly_total_balances_for_year(conn, year_filter, today_d, uid)
    yearly_rows = []
    yearly_total_income = 0.0
    yearly_total_expenses = 0.0
    for month_num in range(1, 13):
        inc = income_by_month.get(month_num, 0.0)
        exp = expense_by_month.get(month_num, 0.0)
        delta = inc - exp
        yearly_total_income += inc
        yearly_total_expenses += exp
        yearly_rows.append(
            {
                "month_label": month_names[month_num],
                "income": inc,
                "expenses": exp,
                "delta": delta,
                "total_balance": yearly_month_balances[month_num - 1],
            }
        )
    yearly_total_delta = yearly_total_income - yearly_total_expenses
    transfer_default_date = datetime.now().date().isoformat()

    investments_section = normalize_investments_section(request.args.get("investments_section"))

    crypto_txs = conn.execute(
        """
        SELECT id, coin_id, coin_symbol, coin_name, tx_type, quantity,
               price_per_unit, fee, exchange, transacted_at, notes
        FROM crypto_transactions
        WHERE user_id = ?
        ORDER BY transacted_at DESC, id DESC
        """,
        (uid,),
    ).fetchall()

    crypto_holdings_raw = compute_crypto_holdings(crypto_txs)

    force_refresh = request.args.get("refresh_prices") == "1"
    coin_ids = [h["coin_id"] for h in crypto_holdings_raw]
    crypto_prices = fetch_coingecko_prices(coin_ids, force=force_refresh) if coin_ids else {}

    crypto_total_value = 0.0
    crypto_total_invested = 0.0
    for h in crypto_holdings_raw:
        cid = h["coin_id"]
        price_info = crypto_prices.get(cid)
        if price_info:
            h["current_price"] = price_info["price"]
            h["change_24h"] = price_info.get("change_24h")
            h["current_value"] = h["quantity"] * price_info["price"]
            h["pnl"] = h["current_value"] - h["total_cost"]
            h["pnl_pct"] = (h["pnl"] / h["total_cost"] * 100) if h["total_cost"] > 0 else 0
            crypto_total_value += h["current_value"]
        else:
            h["current_price"] = None
            h["change_24h"] = None
            h["current_value"] = None
            h["pnl"] = None
            h["pnl_pct"] = 0
        crypto_total_invested += h["total_cost"]

    crypto_total_pnl = crypto_total_value - crypto_total_invested
    crypto_total_pnl_pct = (crypto_total_pnl / crypto_total_invested * 100) if crypto_total_invested > 0 else 0

    cache_age = time.time() - _price_cache["fetched_at"]
    if _price_cache["fetched_at"] > 0 and cache_age < PRICE_CACHE_TTL:
        mins = int(cache_age // 60)
        secs = int(cache_age % 60)
        crypto_prices_age = f"{mins}m {secs}s ago" if mins else f"{secs}s ago"
    else:
        crypto_prices_age = ""

    stock_txs = conn.execute(
        """
        SELECT id, symbol, ticker, instrument_name, tx_type, quantity,
               price_per_unit, fee, broker, transacted_at, notes
        FROM stock_transactions
        WHERE user_id = ?
        ORDER BY transacted_at DESC, id DESC
        """,
        (uid,),
    ).fetchall()

    stock_holdings_raw = compute_stock_holdings(stock_txs)
    stock_symbols = [h["symbol"] for h in stock_holdings_raw]
    force_stock_refresh = request.args.get("refresh_stock_prices") == "1"
    stock_prices = fetch_finnhub_quotes(stock_symbols, force=force_stock_refresh) if stock_symbols else {}

    stock_total_value = 0.0
    stock_total_invested = 0.0
    for h in stock_holdings_raw:
        sym = h["symbol"]
        price_info = stock_prices.get(sym)
        if price_info:
            h["current_price"] = price_info["price"]
            h["change_24h"] = price_info.get("change_24h")
            h["current_value"] = h["quantity"] * price_info["price"]
            h["pnl"] = h["current_value"] - h["total_cost"]
            h["pnl_pct"] = (h["pnl"] / h["total_cost"] * 100) if h["total_cost"] > 0 else 0
            stock_total_value += h["current_value"]
        else:
            h["current_price"] = None
            h["change_24h"] = None
            h["current_value"] = None
            h["pnl"] = None
            h["pnl_pct"] = 0
        stock_total_invested += h["total_cost"]

    stock_total_pnl = stock_total_value - stock_total_invested
    stock_total_pnl_pct = (stock_total_pnl / stock_total_invested * 100) if stock_total_invested > 0 else 0

    stock_cache_age = time.time() - _stock_price_cache["fetched_at"]
    if _stock_price_cache["fetched_at"] > 0 and stock_cache_age < PRICE_CACHE_TTL:
        mins = int(stock_cache_age // 60)
        secs = int(stock_cache_age % 60)
        stock_prices_age = f"{mins}m {secs}s ago" if mins else f"{secs}s ago"
    else:
        stock_prices_age = ""

    reports_section = normalize_reports_section(request.args.get("reports_section"))
    report_account_id = normalize_report_account(request.args.get("report_account"), conn, uid)
    report_account_label = "All accounts"
    if report_account_id is not None:
        for acc in accounts:
            if int(acc["id"]) == int(report_account_id):
                report_account_label = str(acc["name"])
                break

    # Reports involve month-by-month balance/portfolio math and can trigger
    # synchronous external price lookups (CoinGecko/yfinance). Only compute them
    # when the Reports panel is actually being viewed.
    report_live_balance = 0.0
    report_chart_spec = balance_line_chart_spec([])
    crypto_chart_spec = balance_line_chart_spec([])
    stock_chart_spec = balance_line_chart_spec([])
    reports_expenses_table = _empty_expense_pivot()
    if active_panel == "reports":
        jan1_this_year = f"{report_year:04d}-01-01"
        prev_balance_total = account_balance_at_cutoff(
            conn, jan1_this_year, uid, report_account_id
        )
        report_live_cutoff = (today_d + timedelta(days=1)).isoformat()
        report_live_balance = account_balance_at_cutoff(
            conn, report_live_cutoff, uid, report_account_id
        )
        report_balances_list = monthly_total_balances_for_year(
            conn, report_year, today_d, uid, report_account_id
        )
        report_balance_rows = build_monthly_chart_rows(
            report_balances_list, report_year, baseline=prev_balance_total
        )
        report_chart_spec = balance_line_chart_spec(report_balance_rows)
        reports_expenses_table = expense_pivot_for_report_year(conn, report_year, uid)

        crypto_baseline = portfolio_baseline_before_year(
            conn, crypto_txs, report_year, today_d, compute_crypto_holdings, "coin_id", "crypto"
        )
        crypto_monthly = monthly_crypto_portfolio_values_for_year(
            conn, crypto_txs, report_year, today_d
        )
        crypto_chart_rows = build_monthly_chart_rows(
            crypto_monthly, report_year, baseline=crypto_baseline
        )
        crypto_chart_spec = balance_line_chart_spec(crypto_chart_rows)

        stock_baseline = portfolio_baseline_before_year(
            conn, stock_txs, report_year, today_d, compute_stock_holdings, "symbol", "stock"
        )
        stock_monthly = monthly_stock_portfolio_values_for_year(
            conn, stock_txs, report_year, today_d
        )
        stock_chart_rows = build_monthly_chart_rows(
            stock_monthly, report_year, baseline=stock_baseline
        )
        stock_chart_spec = balance_line_chart_spec(stock_chart_rows)

    user_integrations = integrations.get_user_integrations(conn, uid)
    telegram_server = telegram_bot.server_config_for_form(conn)
    telegram_link = telegram_bot.get_telegram_link(conn, uid)
    telegram_link_code = telegram_bot.get_active_link_code(conn, uid)
    telegram_cfg = telegram_bot.get_server_config(conn)
    telegram_enabled = telegram_bot.is_configured(telegram_cfg)
    telegram_bot_username = None
    if telegram_enabled and settings_section == "integrations":
        telegram_bot_username = telegram_bot.get_bot_username(telegram_cfg)

    conn.commit()
    conn.close()

    return render_template(
        "index.html",
        session_username=g.username,
        user_integrations=user_integrations,
        telegram_server=telegram_server,
        telegram_link=telegram_link,
        telegram_link_code=telegram_link_code,
        telegram_enabled=telegram_enabled,
        telegram_bot_username=telegram_bot_username,
        categories=categories,
        income_categories=income_categories,
        expenses=expenses,
        accounts=accounts,
        income_entries=income_entries,
        active_panel=active_panel,
        month_filter=month_filter,
        year_filter=year_filter,
        settings_section=settings_section,
        total_expenses=total_expenses,
        total_income=total_income,
        net_balance=total_income - total_expenses,
        accounts_total_balance=accounts_total_balance,
        expense_breakdown=expense_breakdown,
        income_breakdown=income_breakdown,
        account_balances=account_balances,
        account_balance_by_id=account_balance_by_id,
        account_transfers=account_transfers,
        yearly_rows=yearly_rows,
        yearly_total_income=yearly_total_income,
        yearly_total_expenses=yearly_total_expenses,
        yearly_total_delta=yearly_total_delta,
        expense_page=expense_page,
        expense_total=expense_total,
        expense_num_pages=expense_num_pages,
        income_page=income_page,
        income_total=income_total,
        income_num_pages=income_num_pages,
        list_page_size=LIST_PAGE_SIZE,
        expense_filter_month=expense_filter_month,
        expense_filter_category_id=expense_filter_category_id,
        income_filter_month=income_filter_month,
        income_filter_category_id=income_filter_category_id,
        month_heading=month_heading,
        balance_scope_hint=balance_scope_hint,
        cal=calendar,
        transfer_default_date=transfer_default_date,
        transfer_log_limit=TRANSFER_LOG_LIMIT,
        report_year=report_year,
        reports_section=reports_section,
        report_account_id=report_account_id,
        report_account_label=report_account_label,
        report_live_balance=report_live_balance,
        report_today_month=calendar.month_name[today_d.month],
        report_chart_spec=report_chart_spec,
        crypto_chart_spec=crypto_chart_spec,
        stock_chart_spec=stock_chart_spec,
        reports_expenses_table=reports_expenses_table,
        recurring_entries=recurring_entries,
        investments_section=investments_section,
        crypto_holdings=crypto_holdings_raw,
        crypto_transactions=crypto_txs,
        crypto_total_value=crypto_total_value,
        crypto_total_invested=crypto_total_invested,
        crypto_total_pnl=crypto_total_pnl,
        crypto_total_pnl_pct=crypto_total_pnl_pct,
        crypto_prices_age=crypto_prices_age,
        stock_holdings=stock_holdings_raw,
        stock_transactions=stock_txs,
        stock_total_value=stock_total_value,
        stock_total_invested=stock_total_invested,
        stock_total_pnl=stock_total_pnl,
        stock_total_pnl_pct=stock_total_pnl_pct,
        stock_prices_age=stock_prices_age,
        finnhub_configured=bool(FINNHUB_API_KEY),
    )
