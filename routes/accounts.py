from flask import Blueprint, flash, g, request

from db import get_connection
from helpers import _parse_opening_balance, normalize_txn_day_from_form, redirect_home

bp = Blueprint("accounts", __name__)


def _account_fields_from_form(form):
    """Name, signed opening balance and card settings from an account form, or
    (None, message) when something is invalid.

    For a credit card the opening balance is entered as the amount owed, so it
    is stored negative like any spending on the card would leave it.
    """
    opening_balance = _parse_opening_balance(form.get("opening_balance"))
    if opening_balance is None:
        return None, "Invalid opening balance."
    account_type = "credit" if form.get("account_type") == "credit" else "bank"
    fields = {
        "name": form.get("name", "").strip(),
        "account_type": account_type,
        "opening_balance": opening_balance,
        "credit_limit": None,
        "statement_day": None,
        "due_day": None,
    }
    if account_type == "bank":
        return fields, None

    fields["opening_balance"] = -abs(opening_balance) or 0.0
    limit_raw = (form.get("credit_limit") or "").strip()
    if limit_raw:
        try:
            fields["credit_limit"] = float(limit_raw)
        except ValueError:
            return None, "Invalid credit limit."
        if fields["credit_limit"] < 0:
            return None, "Invalid credit limit."
    for key, label in (("statement_day", "statement day"), ("due_day", "payment due day")):
        raw = (form.get(key) or "").strip()
        if not raw:
            continue
        try:
            day = int(raw)
        except ValueError:
            return None, f"Invalid {label}."
        if not 1 <= day <= 31:
            return None, f"The {label} must be between 1 and 31."
        fields[key] = day
    if fields["due_day"] and not fields["statement_day"]:
        return None, "Set the statement day too, so the due date can be worked out."
    return fields, None

@bp.route("/accounts/add", methods=["POST"])
def add_account():
    fields, error = _account_fields_from_form(request.form)
    if error:
        flash(error, "error")
        return redirect_home()
    if fields["name"]:
        conn = get_connection()
        conn.execute(
            """
            INSERT OR IGNORE INTO accounts
                (user_id, name, opening_balance, account_type, credit_limit, statement_day, due_day)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                g.user_id,
                fields["name"],
                fields["opening_balance"],
                fields["account_type"],
                fields["credit_limit"],
                fields["statement_day"],
                fields["due_day"],
            ),
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
    fields, error = _account_fields_from_form(request.form)
    if error:
        flash(error, "error")
        return redirect_home()
    if fields["name"]:
        conn = get_connection()
        conn.execute(
            """
            UPDATE accounts
            SET name = ?, opening_balance = ?, account_type = ?, credit_limit = ?,
                statement_day = ?, due_day = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                fields["name"],
                fields["opening_balance"],
                fields["account_type"],
                fields["credit_limit"],
                fields["statement_day"],
                fields["due_day"],
                account_id,
                g.user_id,
            ),
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
