from flask import Blueprint, flash, g, request

from db import get_connection
from helpers import _parse_opening_balance, normalize_txn_day_from_form, redirect_home

bp = Blueprint("accounts", __name__)


@bp.route("/accounts/add", methods=["POST"])
def add_account():
    name = request.form.get("name", "").strip()
    opening_balance = _parse_opening_balance(request.form.get("opening_balance"))
    if opening_balance is None:
        flash("Invalid opening balance.", "error")
        return redirect_home()
    if name:
        conn = get_connection()
        conn.execute(
            "INSERT OR IGNORE INTO accounts(user_id, name, opening_balance) VALUES (?, ?, ?)",
            (g.user_id, name, opening_balance),
        )
        conn.commit()
        conn.close()
    return redirect_home()

@bp.route("/accounts/<int:account_id>/delete", methods=["POST"])
def delete_account(account_id):
    conn = get_connection()
    cursor = conn.execute(
        "DELETE FROM accounts WHERE id = ? AND user_id = ?",
        (account_id, g.user_id),
    )
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
        print(f"[vibe-budgeting] refused account delete {account_id} (still referenced or missing)")
    return redirect_home()

@bp.route("/accounts/<int:account_id>/edit", methods=["POST"])
def edit_account(account_id):
    name = request.form.get("name", "").strip()
    opening_balance = _parse_opening_balance(request.form.get("opening_balance"))
    if opening_balance is None:
        flash("Invalid opening balance.", "error")
        return redirect_home()
    if name:
        conn = get_connection()
        conn.execute(
            "UPDATE accounts SET name = ?, opening_balance = ? WHERE id = ? AND user_id = ?",
            (name, opening_balance, account_id, g.user_id),
        )
        conn.commit()
        conn.close()
    return redirect_home()

@bp.route("/transfers/add", methods=["POST"])
def add_transfer():
    from_raw = request.form.get("from_account_id", "").strip()
    to_raw = request.form.get("to_account_id", "").strip()
    amount_raw = request.form.get("amount", "").strip()
    notes = request.form.get("notes", "").strip()
    transferred_at = normalize_txn_day_from_form(request.form.get("transferred_at", "").strip())

    if not from_raw or not to_raw:
        return redirect_home()

    try:
        from_id = int(from_raw)
        to_id = int(to_raw)
    except ValueError:
        flash("Invalid accounts.", "error")
        return redirect_home()

    if from_id == to_id:
        flash("Choose two different accounts.", "error")
        return redirect_home()

    try:
        amt = abs(float(amount_raw))
    except (TypeError, ValueError):
        amt = 0.0

    if amt <= 0:
        flash("Enter a positive amount.", "error")
        return redirect_home()

    conn = get_connection()
    uid = g.user_id
    if not conn.execute(
        "SELECT 1 FROM accounts WHERE id = ? AND user_id = ?",
        (from_id, uid),
    ).fetchone() or not conn.execute(
        "SELECT 1 FROM accounts WHERE id = ? AND user_id = ?",
        (to_id, uid),
    ).fetchone():
        conn.close()
        flash("Invalid accounts.", "error")
        return redirect_home()

    conn.execute(
        """
        INSERT INTO account_transfers (user_id, from_account_id, to_account_id, amount, transferred_at, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (uid, from_id, to_id, amt, transferred_at, notes),
    )
    conn.commit()
    conn.close()
    flash("Transfer recorded.", "success")
    return redirect_home()

@bp.route("/transfers/<int:transfer_id>/delete", methods=["POST"])
def delete_transfer(transfer_id):
    conn = get_connection()
    cursor = conn.execute(
        "DELETE FROM account_transfers WHERE id = ? AND user_id = ?",
        (transfer_id, g.user_id),
    )
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
        flash("Transfer not found.", "error")
    return redirect_home()
