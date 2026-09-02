from flask import Blueprint, current_app, flash, g, request
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
import json

from config import FINNHUB_API_KEY
from db import get_connection
from helpers import _stock_redirect, normalize_txn_day_from_form, redirect_home
from prices import _finnhub_request

bp = Blueprint("investments", __name__)


@bp.route("/stocks/search")
def search_stocks():
    query = (request.args.get("q") or "").strip()
    if len(query) < 1:
        return current_app.response_class(response=json.dumps([]), status=200, mimetype="application/json")
    if not FINNHUB_API_KEY:
        return current_app.response_class(response=json.dumps([]), status=200, mimetype="application/json")
    data = _finnhub_request(f"/search?q={quote(query)}")
    items = []
    for row in (data.get("result") if data else []) or []:
        sym = (row.get("symbol") or "").strip()
        if not sym:
            continue
        desc = (row.get("description") or "").strip()
        typ = (row.get("type") or "").strip()
        items.append(
            {
                "symbol": sym,
                "ticker": sym.split(".")[0].upper(),
                "name": desc or sym,
                "type": typ,
            }
        )
        if len(items) >= 12:
            break
    q_upper = query.upper()
    items.sort(
        key=lambda x: (
            0 if x["symbol"].upper() == q_upper else 1,
            0 if x["symbol"].upper().startswith(q_upper) else 2,
            x["symbol"],
        )
    )
    return current_app.response_class(response=json.dumps(items), status=200, mimetype="application/json")

@bp.route("/stocks/add", methods=["POST"])
def add_stock():
    uid = g.user_id
    symbol = request.form.get("symbol", "").strip().upper()
    ticker = request.form.get("ticker", "").strip().upper()
    instrument_name = request.form.get("instrument_name", "").strip()
    tx_type = request.form.get("tx_type", "").strip().lower()
    quantity_raw = request.form.get("quantity", "").strip()
    price_raw = request.form.get("price_per_unit", "").strip()
    fee_raw = request.form.get("fee", "0").strip()
    broker = request.form.get("broker", "").strip()
    transacted_at = normalize_txn_day_from_form(request.form.get("transacted_at", "").strip())
    notes = request.form.get("notes", "").strip()

    if not symbol or not ticker or not instrument_name:
        flash("Fill in symbol, ticker, and name.", "error")
        return _stock_redirect()

    if tx_type not in ("buy", "sell"):
        flash("Invalid transaction type.", "error")
        return _stock_redirect()

    try:
        quantity = float(quantity_raw)
        price = float(price_raw)
        fee = abs(float(fee_raw)) if fee_raw else 0.0
    except (TypeError, ValueError):
        flash("Invalid quantity, price, or fee.", "error")
        return _stock_redirect()

    if quantity <= 0 or price < 0:
        flash("Quantity must be positive and price non-negative.", "error")
        return _stock_redirect()

    conn = get_connection()
    conn.execute(
        """
        INSERT INTO stock_transactions
            (user_id, symbol, ticker, instrument_name, tx_type, quantity, price_per_unit, fee, broker, transacted_at, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (uid, symbol, ticker, instrument_name, tx_type, quantity, price, fee, broker, transacted_at, notes),
    )
    conn.commit()
    conn.close()
    flash(f"Stock {tx_type} recorded.", "success")
    return _stock_redirect()

@bp.route("/stocks/<int:tx_id>/edit", methods=["POST"])
def edit_stock(tx_id):
    uid = g.user_id
    symbol = request.form.get("symbol", "").strip().upper()
    ticker = request.form.get("ticker", "").strip().upper()
    instrument_name = request.form.get("instrument_name", "").strip()
    tx_type = request.form.get("tx_type", "").strip().lower()
    quantity_raw = request.form.get("quantity", "").strip()
    price_raw = request.form.get("price_per_unit", "").strip()
    fee_raw = request.form.get("fee", "0").strip()
    broker = request.form.get("broker", "").strip()
    transacted_at = normalize_txn_day_from_form(request.form.get("transacted_at", "").strip())
    notes = request.form.get("notes", "").strip()

    if not symbol or not ticker or not instrument_name:
        flash("Fill in symbol, ticker, and name.", "error")
        return _stock_redirect()

    if tx_type not in ("buy", "sell"):
        flash("Invalid transaction type.", "error")
        return _stock_redirect()

    try:
        quantity = float(quantity_raw)
        price = float(price_raw)
        fee = abs(float(fee_raw)) if fee_raw else 0.0
    except (TypeError, ValueError):
        flash("Invalid quantity, price, or fee.", "error")
        return _stock_redirect()

    if quantity <= 0 or price < 0:
        flash("Quantity must be positive and price non-negative.", "error")
        return _stock_redirect()

    conn = get_connection()
    cur = conn.execute(
        """
        UPDATE stock_transactions
        SET symbol = ?, ticker = ?, instrument_name = ?, tx_type = ?,
            quantity = ?, price_per_unit = ?, fee = ?, broker = ?,
            transacted_at = ?, notes = ?
        WHERE id = ? AND user_id = ?
        """,
        (symbol, ticker, instrument_name, tx_type, quantity, price, fee, broker, transacted_at, notes, tx_id, uid),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        flash("Transaction not found.", "error")
    else:
        flash("Transaction updated.", "success")
    return _stock_redirect()

@bp.route("/stocks/<int:tx_id>/delete", methods=["POST"])
def delete_stock(tx_id):
    conn = get_connection()
    conn.execute(
        "DELETE FROM stock_transactions WHERE id = ? AND user_id = ?",
        (tx_id, g.user_id),
    )
    conn.commit()
    conn.close()
    flash("Transaction removed.", "success")
    return _stock_redirect()

@bp.route("/crypto/search")
def search_crypto():
    query = (request.args.get("q") or "").strip()
    if len(query) < 2:
        return current_app.response_class(
            response=json.dumps([]),
            status=200,
            mimetype="application/json",
        )
    url = f"https://api.coingecko.com/api/v3/search?query={query}"
    try:
        req = Request(url, headers={"Accept": "application/json", "User-Agent": "VibeBudgeting/1.0"})
        with urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        coins = [
            {"id": c["id"], "symbol": c["symbol"], "name": c["name"], "thumb": c.get("thumb", "")}
            for c in (data.get("coins") or [])[:10]
        ]
    except (URLError, HTTPError, json.JSONDecodeError, OSError):
        coins = []
    return current_app.response_class(
        response=json.dumps(coins),
        status=200,
        mimetype="application/json",
    )

@bp.route("/crypto/add", methods=["POST"])
def add_crypto():
    uid = g.user_id
    coin_id = request.form.get("coin_id", "").strip().lower()
    coin_symbol = request.form.get("coin_symbol", "").strip().upper()
    coin_name = request.form.get("coin_name", "").strip()
    tx_type = request.form.get("tx_type", "").strip().lower()
    quantity_raw = request.form.get("quantity", "").strip()
    price_raw = request.form.get("price_per_unit", "").strip()
    fee_raw = request.form.get("fee", "0").strip()
    exchange = request.form.get("exchange", "").strip()
    transacted_at = normalize_txn_day_from_form(request.form.get("transacted_at", "").strip())
    notes = request.form.get("notes", "").strip()

    if not coin_id or not coin_symbol or not coin_name:
        flash("Fill in Coin ID, Symbol, and Name.", "error")
        return redirect_home(panel="investments", settings_section="crypto")

    if tx_type not in ("buy", "sell"):
        flash("Invalid transaction type.", "error")
        return redirect_home(panel="investments", settings_section="crypto")

    try:
        quantity = float(quantity_raw)
        price = float(price_raw)
        fee = abs(float(fee_raw)) if fee_raw else 0.0
    except (TypeError, ValueError):
        flash("Invalid quantity, price, or fee.", "error")
        return redirect_home(panel="investments", settings_section="crypto")

    if quantity <= 0 or price < 0:
        flash("Quantity must be positive and price non-negative.", "error")
        return redirect_home(panel="investments", settings_section="crypto")

    conn = get_connection()
    conn.execute(
        """
        INSERT INTO crypto_transactions
            (user_id, coin_id, coin_symbol, coin_name, tx_type, quantity, price_per_unit, fee, exchange, transacted_at, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (uid, coin_id, coin_symbol, coin_name, tx_type, quantity, price, fee, exchange, transacted_at, notes),
    )
    conn.commit()
    conn.close()
    flash(f"Crypto {tx_type} recorded.", "success")
    return redirect_home(panel="investments", settings_section="crypto")

@bp.route("/crypto/<int:tx_id>/edit", methods=["POST"])
def edit_crypto(tx_id):
    uid = g.user_id
    coin_id = request.form.get("coin_id", "").strip().lower()
    coin_symbol = request.form.get("coin_symbol", "").strip().upper()
    coin_name = request.form.get("coin_name", "").strip()
    tx_type = request.form.get("tx_type", "").strip().lower()
    quantity_raw = request.form.get("quantity", "").strip()
    price_raw = request.form.get("price_per_unit", "").strip()
    fee_raw = request.form.get("fee", "0").strip()
    exchange = request.form.get("exchange", "").strip()
    transacted_at = normalize_txn_day_from_form(request.form.get("transacted_at", "").strip())
    notes = request.form.get("notes", "").strip()

    if not coin_id or not coin_symbol or not coin_name:
        flash("Fill in Coin ID, Symbol, and Name.", "error")
        return redirect_home(panel="investments", settings_section="crypto")

    if tx_type not in ("buy", "sell"):
        flash("Invalid transaction type.", "error")
        return redirect_home(panel="investments", settings_section="crypto")

    try:
        quantity = float(quantity_raw)
        price = float(price_raw)
        fee = abs(float(fee_raw)) if fee_raw else 0.0
    except (TypeError, ValueError):
        flash("Invalid quantity, price, or fee.", "error")
        return redirect_home(panel="investments", settings_section="crypto")

    if quantity <= 0 or price < 0:
        flash("Quantity must be positive and price non-negative.", "error")
        return redirect_home(panel="investments", settings_section="crypto")

    conn = get_connection()
    cur = conn.execute(
        """
        UPDATE crypto_transactions
        SET coin_id = ?, coin_symbol = ?, coin_name = ?, tx_type = ?,
            quantity = ?, price_per_unit = ?, fee = ?, exchange = ?,
            transacted_at = ?, notes = ?
        WHERE id = ? AND user_id = ?
        """,
        (coin_id, coin_symbol, coin_name, tx_type, quantity, price, fee, exchange, transacted_at, notes, tx_id, uid),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        flash("Transaction not found.", "error")
    else:
        flash("Transaction updated.", "success")
    return redirect_home(panel="investments", settings_section="crypto")

@bp.route("/crypto/<int:tx_id>/delete", methods=["POST"])
def delete_crypto(tx_id):
    conn = get_connection()
    conn.execute(
        "DELETE FROM crypto_transactions WHERE id = ? AND user_id = ?",
        (tx_id, g.user_id),
    )
    conn.commit()
    conn.close()
    flash("Transaction removed.", "success")
    return redirect_home(panel="investments", settings_section="crypto")
